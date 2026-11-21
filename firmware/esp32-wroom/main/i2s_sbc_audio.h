#pragma once
#include <stdbool.h>
#include <stdint.h>
/* Fill exactly 128 interleaved stereo frames; false means no network playback. */
bool i2s_sbc_audio_fill(int16_t pcm[256]);
unsigned i2s_sbc_decode_failures(void);
unsigned i2s_sbc_max_decode_us(void);
unsigned i2s_sbc_decoded_frames(void);
