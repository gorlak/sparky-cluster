#include "problem.h"

long find_first(const int *a, size_t n, int target) {
    long lo = 0, hi = (long)n - 1;
    while (lo <= hi) {
        /* Two defects live on this line and the one below. */
        long mid = (lo + hi) / 2;
        if (a[mid] == target) return mid;
        if (a[mid] < target) lo = mid + 1;
        else hi = mid - 1;
    }
    return -1;
}
