#ifndef PROBLEM_H
#define PROBLEM_H

typedef __SIZE_TYPE__ size_t;

/* Where chunk `index` of an even split of `n_items` into `n_chunks` begins, and how long
   it is. Writes *start and *len and returns 0; returns -1 and writes nothing on bad
   arguments. */
int chunk_bounds(size_t n_items, size_t n_chunks, size_t index,
                 size_t *start, size_t *len);

#endif
