import re
from datetime import datetime, timezone

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_iso(s: str) -> datetime:
    """Parses ESPN ('2026-10-26T00:15Z') and Odds API ('2026-10-26T00:15:00Z') times
    on any Python 3.8+, always returning an aware UTC datetime."""
    m = _ISO.match(s.strip())
    if not m:
        raise ValueError(f"bad timestamp: {s}")
    y, mo, d, h, mi, sec, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0), tzinfo=timezone.utc)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        tz = tz[1:].replace(":", "")
        from datetime import timedelta
        dt = dt - sign * timedelta(hours=int(tz[:2]), minutes=int(tz[2:]))
    return dt


TWO_WORD = {"maple leafs", "red wings", "blue jackets", "golden knights", "trail blazers",
            "red sox", "white sox", "blue jays"}


def nickname(display_name: str) -> str:
    """'Toronto Maple Leafs' -> 'Maple Leafs', 'Detroit Lions' -> 'Lions'."""
    words = (display_name or "").split()
    if len(words) >= 2 and " ".join(words[-2:]).lower() in TWO_WORD:
        return " ".join(words[-2:])
    return words[-1] if words else ""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
