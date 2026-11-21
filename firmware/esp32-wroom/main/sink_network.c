#include "sink_control.h"
#include "stream_audio.h"
#include "sbc_transport.h"
#include "sink_transport.h"
#include "esp_event.h"
#include "esp_app_desc.h"
#include "esp_coexist.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "json_validate.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "psa/crypto.h"
#include "nvs_flash.h"
#include "sink_setup.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static EventGroupHandle_t wifi_events;
static QueueHandle_t events;
static atomic_uint wifi_reconnects;
static atomic_bool event_overflow;
static int64_t reboot_at;
static char boot_id[37], connection_id[37];
static uint8_t payload[2049];
static uint32_t tx_seq, rx_seq;
static uint32_t io_tx_max_us, io_rx_max_us, io_loop_max_us;
static int sock = -1;
static bool negotiated,control_frame_completed;
static sink_tcp_tx_t control_tx;
static uint8_t control_header[16];
static size_t rx_header_used,rx_payload_used;
static uint32_t rx_payload_length;
static uint32_t tcp_tx_backpressure,tcp_rx_partial,tcp_optional_dropped,tcp_queue_overflow;
static int64_t tx_progress_at;

typedef struct {
    char name[40];
    char detail[128];
    char request_id[37];
} device_event_t;
static const char *TAG = "network";
static bool coex_bt_applied;
void sink_network_command_event(const char *request_id, const char *name, const char *detail) {
    if (!events)
        return;
    device_event_t e = {0};
    snprintf(e.name, sizeof(e.name), "%s", name);
    snprintf(e.detail, sizeof(e.detail), "%s", detail ? detail : "");
    if (request_id)
        snprintf(e.request_id, sizeof(e.request_id), "%s", request_id);
    if(xQueueSend(events,&e,0)!=pdTRUE && e.request_id[0])atomic_store(&event_overflow,true);
}
void sink_network_event(const char *name, const char *detail) {
    sink_network_command_event(NULL, name, detail);
}
static bool uuid_parse(const char *s, uint8_t b[16]) {
    if (!s || strlen(s) != 36)
        return false;
    unsigned n = 0;
    for (unsigned i = 0; i < 36;) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (s[i++] != '-')
                return false;
            continue;
        }
        char x = s[i++], y = s[i++];
        int a = x >= '0' && x <= '9' ? x - '0' : x >= 'a' && x <= 'f' ? x - 'a' + 10 : -1;
        int c = y >= '0' && y <= '9' ? y - '0' : y >= 'a' && y <= 'f' ? y - 'a' + 10 : -1;
        if (a < 0 || c < 0)
            return false;
        b[n++] = (a << 4) | c;
    }
    unsigned any = 0;
    for (unsigned i = 0; i < 16; i++)
        any |= b[i];
    return n == 16 && any != 0;
}
static void uuid_new(char s[37]) {
    uint8_t b[16];
    esp_fill_random(b, 16);
    b[6] = (b[6] & 15) | 64;
    b[8] = (b[8] & 63) | 128;
    snprintf(s, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x", b[0],
             b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13],
             b[14], b[15]);
}
static const char *text(cJSON *j, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}
static bool flush_control(void) {
    if(!control_tx.used)return true;
    size_t n=sink_tx_contiguous(&control_tx);
    int64_t started=esp_timer_get_time();
    int written=send(sock,control_tx.bytes+control_tx.head,n,0);
    uint32_t elapsed=(uint32_t)(esp_timer_get_time()-started);
    if(elapsed>io_tx_max_us)io_tx_max_us=elapsed;
    if(written<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR)) {
        tcp_tx_backpressure++;return true;
    }
    if(written<=0)return false;
    sink_tx_consume(&control_tx,(size_t)written);tx_progress_at=esp_timer_get_time();
    return true;
}
static bool send_payload(uint8_t type,const char *data,size_t n) {
    if(tx_seq==UINT32_MAX)return false;
    unsigned result=sink_tx_frame(&control_tx,type,tx_seq+1,data,n);
    if(result==2){tcp_optional_dropped++;return true;}
    if(!result){tcp_queue_overflow++;return false;}
    tx_seq++;return true;
}
static bool send_json(uint8_t type, cJSON *j) {
    if (!j)
        return false;
    char *s = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (!s)
        return false;
    bool ok = send_payload(type, s, strlen(s));
    free(s);
    return ok;
}
static cJSON *object(void) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "connection_id", connection_id);
    return j;
}
static bool error(const char *code, const char *message) {
    cJSON *j = object();
    cJSON_AddStringToObject(j, "code", code);
    cJSON_AddStringToObject(j, "message", message);
    return send_json(9, j);
}
static cJSON *status(void) {
    cJSON *j = stream_audio_status();
    cJSON_AddStringToObject(j, "connection_id", connection_id);
#ifdef CONFIG_SINK_OUTPUT_PCM5102
    cJSON_AddItemToObject(j, "output", sink_output_status());
#else
    cJSON_AddItemToObject(j, "bluetooth", sink_output_status());
#endif
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        cJSON_AddNumberToObject(j, "wifi_rssi", ap.rssi);
        cJSON_AddNumberToObject(j, "wifi_channel", ap.primary);
        char bssid[18];
        snprintf(bssid, sizeof(bssid), "%02x:%02x:%02x:%02x:%02x:%02x",
                 ap.bssid[0], ap.bssid[1], ap.bssid[2], ap.bssid[3], ap.bssid[4], ap.bssid[5]);
        cJSON_AddStringToObject(j, "wifi_bssid", bssid);
    }
    cJSON_AddNumberToObject(j, "wifi_reconnects", wifi_reconnects);
    cJSON *rx_config = cJSON_AddObjectToObject(j, "network_config");
    cJSON_AddBoolToObject(rx_config, "tcp_sack", CONFIG_LWIP_TCP_SACK_OUT);
    cJSON_AddNumberToObject(rx_config, "wifi_static_rx", CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM);
    cJSON_AddNumberToObject(rx_config, "wifi_dynamic_rx", CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM);
    cJSON_AddNumberToObject(rx_config, "wifi_rx_ba_window", CONFIG_ESP_WIFI_RX_BA_WIN);
    cJSON_AddStringToObject(j,"coex_preference",coex_bt_applied?"bluetooth":"system_default");
    cJSON_AddNumberToObject(j,"io_tx_us",io_tx_max_us);
    cJSON_AddNumberToObject(j,"io_rx_us",io_rx_max_us);
    cJSON_AddNumberToObject(j,"io_loop_us",io_loop_max_us);
    cJSON_AddNumberToObject(j,"control_tx_high_water",control_tx.high_water);
    cJSON_AddNumberToObject(j,"control_backpressure",tcp_tx_backpressure);
    cJSON_AddNumberToObject(j,"control_rx_partial",tcp_rx_partial);
    cJSON_AddNumberToObject(j,"control_status_dropped",tcp_optional_dropped);
    cJSON_AddNumberToObject(j,"control_overflow",tcp_queue_overflow);
    cJSON_AddNumberToObject(j, "network_stack_min_free_bytes", uxTaskGetStackHighWaterMark(NULL));
    esp_netif_ip_info_t ip;
    esp_netif_t *iface = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (iface && esp_netif_get_ip_info(iface, &ip) == ESP_OK) {
        char b[20];
        snprintf(b, sizeof(b), IPSTR, IP2STR(&ip.ip));
        cJSON_AddStringToObject(j, "ip", b);
    }
    return j;
}
static cJSON *capabilities(void) {
    cJSON *j = cJSON_CreateObject();
    if (!j)
        return NULL;
#ifdef CONFIG_SINK_OUTPUT_PCM5102
    cJSON_AddBoolToObject(j, "bluetooth_management", false);
#else
    cJSON_AddBoolToObject(j, "bluetooth_management", true);
#endif
    cJSON_AddNumberToObject(j,"max_control_payload_bytes",SINK_TCP_MAX_OUTPUT);
    cJSON_AddStringToObject(j,"sbc_audio",SINK_SBC_PROFILE);
    cJSON_AddBoolToObject(j, "adaptive_buffer", true);
    cJSON_AddNumberToObject(j,"output_timing",1);
    cJSON_AddBoolToObject(j, "scheduled_start", false);
    cJSON_AddBoolToObject(j, "clock_sync", false);
    cJSON_AddBoolToObject(j, "rate_adjustment", false);
    cJSON_AddBoolToObject(j, "hardware_volume", false);
    cJSON_AddStringToObject(j, "device_volume_method", "digital_attenuation");
    cJSON_AddStringToObject(j, "presentation_feedback", "submission_only");
    cJSON_AddNumberToObject(j, "buffer_capacity_frames", SINK_SBC_BUFFER_BYTES);
    return j;
}
static cJSON *device_info(void) {
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_BASE));
    char id[32];
    snprintf(id, sizeof(id), "esp32-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3],
             mac[4], mac[5]);
    cJSON *j = cJSON_CreateObject();
    if (!j)
        return NULL;
    cJSON_AddStringToObject(j, "device_id", id);
    cJSON_AddStringToObject(j, "boot_id", boot_id);
    cJSON_AddStringToObject(j, "firmware", esp_app_get_description()->version);
    cJSON_AddNumberToObject(j, "protocol", 1);
    cJSON *versions = cJSON_AddArrayToObject(j, "versions");
    if (!versions) {
        cJSON_Delete(j);
        return NULL;
    }
    cJSON_AddItemToArray(versions, cJSON_CreateNumber(1));
    cJSON_AddItemToObject(j, "output", sink_output_status());
    cJSON_AddItemToObject(j, "capabilities", capabilities());
    cJSON_AddNumberToObject(j, "buffer_capacity_frames", SINK_SBC_BUFFER_BYTES);
    return j;
}
static bool hello(void) { return send_json(1, device_info()); }
/* The network task serializes command execution; completed IDs are cached within
   the TCP connection. Async Bluetooth operations finish only on real events. */
