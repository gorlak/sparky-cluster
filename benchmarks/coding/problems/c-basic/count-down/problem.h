#ifndef PROBLEM_H
#define PROBLEM_H

typedef __SIZE_TYPE__ size_t;

/* Sum the last `k` elements of `a[0..n)`. Sums everything if k > n; zero if n == 0. */
long sum_last(const int *a, size_t n, size_t k);

#endif
