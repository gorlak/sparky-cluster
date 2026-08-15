#ifndef PROBLEM_H
#define PROBLEM_H

typedef __SIZE_TYPE__ size_t;

/* Run-length encode `src[0..n)` into `dst` as (count, value) byte pairs, splitting runs
   longer than 255. Returns bytes written, or -1 if it would exceed `cap`. */
long rle_encode(const unsigned char *src, size_t n, unsigned char *dst, size_t cap);

#endif
