#include "esp_a2dp_api.h"
#include "esp_avrc_api.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "private/esp_coexist_internal.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sink_control.h"
#include "stream_audio.h"
#include "remote_buttons.h"
#include "hci_diagnostics.h"
#include "sbc_transport.h"
#include "sbc_silence.h"
#include <stdio.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

/* Manager owns all mutable Bluetooth policy and operation state. SDK callbacks
 * copy bounded events; SBC output runs in its own task. */
typedef struct {
    int kind;
    esp_a2d_cb_event_t event;
    esp_a2d_cb_param_t param;
    char command[32];
    char request_id[37];
    uint8_t address[6];
    int status;
} message_t;
/* Read-only internal getter exported by the pinned coexistence libraries.
 * Unlike coex_status_get(bitmap), this returns the per-module scheme bits.
 * Its one-argument ABI was checked against both pinned ESP32 libraries. */
extern uint32_t coex_schm_status_get(uint32_t type);
extern void wifi_sink_sbc_diagnostics(uint32_t out[10]);
static QueueHandle_t queue;
static atomic_bool volume_supported;
static atomic_bool rssi_pending;
static atomic_int rssi_delta = 128;
static atomic_uint rssi_at_ms;
static atomic_bool sbc_codec_ready;
static atomic_uint sbc_handle, sbc_mtu;
static int tx_power_min = -99, tx_power_max = -99;
extern void wifi_sink_avrc_set_playback_status(uint8_t status);
static atomic_bool play_notification_pending;
static atomic_int notified_play_status;
static atomic_int speaker_delay_tenths_ms = -1;
static atomic_int speaker_volume=-1;
static uint8_t volume_label;
static const char *TAG = "sink";
static esp_bd_addr_t preferred_peer, active_peer, forget_peer;
static bool preferred, connected, connecting, scanning, streaming, autoreconnect = true, ready;
static bool select_on_success, forgetting, scan_requested, pairing_notified;
static unsigned retry = 2;
static int64_t deadline, next_retry, operation_deadline, forget_deadline;
static nvs_handle_t prefs;
static char operation_id[37], operation_event[40];

typedef struct {
    bool ready, connected, connecting, scanning, streaming, autoreconnect, preferred;
    char connected_name[49];
    uint8_t preferred_peer[6], active_peer[6];
    unsigned retry;
    int64_t next_retry;
} output_snapshot_t;
static output_snapshot_t snapshot;
static portMUX_TYPE snapshot_lock = portMUX_INITIALIZER_UNLOCKED;
static uint8_t name_peer[6];
static char peer_name[49];

