#include "problem.h"

long sum_last(const int *a, size_t n, size_t k) {
    long total = 0;
    /* Clamp BEFORE subtracting: size_t cannot represent a negative start. */
    size_t take = k < n ? k : n;
    for (size_t i = n - take; i < n; i++) {
        total += a[i];
    }
    return total;
}
