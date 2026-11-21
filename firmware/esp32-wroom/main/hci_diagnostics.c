#include "hci_diagnostics.h"
#include "esp_bt.h"
#include "esp_timer.h"
#include <stdatomic.h>

static esp_vhci_host_callback_t host;
static atomic_uint acl_sent, acl_received, acl_completed, events_received;
static atomic_uint last_send_ms, last_complete_ms;
static atomic_uint create_connection_status;

void sink_hci_reset_connect_status(void) { atomic_store(&create_connection_status, 0); }
unsigned sink_hci_connect_status(void) { return atomic_load(&create_connection_status); }

void __real_esp_vhci_host_send_packet(uint8_t *data, uint16_t length);
esp_err_t __real_esp_vhci_host_register_callback(const esp_vhci_host_callback_t *callback);

void __wrap_esp_vhci_host_send_packet(uint8_t *data, uint16_t length) {
    if (length >= 5 && data[0] == 2) {
        atomic_fetch_add(&acl_sent, 1);
        atomic_store(&last_send_ms, (uint32_t)(esp_timer_get_time()/1000));
    }
    __real_esp_vhci_host_send_packet(data, length);
}

static void send_available(void) {
    if (host.notify_host_send_available) host.notify_host_send_available();
}

static int received(uint8_t *data, uint16_t length) {
    if (length >= 5 && data[0] == 2) atomic_fetch_add(&acl_received, 1);
    if (length >= 3 && data[0] == 4) {
        atomic_fetch_add(&events_received, 1);
        /* HCI Command Status: status, command credits, opcode (little-endian).
         * Keep controller connection failures instead of reporting only the
         * later generic A2DP disconnect. Never alter traffic sent to Bluedroid. */
        if (data[1] == 0x0f && length >= 7 && data[2] == 4 &&
            data[5] == 0x05 && data[6] == 0x04)
            atomic_store(&create_connection_status, data[3]);
        if (data[1] == 0x13 && length >= 4 &&
            data[2] >= 1 && data[2] <= length-3 &&
            1u+4u*data[3] <= data[2]) {
            unsigned completed = 0;
            for (unsigned i = 0; i < data[3]; i++) {
                unsigned offset = 6+4*i;
                completed += data[offset] | ((unsigned)data[offset+1] << 8);
            }
            atomic_fetch_add(&acl_completed, completed);
            if (completed) atomic_store(&last_complete_ms, (uint32_t)(esp_timer_get_time()/1000));
        }
    }
    return host.notify_host_recv ? host.notify_host_recv(data, length) : 0;
}

esp_err_t __wrap_esp_vhci_host_register_callback(const esp_vhci_host_callback_t *callback) {
    if (!callback) return __real_esp_vhci_host_register_callback(callback);
    host = *callback;
    static const esp_vhci_host_callback_t observer = {
        .notify_host_send_available = send_available, .notify_host_recv = received
    };
    return __real_esp_vhci_host_register_callback(&observer);
}

cJSON *sink_hci_diagnostics(void) {
    cJSON *result = cJSON_CreateObject();
    if (!result) return NULL;
    /* Load timestamps before the clock: callbacks can update them concurrently.
     * Reading the clock first could underflow the reported unsigned age. */
    uint32_t sent_at = atomic_load(&last_send_ms);
    uint32_t complete_at = atomic_load(&last_complete_ms);
    uint32_t now = (uint32_t)(esp_timer_get_time()/1000);
    cJSON_AddNumberToObject(result, "acl_sent", atomic_load(&acl_sent));
    cJSON_AddNumberToObject(result, "acl_completed", atomic_load(&acl_completed));
    cJSON_AddNumberToObject(result, "acl_received", atomic_load(&acl_received));
    cJSON_AddNumberToObject(result, "events_received", atomic_load(&events_received));
    cJSON_AddNumberToObject(result, "send_age_ms", now-sent_at);
    cJSON_AddNumberToObject(result, "complete_age_ms", now-complete_at);
    cJSON_AddNumberToObject(result, "connect_status", sink_hci_connect_status());
    return result;
}
