#pragma once
#include "cJSON.h"
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
/* Validate UTF-8 and limit parser recursion before entering cJSON. */
static bool bounded_json_text(const uint8_t *s, size_t n) {
    unsigned depth = 0;
    bool quoted = false, escaped = false;
    for (size_t i = 0; i < n; i++) {
        unsigned ch = s[i];
        if (ch >= 128) {
            unsigned count = ch >= 0xc2 && ch <= 0xdf   ? 1
                             : ch >= 0xe0 && ch <= 0xef ? 2
                             : ch >= 0xf0 && ch <= 0xf4 ? 3
                                                        : 0;
            if (!count || i + count >= n)
                return false;
            unsigned next = s[i + 1];
            if ((ch == 0xe0 && next < 0xa0) || (ch == 0xed && next >= 0xa0) ||
                (ch == 0xf0 && next < 0x90) || (ch == 0xf4 && next >= 0x90))
                return false;
            for (unsigned k = 1; k <= count; k++)
                if ((s[i + k] & 0xc0) != 0x80)
                    return false;
            i += count;
            continue;
        }
        if (ch == 0)
            return false;
        if (quoted) {
            if (escaped) {
                /* cJSON represents strings as C strings, so embedded U+0000
                   would silently alias command names, IDs or object keys. */
                if (ch == 'u' && i + 4 < n && !memcmp(s + i + 1, "0000", 4))
                    return false;
                escaped = false;
            } else if (ch == '\\')
                escaped = true;
            else if (ch == '"')
                quoted = false;
            continue;
        }
        if (ch == '"')
            quoted = true;
        else if (ch == '{' || ch == '[') {
            if (++depth > 8)
                return false;
        } else if (ch == '}' || ch == ']') {
            if (!depth)
                return false;
            depth--;
        }
    }
    return !quoted && !depth;
}
static bool unique_json_keys(cJSON *j) {
    cJSON *a, *b;
    cJSON_ArrayForEach(a, j) {
        if (cJSON_IsNumber(a) && !isfinite(a->valuedouble))
            return false;
        if (cJSON_IsObject(j))
            for (b = a->next; b; b = b->next)
                if (a->string && b->string && !strcmp(a->string, b->string))
                    return false;
        if ((cJSON_IsObject(a) || cJSON_IsArray(a)) && !unique_json_keys(a))
            return false;
    }
    return true;
}
