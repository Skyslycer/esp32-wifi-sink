#include "i2s_sbc_audio.h"
#include "sbc_transport.h"
#include "sink_sbc_decoder.h"
#include "stream_audio.h"
#include "sink_control.h"
#include "volume_ramp.h"
#include "esp_timer.h"
#include <stdatomic.h>
#include <string.h>

static bool valid;
static sink_volume_ramp_t volume_ramp;
static uint32_t decoder_generation;
static uint64_t decoder_next_position;
static atomic_uint failures, max_decode_us, decoded_frames;

bool i2s_sbc_audio_fill(int16_t pcm[256]) {
    if (!stream_audio_playback_active()) { valid=false; volume_ramp.initialized=false; return false; }
    memset(pcm, 0, 512);
    uint8_t encoded[SINK_SBC_FRAME_BYTES];
    uint32_t token;
    uint64_t position;
    if (!stream_audio_sbc_peek(encoded, 1, &token, &position)) {
        valid=false; /* Reset synthesis history after silence/rebuffering. */
        return true;
    }
    if (decoder_generation!=token) volume_ramp.initialized=false;
    bool reset=!valid || decoder_generation!=token || decoder_next_position!=position;
    int64_t start=esp_timer_get_time();
    bool ok=(!reset || sink_sbc_decoder_reset()) &&
            sink_sbc_decode_frame(encoded, sizeof(encoded), pcm);
    unsigned duration=(unsigned)(esp_timer_get_time()-start);
    unsigned maximum=atomic_load(&max_decode_us);
    if (duration>maximum) atomic_store(&max_decode_us, duration);
    valid=ok; decoder_generation=token; decoder_next_position=position+128;
    /* A pause/new assignment during decoding must discard the old PCM. */
    if (!stream_audio_sbc_commit(1, token, position)) {
        valid=false; memset(pcm, 0, 512); return true;
    }
    if (!ok) {
        memset(pcm, 0, 512);
        atomic_fetch_add(&failures, 1);
        sink_network_event("ERROR", "SBC I2S decode failed");
        return true;
    }
    atomic_fetch_add(&decoded_frames, 1);
    sink_volume_ramp_apply(&volume_ramp,pcm,128,stream_audio_get_volume());
    return true;
}
unsigned i2s_sbc_decode_failures(void) { return atomic_load(&failures); }
unsigned i2s_sbc_max_decode_us(void) { return atomic_load(&max_decode_us); }
unsigned i2s_sbc_decoded_frames(void) { return atomic_load(&decoded_frames); }
