#include "problem.h"

size_t dedupe(int *a, size_t n) {
    size_t w = 0;
    for (size_t i = 0; i < n; i++) {
        int seen = 0;
        for (size_t j = 0; j < w; j++) {
            if (a[j] == a[i]) { seen = 1; break; }
        }
        if (!seen) a[w++] = a[i];
    }
    return w;
}
