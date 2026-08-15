def test_parses_and_falls_back():
    assert to_int("12") == 12
    assert to_int("nope") == 0


def test_the_default_is_honoured():
    assert to_int("nope", -1) == -1


@weight(2)
def test_surrounding_whitespace():
    assert to_int("  7  ") == 7


@weight(3)
def test_a_wrong_type_raises_rather_than_falling_back():
    # A TypeError is a bug in the caller, not a parse failure to swallow.
    for bad in (None, [1]):
        try:
            to_int(bad)
        except TypeError:
            continue
        raise AssertionError(f"to_int({bad!r}) must raise TypeError, not return the default")
