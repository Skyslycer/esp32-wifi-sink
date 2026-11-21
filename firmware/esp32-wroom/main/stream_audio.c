#include "stream_audio.h"
#include "sbc_transport.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#ifdef ESP_PLATFORM
#include "esp_debug_helpers.h"
#include "freertos/task.h"
#endif

#define CAPACITY SINK_SBC_BUFFER_BYTES
#define FRAME_CAPACITY CAPACITY
_Static_assert(CAPACITY == 65536, "SBC requires the 64 KiB ring");
static uint8_t *ring;
static atomic_uint device_volume = 100;
static atomic_uint allocation_failures, failed_allocation_bytes, failed_allocation_caps;
#ifdef ESP_PLATFORM
static portMUX_TYPE trace_lock=portMUX_INITIALIZER_UNLOCKED;
static uint32_t allocation_trace[6];
static char allocation_task[17];
#endif
static void allocation_failed(size_t size, uint32_t caps, const char *function) {
#ifdef ESP_PLATFORM
    uint32_t trace[6]={0};char name[17]={0};
    const char *task=pcTaskGetName(NULL);
    if(task)for(unsigned i=0;i<16&&task[i];i++)name[i]=task[i];
    esp_backtrace_frame_t frame={0};
    esp_backtrace_get_start(&frame.pc,&frame.sp,&frame.next_pc);
    for(unsigned i=0;i<6;i++) {
        trace[i]=frame.pc;
        if(!esp_backtrace_get_next_frame(&frame))break;
    }
    portENTER_CRITICAL(&trace_lock);
    memcpy(allocation_trace,trace,sizeof(trace));memcpy(allocation_task,name,sizeof(name));
    portEXIT_CRITICAL(&trace_lock);
#endif
    atomic_store(&failed_allocation_bytes, size);
    atomic_store(&failed_allocation_caps, caps);
    atomic_fetch_add(&allocation_failures, 1);
}
void stream_audio_init(void) {
    _Static_assert(CAPACITY % SINK_SBC_SLOT_BYTES == 0, "Ring must hold whole SBC slots");
    ESP_ERROR_CHECK(heap_caps_register_failed_alloc_callback(allocation_failed));
    ring = heap_caps_malloc(CAPACITY, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    configASSERT(ring);
    nvs_handle_t handle;
    ESP_ERROR_CHECK(nvs_open("sink_audio", NVS_READWRITE, &handle));
    uint8_t volume = 100;
    if (nvs_get_u8(handle, "volume", &volume) == ESP_OK && volume <= 100)
        atomic_store(&device_volume, volume);
    nvs_close(handle);
}
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static size_t head, tail, fill;
static bool active, prefilled, output_ready;
static unsigned buffer_target = CAPACITY, interval_min_fill = CAPACITY;
static bool clocked_playback;
static unsigned output_batch_frames;
static uint8_t playback_id[16];
static uint32_t epoch;
/* Local generation distinguishes reused epochs/positions after reconnects. */
static uint32_t generation;
static uint64_t next_position, received_frames, consumed_frames, underruns, overruns, packets;
static uint64_t submitted_position;
static int64_t last_sbc_us, largest_sbc_gap_us, interval_delivery_gap_us;
static int64_t last_callback_us, largest_callback_gap_us;
static uint64_t read64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; i++)
        v = (v << 8) | p[i];
    return v;
}
static uint32_t read32(const uint8_t *p) {
    uint32_t v = 0;
    for (int i = 0; i < 4; i++)
        v = (v << 8) | p[i];
    return v;
}
bool stream_audio_playback_active(void) {
    portENTER_CRITICAL(&lock);
    bool value = active;
    portEXIT_CRITICAL(&lock);
    return value;
}
void stream_audio_clear(void) {
    portENTER_CRITICAL(&lock);
    active = false;
    last_callback_us = 0;
    prefilled = false;
    head = tail = fill = 0;
    
    portEXIT_CRITICAL(&lock);
}
void stream_audio_output_ready(bool ready) {
    portENTER_CRITICAL(&lock);
    output_ready = ready;
    if (!ready) {
        active = false;
        prefilled = false;
        head = tail = fill = 0;
        last_callback_us=0;
    }
    portEXIT_CRITICAL(&lock);
}
bool stream_audio_start(const uint8_t id[16], uint32_t e, uint64_t position) {
    portENTER_CRITICAL(&lock);
    if (!output_ready || position > UINT64_MAX-FRAME_CAPACITY || position%128) {
        portEXIT_CRITICAL(&lock);
        return false;
    }
    memcpy(playback_id, id, 16);
    epoch = e;
    generation++;
    next_position = position;
    submitted_position = position;
    
    head = tail = fill = 0;
    received_frames = consumed_frames = 0;
    prefilled = false;
    buffer_target = interval_min_fill = CAPACITY;
    clocked_playback=false;output_batch_frames=0;
    active = true;
    last_sbc_us = largest_sbc_gap_us = interval_delivery_gap_us = 0;
    last_callback_us = largest_callback_gap_us = 0;
    portEXIT_CRITICAL(&lock);
    return true;
}
bool stream_audio_buffer_target(const uint8_t id[16], uint32_t e, unsigned frames) {
    if (frames < 1024 || frames > CAPACITY || frames % 1024) return false;
    portENTER_CRITICAL(&lock);
    bool ok = active && e == epoch && !memcmp(id, playback_id, 16);
    if (ok) buffer_target = frames;
    portEXIT_CRITICAL(&lock);
    return ok;
}
void stream_audio_clocked(bool enabled) {
    portENTER_CRITICAL(&lock);clocked_playback=enabled;portEXIT_CRITICAL(&lock);
}
static void decimal(cJSON *j, const char *key, uint64_t v) {
    char b[24];
    snprintf(b, sizeof(b), "%" PRIu64, v);
    cJSON_AddStringToObject(j, key, b);
}
cJSON *stream_audio_status(void) {
    uint8_t id[16];
    bool a, o;
    size_t f;
    uint32_t e;
    uint64_t r, c, u, v, p, s, g, callback_gap;
    portENTER_CRITICAL(&lock);
    memcpy(id, playback_id, 16);
    a = active;
    o = output_ready;
    f = fill;
    e = epoch;
    r = received_frames;
    c = consumed_frames;
    u = underruns;
    v = overruns;
    p = packets;
    s = submitted_position;
    g = largest_sbc_gap_us;
    callback_gap=largest_callback_gap_us;
    portEXIT_CRITICAL(&lock);
    char uuid[37];
    snprintf(uuid, sizeof(uuid),
             "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x", id[0], id[1],
             id[2], id[3], id[4], id[5], id[6], id[7], id[8], id[9], id[10], id[11], id[12], id[13],
             id[14], id[15]);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "active", a);
    cJSON_AddBoolToObject(j, "output_ready", o);
    cJSON_AddStringToObject(j, "playback_id", uuid);
    cJSON_AddNumberToObject(j, "epoch", e);
    cJSON_AddNumberToObject(j, "buffer_fill_frames", f);
    cJSON_AddNumberToObject(j, "buffer_capacity_frames", FRAME_CAPACITY);
    decimal(j, "received_total_frames", r);
    decimal(j, "grant_total_frames", FRAME_CAPACITY + c);
    decimal(j, "submitted_position", s);
    decimal(j, "underruns", u);
    decimal(j, "overruns", v);
    decimal(j, "sbc_packets", p);
    decimal(j, "largest_sbc_gap_us", g);
    decimal(j,"callback_gap_us",callback_gap);
    decimal(j, "uptime_ms", esp_timer_get_time() / 1000);
    cJSON_AddNumberToObject(j, "free_heap", esp_get_free_heap_size());
    cJSON_AddNumberToObject(j, "minimum_free_heap", esp_get_minimum_free_heap_size());
    cJSON_AddNumberToObject(j, "allocation_failures", atomic_load(&allocation_failures));
    cJSON_AddNumberToObject(j, "failed_allocation_bytes", atomic_load(&failed_allocation_bytes));
    cJSON_AddNumberToObject(j, "failed_allocation_caps", atomic_load(&failed_allocation_caps));
    cJSON_AddNumberToObject(
        j, "largest_free_internal_block",
        heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    cJSON_AddNumberToObject(j, "device_volume", stream_audio_get_volume());
    cJSON_AddNullToObject(j, "presented_position");
    cJSON_AddStringToObject(j,"audio_transport","tcp");
    cJSON_AddStringToObject(j,"audio_codec",SINK_SBC_PROFILE);
    return j;
}

