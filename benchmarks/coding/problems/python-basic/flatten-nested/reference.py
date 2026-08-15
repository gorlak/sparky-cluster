def flatten(d, sep="."):
    out = {}
    for key, value in d.items():
        if isinstance(value, dict):
            # An empty dict contributes no leaves, which falls out of recursing rather
            # than needing a special case.
            for sub, leaf in flatten(value, sep).items():
                out[f"{key}{sep}{sub}"] = leaf
        else:
            out[key] = value
    return out
