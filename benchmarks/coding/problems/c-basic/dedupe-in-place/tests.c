#include "problem.h"

extern void report(const char *name, int weight, int ok);

static int prefix_is(const int *a, size_t n, const int *want, size_t wn) {
    if (n != wn) return 0;
    for (size_t i = 0; i < n; i++) {
        if (a[i] != want[i]) return 0;
    }
    return 1;
}

void run_tests(void) {
    {
        int a[] = {3, 1, 3, 2, 1};
        const int want[] = {3, 1, 2};
        report("keeps first occurrence", 1, prefix_is(a, dedupe(a, 5), want, 3));
    }
    {
        int a[] = {1, 2, 3};
        const int want[] = {1, 2, 3};
        report("no duplicates is unchanged", 1, prefix_is(a, dedupe(a, 3), want, 3));
    }
    {
        int a[] = {7, 7, 7, 7};
        const int want[] = {7};
        report("all duplicates collapse", 2, prefix_is(a, dedupe(a, 4), want, 1));
    }
    {
        /* n == 0 must not dereference, and a is deliberately non-null so a crash here is
           the answer reading past the length rather than reading null. */
        int a[] = {9};
        report("empty range returns zero", 3, dedupe(a, 0) == 0);
    }
    {
        /* Order is FIRST occurrence, not last — the mistake a sort-based answer makes. */
        int a[] = {2, 1, 2, 3, 1};
        const int want[] = {2, 1, 3};
        report("order is not sorted order", 3, prefix_is(a, dedupe(a, 5), want, 3));
    }
    {
        int a[] = {-1, 0, -1, 0, 5};
        const int want[] = {-1, 0, 5};
        report("negatives and zero", 2, prefix_is(a, dedupe(a, 5), want, 3));
    }
}
