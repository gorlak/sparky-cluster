def test_even_split():
    assert chunk([1, 2, 3, 4, 5, 6], 3) == [[1, 2], [3, 4], [5, 6]]


def test_single_chunk_takes_everything():
    assert chunk([1, 2, 3], 1) == [[1, 2, 3]]


def test_no_items_still_yields_the_chunks():
    assert chunk([], 2) == [[], []]


@weight(3)
def test_earlier_chunks_take_the_extra():
    # 7 into 3 is 3,2,2 — not 2,2,3. The tie-break is the whole problem.
    assert chunk([1, 2, 3, 4, 5, 6, 7], 3) == [[1, 2, 3], [4, 5], [6, 7]]


@weight(3)
def test_fewer_items_than_chunks_leaves_trailing_empties():
    assert chunk([1, 2], 4) == [[1], [2], [], []]


@weight(2)
def test_n_below_one_raises():
    try:
        chunk([1], 0)
    except ValueError:
        return
    raise AssertionError("n=0 must raise ValueError")


@weight(3)
def test_every_element_appears_exactly_once_in_order():
    flat = [x for c in chunk(list(range(10)), 4) for x in c]
    assert flat == list(range(10))