static void publish(void) {
    output_snapshot_t value = {.ready = ready,
                               .connected = connected,
                               .connecting = connecting,
                               .scanning = scanning,
                               .streaming = streaming,
                               .autoreconnect = autoreconnect,
                               .preferred = preferred,
                               .retry = retry,
                               .next_retry = next_retry};
    memcpy(value.preferred_peer, preferred_peer, 6);
    memcpy(value.active_peer, active_peer, 6);
    portENTER_CRITICAL(&snapshot_lock);
    if (!memcmp(name_peer, active_peer, 6))
        memcpy(value.connected_name, peer_name, sizeof(peer_name));
    snapshot = value;
    portEXIT_CRITICAL(&snapshot_lock);
}
static void address_text(const uint8_t *b, char text[18]) {
    snprintf(text, 18, "%02x:%02x:%02x:%02x:%02x:%02x", b[0], b[1], b[2], b[3], b[4], b[5]);
}
cJSON *sink_output_status(void) {
    output_snapshot_t value;
    portENTER_CRITICAL(&snapshot_lock);
    value = snapshot;
    portEXIT_CRITICAL(&snapshot_lock);
    cJSON *j = cJSON_CreateObject();
    if (!j)
        return NULL;
    cJSON_AddStringToObject(j, "kind", "bluetooth_a2dp");
    cJSON_AddStringToObject(j,"audio_codec",SINK_SBC_PROFILE);
    cJSON_AddBoolToObject(j,"codec_ready",atomic_load(&sbc_codec_ready));
    cJSON_AddNumberToObject(j,"audio_mtu",atomic_load(&sbc_mtu));
    cJSON_AddBoolToObject(j, "modem_sleep_enabled", false);
    int minimum_power = tx_power_min, maximum_power = tx_power_max;
    esp_power_level_t live_min, live_max;
    /* The setter posts asynchronously to the controller. Query running state. */
    if (esp_bredr_tx_power_get(&live_min, &live_max) == ESP_OK) {
        minimum_power = 3*(int)live_min-12;
        maximum_power = 3*(int)live_max-12;
    }
    cJSON_AddNumberToObject(j, "tx_power_min_dbm", minimum_power);
    cJSON_AddNumberToObject(j, "tx_power_max_dbm", maximum_power);
    int delay=atomic_load(&speaker_delay_tenths_ms);
    cJSON_AddBoolToObject(j,"speaker_delay_valid",delay>=0);
    if(delay>=0)cJSON_AddNumberToObject(j,"speaker_delay_ms",delay/10.0);
    else cJSON_AddNullToObject(j,"speaker_delay_ms");
    cJSON_AddBoolToObject(j, "coex_bt_running", coex_status_get(COEX_STATUS_GET_BT_BITMAP) != 0);
    cJSON_AddNumberToObject(j, "coex_bt_state", coex_schm_status_get(COEX_SCHM_ST_TYPE_BT));
    cJSON_AddNumberToObject(j, "coex_wifi_state", coex_schm_status_get(COEX_SCHM_ST_TYPE_WIFI));
    int delta = atomic_load(&rssi_delta);
    if (delta == 128) cJSON_AddNullToObject(j, "rssi_delta");
    else {
        uint32_t measured_at = atomic_load(&rssi_at_ms);
        cJSON_AddNumberToObject(j, "rssi_delta", delta);
        cJSON_AddNumberToObject(j, "rssi_delta_age_ms",
            (uint32_t)(esp_timer_get_time()/1000) - measured_at);
    }
    cJSON_AddItemToObject(j, "hci", sink_hci_diagnostics());
    uint32_t sdk[10];wifi_sink_sbc_diagnostics(sdk);
    const char *keys[10]={"q","qmax","drop","mtu","encode_us","tick_us","in","out","deq_us","flush"};
    cJSON *telemetry=cJSON_AddObjectToObject(j,"sbc");
    if(telemetry)for(unsigned i=0;i<10;i++)cJSON_AddNumberToObject(telemetry,keys[i],sdk[i]);
    cJSON_AddBoolToObject(j,"absolute_volume_supported",atomic_load(&volume_supported));
    cJSON_AddNumberToObject(j,"speaker_volume",atomic_load(&speaker_volume));
    const char *state = !value.ready       ? "uninitialized"
                        : value.scanning   ? "scanning"
                        : value.connecting ? "connecting"
                        : value.connected  ? (value.streaming ? "streaming" : "connected")
                        : value.preferred && value.autoreconnect ? "reconnecting"
                                                                 : "ready";
    cJSON_AddStringToObject(j, "state", state);
    cJSON_AddBoolToObject(j, "ready", value.ready);
    cJSON_AddBoolToObject(j, "connected", value.connected);
    cJSON_AddBoolToObject(j, "connecting", value.connecting);
    cJSON_AddBoolToObject(j, "scanning", value.scanning);
    cJSON_AddBoolToObject(j, "autoreconnect", value.autoreconnect);
    if (value.connected && value.connected_name[0])
        cJSON_AddStringToObject(j, "connected_name", value.connected_name);
    else
        cJSON_AddNullToObject(j, "connected_name");
    char text[18];
    if (value.preferred) {
        address_text(value.preferred_peer, text);
        cJSON_AddStringToObject(j, "preferred_address", text);
    } else
        cJSON_AddNullToObject(j, "preferred_address");
    if (value.connected || value.connecting) {
        address_text(value.active_peer, text);
        cJSON_AddStringToObject(j, "active_address", text);
    } else
        cJSON_AddNullToObject(j, "active_address");
    if (value.connected) {
        address_text(value.active_peer, text);
        cJSON_AddStringToObject(j, "connected_address", text);
    } else
        cJSON_AddNullToObject(j, "connected_address");
    cJSON_AddNumberToObject(j, "retry_backoff_seconds", value.retry);
    int64_t remaining = value.next_retry - esp_timer_get_time();
    cJSON_AddNumberToObject(j, "next_retry_ms",
                            !value.connected && !value.connecting && value.autoreconnect &&
                                    value.preferred && remaining > 0
                                ? remaining / 1000
                                : 0);
    cJSON *bonds = cJSON_AddArrayToObject(j, "bonded_addresses");
    if (!bonds) {
        cJSON_Delete(j);
        return NULL;
    }
    if (value.ready) {
        int total = esp_bt_gap_get_bond_device_num(), count = total < 8 ? total : 8;
        esp_bd_addr_t addresses[8];
        if (count > 0 && esp_bt_gap_get_bond_device_list(&count, addresses) == ESP_OK)
            for (int i = 0; i < count; i++) {
                address_text(addresses[i], text);
                cJSON_AddItemToArray(bonds, cJSON_CreateString(text));
            }
        cJSON_AddNumberToObject(j, "bond_count", total);
        cJSON_AddBoolToObject(j, "bond_list_truncated", total > 8);
    }
    return j;
}
bool sink_output_command(const char *command, const char *request_id) {
    message_t m = {0};
    if (!queue || strlen(command) >= sizeof(m.command) ||
        (request_id && strlen(request_id) >= sizeof(m.request_id)))
        return false;
    snprintf(m.command, sizeof(m.command), "%s", command);
    if (request_id)
        snprintf(m.request_id, sizeof(m.request_id), "%s", request_id);
    return xQueueSend(queue, &m, 0) == pdTRUE;
}
static void emit(const char *name, const char *detail) {
    bool complete = operation_id[0] &&
                    (!strcmp(name, operation_event) || !strcmp(name, "COMMAND_FAILED") ||
                     (!strcmp(operation_event, "BT_CONNECTED") &&
                      (!strcmp(name, "BT_PAIRING_FAILED") || !strcmp(name, "BT_DISCONNECTED"))));
    bool progress = operation_id[0] && !strcmp(operation_event, "BT_CONNECTED") &&
                    (!strcmp(name, "BT_PAIRING_STARTED") || !strcmp(name, "BT_PAIRING_SUCCEEDED"));
    sink_network_command_event(complete || progress ? operation_id : NULL, name, detail);
    if (complete)
        operation_id[0] = operation_event[0] = 0;
}
static void fail(const char *code) { emit("COMMAND_FAILED", code); }
static bool sdk_result(const char *op, esp_err_t result) {
    ESP_LOGI(TAG, "%s: %s", op, esp_err_to_name(result));
    if (result != ESP_OK) {
        fail("SDK_REJECTED");
        return false;
    }
    return true;
}
static bool save(void) {
    esp_err_t result =
        preferred ? nvs_set_blob(prefs, "peer", preferred_peer, 6) : nvs_erase_key(prefs, "peer");
    if (result == ESP_ERR_NVS_NOT_FOUND)
        result = ESP_OK;
    if (result == ESP_OK)
        result = nvs_set_u8(prefs, "auto", autoreconnect);
    if (result == ESP_OK)
        result = nvs_commit(prefs);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Preference write failed: %s", esp_err_to_name(result));
        return false;
    }
    return true;
}
static bool is_bonded(const uint8_t *address) {
    int count = esp_bt_gap_get_bond_device_num();
    if (count > 16)
        count = 16;
    esp_bd_addr_t addresses[16];
    if (count <= 0 || esp_bt_gap_get_bond_device_list(&count, addresses) != ESP_OK)
        return false;
    for (int i = 0; i < count; i++)
        if (!memcmp(addresses[i], address, 6))
            return true;
    return false;
}
static void a2dp(esp_a2d_cb_event_t event, esp_a2d_cb_param_t *p) {
    message_t m = {.kind = 1, .event = event, .param = *p};
    if (xQueueSend(queue, &m, 0) != pdTRUE)
        sink_network_event("ERROR", "Bluetooth event queue full");
}
static void avrc_target(esp_avrc_tg_cb_event_t event, esp_avrc_tg_cb_param_t *p) {
    static bool target_connected;
    static uint8_t held;
    if (event == ESP_AVRC_TG_CONNECTION_STATE_EVT) {
        target_connected = p->conn_stat.connected;
        held = 0;
        atomic_store(&play_notification_pending, false);
    } else if (event == ESP_AVRC_TG_REGISTER_NOTIFICATION_EVT && target_connected &&
               p->reg_ntf.event_id == ESP_AVRC_RN_PLAY_STATUS_CHANGE) {
        esp_avrc_rn_param_t value = {.playback = stream_audio_playback_active() ?
                                    ESP_AVRC_PLAYBACK_PLAYING : ESP_AVRC_PLAYBACK_PAUSED};
        if (esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_PLAY_STATUS_CHANGE, ESP_AVRC_RN_RSP_INTERIM, &value) == ESP_OK) {
            atomic_store(&notified_play_status, value.playback);
            atomic_store(&play_notification_pending, true);
        }
    } else if (event == ESP_AVRC_TG_PASSTHROUGH_CMD_EVT && target_connected) {
        const char *action = sink_remote_button(p->psth_cmd.key_code, p->psth_cmd.key_state, &held);
        if (action) sink_network_event("REMOTE_CONTROL", action);
    }
}
static void avrc(esp_avrc_ct_cb_event_t event, esp_avrc_ct_cb_param_t *p) {
    if(event==ESP_AVRC_CT_CONNECTION_STATE_EVT) {
        atomic_store(&volume_supported,false); atomic_store(&speaker_volume,-1);
        if(p->conn_stat.connected) esp_avrc_ct_send_get_rn_capabilities_cmd(0);
    } else if(event==ESP_AVRC_CT_GET_RN_CAPABILITIES_RSP_EVT) {
        bool supported=esp_avrc_rn_evt_bit_mask_operation(ESP_AVRC_BIT_MASK_OP_TEST,&p->get_rn_caps_rsp.evt_set,ESP_AVRC_RN_VOLUME_CHANGE);
        atomic_store(&volume_supported,supported);
        if(supported)esp_avrc_ct_send_register_notification_cmd(1,ESP_AVRC_RN_VOLUME_CHANGE,0);
    } else if(event==ESP_AVRC_CT_CHANGE_NOTIFY_EVT && p->change_ntf.event_id==ESP_AVRC_RN_VOLUME_CHANGE) {
        message_t m={.kind=8,.status=p->change_ntf.event_parameter.volume}; xQueueSend(queue,&m,0);
        esp_avrc_ct_send_register_notification_cmd(1,ESP_AVRC_RN_VOLUME_CHANGE,0);
    } else if(event==ESP_AVRC_CT_SET_ABSOLUTE_VOLUME_RSP_EVT) {
        message_t m={.kind=9,.status=p->set_volume_rsp.volume};xQueueSend(queue,&m,0);
    }
}
static void gap(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *p) {
    if (event == ESP_BT_GAP_READ_RSSI_DELTA_EVT) {
        portENTER_CRITICAL(&snapshot_lock);
        bool same_peer = snapshot.connected && !memcmp(snapshot.active_peer, p->read_rssi_delta.bda, 6);
        portEXIT_CRITICAL(&snapshot_lock);
        if (same_peer && p->read_rssi_delta.stat == ESP_BT_STATUS_SUCCESS) {
            atomic_store(&rssi_delta, p->read_rssi_delta.rssi_delta);
            atomic_store(&rssi_at_ms, (uint32_t)(esp_timer_get_time()/1000));
        }
        atomic_store(&rssi_pending, false);
    }
    if (event == ESP_BT_GAP_DISC_RES_EVT) {
        char name[249] = "";
        int rssi = -129;
        for (int i = 0; i < p->disc_res.num_prop; i++) {
            esp_bt_gap_dev_prop_t *v = &p->disc_res.prop[i];
            if (v->type == ESP_BT_GAP_DEV_PROP_RSSI)
                rssi = *(int8_t *)v->val;
            if (v->type == ESP_BT_GAP_DEV_PROP_BDNAME) {
                size_t n = v->len < 248 ? v->len : 248;
                memcpy(name, v->val, n);
                name[n] = 0;
            }
            if (v->type == ESP_BT_GAP_DEV_PROP_EIR) {
                uint8_t n = 0;
                uint8_t *s =
                    esp_bt_gap_resolve_eir_data(v->val, ESP_BT_EIR_TYPE_CMPL_LOCAL_NAME, &n);
                if (!s)
                    s = esp_bt_gap_resolve_eir_data(v->val, ESP_BT_EIR_TYPE_SHORT_LOCAL_NAME, &n);
                if (s) {
                    memcpy(name, s, n);
                    name[n] = 0;
                }
            }
        }
        char address[18], detail[128];
        address_text(p->disc_res.bda, address);
        ESP_LOGI(TAG, "BT_DEVICE_FOUND %s name=%s RSSI=%d", address, name, rssi);
        snprintf(detail, sizeof(detail), "%s %s", address, name);
        sink_network_event("BT_DEVICE_FOUND", detail);
    } else if (event == ESP_BT_GAP_DISC_STATE_CHANGED_EVT) {
        message_t m = {.kind = p->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STARTED ? 2 : 3};
        if (xQueueSend(queue, &m, 0) != pdTRUE)
            sink_network_event("ERROR", "Discovery event queue full");
    } else if (event == ESP_BT_GAP_AUTH_CMPL_EVT) {
        message_t m = {.kind = 5, .status = p->auth_cmpl.stat};
        memcpy(m.address, p->auth_cmpl.bda, 6);
        if (xQueueSend(queue, &m, 0) != pdTRUE)
            sink_network_event("ERROR", "Authentication event queue full");
    } else if (event == ESP_BT_GAP_REMOVE_BOND_DEV_COMPLETE_EVT) {
        message_t m = {.kind = 4, .status = p->remove_bond_dev_cmpl.status};
        memcpy(m.address, p->remove_bond_dev_cmpl.bda, 6);
        if (xQueueSend(queue, &m, 0) != pdTRUE)
            sink_network_event("ERROR", "Bond event queue full");
    } else if (event == ESP_BT_GAP_READ_REMOTE_NAME_EVT &&
               p->read_rmt_name.stat == ESP_BT_STATUS_SUCCESS) {
        const char *source = (const char *)p->read_rmt_name.rmt_name;
        size_t n = strnlen(source, ESP_BT_GAP_MAX_BDNAME_LEN);
        if (n >= sizeof(peer_name))
            n = sizeof(peer_name) - 1;
        while (n && ((unsigned char)source[n] & 0xc0) == 0x80)
            n--;
        portENTER_CRITICAL(&snapshot_lock);
        memcpy(name_peer, p->read_rmt_name.bda, 6);
        memcpy(peer_name, source, n);
        peer_name[n] = 0;
        if (!memcmp(snapshot.active_peer, name_peer, 6))
            memcpy(snapshot.connected_name, peer_name, sizeof(peer_name));
        portEXIT_CRITICAL(&snapshot_lock);
    } else if (event == ESP_BT_GAP_MODE_CHG_EVT) {
        char detail[80];
        snprintf(detail, sizeof(detail), "mode=%u interval_slots=%u",
                 (unsigned)p->mode_chg.mode, (unsigned)p->mode_chg.interval);
        sink_network_event("BT_POWER_MODE_CHANGED", detail);
    } else if(event==ESP_BT_GAP_ACL_DISCONN_CMPL_STAT_EVT) {
        char detail[80];snprintf(detail,sizeof(detail),"ACL status=%u handle=%u",(unsigned)p->acl_disconn_cmpl_stat.reason,(unsigned)p->acl_disconn_cmpl_stat.handle);
        sink_network_event("BT_LINK_LOST",detail);
    } else if (event == ESP_BT_GAP_ACL_CONN_CMPL_STAT_EVT) {
        message_t m = {.kind = 6, .status = p->acl_conn_cmpl_stat.stat};
        memcpy(m.address, p->acl_conn_cmpl_stat.bda, 6);
        if (xQueueSend(queue, &m, 0) != pdTRUE)
            sink_network_event("ERROR", "ACL event queue full");
    } else if (event == ESP_BT_GAP_PIN_REQ_EVT) {
        esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
        esp_bt_gap_pin_reply(p->pin_req.bda, !p->pin_req.min_16_digit, 4, pin);
    } else if (event == ESP_BT_GAP_CFM_REQ_EVT) {
        uint8_t selected[6];
        bool allowed;
        portENTER_CRITICAL(&snapshot_lock);
        memcpy(selected, snapshot.active_peer, 6);
        allowed = snapshot.connected || snapshot.connecting;
        portEXIT_CRITICAL(&snapshot_lock);
        esp_bt_gap_ssp_confirm_reply(p->cfm_req.bda,
                                     allowed && !memcmp(selected, p->cfm_req.bda, 6));
    }
}
static bool connect_address(const uint8_t *address, bool select) {
    if (!ready) {
        fail("NOT_READY");
        return false;
    }
    if (connected) {
        if (!memcmp(address, active_peer, 6)) {
            if (select) {
                memcpy(preferred_peer, active_peer, 6);
                preferred = true;
                if (!save()) {
                    fail("PERSISTENCE_ERROR");
                    return false;
                }
            }
            emit("BT_CONNECTED", "Already connected");
            return true;
        }
        fail("BUSY");
        return false;
    }
    if (connecting || scanning || scan_requested || forgetting) {
        fail("BUSY");
        return false;
    }
    memcpy(active_peer, address, 6);
    select_on_success = select;
    pairing_notified = false;
    publish();
    sink_hci_reset_connect_status();
    esp_err_t result = esp_a2d_source_connect(active_peer);
    ESP_LOGI(TAG, "connect accepted: %s", esp_err_to_name(result));
    if (result == ESP_OK) {
        connecting = true;
        deadline = esp_timer_get_time() + 30000000;
        publish();
        return true;
    }
    next_retry = esp_timer_get_time() + 10000000;
    fail("SDK_REJECTED");
    return false;
}
static void handle_command(message_t *m) {
    const char *c = m->command;
    /* A scan-stop supersedes its in-progress start. The network reports the
       cancelled start separately, and the stop completes on the real callback. */
    if (operation_id[0] && strcmp(c, "scan-stop")) {
        sink_network_command_event(m->request_id, "COMMAND_FAILED", "BUSY");
        return;
    }
    snprintf(operation_id, sizeof(operation_id), "%s", m->request_id);
    operation_event[0] = 0;
    operation_deadline = esp_timer_get_time() + 10000000;
    if (!strncmp(c,"speaker-volume ",15)) {
        strcpy(operation_event,"BT_VOLUME_SET");
        if(!atomic_load(&volume_supported))fail("UNSUPPORTED_FEATURE");
        else sdk_result("speaker volume",esp_avrc_ct_send_set_absolute_volume_cmd((++volume_label%14)+2,atoi(c+15)*127/100));
    } else if (!strcmp(c, "scan")) {
        strcpy(operation_event, "BT_SCAN_STARTED");
        operation_deadline = esp_timer_get_time() + 20000000;
        if (!ready)
            fail("NOT_READY");
        else if (scanning || connecting || forgetting)
            fail("BUSY");
        else if (sdk_result("scan",
                            esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, 10, 0)))
            scan_requested = true;
    } else if (!strcmp(c, "scan-stop")) {
        strcpy(operation_event, "BT_SCAN_STOPPED");
        if (!scanning && !scan_requested)
            emit("BT_SCAN_STOPPED", "Already stopped");
        else
            sdk_result("scan stop", esp_bt_gap_cancel_discovery());
    } else if (!strncmp(c, "connect ", 8)) {
        strcpy(operation_event, "BT_CONNECTED");
        operation_deadline = esp_timer_get_time() + 35000000;
        uint8_t address[6];
        if (!sink_parse_address(c + 8, address))
            fail("INVALID_ARGUMENT");
        else
            connect_address(address, true);
    } else if (!strncmp(c, "preferred ", 10)) {
        strcpy(operation_event, "BT_PREFERRED_SAVED");
        uint8_t address[6];
        if (connecting || forgetting)
            fail("BUSY");
        else if (!strcmp(c + 10, "none")) {
            preferred = false;
            if (save())
                emit("BT_PREFERRED_SAVED", "");
            else
                fail("PERSISTENCE_ERROR");
        } else if (!sink_parse_address(c + 10, address))
            fail("INVALID_ARGUMENT");
        else {
            memcpy(preferred_peer, address, 6);
            preferred = true;
            next_retry = 0;
            if (save())
                emit("BT_PREFERRED_SAVED", "");
            else
                fail("PERSISTENCE_ERROR");
        }
    } else if (!strcmp(c, "disconnect")) {
        strcpy(operation_event, "BT_DISCONNECTED");
        autoreconnect = false;
        if (!save())
            fail("PERSISTENCE_ERROR");
        else if (!connected && !connecting)
            emit("BT_DISCONNECTED", "Already disconnected");
        else
            sdk_result("disconnect", esp_a2d_source_disconnect(active_peer));
    } else if (!strcmp(c, "auto on") || !strcmp(c, "auto off")) {
        strcpy(operation_event, "BT_POLICY_SAVED");
        autoreconnect = !strcmp(c, "auto on");
        if (save())
            emit("BT_POLICY_SAVED", "");
        else
            fail("PERSISTENCE_ERROR");
    } else if (!strcmp(c, "forget") || !strncmp(c, "forget ", 7)) {
        strcpy(operation_event, "BT_FORGOTTEN");
        bool valid;
        if (!strcmp(c, "forget")) {
            valid = preferred;
            if (valid)
                memcpy(forget_peer, preferred_peer, 6);
        } else
            valid = sink_parse_address(c + 7, forget_peer);
        if (!valid)
            fail("INVALID_ARGUMENT");
        else if (forgetting || ((connected || connecting) && !memcmp(active_peer, forget_peer, 6)))
            fail("BUSY");
        else if (sdk_result("forget bond", esp_bt_gap_remove_bond_device(forget_peer))) {
            forgetting = true;
            forget_deadline = esp_timer_get_time() + 10000000;
        }
    } else if (!strcmp(c, "status")) {
        ESP_LOGI(TAG, "ready=%d connected=%d connecting=%d scan=%d auto=%d heap=%lu minimum=%lu",
                 ready, connected, connecting, scanning, autoreconnect,
                 (unsigned long)esp_get_free_heap_size(),
                 (unsigned long)esp_get_minimum_free_heap_size());
        operation_id[0] = 0;
    } else
        fail("UNKNOWN_COMMAND");
    publish();
}
static void manager(void *arg) {
    message_t m;
    int64_t next_radio_check = 0, rssi_requested_at = 0;
    while (1) {
        if (xQueueReceive(queue, &m, pdMS_TO_TICKS(200))) {
            if (m.kind == 0)
                handle_command(&m);
            else if (m.kind == 2) {
                scanning = true;
                scan_requested = false;
                emit("BT_SCAN_STARTED", "");
            } else if (m.kind == 3) {
                scanning = false;
                scan_requested = false;
                emit("BT_SCAN_STOPPED", "");
            } else if (m.kind == 4 && forgetting && !memcmp(m.address, forget_peer, 6)) {
                forgetting = false;
                if (m.status != ESP_BT_STATUS_SUCCESS)
                    fail("SDK_REJECTED");
                else {
                    if (preferred && !memcmp(preferred_peer, forget_peer, 6))
                        preferred = false;
                    if (save())
                        emit("BT_FORGOTTEN", "");
                    else
                        fail("PERSISTENCE_ERROR");
                }
            } else if (m.kind == 6 && !memcmp(m.address, active_peer, 6)) {
                if (m.status != ESP_BT_STATUS_SUCCESS)
                    fail("SDK_REJECTED");
                else if (!pairing_notified && !is_bonded(active_peer)) {
                    pairing_notified = true;
                    emit("BT_PAIRING_STARTED", "");
                }
            } else if (m.kind == 5 && !memcmp(m.address, active_peer, 6)) {
                emit(m.status == ESP_BT_STATUS_SUCCESS ? "BT_PAIRING_SUCCEEDED"
                                                       : "BT_PAIRING_FAILED",
                     m.status == ESP_BT_STATUS_SUCCESS ? "" : "BT_AUTH_FAILED");
            } else if (m.kind == 8 || m.kind == 9) {
                atomic_store(&speaker_volume,m.status);
                char detail[8];snprintf(detail,sizeof(detail),"%d",m.status);
                if(m.kind==9)emit("BT_VOLUME_SET",detail);
                sink_network_event("BT_VOLUME_CHANGED",detail);
            } else if (m.kind == 1) {
                if (m.event == ESP_A2D_PROF_STATE_EVT &&
                    m.param.a2d_prof_stat.init_state == ESP_A2D_INIT_SUCCESS) {
                    esp_a2d_mcc_t codec={.type=ESP_A2D_MCT_SBC,
                        .cie.sbc_info={.samp_freq=2,.ch_mode=2,.block_len=1,
                            .num_subbands=1,.alloc_mthd=1,.min_bitpool=52,.max_bitpool=52}};
                    ESP_ERROR_CHECK(esp_a2d_source_register_stream_endpoint(0,&codec));
                    ESP_LOGI(TAG, "READY scan | connect MAC | preferred MAC/none | speaker-volume "
                                  "0..100 | disconnect | auto on/off | forget [MAC] | status");
                }
                if(m.event==ESP_A2D_REPORT_SNK_DELAY_VALUE_EVT)
                    atomic_store(&speaker_delay_tenths_ms,m.param.a2d_report_delay_value_stat.delay_value);
                if(m.event==ESP_A2D_CONNECTION_STATE_EVT && m.param.conn_stat.state==ESP_A2D_CONNECTION_STATE_DISCONNECTED)
                    atomic_store(&speaker_delay_tenths_ms,-1);
                if(m.event==ESP_A2D_SEP_REG_STATE_EVT) {
                    ready=m.param.a2d_sep_reg_stat.reg_state==ESP_A2D_SEP_REG_SUCCESS;
                    if(!ready)sink_network_event("ERROR","SBC endpoint registration failed");
                }
                if(m.event==ESP_A2D_AUDIO_CFG_EVT) {
                    esp_a2d_cie_sbc_t c=m.param.audio_cfg.mcc.cie.sbc_info;
                    bool valid=m.param.audio_cfg.mcc.type==ESP_A2D_MCT_SBC &&
                        c.samp_freq==2 && c.ch_mode==2 && c.block_len==1 &&
                        c.num_subbands==1 && c.alloc_mthd==1 && c.min_bitpool<=52 && c.max_bitpool>=52;
                    atomic_store(&sbc_codec_ready,valid);
                    atomic_store(&sbc_handle,m.param.audio_cfg.conn_hdl);
                    ESP_LOGI(TAG,"SBC codec negotiated valid=%d bitpool=%u..%u",valid,c.min_bitpool,c.max_bitpool);
                    if(!valid)sink_network_event("ERROR","Speaker rejected SBC transport profile");
                    if(streaming) {
                        bool usable=valid && atomic_load(&sbc_mtu)>=129;
                        stream_audio_output_ready(usable);
                        if(usable)emit("OUTPUT_READY", "");
                    }
                }
                if (m.event == ESP_A2D_CONNECTION_STATE_EVT) {
                    int state = m.param.conn_stat.state;
                    ESP_LOGI(TAG, "A2DP_CONNECTION state=%d", state);
                    if (state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
                        /* Permit the selected peer to initiate reconnection, but
                         * do not let another saved speaker take this sink over. */
                        bool expected = connecting ?
                            !memcmp(active_peer, m.param.conn_stat.remote_bda, 6) :
                            preferred && autoreconnect &&
                            !memcmp(preferred_peer, m.param.conn_stat.remote_bda, 6);
                        if (!expected) {
                            ESP_LOGW(TAG, "Rejecting connection from an unselected peer");
                            esp_a2d_source_disconnect(m.param.conn_stat.remote_bda);
                            continue;
                        }
                        atomic_store(&sbc_handle,m.param.conn_stat.conn_hdl);
                        atomic_store(&sbc_mtu,m.param.conn_stat.audio_mtu);
                        connected = true;
                        connecting = false;
                        streaming = false;
                        retry = 2;
                        memcpy(active_peer, m.param.conn_stat.remote_bda, 6);
                        bool saved = true;
                        if (select_on_success) {
                            memcpy(preferred_peer, active_peer, 6);
                            preferred = true;
                            saved = save();
                        }
                        select_on_success = false;
                        esp_bt_gap_read_remote_name(active_peer);
                        if (saved)
                            emit("BT_CONNECTED", "");
                        else
                            fail("PERSISTENCE_ERROR");
                        sdk_result("media ready",
                                   esp_a2d_media_ctrl(ESP_A2D_MEDIA_CTRL_CHECK_SRC_RDY));
                    } else if (state == ESP_A2D_CONNECTION_STATE_DISCONNECTED) {
                        bool was_connecting = connecting;
                        atomic_store(&sbc_codec_ready,false);
                        atomic_store(&rssi_delta, 128);
                        atomic_store(&rssi_pending, false);
                        connected = false;
                        connecting = false;
                        streaming = false;
                        stream_audio_output_ready(false);
                        char reason[48];
                        snprintf(reason, sizeof(reason), "A2DP disconnect reason %d",
                                 m.param.conn_stat.disc_rsn);
                        if (was_connecting && sink_hci_connect_status() == ESP_HCI_ERR_CONN_LIMIT_EXCEEDED)
                            fail("BT_CONNECTION_LIMIT");
                        emit("BT_DISCONNECTED", reason);
                        next_retry = esp_timer_get_time() + (int64_t)retry * 1000000;
                        retry = retry < 30 ? retry * 2 : 60;
                    }
                } else if (m.event == ESP_A2D_MEDIA_CTRL_ACK_EVT) {
                    ESP_LOGI(TAG, "MEDIA_ACK cmd=%d status=%d", m.param.media_ctrl_stat.cmd,
                             m.param.media_ctrl_stat.status);
                    if (m.param.media_ctrl_stat.status != ESP_A2D_MEDIA_CTRL_ACK_SUCCESS)
                        sink_network_event("ERROR", "Bluetooth media request failed");
                    else if (m.param.media_ctrl_stat.cmd == ESP_A2D_MEDIA_CTRL_CHECK_SRC_RDY)
                        sdk_result("media start", esp_a2d_media_ctrl(ESP_A2D_MEDIA_CTRL_START));
                } else if (m.event == ESP_A2D_AUDIO_STATE_EVT) {
                    ESP_LOGI(TAG, "AUDIO_STATE %d", m.param.audio_stat.state);
                    streaming = m.param.audio_stat.state == ESP_A2D_AUDIO_STATE_STARTED;
                    bool usable=streaming;
                    usable=usable && atomic_load(&sbc_codec_ready) && atomic_load(&sbc_mtu)>=129;
                    stream_audio_output_ready(usable);
                    emit(usable ? "OUTPUT_READY" : "OUTPUT_STOPPED",
                         "");
                }
            }
            publish();
        }
        int64_t now = esp_timer_get_time();
        if (ready && now >= next_radio_check) {
            next_radio_check = now + 5000000;
            if (connected && (!atomic_load(&rssi_pending) || now-rssi_requested_at>10000000)) {
                atomic_store(&rssi_pending, true);
                rssi_requested_at = now;
                if (esp_bt_gap_read_rssi_delta(active_peer) != ESP_OK)
                    atomic_store(&rssi_pending, false);
            }
        }
        int play_status = stream_audio_playback_active() ? ESP_AVRC_PLAYBACK_PLAYING : ESP_AVRC_PLAYBACK_PAUSED;
        wifi_sink_avrc_set_playback_status((uint8_t)play_status);
        if (atomic_load(&play_notification_pending) && play_status != atomic_load(&notified_play_status) &&
            atomic_exchange(&play_notification_pending, false)) {
            esp_avrc_rn_param_t value = {.playback = play_status};
            if (esp_avrc_tg_send_rn_rsp(ESP_AVRC_RN_PLAY_STATUS_CHANGE, ESP_AVRC_RN_RSP_CHANGED, &value) != ESP_OK)
                atomic_store(&play_notification_pending, true);
        }
        if (operation_id[0] && now > operation_deadline)
            fail("TIMEOUT");
        if (forgetting && now > forget_deadline) {
            forgetting = false;
            fail("TIMEOUT");
        }
        if (connecting && now > deadline) {
            ESP_LOGW(TAG, "Connection timeout; requesting disconnect");
            esp_a2d_source_disconnect(active_peer);
            connecting = false;
            next_retry = now + 60000000;
        }
        if (ready && preferred && autoreconnect && !connected && !connecting && !scanning &&
            !scan_requested && !forgetting && !operation_id[0] && now >= next_retry) {
            char address[18];
            address_text(preferred_peer, address);
            sink_network_event("BT_RECONNECTING", address);
            connect_address(preferred_peer, false);
        }
        publish();
    }
}
static void console(void *arg) {
    char line[32];
    size_t n = 0;
    bool overflow = false;
    while (1) {
        int c = getchar();
        if (c == EOF) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (c == '\n' || c == '\r') {
            if (n && !overflow) {
                line[n] = 0;
                sink_output_command(line, NULL);
            }
            n = 0;
            overflow = false;
        } else if (n < sizeof(line) - 1)
            line[n++] = c;
        else
            overflow = true;
    }
}
static void sbc_output_task(void *arg) {
    uint8_t data[7*116];
    uint32_t timestamp=0,fraction=0;
    TickType_t interval=pdMS_TO_TICKS(2);if(!interval)interval=1;
    int64_t due=0;
    while(1) {
        vTaskDelay(interval);
        output_snapshot_t state;
        portENTER_CRITICAL(&snapshot_lock);state=snapshot;portEXIT_CRITICAL(&snapshot_lock);
        if(!state.connected || !state.streaming || !atomic_load(&sbc_codec_ready)) {due=0;continue;}
        int64_t now=esp_timer_get_time();
        if(!due || now-due>100000)due=now;
        if(now<due)continue;
        uint32_t stats[10];wifi_sink_sbc_diagnostics(stats);
        if(stats[0]>=2)continue;
        unsigned maximum=(atomic_load(&sbc_mtu)-13)/116;
        if(maximum>7)maximum=7;
        if(!maximum)continue;
        uint32_t token=0;uint64_t position=0;
        unsigned count=stream_audio_sbc_peek(data,maximum,&token,&position);
        bool audio=count>0;
        if(!count) {
            count=maximum;
            for(unsigned i=0;i<count;i++)memcpy(data+i*116,sink_sbc_silence,116);
        }
        esp_a2d_audio_buff_t *packet=esp_a2d_audio_buff_alloc(count*116);
        if(!packet)continue;
        memcpy(packet->data,data,count*116);
        packet->data_len=count*116;packet->number_frame=count;packet->timestamp=timestamp;
        esp_err_t result=esp_a2d_source_audio_data_send(atomic_load(&sbc_handle),packet);
        if(result!=ESP_OK) {esp_a2d_audio_buff_free(packet);continue;}
        if(audio)stream_audio_sbc_commit(count,token,position);
        timestamp+=count*128;
        due+=sink_sbc_duration_us(count,&fraction);
    }
}
void app_main(void) {
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(nvs_open("sink", NVS_READWRITE, &prefs));
    size_t size = 6;
    preferred = nvs_get_blob(prefs, "peer", preferred_peer, &size) == ESP_OK && size == 6;
    uint8_t a = 1;
    nvs_get_u8(prefs, "auto", &a);
    autoreconnect = a;
    publish();
    stream_audio_init();
    queue = xQueueCreate(16, sizeof(message_t));
    configASSERT(queue);
    xTaskCreate(manager, "bt_manager", 4096, NULL, 5, NULL);
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    esp_power_level_t power_min, power_max;
    if (esp_bredr_tx_power_get(&power_min, &power_max) == ESP_OK)
        ESP_LOGI(TAG, "Initial BR/EDR TX range %d..%d dBm", 3*(int)power_min-12, 3*(int)power_max-12);
#if CONFIG_SINK_BT_FIXED_TX_POWER_9DBM
    ESP_ERROR_CHECK(esp_bredr_tx_power_set(ESP_PWR_LVL_P9, ESP_PWR_LVL_P9));
#endif
    if (esp_bredr_tx_power_get(&power_min, &power_max) == ESP_OK) {
        tx_power_min = 3*(int)power_min-12;
        tx_power_max = 3*(int)power_max-12;
        ESP_LOGI(TAG, "Immediate BR/EDR TX range %d..%d dBm", tx_power_min, tx_power_max);
    }
#if CONFIG_BTDM_CTRL_MODEM_SLEEP
    /* USB-powered audio bridge: test without controller sleep/wake transitions.
     * ACL sniff mode is separate and remains visible through GAP mode events. */
    ESP_ERROR_CHECK(esp_bt_sleep_disable());
#endif
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());
    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap));
    ESP_ERROR_CHECK(esp_bt_gap_set_device_name("ESP32 WiFi Sink"));
    esp_bt_io_cap_t cap = ESP_BT_IO_CAP_NONE;
    ESP_ERROR_CHECK(esp_bt_gap_set_security_param(ESP_BT_SP_IOCAP_MODE, &cap, sizeof(cap)));
    ESP_ERROR_CHECK(esp_avrc_ct_init());
    ESP_ERROR_CHECK(esp_avrc_ct_register_callback(avrc));
    ESP_ERROR_CHECK(esp_avrc_tg_init());
    ESP_ERROR_CHECK(esp_avrc_tg_register_callback(avrc_target));
    esp_avrc_psth_bit_mask_t commands = {0};
    const esp_avrc_pt_cmd_t buttons[] = {ESP_AVRC_PT_CMD_PLAY, ESP_AVRC_PT_CMD_PAUSE,
                                       ESP_AVRC_PT_CMD_FORWARD, ESP_AVRC_PT_CMD_BACKWARD};
    for (unsigned i = 0; i < sizeof(buttons)/sizeof(buttons[0]); i++)
        configASSERT(esp_avrc_psth_bit_mask_operation(ESP_AVRC_BIT_MASK_OP_SET, &commands, buttons[i]));
    ESP_ERROR_CHECK(esp_avrc_tg_set_psth_cmd_filter(ESP_AVRC_PSTH_FILTER_SUPPORTED_CMD, &commands));
    esp_avrc_rn_evt_cap_mask_t notifications = {0};
    configASSERT(esp_avrc_rn_evt_bit_mask_operation(ESP_AVRC_BIT_MASK_OP_SET, &notifications, ESP_AVRC_RN_PLAY_STATUS_CHANGE));
    ESP_ERROR_CHECK(esp_avrc_tg_set_rn_evt_cap(&notifications));
    ESP_ERROR_CHECK(esp_a2d_register_callback(a2dp));
    ESP_ERROR_CHECK(esp_a2d_source_init());
    ESP_ERROR_CHECK(esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_NON_DISCOVERABLE));
    xTaskCreate(console, "console", 3072, NULL, 3, NULL);
    xTaskCreatePinnedToCore(sbc_output_task,"sbc_output",4096,NULL,5,NULL,1);
    sink_network_start();
}
