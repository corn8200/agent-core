"""Canonical time helpers for agent-core and VPS services.

VPS clock runs UTC. User lives in America/New_York (Eastern, EDT/EST).
Rule of thumb: store UTC, render Eastern. Convert at the edge.

Use:
- `now_local()` for any user-facing render (email subject, iMessage, brief, dashboard).
- `now_utc()` for DB writes, event records, log keys that should stay stable across DST.
- `today_local()` to pick "today's content" in Eastern-date-keyed logic
  (e.g. friday-email's TV/movie/streaming pickers) instead of naive `utcnow().date()`,
  which flips to tomorrow between 8 PM ET and midnight ET.
- `to_local(dt)` / `to_utc(dt)` to convert arbitrary datetimes. Naive inputs are
  assumed UTC (true on the VPS; also the safer default when reading DB ISO strings
  without offset).
- `fmt_local(dt, fmt)` for convenience string formatting with %Z producing EDT/EST.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc


def now_local() -> datetime:
    return datetime.now(EASTERN)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EASTERN)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def fmt_local(dt: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    if dt is None:
        dt = now_local()
    else:
        dt = to_local(dt)
    return dt.strftime(fmt)


def today_local() -> date:
    return now_local().date()
