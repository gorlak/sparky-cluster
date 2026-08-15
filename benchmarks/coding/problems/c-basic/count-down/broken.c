#include "problem.h"

long sum_last(const int *a, size_t n, size_t k) {
    long total = 0;
    /* `n - k` is unsigned: when k > n it does not go negative, it wraps to an enormous
       value — and when k is 0 the loop start is n, which is fine, but the guard below
       is what actually breaks. */
    for (size_t i = n - k; i < n; i++) {
        total += a[i];
    }
    return total;
}
