def dedupe(items):
    out = []
    for item in items:
        # Equality, not a set: the problem promises unhashable elements work.
        if not any(item == seen for seen in out):
            out.append(item)
    return out
