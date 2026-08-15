def test_overlapping_merge():
    assert merge([(1, 3), (2, 6), (8, 10)]) == [(1, 6), (8, 10)]


def test_empty():
    assert merge([]) == []


def test_unsorted_input_is_sorted():
    assert merge([(5, 7), (1, 2)]) == [(1, 2), (5, 7)]


@weight(3)
def test_touching_intervals_merge():
    # (1,2) and (2,3) touch but do not overlap. The memorised answer keeps them apart.
    assert merge([(1, 2), (2, 3)]) == [(1, 3)]


@weight(2)
def test_fully_contained_interval():
    assert merge([(1, 10), (2, 3)]) == [(1, 10)]


@weight(2)
def test_zero_width_intervals():
    assert merge([(1, 1), (1, 1)]) == [(1, 1)]


@weight(3)
def test_the_input_is_not_mutated():
    src = [(3, 4), (1, 2)]
    merge(src)
    assert src == [(3, 4), (1, 2)], "input was mutated"
