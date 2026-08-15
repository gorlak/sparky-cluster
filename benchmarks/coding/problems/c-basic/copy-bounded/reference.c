#include "problem.h"

size_t copy_bounded(char *dst, size_t cap, const char *src) {
    if (cap == 0) return 0;
    size_t i = 0;
    /* cap counts the terminator, so the last writable character index is cap - 2. */
    while (src[i] != '\0' && i + 1 < cap) {
        dst[i] = src[i];
        i++;
    }
    dst[i] = '\0';
    return i;
}
