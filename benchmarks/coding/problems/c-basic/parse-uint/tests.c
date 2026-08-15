#include "problem.h"

extern void report(const char *name, int weight, int ok);

void run_tests(void) {
    { u32 v = 7; report("a plain number", 1, parse_uint("123", &v) == 0 && v == 123u); }
    { u32 v = 7; report("zero", 1, parse_uint("0", &v) == 0 && v == 0u); }
    { u32 v = 7; report("the largest value", 2,
        parse_uint("4294967295", &v) == 0 && v == U32_MAX); }
    { u32 v = 7; report("empty string", 2, parse_uint("", &v) == -1 && v == 7u); }
    { u32 v = 7; report("leading sign", 2, parse_uint("+1", &v) == -1 && v == 7u); }
    { u32 v = 7; report("trailing junk", 3, parse_uint("12x", &v) == -1 && v == 7u); }
    { u32 v = 7; report("surrounding space", 2, parse_uint(" 12", &v) == -1 && v == 7u); }
    /* One past the top: an implementation that only checks after the loop has already
       wrapped by here. */
    { u32 v = 7; report("one past the maximum", 3,
        parse_uint("4294967296", &v) == -1 && v == 7u); }
    { u32 v = 7; report("far past the maximum", 3,
        parse_uint("99999999999999999999", &v) == -1 && v == 7u); }
    { u32 v = 7; report("leading zeroes are fine", 1,
        parse_uint("007", &v) == 0 && v == 7u); }
}
