"""Lock and unlock a future shift (SCRUM-39, spec sections 3 and 4).

Spec section 3: "shifts that have started or are completed, and locked shifts, are kept
with their breaks"; spec section 4 lists locking and unlocking a future shift among the
mandatory chat capabilities, and the database stores locks.

A lock belongs to a shift of the active (approved) schedule: it tells every later repair
to keep that shift exactly as it is. Only a shift that has not started can be locked or
unlocked; a started or completed shift is kept anyway. Locking does not create a new
schedule version and changes no shift; it is recorded in the change log.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from shiftopt import clock
from shiftopt.db import Store
from shiftopt.db.store import StaleState
from shiftopt.domain import fmt_minutes


class LockRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class LockOutcome:
    schedule_version: str
    employee_id: str
    date: dt.date
    shift_id: str
    start: str
    end: str
    is_locked: bool
    changed: bool        # False when the shift already had the requested state
    now: dt.datetime     # the time the started-shift check used


def set_lock(store: Store, employee_id: str, date: dt.date, locked: bool, *,
             now: dt.datetime | None = None) -> LockOutcome:
    active = store.active_schedule_version()
    if active is None:
        raise LockRefused("NOT_FOUND", "There is no active schedule yet. Locks apply to the approved schedule; "
                                       "approve a draft first.")
    schedule = store.load_schedule(active)
    shift = next((a for a in schedule.assignments if a.employee_id == employee_id and a.date == date), None)
    if shift is None:
        raise LockRefused("NOT_FOUND", f"{employee_id} has no shift on {date.isoformat()} in the active schedule "
                                       f"{active}; there is nothing to {'lock' if locked else 'unlock'}.")
    at = now or clock.now()
    if clock.has_started(date, shift.start, at):
        raise LockRefused("SHIFT_STARTED",
                          f"The shift of {employee_id} on {date.isoformat()} ({fmt_minutes(shift.start)}-"
                          f"{fmt_minutes(shift.end)}) started before {at:%Y-%m-%d %H:%M}; started and completed "
                          "shifts are kept as they are and cannot be locked or unlocked.")

    changed = shift.is_locked != locked
    if changed:
        try:
            store.set_lock(active, employee_id, date, locked)
        except StaleState as e:
            raise LockRefused("CONFLICT", "The active schedule changed meanwhile; nothing was changed. "
                                          "Try again.") from e
    return LockOutcome(schedule_version=active, employee_id=employee_id, date=date, shift_id=shift.shift_id,
                       start=fmt_minutes(shift.start), end=fmt_minutes(shift.end), is_locked=locked,
                       changed=changed, now=at)
