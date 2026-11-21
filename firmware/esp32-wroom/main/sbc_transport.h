#pragma once
#ifdef ESP_PLATFORM
#include "sdkconfig.h"
#endif
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define SINK_SBC_FRAME_BYTES 116u
#define SINK_SBC_SAMPLES 128u
#define SINK_SBC_SLOT_BYTES 128u
#define SINK_SBC_PROFILE "sbc_44100_stereo_52"
#if defined(ESP_PLATFORM) && CONFIG_SINK_OUTPUT_BLUETOOTH && !CONFIG_BT_A2DP_USE_EXTERNAL_CODEC
#error "Bluetooth output requires the external SBC codec"
#endif
#if defined(ESP_PLATFORM) && (!CONFIG_LWIP_TCP_QUEUE_OOSEQ || !CONFIG_LWIP_TCP_SACK_OUT)
#error "SBC network audio requires TCP out-of-order queuing and SACK"
#endif
#define SINK_SBC_BUFFER_BYTES 65536u
static inline bool sink_sbc_valid(const uint8_t *p,size_t n) {
    if(n!=116 || p[0]!=0x9c || p[1]!=0xb9 || p[2]!=52)return false;
    uint8_t crc=0x0f;
    for(unsigned i=0;i<10;i++) {
        uint8_t byte=p[i<2?i+1:i+2];
        for(int bit=7;bit>=0;bit--) {
            unsigned carry=((crc>>7)^(byte>>bit))&1;
            crc<<=1;if(carry)crc^=0x1d;
        }
    }
    return crc==p[3];
}

/* Carry the fractional microseconds so packet pacing has no rounding drift. */
static inline uint32_t sink_sbc_duration_us(unsigned frames,uint32_t *fraction) {
    uint64_t numerator=(uint64_t)frames*128*1000000+*fraction;
    *fraction=numerator%44100;
    return numerator/44100;
}
