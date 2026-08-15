def rank(records):
    # `sorted` is stable, so equal scores keep input order for free — the broken version
    # negated the key AND reversed, which cancels out into ascending.
    return sorted(records, key=lambda r: r["score"], reverse=True)
