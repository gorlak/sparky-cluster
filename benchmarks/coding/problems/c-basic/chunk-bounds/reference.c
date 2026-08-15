#include "problem.h"

int chunk_bounds(size_t n_items, size_t n_chunks, size_t index,
                 size_t *start, size_t *len) {
    if (n_chunks == 0 || index >= n_chunks) return -1;
    size_t base = n_items / n_chunks;
    size_t extra = n_items % n_chunks;
    /* Every earlier chunk took `base`, and the first `extra` of them took one more. */
    size_t before = index < extra ? index : extra;
    *start = index * base + before;
    *len = base + (index < extra ? 1 : 0);
    return 0;
}
