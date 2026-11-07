"""Shared domain model (SCRUM-16).

All times are stored as minutes since midnight and all intervals are half-open
[start, end). Coverage is measured on 30-minute slots: a slot is identified by
its start minute, so slot 600 is 10:00-10:30.

Spec references (project specification v1.0):
  * section 1: "time ranges include the start time and exclude the end time";
    a break 10:00-10:30 removes the employee from coverage at 10:00 only.
  * section 1: shifts and breaks start on multiples of 30 minutes; demand is hourly
    and applies to each half of the hour separately.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

SLOT_MINUTES = 30
MINUTES_PER_DAY = 24 * 60
ALLOWED_DURATIONS_HOURS = (4, 6, 8)
MIN_SHIFTS_PER_WEEK = 4
MAX_SHIFTS_PER_WEEK = 5
MAX_PAID_MINUTES_PER_WEEK = 40 * 60

# Day of week as used in the input: 1 Sunday ... 6 Friday (Saturday is not allowed).
DAY_NAMES = {1: "Sunday", 2: "Monday", 3: "Tuesday", 4: "Wednesday", 5: "Thursday", 6: "Friday"}


# ---------------------------------------------------------------- time helpers
def time_to_minutes(t: dt.time) -> int:
    """09:30 -> 570. Seconds are not allowed by the schema and are ignored here."""
    return t.hour * 60 + t.minute


def minutes_to_time(m: int) -> dt.time:
    """570 -> 09:30. 1440 (end of day) maps to 00:00 for display only."""
    m %= MINUTES_PER_DAY
    return dt.time(m // 60, m % 60)


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def is_slot_aligned(m: int) -> bool:
    return m % SLOT_MINUTES == 0


def slots_in(start: int, end: int) -> range:
    """Slot start minutes covered by the half-open interval [start, end).
    Both ends must be slot-aligned."""
    return range(start, end, SLOT_MINUTES)


def day_of_week(d: dt.date) -> int:
    """Spec numbering: Sunday=1 ... Saturday=7 (7 is never valid in this project)."""
    return (d.weekday() + 1) % 7 + 1


# ---------------------------------------------------------------- input model
@dataclass(frozen=True)
class Employee:
    employee_id: str
    name: str
    unavailable_days: frozenset[int]  # subset of 1..6, at most two
    preferred_start: int | None = None  # minutes; stored, ignored by a pair team


@dataclass(frozen=True)
class DemandHour:
    date: dt.date
    hour_start: int  # minutes, whole hour
    required: int


@dataclass(frozen=True)
class ShiftTemplate:
    shift_id: str
    day_of_week: int
    start: int  # minutes
    duration_hours: int
    cost: float

    @property
    def end(self) -> int:
        return self.start + self.duration_hours * 60

    @property
    def paid_minutes(self) -> int:
        return self.duration_hours * 60


@dataclass(frozen=True)
class BreakRule:
    duration_hours: int
    break_count: int
    break_minutes: int
    min_start_gap: int
    min_end_gap: int
    min_between_breaks: int


@dataclass
class InputData:
    schema_version: str
    week_start: dt.date
    employees: dict[str, Employee]
    demand: dict[tuple[dt.date, int], DemandHour]  # key (date, hour_start minutes)
    templates: dict[str, ShiftTemplate]
    break_rules: dict[int, BreakRule]  # key duration hours
    source_file: str = ""

    # ---- derived views
    @property
    def week_dates(self) -> list[dt.date]:
        """Sunday..Friday of the planning week."""
        return [self.week_start + dt.timedelta(days=i) for i in range(6)]

    def allowed_hours(self, d: dt.date) -> set[int]:
        """Hour starts (minutes) listed in Demand for date d. Any other hour is
        forbidden for work, even between active hours (spec section 1)."""
        return {h for (dd, h) in self.demand if dd == d}

    def required_in_slot(self, d: dt.date, slot_start: int) -> int | None:
        """Hourly demand applied to each half of the hour; None if the hour is not active."""
        dh = self.demand.get((d, slot_start - slot_start % 60))
        return None if dh is None else dh.required

    def date_for_day(self, day: int) -> dt.date:
        return self.week_start + dt.timedelta(days=day - 1)

    def templates_on(self, day: int) -> list[ShiftTemplate]:
        return [t for t in self.templates.values() if t.day_of_week == day]


# ---------------------------------------------------------------- schedule model
@dataclass(frozen=True)
class Break:
    start: int
    end: int


@dataclass
class Assignment:
    """One shift for one employee on one date (key: employee_id + date)."""
    date: dt.date
    employee_id: str
    shift_id: str
    start: int
    end: int
    breaks: tuple[Break, ...]
    paid_hours: float
    cost: float
    is_locked: bool = False

    def working_slots(self) -> list[int]:
        """Slots in which the employee is actively working (breaks removed)."""
        on_break = {s for b in self.breaks for s in slots_in(b.start, b.end)}
        return [s for s in slots_in(self.start, self.end) if s not in on_break]


@dataclass
class Schedule:
    assignments: list[Assignment] = field(default_factory=list)
    # Employees exempt from the 4-shift minimum because of a reported absence (spec section 3).
    min_shifts_exempt: set[str] = field(default_factory=set)
