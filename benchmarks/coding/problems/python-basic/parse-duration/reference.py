import re


def parse_duration(text):
    match = re.fullmatch(r"\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*", text or "")
    if not match or not any(match.groups()):
        raise ValueError(f"not a duration: {text!r}")
    h, m, s = (int(g or 0) for g in match.groups())
    return h * 3600 + m * 60 + s
