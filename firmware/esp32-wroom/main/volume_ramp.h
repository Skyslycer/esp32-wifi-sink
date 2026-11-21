#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Gain uses Q16 unity; 6615 stereo frames are 150 ms at 44.1 kHz. */
#define SINK_VOLUME_RAMP_FRAMES 6615

typedef struct {
    int32_t current, start, target;
    unsigned remaining;
    bool initialized;
} sink_volume_ramp_t;

static inline void sink_volume_ramp_apply(sink_volume_ramp_t *ramp, int16_t *pcm,
                                         unsigned frames, unsigned percent) {
    int32_t target=(int32_t)percent*65536/100;
    if (!ramp->initialized) {
        ramp->current=ramp->start=ramp->target=target;
        ramp->remaining=0;
        ramp->initialized=true;
    } else if (ramp->target!=target) {
        ramp->start=ramp->current;
        ramp->target=target;
        ramp->remaining=SINK_VOLUME_RAMP_FRAMES;
    }
    if (!ramp->remaining && ramp->current==65536) return;
    for (unsigned i=0;i<frames;i++) {
        if (ramp->remaining) {
            ramp->remaining--;
            ramp->current=ramp->start+(ramp->target-ramp->start)*
                (int32_t)(SINK_VOLUME_RAMP_FRAMES-ramp->remaining)/SINK_VOLUME_RAMP_FRAMES;
        }
        pcm[2*i]=(int32_t)pcm[2*i]*ramp->current/65536;
        pcm[2*i+1]=(int32_t)pcm[2*i+1]*ramp->current/65536;
    }
}