size_t stream_audio_format_flow(char *out,size_t capacity,const char *connection) {
    uint8_t id[16];
    bool a, o, started;
    unsigned minimum, target;
    uint64_t u, delivery_gap, output_time, output_pos, device_time;
    unsigned batch;
    uint32_t e;
    uint64_t r, g;
    size_t f;
    portENTER_CRITICAL(&lock);
    memcpy(id, playback_id, 16);
    a = active;
    o = output_ready;
    e = epoch;
    r = received_frames;
    g = FRAME_CAPACITY + consumed_frames;
    f = fill;
    output_time=last_callback_us;output_pos=submitted_position;batch=output_batch_frames;device_time=esp_timer_get_time();
    started = prefilled; minimum = interval_min_fill; target = buffer_target; u = underruns;
    delivery_gap = last_sbc_us ? esp_timer_get_time()-last_sbc_us : 0;
    if ((uint64_t)interval_delivery_gap_us > delivery_gap)delivery_gap=interval_delivery_gap_us;
    interval_min_fill = prefilled ? fill : CAPACITY;
    interval_delivery_gap_us = 0;
    portEXIT_CRITICAL(&lock);
    char uuid[37];
    snprintf(uuid, sizeof(uuid),
             "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x", id[0], id[1],
             id[2], id[3], id[4], id[5], id[6], id[7], id[8], id[9], id[10], id[11], id[12], id[13],
             id[14], id[15]);
    if(!connection || strlen(connection)!=36)return 0;
    int n=snprintf(out,capacity,
        "{\"active\":%s,\"output_ready\":%s,\"playback_id\":\"%s\",\"epoch\":%" PRIu32
        ",\"grant_total_frames\":\"%" PRIu64 "\",\"received_total_frames\":\"%" PRIu64
        "\",\"buffer_fill_frames\":%u,\"connection_id\":\"%s\",\"playout_started\":%s,\"min_fill_frames\":%u,\"buffer_target_frames\":%u,\"underruns\":\"%" PRIu64 "\",\"delivery_gap_us\":%" PRIu64 ",\"output_position\":\"%" PRIu64 "\",\"output_time_us\":\"%" PRIu64 "\",\"output_batch_frames\":%u,\"device_time_us\":\"%" PRIu64 "\"}",
        a?"true":"false",o?"true":"false",uuid,e,g,r,(unsigned)(f),connection,started?"true":"false",minimum,target,u,delivery_gap,output_pos,output_time,batch,device_time);
    return n>0 && (size_t)n<capacity?(size_t)n:0;
}

