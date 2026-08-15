#include "problem.h"

size_t copy_bounded(char *dst, size_t cap, const char *src) {
    size_t i = 0;
    while (src[i] != '\0' && i < cap) {
        dst[i] = src[i];
        i++;
    }
    dst[i] = '\0';
    return i;
}