static char pending_id[37], pending_op[40];
static int64_t pending_deadline;
#define CACHE_COUNT 4
static char cached_id[CACHE_COUNT][37];
static uint8_t cached_body[CACHE_COUNT][32];
static char *cached_result[CACHE_COUNT];
static unsigned cache_next;
static bool complete(const char *id, bool ok, const char *code, cJSON *result) {
    cJSON *j = object();
    if (!j) {
        cJSON_Delete(result);
        return false;
    }
    cJSON_AddStringToObject(j, "event", "COMMAND_COMPLETED");
    cJSON_AddStringToObject(j, "request_id", id);
    cJSON_AddBoolToObject(j, "success", ok);
    if (result)
        cJSON_AddItemToObject(j, "result", result);
    if (!ok) {
        cJSON *e = cJSON_AddObjectToObject(j, "error");
        cJSON_AddStringToObject(e, "code", code);
        cJSON_AddStringToObject(e, "message", code);
    }
    for (int i = 0; i < CACHE_COUNT; i++)
        if (!strcmp(id, cached_id[i])) {
            free(cached_result[i]);
            cached_result[i] = cJSON_PrintUnformatted(j);
            if (!cached_result[i]) {
                cJSON_Delete(j);
                return false;
            }
            break;
        }
    /* Reuse the exact cached wire result; avoid a second JSON serialization. */
    for(int i=0;i<CACHE_COUNT;i++)if(!strcmp(id,cached_id[i])&&cached_result[i]) {
        cJSON_Delete(j);
        return send_payload(4,cached_result[i],strlen(cached_result[i]));
    }
    return send_json(4, j);
}
static bool ack(const char *id, bool accepted, const char *code) {
    cJSON *j = object();
    cJSON_AddStringToObject(j, "request_id", id);
    cJSON_AddBoolToObject(j, "accepted", accepted);
    if (!accepted) {
        cJSON *e = cJSON_AddObjectToObject(j, "error");
        cJSON_AddStringToObject(e, "code", code);
        cJSON_AddStringToObject(e, "message", code);
    }
    return send_json(3, j);
}
static bool args_only(cJSON *args, const char *allowed) {
    if (!args)
        return true;
    if (!cJSON_IsObject(args))
        return false;
    cJSON *child;
    cJSON_ArrayForEach(child, args) {
        char key[80];
        if (!child->string || strlen(child->string) > 76 || strchr(child->string, '|'))
            return false;
        snprintf(key, sizeof(key), "|%s|", child->string);
        if (!strstr(allowed, key))
            return false;
    }
    return true;
}
static bool integer(cJSON *value, double min, double max) {
    return cJSON_IsNumber(value) && value->valuedouble >= min && value->valuedouble <= max &&
           value->valuedouble == (uint32_t)value->valuedouble;
}
static cJSON *volume_result(void) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddNumberToObject(j, "volume", stream_audio_get_volume());
    cJSON_AddStringToObject(j, "method", "device_digital_attenuation");
    cJSON_AddBoolToObject(j, "hardware_volume", false);
    return j;
}
static bool command(cJSON *j, const char *raw) {
    const char *id = text(j, "request_id"), *cmd = text(j, "command"),
               *conn = text(j, "connection_id");
    uint8_t uuid[16];
    if (!id || !cmd || !conn || strcmp(conn, connection_id) || !uuid_parse(id, uuid))
        return error("INVALID_ARGUMENT", "Invalid command identity");
    if (strlen(raw) >= 768)
        return ack(id, false, "INVALID_ARGUMENT");
    uint8_t hash[32];
    size_t hash_length = 0;
    if (psa_hash_compute(PSA_ALG_SHA_256, (const uint8_t *)raw, strlen(raw),
                         hash, sizeof(hash), &hash_length) != PSA_SUCCESS ||
        hash_length != sizeof(hash))
        return ack(id, false, "INTERNAL_ERROR");
    for (int i = 0; i < CACHE_COUNT; i++)
        if (!strcmp(id, cached_id[i])) {
            if (memcmp(hash, cached_body[i], 32))
                return ack(id, false, "REQUEST_ID_CONFLICT");
            if (!ack(id, true, NULL))
                return false;
            return cached_result[i] ? send_payload(4, cached_result[i], strlen(cached_result[i]))
                                    : true;
        }
    cJSON *args = cJSON_GetObjectItemCaseSensitive(j, "args");
    bool bluetooth = !strncmp(cmd, "BLUETOOTH_", 10);
#ifdef CONFIG_SINK_OUTPUT_PCM5102
    if (bluetooth)
        return ack(id, false, "UNSUPPORTED_OUTPUT");
#endif
    const char *allowed = NULL;
    char serial[96] = {0};
    const char *finish = NULL;
    uint8_t bid[16] = {0};
    uint32_t requested_epoch = 0;
    uint64_t position = 0;
    bool clocked=false;
    unsigned volume = 0, buffer_target = SINK_SBC_BUFFER_BYTES;
    if (!strcmp(cmd, "GET_INFO") || !strcmp(cmd, "GET_STATUS") || !strcmp(cmd, "GET_DIAGNOSTICS") ||
        !strcmp(cmd, "GET_CAPABILITIES") || !strcmp(cmd, "GET_OUTPUT_TIMING") || !strcmp(cmd, "GET_VOLUME") ||
        !strcmp(cmd, "RESET_STATISTICS") || !strcmp(cmd, "REBOOT") || !strcmp(cmd, "AUDIO_STOP") ||
        !strcmp(cmd, "AUDIO_PAUSE") || !strcmp(cmd, "AUDIO_FLUSH"))
        allowed = "";
    else if (!strcmp(cmd, "SET_VOLUME")) {
        allowed = "|volume|";
        cJSON *v = cJSON_GetObjectItemCaseSensitive(args, "volume");
        if (!integer(v, 0, 100))
            return ack(id, false, "INVALID_ARGUMENT");
        volume = v->valueint;
    } else if (!strcmp(cmd, "AUDIO_CONFIGURE")) {
        allowed = "|rate||channels||bits||encoding|";
        cJSON *r = cJSON_GetObjectItemCaseSensitive(args, "rate"),
              *c = cJSON_GetObjectItemCaseSensitive(args, "channels"),
              *b = cJSON_GetObjectItemCaseSensitive(args, "bits");
        const char *encoding = text(args, "encoding");
        if (!integer(r, 44100, 44100) || !integer(c, 2, 2) || !integer(b, 16, 16) ||
            (cJSON_HasObjectItem(args, "encoding") && (!encoding || strcmp(encoding, "s16le"))))
            return ack(id, false, "INVALID_ARGUMENT");
    } else if (!strcmp(cmd, "AUDIO_START") || !strcmp(cmd, "AUDIO_RESUME") || !strcmp(cmd,"SET_BUFFER_TARGET")) {
        allowed = !strcmp(cmd,"SET_BUFFER_TARGET") ? "|playback_id||epoch||buffer_target_frames|" : "|playback_id||epoch||next_sample_position||presentation_us||buffer_target_frames||clocked|";
        const char *p = text(args, "playback_id"), *pos = !strcmp(cmd,"SET_BUFFER_TARGET") ? "0" : text(args, "next_sample_position");
        cJSON *clocked_arg=cJSON_GetObjectItemCaseSensitive(args,"clocked");
        if(clocked_arg && !cJSON_IsBool(clocked_arg))return ack(id,false,"INVALID_ARGUMENT");
        clocked=cJSON_IsTrue(clocked_arg);
        cJSON *target = cJSON_GetObjectItemCaseSensitive(args,"buffer_target_frames");
        if (target && (!integer(target,1024,SINK_SBC_BUFFER_BYTES) || target->valueint%1024))
            return ack(id,false,"INVALID_ARGUMENT");
        if (!strcmp(cmd,"SET_BUFFER_TARGET") && !target) return ack(id,false,"INVALID_ARGUMENT");
        if(target)buffer_target=target->valueint;
        cJSON *e = cJSON_GetObjectItemCaseSensitive(args, "epoch");
        if (cJSON_HasObjectItem(args, "presentation_us"))
            return ack(id, false, "UNSUPPORTED_FEATURE");
        if (!p || !uuid_parse(p, bid) || !pos || !pos[0] || !integer(e, 1, UINT32_MAX))
            return ack(id, false, "INVALID_ARGUMENT");
        for (const char *cursor = pos; *cursor; cursor++)
            if (*cursor < '0' || *cursor > '9')
                return ack(id, false, "INVALID_ARGUMENT");
        errno = 0;
        char *end;
        position = strtoull(pos, &end, 10);
        if (errno || *end || position == UINT64_MAX)
            return ack(id, false, "INVALID_ARGUMENT");
        requested_epoch = (uint32_t)e->valuedouble;
    } else if (!strcmp(cmd,"BLUETOOTH_SET_VOLUME")) {
        allowed="|volume|";
        cJSON *v=cJSON_GetObjectItemCaseSensitive(args,"volume");
        if(!integer(v,0,100))return ack(id,false,"INVALID_ARGUMENT");
        snprintf(serial,sizeof(serial),"speaker-volume %d",v->valueint);
        finish="BT_VOLUME_SET";
    } else if (!strcmp(cmd, "BLUETOOTH_SCAN_START")) {
        allowed = "";
        strcpy(serial, "scan");
        finish = "BT_SCAN_STARTED";
    } else if (!strcmp(cmd, "BLUETOOTH_SCAN_STOP")) {
        allowed = "";
        strcpy(serial, "scan-stop");
        finish = "BT_SCAN_STOPPED";
    } else if (!strcmp(cmd, "BLUETOOTH_CONNECT") || !strcmp(cmd, "BLUETOOTH_PAIR") ||
               !strcmp(cmd, "BLUETOOTH_SET_PREFERRED")) {
        allowed = "|address|";
        cJSON *item = cJSON_GetObjectItemCaseSensitive(args, "address");
        const char *address = text(args, "address");
        uint8_t b[6];
        bool setting = !strcmp(cmd, "BLUETOOTH_SET_PREFERRED");
        if (setting && cJSON_IsNull(item)) {
            strcpy(serial, "preferred none");
            finish = "BT_PREFERRED_SAVED";
        } else {
            if (!sink_parse_address(address, b))
                return ack(id, false, "INVALID_ARGUMENT");
            snprintf(serial, sizeof(serial), "%s %s", setting ? "preferred" : "connect", address);
            finish = setting ? "BT_PREFERRED_SAVED" : "BT_CONNECTED";
        }
    } else if (!strcmp(cmd, "BLUETOOTH_DISCONNECT")) {
        allowed = "";
        strcpy(serial, "disconnect");
        finish = "BT_DISCONNECTED";
    } else if (!strcmp(cmd, "BLUETOOTH_FORGET")) {
        allowed = "|address|";
        const char *address = text(args, "address");
        uint8_t b[6];
        if (cJSON_HasObjectItem(args, "address")) {
            if (!sink_parse_address(address, b))
                return ack(id, false, "INVALID_ARGUMENT");
            snprintf(serial, sizeof(serial), "forget %s", address);
        } else
            strcpy(serial, "forget");
        finish = "BT_FORGOTTEN";
    } else if (!strcmp(cmd, "BLUETOOTH_SET_AUTORECONNECT")) {
        allowed = "|enabled|";
        cJSON *enabled = cJSON_GetObjectItemCaseSensitive(args, "enabled");
        if (!cJSON_IsBool(enabled))
            return ack(id, false, "INVALID_ARGUMENT");
        strcpy(serial, cJSON_IsTrue(enabled) ? "auto on" : "auto off");
        finish = "BT_POLICY_SAVED";
    }
    if (!allowed)
        return ack(id, false, "UNKNOWN_COMMAND");
    if (!args_only(args, allowed))
        return ack(id, false, "INVALID_ARGUMENT");
    if (bluetooth && pending_id[0]) {
        if (!strcmp(cmd, "BLUETOOTH_SCAN_STOP") && !strcmp(pending_op, "BT_SCAN_STARTED")) {
            if (!complete(pending_id, false, "CANCELLED", NULL))
                return false;
            pending_id[0] = 0;
        } else
            return ack(id, false, "BUSY");
    }
    unsigned slot;
    do {
        slot = cache_next++ % CACHE_COUNT;
    } while (pending_id[0] && !strcmp(cached_id[slot], pending_id));
    snprintf(cached_id[slot], 37, "%s", id);
    memcpy(cached_body[slot], hash, 32);
    free(cached_result[slot]);
    cached_result[slot] = NULL;
    if (!ack(id, true, NULL))
        return false;
    if (!strcmp(cmd, "GET_INFO"))
        return complete(id, true, NULL, device_info());
    if (!strcmp(cmd, "GET_CAPABILITIES"))
        return complete(id, true, NULL, capabilities());
    if (!strcmp(cmd, "GET_STATUS") || !strcmp(cmd, "GET_DIAGNOSTICS"))
        return complete(id, true, NULL, status());
    if (!strcmp(cmd,"GET_OUTPUT_TIMING"))
        return complete(id,true,NULL,stream_audio_flow());
    if (!strcmp(cmd, "GET_VOLUME"))
        return complete(id, true, NULL, volume_result());
    if (!strcmp(cmd, "SET_VOLUME")) {
        bool ok = stream_audio_set_volume(volume);
        return complete(id, ok, "PERSISTENCE_ERROR", ok ? volume_result() : NULL);
    }
    if (!strcmp(cmd, "RESET_STATISTICS")) {
        stream_audio_reset_statistics();
        wifi_reconnects = 0;
        io_tx_max_us=io_rx_max_us=io_loop_max_us=0;
        return complete(id, true, NULL, status());
    }
    if (!strcmp(cmd, "REBOOT")) {
        stream_audio_clear();
        bool ok = complete(id, true, NULL, NULL);
        if (ok)
            reboot_at = esp_timer_get_time() + 250000;
        return ok;
    }
    if (!strcmp(cmd, "AUDIO_STOP") || !strcmp(cmd, "AUDIO_PAUSE") || !strcmp(cmd, "AUDIO_FLUSH")) {
        stream_audio_clear();
        return complete(id, true, NULL, stream_audio_flow());
    }
    if (!strcmp(cmd, "AUDIO_CONFIGURE"))
        return complete(id, true, NULL, NULL);
    if (!strcmp(cmd,"SET_BUFFER_TARGET")) {
        bool ok=stream_audio_buffer_target(bid,requested_epoch,buffer_target);
        return complete(id,ok,"STALE_STREAM",stream_audio_flow());
    }
    if (!strcmp(cmd, "AUDIO_START") || !strcmp(cmd, "AUDIO_RESUME")) {
        bool ok = stream_audio_start(bid, requested_epoch, position);
        if(ok){ok=stream_audio_buffer_target(bid,requested_epoch,buffer_target);stream_audio_clocked(clocked);}
        return complete(id, ok, "OUTPUT_DISCONNECTED", stream_audio_flow());
    }
    if (serial[0]) {
        if (!sink_output_command(serial, id))
            return complete(id, false, "BUSY", NULL);
        snprintf(pending_id, sizeof(pending_id), "%s", id);
        snprintf(pending_op, sizeof(pending_op), "%s", finish);
        pending_deadline = esp_timer_get_time() + (!strcmp(finish, "BT_SCAN_STOPPED") ? 20000000
                                                   : !strcmp(finish, "BT_CONNECTED")  ? 35000000
                                                                                      : 10000000);
        return true;
    }
    return complete(id, false, "UNKNOWN_COMMAND", NULL);
}
static bool receive_frame(void);
static bool process_frame(const uint8_t h[16],uint32_t n) {
    payload[n]=0;
    if (h[5] == 11) {
        if (!negotiated)
            return false;
        if (!(stream_audio_push_sbc(payload,n)))
            return error("STALE_STREAM",
                         "SBC rejected by stream bounds, identity, position or credit");
        return true;
    }
    if (!bounded_json_text(payload, n))
        return false;
    cJSON *j = cJSON_ParseWithLengthOpts((char *)payload, n + 1, NULL, true);
    if (!cJSON_IsObject(j) || !unique_json_keys(j)) {
        cJSON_Delete(j);
        return false;
    }
    bool ok = false;
    if (!negotiated && h[5] == 1) {
        const char *id = text(j, "connection_id");
        uint8_t b[16];
        cJSON *version = cJSON_GetObjectItemCaseSensitive(j, "version");
        if (id && uuid_parse(id, b) && cJSON_IsNumber(version) && version->valuedouble == 1) {
            snprintf(connection_id, 37, "%s", id);
            if((!text(j,"audio_codec") || strcmp(text(j,"audio_codec"),SINK_SBC_PROFILE))) {
                cJSON_Delete(j);return error("UNSUPPORTED_CODEC","Server must negotiate SBC transport");
            }
            negotiated = true;
            ok = !cJSON_HasObjectItem(j, "audio_transport");
            ESP_LOGI(TAG, "Protocol session established");
        }
    } else if (negotiated) {
        const char *conn = text(j, "connection_id");
        if (!conn || strcmp(conn, connection_id)) {
            cJSON_Delete(j);
            return error("STALE_CONNECTION", "Connection identity mismatch");
        }
        if (h[5] == 2)
            ok = command(j, (char *)payload);
        else if (h[5] == 8)
            ok = send_json(8, object());
        else if (h[5] == 10)
            ok = error("UNSUPPORTED_FEATURE", "Clock synchronization is not implemented");
        else
            ok = error("UNKNOWN_TYPE", "Unexpected message type");
    }
    cJSON_Delete(j);
    return ok;
}
static bool receive_frame(void) {
    control_frame_completed=false;
    int64_t started=esp_timer_get_time();
    if(rx_header_used<16) {
        int n=recv(sock,control_header+rx_header_used,16-rx_header_used,0);
        uint32_t call_us=(uint32_t)(esp_timer_get_time()-started);if(call_us>io_rx_max_us)io_rx_max_us=call_us;
        if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR))return true;
        if(n<=0)return false;
        rx_header_used+=(size_t)n;
        if(rx_header_used<16){tcp_rx_partial++;return true;}
        if(!sink_tcp_header(control_header,rx_seq,&rx_payload_length))return false;
    }
    /* A queued diagnostic must never gate TCP SBC reception. Only control
     * frames need response space; inspect their header before applying it. */
    if(control_header[5]!=11 && control_tx.used>256)return true;
    if(rx_payload_used<rx_payload_length) {
        int n=recv(sock,payload+rx_payload_used,rx_payload_length-rx_payload_used,0);
        uint32_t call_us=(uint32_t)(esp_timer_get_time()-started);if(call_us>io_rx_max_us)io_rx_max_us=call_us;
        if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR))return true;
        if(n<=0)return false;
        rx_payload_used+=(size_t)n;
        if(rx_payload_used<rx_payload_length){tcp_rx_partial++;return true;}
    }
    uint32_t elapsed=(uint32_t)(esp_timer_get_time()-started);
    if(elapsed>io_rx_max_us)io_rx_max_us=elapsed;
    rx_seq=sink_get32(control_header+8);
    bool ok=process_frame(control_header,rx_payload_length);
    rx_header_used=rx_payload_used=rx_payload_length=0;
    control_frame_completed=true;return ok;
}
static void wifi_event(void *arg, esp_event_base_t base, int32_t event, void *data) {
    if (base == IP_EVENT && event == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, 1);
        sink_network_event("WIFI_CONNECTED", "");
        ESP_LOGI(TAG, "Wi-Fi acquired an IPv4 address");
    }
    if (base == WIFI_EVENT && event == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, 1);
        wifi_reconnects++;
        sink_network_event("WIFI_DISCONNECTED", "");
    }
}
static void network_task(void *arg) {
    unsigned delay = 1;
    int64_t wifi_next = 0;
    unsigned wifi_delay = 1;
    while (1) {

        if (!(xEventGroupGetBits(wifi_events) & 1)) {
            if (esp_timer_get_time() >= wifi_next) {
                esp_wifi_connect();
                wifi_next = esp_timer_get_time() + (int64_t)wifi_delay * 1000000;
                wifi_delay = wifi_delay < 30 ? wifi_delay * 2 : 60;
            }
            vTaskDelay(pdMS_TO_TICKS(250));
            continue;
        }
        wifi_delay = 1;
        struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(sink_setup_port())};
        inet_pton(AF_INET, sink_setup_server(), &address.sin_addr);
        sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        int one = 1;
        setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        /* Nonblocking connect bounds unreachable-server attempts. */
        int flags = fcntl(sock, F_GETFL, 0);
        int r=-1;
        if(flags>=0 && fcntl(sock,F_SETFL,flags|O_NONBLOCK)==0)
            r=connect(sock,(struct sockaddr*)&address,sizeof(address));
        if (r < 0 && errno == EINPROGRESS) {
            fd_set w;
            FD_ZERO(&w);
            FD_SET(sock, &w);
            struct timeval t = {.tv_sec = 5};
            r = select(sock + 1, NULL, &w, NULL, &t);
            if (r > 0) {
                int e = 0;
                socklen_t l = sizeof(e);
                getsockopt(sock, SOL_SOCKET, SO_ERROR, &e, &l);
                r = e ? -1 : 0;
            } else
                r = -1;
        }
        /* Keep O_NONBLOCK for the entire established control session. */
        if (r == 0) {
            tx_seq = rx_seq = 0;
            memset(&control_tx,0,sizeof(control_tx));
            rx_header_used=rx_payload_used=rx_payload_length=0;
            tx_progress_at=esp_timer_get_time();
            negotiated = false;
            connection_id[0] = pending_id[0] = 0;
            cache_next = 0;
            for (int i = 0; i < CACHE_COUNT; i++) {
                cached_id[i][0] = 0;
                free(cached_result[i]);
                cached_result[i] = NULL;
            }
            stream_audio_clear();
            xQueueReset(events);
            atomic_store(&event_overflow,false);
            int64_t last_status = 0, last_flow = 0, started = esp_timer_get_time(),
                    last_rx = started;
            bool ok = hello();
            int64_t previous_loop=esp_timer_get_time();
            while (ok && (xEventGroupGetBits(wifi_events) & 1)) {
                int64_t now = esp_timer_get_time();
                uint32_t loop_us=(uint32_t)(now-previous_loop);
                if(loop_us>io_loop_max_us)io_loop_max_us=loop_us;
                previous_loop=now;
                if(atomic_load(&event_overflow))break;
                if(sink_reboot_ready(now,reboot_at,&control_tx))esp_restart();
                if(control_tx.used && now-tx_progress_at>15000000)break;
                if ((!negotiated && now - started > 5000000) || now - last_rx > 15000000)
                    break;
                /* Audio ingress always runs before optional control traffic. */
                ok=flush_control();
                if(ok){ok=receive_frame();if(control_frame_completed)last_rx=esp_timer_get_time();}
                if (negotiated && ok && now - last_flow > 20000) {
                        char flow[768];size_t n=stream_audio_format_flow(flow,sizeof(flow),connection_id);
                        ok=n && send_payload(7,flow,n);
                    last_flow=now;
                }
                if (negotiated && ok && now - last_status > 1000000) {
                    ok = send_json(5, status());
                    last_status = now;
                }
                if(negotiated&&ok) {
                    cJSON *failure=stream_audio_failure_event();
                    if(failure) {cJSON_AddStringToObject(failure,"connection_id",connection_id);ok=send_json(4,failure);}
                }
                device_event_t e;
                unsigned event_budget=0;
                while (negotiated && ok && control_tx.used<=512 && event_budget++<2 && xQueueReceive(events, &e, 0)) {
                    cJSON *j = object();
                    cJSON_AddStringToObject(j, "event", e.name);
                    cJSON_AddStringToObject(j, "detail", e.detail);
                    if (e.request_id[0])
                        cJSON_AddStringToObject(j, "request_id", e.request_id);
                    ok = send_json(4, j);
                    if (pending_id[0] && e.request_id[0] && !strcmp(e.request_id, pending_id) &&
                        (!strcmp(e.name, pending_op) || !strcmp(e.name, "COMMAND_FAILED") ||
                         !strcmp(e.name, "BT_PAIRING_FAILED") ||
                         (!strcmp(e.name, "BT_DISCONNECTED") &&
                          !strcmp(pending_op, "BT_CONNECTED")))) {
                        bool success = !strcmp(e.name, pending_op);
                        const char *code = !strcmp(e.name, "COMMAND_FAILED") ? e.detail
                                           : !strcmp(e.name, "BT_PAIRING_FAILED")
                                               ? "BT_AUTH_FAILED"
                                               : "OUTPUT_DISCONNECTED";
                        ok = complete(pending_id, success, success ? NULL : code, NULL);
                        pending_id[0] = 0;
                    }
                }
                if (pending_id[0] && now > pending_deadline) {
                    ok = complete(pending_id, false, "TIMEOUT", NULL);
                    pending_id[0] = 0;
                }
                if(ok)ok=flush_control();
                if (negotiated)delay=1;
                /* One tick yields CPU to Wi-Fi/BT without waiting on TCP readiness. */
                vTaskDelay(pdMS_TO_TICKS(1));
            }
        }
        reboot_at=0; /* A failed session must not claim its queued reboot completed. */
        close(sock);
        sock = -1;
        stream_audio_clear();
        ESP_LOGW(TAG, "Server connection closed; retrying");
        vTaskDelay(pdMS_TO_TICKS(delay * 1000));
        delay = delay < 15 ? delay * 2 : 30;
    }
}
void sink_network_start(void) {
    sink_setup_init();
    if (!sink_setup_ssid()[0]) {
        ESP_LOGW("setup", "No network settings; provision this board through /setup");
        return;
    }
    wifi_events = xEventGroupCreate();
    events = xQueueCreate(12, sizeof(device_event_t));
    configASSERT(wifi_events && events);
    uuid_new(boot_id);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    configASSERT(esp_netif_create_default_wifi_sta());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_log_level_set("wifi", ESP_LOG_WARN);
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t station = {0};
    station.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    station.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    memcpy(station.sta.ssid, sink_setup_ssid(), strlen(sink_setup_ssid()));
    memcpy(station.sta.password, sink_setup_password(), strlen(sink_setup_password()));
    station.sta.pmf_cfg.capable = true;
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &station));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    /* Bluedroid already reports A2DP streaming to the coexistence controller.
     * Keep the automatic policy rather than forcing the earlier BT preference. */
    configASSERT(xTaskCreatePinnedToCore(network_task, "sink_network", 5120, NULL, 4, NULL,0) == pdPASS);
}