cJSON *stream_audio_flow(void) {
    uint8_t id[16];
    bool a, o, started;
    unsigned minimum, target;
    uint64_t u, delivery_gap, output_time, output_pos, device_time;
    unsigned batch;
    uint32_t e;
    uint64_t r, g;
    size_t f;
    portENTER_CRITICAL(&lock);
    memcpy(id, playback_id, 16);
    a = active;
    o = output_ready;
    e = epoch;
    r = received_frames;
    g = FRAME_CAPACITY + consumed_frames;
    f = fill;
    output_time=last_callback_us;output_pos=submitted_position;batch=output_batch_frames;device_time=esp_timer_get_time();
    started = prefilled; minimum = interval_min_fill; target = buffer_target; u = underruns;
    delivery_gap = last_sbc_us ? esp_timer_get_time()-last_sbc_us : 0;
    if ((uint64_t)interval_delivery_gap_us > delivery_gap)delivery_gap=interval_delivery_gap_us;
    portEXIT_CRITICAL(&lock);
    char uuid[37];
    snprintf(uuid, sizeof(uuid),
             "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x", id[0], id[1],
             id[2], id[3], id[4], id[5], id[6], id[7], id[8], id[9], id[10], id[11], id[12], id[13],
             id[14], id[15]);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "active", a);
    cJSON_AddBoolToObject(j, "output_ready", o);
    cJSON_AddStringToObject(j, "playback_id", uuid);
    cJSON_AddNumberToObject(j, "epoch", e);
    decimal(j, "grant_total_frames", g);
    decimal(j, "received_total_frames", r);
    cJSON_AddNumberToObject(j, "buffer_fill_frames", f);
    cJSON_AddBoolToObject(j,"playout_started",started);
    cJSON_AddNumberToObject(j,"min_fill_frames",minimum);
    cJSON_AddNumberToObject(j,"buffer_target_frames",target);
    decimal(j,"underruns",u);
    decimal(j,"delivery_gap_us",delivery_gap);
    decimal(j,"output_position",output_pos);
    decimal(j,"output_time_us",output_time);
    decimal(j,"device_time_us",device_time);
    cJSON_AddNumberToObject(j,"output_batch_frames",batch);
    return j;
}

unsigned stream_audio_get_volume(void) { return atomic_load(&device_volume); }
bool stream_audio_set_volume(unsigned percent) {
    if (percent > 100)
        return false;
    nvs_handle_t handle;
    if (nvs_open("sink_audio", NVS_READWRITE, &handle) != ESP_OK)
        return false;
    esp_err_t result = nvs_set_u8(handle, "volume", percent);
    if (result == ESP_OK)
        result = nvs_commit(handle);
    nvs_close(handle);
    if (result != ESP_OK)
        return false;
    atomic_store(&device_volume, percent);
    return true;
}
void stream_audio_reset_statistics(void) {
    portENTER_CRITICAL(&lock);
    underruns = overruns = packets = 0;
    last_sbc_us = largest_sbc_gap_us = interval_delivery_gap_us = 0;
    last_callback_us = largest_callback_gap_us = 0;
    /* Keep received_frames/consumed_frames, identity and positions unchanged:
       these are live cumulative flow accounting, not diagnostic counters. */
    portEXIT_CRITICAL(&lock);
    atomic_store(&allocation_failures, 0);
    atomic_store(&failed_allocation_bytes, 0);
    atomic_store(&failed_allocation_caps, 0);
}

