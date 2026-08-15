def test_one_level():
    assert flatten({"a": {"b": 1}}) == {"a.b": 1}


def test_mixed_depths():
    assert flatten({"a": 1, "b": {"c": {"d": 2}}}) == {"a": 1, "b.c.d": 2}


def test_empty_mapping():
    assert flatten({}) == {}


@weight(3)
def test_lists_are_values_not_containers():
    assert flatten({"a": [1, {"b": 2}]}) == {"a": [1, {"b": 2}]}


@weight(3)
def test_an_empty_nested_mapping_contributes_nothing():
    assert flatten({"a": {}, "b": 1}) == {"b": 1}


@weight(2)
def test_the_separator_is_honoured():
    assert flatten({"a": {"b": 1}}, sep="/") == {"a/b": 1}


@weight(2)
def test_none_is_a_value_not_an_absence():
    assert flatten({"a": {"b": None}}) == {"a.b": None}
