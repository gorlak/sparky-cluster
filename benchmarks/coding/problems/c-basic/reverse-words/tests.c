#include "problem.h"

extern void report(const char *name, int weight, int ok);

static int same(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

void run_tests(void) {
    { char s[] = "a b c"; size_t w = reverse_words(s);
      report("three words", 1, w == 3 && same(s, "c b a")); }
    { char s[] = "hello"; size_t w = reverse_words(s);
      report("a single word is unchanged", 1, w == 1 && same(s, "hello")); }
    { char s[] = ""; size_t w = reverse_words(s);
      report("empty string", 2, w == 0 && same(s, "")); }
    { char s[] = "one two"; size_t w = reverse_words(s);
      report("words of different lengths", 3, w == 2 && same(s, "two one")); }
    { char s[] = "aa bbbb c dd"; size_t w = reverse_words(s);
      report("four uneven words", 3, w == 4 && same(s, "dd c bbbb aa")); }
    { char s[16]; for (int i = 0; i < 16; i++) s[i] = 'Z';
      s[0]='a'; s[1]=' '; s[2]='b'; s[3]='\0';
      reverse_words(s);
      report("writes nothing past the terminator", 3, s[4] == 'Z' && same(s, "b a")); }
    { char s[] = "x y"; size_t w = reverse_words(s);
      report("two single letters", 1, w == 2 && same(s, "y x")); }
}
