#include "problem.h"

int parse_uint(const char *s, u32 *out) {
    if (s == 0 || s[0] == '\0') return -1;
    unsigned long long acc = 0;
    for (size_t i = 0; s[i] != '\0'; i++) {
        if (s[i] < '0' || s[i] > '9') return -1;
        acc = acc * 10u + (unsigned)(s[i] - '0');
        if (acc > U32_MAX) return -1;      /* checked every step, not at the end */
    }
    *out = (u32)acc;
    return 0;
}
