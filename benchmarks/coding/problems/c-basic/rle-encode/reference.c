#include "problem.h"

long rle_encode(const unsigned char *src, size_t n, unsigned char *dst, size_t cap) {
    size_t out = 0, i = 0;
    while (i < n) {
        unsigned char value = src[i];
        size_t run = 0;
        while (i + run < n && src[i + run] == value && run < 255) run++;
        if (out + 2 > cap) return -1;
        dst[out++] = (unsigned char)run;
        dst[out++] = value;
        i += run;
    }
    return (long)out;
}
