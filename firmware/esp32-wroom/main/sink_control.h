#pragma once
#include "cJSON.h"
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* All output operations are serialized by the output manager. Request IDs are
 * echoed on completion events; unrelated background retries cannot complete them. */
bool sink_output_command(const char *command, const char *request_id);
cJSON *sink_output_status(void);
void sink_network_event(const char *name, const char *detail);
void sink_network_command_event(const char *request_id, const char *name, const char *detail);

static inline bool sink_parse_address(const char *s, uint8_t b[6]) {
    if (!s || strlen(s) != 17)
        return false;
    unsigned any = 0, all = 255;
    for (unsigned i = 0; i < 6; i++) {
        unsigned value = 0;
        for (unsigned j = 0; j < 2; j++) {
            char c = s[i * 3 + j];
            unsigned digit;
            if (c >= '0' && c <= '9')
                digit = c - '0';
            else if (c >= 'a' && c <= 'f')
                digit = c - 'a' + 10;
            else if (c >= 'A' && c <= 'F')
                digit = c - 'A' + 10;
            else
                return false;
            value = (value << 4) | digit;
        }
        if (i != 5 && s[i * 3 + 2] != ':')
            return false;
        b[i] = value;
        any |= value;
        all &= value;
    }
    return any != 0 && all != 255;
}
