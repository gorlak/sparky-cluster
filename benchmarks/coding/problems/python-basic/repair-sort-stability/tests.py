def test_descending_by_score():
    out = rank([{"n": "a", "score": 2}, {"n": "b", "score": 1}])
    assert [r["n"] for r in out] == ["a", "b"]


@weight(3)
def test_ties_keep_input_order():
    out = rank([{"n": "x", "score": 5}, {"n": "y", "score": 5}, {"n": "z", "score": 9}])
    assert [r["n"] for r in out] == ["z", "x", "y"]


def test_empty():
    assert rank([]) == []


@weight(2)
def test_negative_scores_still_sort_descending():
    out = rank([{"n": "a", "score": -1}, {"n": "b", "score": 3}, {"n": "c", "score": 0}])
    assert [r["n"] for r in out] == ["b", "c", "a"]
