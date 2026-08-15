def to_int(text, default=0):
    # ValueError only. A TypeError means the CALLER passed something that is not a string
    # or number, which is a bug to surface rather than a parse failure to swallow.
    try:
        return int(text)
    except ValueError:
        return default
