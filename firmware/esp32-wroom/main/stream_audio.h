#pragma once
#include "cJSON.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
void stream_audio_init(void);
void stream_audio_output_ready(bool ready);
void stream_audio_clear(void);
bool stream_audio_playback_active(void);
bool stream_audio_set_volume(unsigned percent);
unsigned stream_audio_get_volume(void);
void stream_audio_reset_statistics(void);
bool stream_audio_start(const uint8_t id[16], uint32_t epoch, uint64_t position);
bool stream_audio_buffer_target(const uint8_t id[16], uint32_t epoch, unsigned frames);
void stream_audio_clocked(bool enabled);
cJSON *stream_audio_status(void);
cJSON *stream_audio_flow(void);
size_t stream_audio_format_flow(char *out,size_t capacity,const char *connection);
void sink_network_start(void);

cJSON *stream_audio_failure_event(void);

bool stream_audio_push_sbc(const uint8_t *payload,size_t length);
unsigned stream_audio_sbc_peek(uint8_t *out,unsigned maximum,uint32_t *token,uint64_t *position);
bool stream_audio_sbc_commit(unsigned count,uint32_t token,uint64_t position);
