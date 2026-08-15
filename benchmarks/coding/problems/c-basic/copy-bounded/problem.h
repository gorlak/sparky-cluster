#ifndef PROBLEM_H
#define PROBLEM_H

typedef __SIZE_TYPE__ size_t;

/* Copy the NUL-terminated `src` into `dst`, writing at most `cap` bytes INCLUDING the
   terminator. `dst` is always left NUL-terminated when cap > 0. Returns the number of
   characters copied, excluding the terminator. */
size_t copy_bounded(char *dst, size_t cap, const char *src);

#endif
