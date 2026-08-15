#include "problem.h"

static void flip(char *a, char *b) {           /* [a, b] inclusive */
    while (a < b) { char t = *a; *a = *b; *b = t; a++; b--; }
}

size_t reverse_words(char *s) {
    size_t n = 0;
    while (s[n] != '\0') n++;
    if (n == 0) return 0;
    /* Reverse the whole buffer, then un-reverse each word: no allocation needed. */
    flip(s, s + n - 1);
    size_t words = 0, start = 0;
    for (size_t i = 0; i <= n; i++) {
        if (s[i] == ' ' || s[i] == '\0') {
            flip(s + start, s + i - 1);
            words++;
            start = i + 1;
        }
    }
    return words;
}
