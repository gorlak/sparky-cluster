#include "problem.h"

extern void report(const char *name, int weight, int ok);

void run_tests(void) {
    { const int a[] = {1, 2, 3}; report("present, unique", 1, find_first(a, 3, 2) == 1); }
    { const int a[] = {1, 2, 3}; report("absent", 1, find_first(a, 3, 9) == -1); }
    { const int a[] = {1}; report("single element", 1, find_first(a, 1, 1) == 0); }
    { const int a[] = {1}; report("empty range", 2, find_first(a, 0, 1) == -1); }
    /* The stated defect: any occurrence will not do. */
    { const int a[] = {1, 2, 2, 2, 3};
      report("first of several duplicates", 3, find_first(a, 5, 2) == 1); }
    { const int a[] = {2, 2, 2}; report("all elements match", 3, find_first(a, 3, 2) == 0); }
    { const int a[] = {1, 1, 2}; report("duplicates at the start", 2, find_first(a, 3, 1) == 0); }
    { const int a[] = {1, 2, 2}; report("duplicates at the end", 2, find_first(a, 3, 2) == 1); }
}
