#ifndef PROBLEM_H
#define PROBLEM_H

typedef __SIZE_TYPE__ size_t;

/* Index of the FIRST occurrence of `target` in the sorted `a[0..n)`, or -1 if absent. */
long find_first(const int *a, size_t n, int target);

#endif
