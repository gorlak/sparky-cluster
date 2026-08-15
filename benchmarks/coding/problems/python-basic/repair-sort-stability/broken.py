def rank(records):
    return sorted(records, key=lambda r: -r["score"], reverse=True)
