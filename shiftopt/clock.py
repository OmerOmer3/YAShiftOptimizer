"""The current time used by the rules that depend on it (SCRUM-39, spec section 3).

Spec section 3: shifts that have started or are completed, and locked shifts, are kept
with their breaks; an absence is reported before the affected shift starts. Whether a
shift has started depends on "now".

By default "now" is the computer's clock. It can be set to a fixed time instead, so that
tests and demos can use the course sample files, whose week (1-6 November 2026) is
already in the past by the real clock:

    shiftopt --now 2026-11-03T07:00 lock E003 2026-11-04       # command line
    SHIFTOPT_NOW=2026-11-03T07:00 shiftopt-mcp ...             # chat server (or --now)

Times are local and naive, like the dates and times in the input workbook.
"""
from __future__ import annotations

import datetime as dt
import os

ENV_VAR = "SHIFTOPT_NOW"
_override: dt.datetime | None = None


def parse(text: str) -> dt.datetime:
    """'2026-11-03T07:00', '2026-11-03 07:00' or '2026-11-03' (midnight)."""
    t = text.strip()
    try:
        return dt.datetime.fromisoformat(t)
    except ValueError as e:
        raise ValueError(f"'{text}' is not a date and time such as 2026-11-03T07:00") from e


def set_now(value: dt.datetime | str | None) -> None:
    """Fix 'now' (a datetime or an ISO string), or go back to the real clock with None."""
    global _override
    _override = parse(value) if isinstance(value, str) else value


def now() -> dt.datetime:
    if _override is not None:
        return _override
    env = os.environ.get(ENV_VAR)
    if env:
        return parse(env)
    return dt.datetime.now().replace(microsecond=0)


def is_fixed() -> bool:
    return _override is not None or bool(os.environ.get(ENV_VAR))


def shift_start(date: dt.date, start_minutes: int) -> dt.datetime:
    return dt.datetime.combine(date, dt.time()) + dt.timedelta(minutes=start_minutes)


def has_started(date: dt.date, start_minutes: int, at: dt.datetime | None = None) -> bool:
    """A shift has started at its start time; a completed shift has also started."""
    return (at or now()) >= shift_start(date, start_minutes)
