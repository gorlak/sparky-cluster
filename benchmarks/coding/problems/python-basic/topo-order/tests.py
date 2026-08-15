def test_simple_dependency():
    assert topo_order({"a": [], "b": ["a"]}) == ["a", "b"]


def test_empty_graph():
    assert topo_order({}) == []


@weight(3)
def test_ties_break_alphabetically():
    assert topo_order({"b": [], "a": [], "c": ["a", "b"]}) == ["a", "b", "c"]


@weight(3)
def test_a_node_seen_only_as_a_dependency_still_appears():
    assert topo_order({"a": ["z"]}) == ["z", "a"]


@weight(2)
def test_diamond():
    assert topo_order({"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}) \
        == ["a", "b", "c", "d"]


@weight(3)
def test_a_cycle_raises():
    for cyclic in [{"a": ["b"], "b": ["a"]}, {"a": ["a"]}]:
        try:
            topo_order(cyclic)
        except ValueError:
            continue
        raise AssertionError("a cycle must raise ValueError")
