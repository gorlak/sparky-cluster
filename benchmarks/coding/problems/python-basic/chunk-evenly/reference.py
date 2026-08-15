def chunk(items, n):
    if n < 1:
        raise ValueError("n must be >= 1")
    base, extra = divmod(len(items), n)
    out, i = [], 0
    for index in range(n):
        size = base + (1 if index < extra else 0)
        out.append(items[i:i + size])
        i += size
    return out
