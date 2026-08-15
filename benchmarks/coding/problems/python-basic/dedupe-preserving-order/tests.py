def test_keeps_first_occurrence():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_empty_and_all_duplicates():
    assert dedupe([]) == []
    assert dedupe(["a", "a", "a"]) == ["a"]


@weight(3)
def test_unhashable_elements():
    # A set-based one-liner — the memorised answer — raises here.
    assert dedupe([[1], [2], [1]]) == [[1], [2]]


@weight(2)
def test_order_is_first_occurrence_not_last():
    assert dedupe([1, 2, 1, 3, 2]) == [1, 2, 3]


@weight(2)
def test_the_input_is_not_mutated():
    src = [1, 1, 2]
    dedupe(src)
    assert src == [1, 1, 2]


@weight(3)
def test_distinct_but_equal_values():
    # True == 1 and False == 0, so the first occurrence wins.
    assert dedupe([0, False, 1, True]) == [0, 1]
