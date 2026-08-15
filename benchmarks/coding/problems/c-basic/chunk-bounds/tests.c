#include "problem.h"

extern void report(const char *name, int weight, int ok);

static int at(size_t n_items, size_t n_chunks, size_t index,
              size_t want_start, size_t want_len) {
    size_t start = (size_t)-1, len = (size_t)-1;
    if (chunk_bounds(n_items, n_chunks, index, &start, &len) != 0) return 0;
    return start == want_start && len == want_len;
}

void run_tests(void) {
    report("even split", 1, at(6, 3, 0, 0, 2) && at(6, 3, 1, 2, 2) && at(6, 3, 2, 4, 2));
    report("single chunk takes everything", 1, at(5, 1, 0, 0, 5));
    /* 7 into 3 is 3,2,2 — the extra goes to the EARLIER chunk, and the later starts must
       account for it. Getting the length right but the start wrong is the common miss. */
    report("earlier chunks take the extra", 3,
           at(7, 3, 0, 0, 3) && at(7, 3, 1, 3, 2) && at(7, 3, 2, 5, 2));
    report("two extras land in the first two", 3,
           at(8, 3, 0, 0, 3) && at(8, 3, 1, 3, 3) && at(8, 3, 2, 6, 2));
    /* Fewer items than chunks: trailing chunks are empty but still positioned. */
    report("trailing chunks are empty but placed", 3,
           at(2, 4, 0, 0, 1) && at(2, 4, 1, 1, 1) && at(2, 4, 2, 2, 0) && at(2, 4, 3, 2, 0));
    report("no items at all", 2, at(0, 3, 0, 0, 0) && at(0, 3, 2, 0, 0));
    {
        size_t start = 111, len = 222;
        int bad_chunks = chunk_bounds(4, 0, 0, &start, &len) == -1;
        int bad_index = chunk_bounds(4, 2, 2, &start, &len) == -1;
        /* "writes nothing" is part of the contract, not decoration. */
        report("bad arguments return -1 and write nothing", 2,
               bad_chunks && bad_index && start == 111 && len == 222);
    }
}
