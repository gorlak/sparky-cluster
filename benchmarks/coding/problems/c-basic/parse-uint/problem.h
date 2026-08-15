#ifndef PROBLEM_H
#define PROBLEM_H

typedef __SIZE_TYPE__ size_t;
typedef unsigned int u32;

#define U32_MAX 4294967295u

/* Parse an all-digits string into *out. Returns 0, or -1 (leaving *out alone) if the
   string is empty, contains anything but digits, or overflows a u32. */
int parse_uint(const char *s, u32 *out);

#endif
