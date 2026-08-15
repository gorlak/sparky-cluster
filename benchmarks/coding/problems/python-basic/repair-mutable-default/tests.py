@weight(3)
def test_the_default_does_not_persist_between_calls():
    assert collect(1) == [1]
    assert collect(2) == [2], "the default must not persist between calls"


@weight(2)
def test_an_explicit_list_is_still_appended_to():
    target = [0]
    assert collect(1, target) == [0, 1]
    assert target == [0, 1], "an explicitly passed list must still be appended to"