cJSON *stream_audio_failure_event(void) {
#ifdef ESP_PLATFORM
    static unsigned reported;
    unsigned count=atomic_load(&allocation_failures);
    if(count==reported)return NULL;
    reported=count;if(!count)return NULL;
    uint32_t trace[6];char task[17];
    portENTER_CRITICAL(&trace_lock);
    memcpy(trace,allocation_trace,sizeof(trace));memcpy(task,allocation_task,sizeof(task));
    portEXIT_CRITICAL(&trace_lock);
    cJSON *j=cJSON_CreateObject();
    cJSON_AddStringToObject(j,"event","HEAP_ALLOCATION_FAILED");
    cJSON_AddStringToObject(j,"task",task);
    cJSON_AddNumberToObject(j,"count",count);
    cJSON_AddNumberToObject(j,"requested_bytes",atomic_load(&failed_allocation_bytes));
    cJSON_AddNumberToObject(j,"caps",atomic_load(&failed_allocation_caps));
    cJSON *list=cJSON_AddArrayToObject(j,"backtrace");
    for(unsigned i=0;i<6&&trace[i];i++) {char pc[12];snprintf(pc,sizeof(pc),"0x%08"PRIx32,trace[i]);cJSON_AddItemToArray(list,cJSON_CreateString(pc));}
    return j;
#else
    return NULL;
#endif
}

/* Compressed frames remain in fixed 128-byte slots until Bluetooth accepts
 * ownership of a bounded packet. No decoder or descriptor allocation is needed. */
bool stream_audio_push_sbc(const uint8_t *p,size_t n) {
    if(n<40+116 || n>40+8*116 || (n-40)%116)return false;
    unsigned frames=(n-40)/116;
    uint32_t count=read32(p+36),e=read32(p+16);
    uint64_t pos=read64(p+20);
    if(count!=frames*128 || read64(p+28)!=UINT64_MAX || pos%128 || pos>UINT64_MAX-count)return false;
    for(unsigned i=0;i<frames;i++)if(!sink_sbc_valid(p+40+i*116,116))return false;
    int64_t now=esp_timer_get_time();
    portENTER_CRITICAL(&lock);
    if(!active || e!=epoch || memcmp(p,playback_id,16) || pos!=next_position ||
       frames*128>CAPACITY-fill) {portEXIT_CRITICAL(&lock);return false;}
    for(unsigned i=0;i<frames;i++) {
        memcpy(ring+tail,p+40+i*116,116);
        tail=(tail+128)%CAPACITY;fill+=128;
    }
    received_frames+=count;next_position+=count;packets++;
    if(last_sbc_us && now-last_sbc_us>interval_delivery_gap_us)interval_delivery_gap_us=now-last_sbc_us;
    if(last_sbc_us && now-last_sbc_us>largest_sbc_gap_us)largest_sbc_gap_us=now-last_sbc_us;
    last_sbc_us=now;
    portEXIT_CRITICAL(&lock);return true;
}
unsigned stream_audio_sbc_peek(uint8_t *out,unsigned maximum,uint32_t *token,uint64_t *position) {
    if(!maximum || maximum>7)return 0;
    portENTER_CRITICAL(&lock);
    if(!active || !output_ready){portEXIT_CRITICAL(&lock);return 0;}
    if(!prefilled && fill>=(clocked_playback && buffer_target>8192 ? 8192 : buffer_target)){prefilled=true;interval_min_fill=fill;}
    if(!prefilled){portEXIT_CRITICAL(&lock);return 0;}
    if(!fill){underruns++;prefilled=false;portEXIT_CRITICAL(&lock);return 0;}
    unsigned count=fill/128;if(count>maximum)count=maximum;
    for(unsigned i=0;i<count;i++)memcpy(out+i*116,ring+(head+i*128)%CAPACITY,116);
    *token=generation;*position=submitted_position;
    portEXIT_CRITICAL(&lock);return count;
}
bool stream_audio_sbc_commit(unsigned count,uint32_t token,uint64_t position) {
    portENTER_CRITICAL(&lock);
    bool accepted=active && token==generation && position==submitted_position && count && count<=7 && fill>=count*128;
    if(accepted) {
        head=(head+count*128)%CAPACITY;fill-=count*128;
        if(fill<interval_min_fill)interval_min_fill=fill;
        consumed_frames+=count*128;submitted_position+=count*128;
        int64_t now=esp_timer_get_time();
        if(last_callback_us && now-last_callback_us>largest_callback_gap_us)largest_callback_gap_us=now-last_callback_us;
        last_callback_us=now;output_batch_frames=count*128;
    }
    portEXIT_CRITICAL(&lock);
    return accepted;
}
