#include "problem.h"

extern void report(const char *name, int weight, int ok);

static int same(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

void run_tests(void) {
    { char b[8] = {0}; size_t n = copy_bounded(b, 8, "abc");
      report("copies a short string", 1, n == 3 && same(b, "abc")); }
    { char b[8] = {0}; size_t n = copy_bounded(b, 8, "");
      report("empty source", 1, n == 0 && b[0] == '\0'); }
    /* The bug: writing cap characters plus a terminator is cap+1 bytes. */
    { char b[5]; for (int i = 0; i < 5; i++) b[i] = 'X';
      size_t n = copy_bounded(b, 4, "abcd");
      report("truncates to fit the terminator", 3,
             n == 3 && same(b, "abc") && b[4] == 'X'); }
    { char b[4]; for (int i = 0; i < 4; i++) b[i] = 'X';
      size_t n = copy_bounded(b, 1, "abc");
      report("cap of one leaves only a terminator", 3,
             n == 0 && b[0] == '\0' && b[1] == 'X'); }
    { char b[2]; b[0] = 'X'; b[1] = 'X';
      size_t n = copy_bounded(b, 0, "abc");
      report("cap of zero writes nothing at all", 3, n == 0 && b[0] == 'X'); }
    { char b[16] = {0}; size_t n = copy_bounded(b, 16, "exact");
      report("exact fit", 2, n == 5 && same(b, "exact")); }
}
