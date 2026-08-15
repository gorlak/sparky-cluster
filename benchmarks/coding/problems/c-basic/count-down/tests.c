#include "problem.h"

extern void report(const char *name, int weight, int ok);

static const int A[5] = {1, 2, 3, 4, 5};

void run_tests(void) {
    report("last two", 1, sum_last(A, 5, 2) == 9);
    report("all of them", 1, sum_last(A, 5, 5) == 15);
    /* k > n wraps `n - k` to a huge size_t; the loop then reads far out of bounds. */
    report("k larger than n sums everything", 3, sum_last(A, 5, 99) == 15);
    /* k == 0 must sum nothing, not everything. */
    report("k of zero sums nothing", 3, sum_last(A, 5, 0) == 0);
    report("empty array", 2, sum_last(A, 0, 3) == 0);
    report("k of zero on an empty array", 2, sum_last(A, 0, 0) == 0);
    report("single element", 1, sum_last(A, 1, 1) == 1);
}
