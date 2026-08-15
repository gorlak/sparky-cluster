def test_single_units():
    assert parse_duration("45s") == 45
    assert parse_duration("2h") == 7200


def test_combined_units():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("1h2m3s") == 3723


@weight(2)
def test_surrounding_whitespace_is_allowed():
    assert parse_duration("  90m  ") == 5400


@weight(2)
def test_zero_is_a_duration_not_an_absence():
    assert parse_duration("0s") == 0


@weight(3)
def test_malformed_input_raises():
    for bad in ["", "  ", "30", "1x", "m30", "30m1h", "1h-2m", "1.5h", "h", "1h 30m x"]:
        try:
            parse_duration(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must raise ValueError")
