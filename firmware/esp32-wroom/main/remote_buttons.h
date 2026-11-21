#pragma once
#include <stdint.h>

/* AVRCP passthrough IDs. Act once per press, never again on release/repeat. */
static inline const char *sink_remote_button(uint8_t code, uint8_t state, uint8_t *held) {
    const char *action;
    uint8_t bit;
    switch (code) {
    case 0x44: action = "play"; bit = 1; break;
    case 0x46: action = "pause"; bit = 2; break;
    case 0x4b: action = "next"; bit = 4; break;
    case 0x4c: action = "previous"; bit = 8; break;
    default: return 0;
    }
    if (state == 1) { *held &= (uint8_t)~bit; return 0; }
    if (state != 0 || (*held & bit)) return 0;
    *held |= bit;
    return action;
}
