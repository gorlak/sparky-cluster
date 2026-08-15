def last_n(items, n):
    # `items[len-n:]` goes negative when n > len and silently truncates. Guarding n is
    # the fix.
    return items[len(items) - n:] if n <= len(items) else list(items)
