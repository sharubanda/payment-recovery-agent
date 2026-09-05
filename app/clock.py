"""One clock for the whole agent: naive UTC datetimes, deprecation-free.

Every stored timestamp and every comparison speaks naive-UTC. Mixing in an aware
datetime.now(UTC) raises TypeError at the first comparison, so all time comes from here.
"""
import calendar
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_unix(dt: datetime) -> int:
    """Naive-UTC datetime -> unix seconds. timegm avoids the local-timezone trap of .timestamp()."""
    return calendar.timegm(dt.timetuple())
