#include "problem.h"

extern void report(const char *name, int weight, int ok);

static int bytes_are(const unsigned char *a, size_t n, const unsigned char *want, size_t wn) {
    if (n != wn) return 0;
    for (size_t i = 0; i < n; i++) if (a[i] != want[i]) return 0;
    return 1;
}

void run_tests(void) {
    {
        const unsigned char s[] = {5, 5, 5, 7};
        const unsigned char w[] = {3, 5, 1, 7};
        unsigned char d[16];
        long n = rle_encode(s, 4, d, 16);
        report("a run and a singleton", 1, n == 4 && bytes_are(d, (size_t)n, w, 4));
    }
    {
        unsigned char d[4];
        report("empty input writes nothing", 2, rle_encode(d, 0, d, 4) == 0);
    }
    {
        const unsigned char s[] = {1, 2, 3};
        unsigned char d[8];
        const unsigned char w[] = {1, 1, 1, 2, 1, 3};
        long n = rle_encode(s, 3, d, 8);
        report("no runs at all", 1, n == 6 && bytes_are(d, (size_t)n, w, 6));
    }
    {
        /* 300 identical bytes: 255 then 45, because a count is one byte. */
        unsigned char s[300];
        for (int i = 0; i < 300; i++) s[i] = 9;
        unsigned char d[16];
        const unsigned char w[] = {255, 9, 45, 9};
        long n = rle_encode(s, 300, d, 16);
        report("a run longer than 255 splits", 3, n == 4 && bytes_are(d, (size_t)n, w, 4));
    }
    {
        /* Exactly 255 must NOT emit a spurious empty second pair. */
        unsigned char s[255];
        for (int i = 0; i < 255; i++) s[i] = 3;
        unsigned char d[16];
        const unsigned char w[] = {255, 3};
        long n = rle_encode(s, 255, d, 16);
        report("exactly 255 is one pair", 3, n == 2 && bytes_are(d, (size_t)n, w, 2));
    }
    {
        const unsigned char s[] = {1, 2, 3};
        unsigned char d[4];
        report("refuses to overrun the buffer", 3, rle_encode(s, 3, d, 4) == -1);
    }
    {
        const unsigned char s[] = {1, 1};
        unsigned char d[2];
        long n = rle_encode(s, 2, d, 2);
        report("an exact fit is not an overrun", 2, n == 2 && d[0] == 2 && d[1] == 1);
    }
}
