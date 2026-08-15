#include "problem.h"

long find_first(const int *a, size_t n, int target) {
    long lo = 0, hi = (long)n - 1, found = -1;
    while (lo <= hi) {
        /* lo + (hi - lo) / 2 cannot overflow the way (lo + hi) can. */
        long mid = lo + (hi - lo) / 2;
        if (a[mid] == target) {
            found = mid;        /* keep looking to the LEFT for an earlier one */
            hi = mid - 1;
        } else if (a[mid] < target) {
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    return found;
}
