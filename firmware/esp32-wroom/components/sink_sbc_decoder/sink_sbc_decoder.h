#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
/* Single output-task-owned decoder, static workspace; no per-frame allocation. */
/* Reset before the first frame and after a playback discontinuity. */
bool sink_sbc_decoder_reset(void);
bool sink_sbc_decode_frame(const uint8_t *frame, size_t length, int16_t pcm[256]);
size_t sink_sbc_decoder_memory_bytes(void);
