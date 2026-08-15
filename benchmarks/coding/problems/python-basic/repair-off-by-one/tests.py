def test_ordinary_tail():
    assert last_n([1, 2, 3], 2) == [2, 3]


def test_zero_and_exact_length():
    assert last_n([1, 2, 3], 0) == []
    assert last_n([1, 2, 3], 3) == [1, 2, 3]


@weight(3)
def test_n_larger_than_the_list():
    # The bug: len-n goes negative and the slice silently truncates.
    assert last_n([1, 2, 3], 5) == [1, 2, 3]


@weight(2)
def test_empty_list():
    assert last_n([], 3) == []
