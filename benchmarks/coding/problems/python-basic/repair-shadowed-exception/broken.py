def to_int(text, default=0):
    try:
        return int(text)
    except Exception:
        return default
