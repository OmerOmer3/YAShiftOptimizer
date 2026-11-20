"""Resolve what the manager referred to: one employee, one date, one draft (SCRUM-36).

Spec section 4: a request that does not identify an employee, a date or a draft
unambiguously must be answered with a request for clarification. Each resolver returns
exactly one match or raises Unresolved(code, message, candidates):

  * AMBIGUOUS  - more than one match; candidates are the matches;
  * NOT_FOUND  - no match; candidates are the valid choices or the closest ones.

Resolvers only read. The MCP tools turn Unresolved into a structured answer, so the chat
client asks the manager instead of guessing. No partial-word or "closest" match is ever
accepted silently: suggestions are offered, never chosen.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
import unicodedata

from shiftopt.domain import InputData


class Unresolved(Exception):
    def __init__(self, code: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.candidates = code, message, candidates or []

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = s.replace("׳", "'").replace("״", '"').replace("`", "'").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- employees
def _label(data: InputData, eid: str) -> str:
    return f"{eid} ({data.employees[eid].name})"


def resolve_employee(data: InputData, text: str) -> str:
    """By ID ("E003", "e3", "3"), full name, or a whole word of the name."""
    q = _norm(text or "")
    if not q:
        raise Unresolved("NOT_FOUND", "No employee was named.", [])
    ids = list(data.employees)

    exact = [e for e in ids if e.casefold() == q]
    if exact:
        return exact[0]
    m = re.fullmatch(r"[a-z]?0*(\d+)", q)
    if m:
        by_num = [e for e in ids if re.fullmatch(r"[A-Za-z]*0*" + m.group(1), e)]
        if len(by_num) == 1:
            return by_num[0]
        if len(by_num) > 1:
            raise Unresolved("AMBIGUOUS", f"'{text}' matches several employee IDs.", [_label(data, e) for e in by_num])

    names = {e: _norm(data.employees[e].name) for e in ids}
    full = [e for e in ids if names[e] == q]
    if len(full) == 1:
        return full[0]
    if len(full) > 1:
        raise Unresolved("AMBIGUOUS", f"Several employees are called '{text}'.", [_label(data, e) for e in full])

    words = q.split(" ")
    by_word = [e for e in ids if all(w in names[e].split(" ") for w in words)]
    if len(by_word) == 1:
        return by_word[0]
    if len(by_word) > 1:
        raise Unresolved("AMBIGUOUS", f"'{text}' matches {len(by_word)} employees; which one?",
                         [_label(data, e) for e in by_word])

    close = difflib.get_close_matches(q, list(names.values()) + [e.casefold() for e in ids], n=5, cutoff=0.6)
    sugg = []
    for c in close:
        for e in ids:
            if (names[e] == c or e.casefold() == c) and _label(data, e) not in sugg:
                sugg.append(_label(data, e))
    raise Unresolved("NOT_FOUND", f"No employee matches '{text}'.", sugg)


# ---------------------------------------------------------------- dates
_DAY_NAMES = {
    1: ["sunday", "sun", "ראשון", "יום ראשון", "יום א'", "יום א", "א'"],
    2: ["monday", "mon", "שני", "יום שני", "יום ב'", "יום ב", "ב'"],
    3: ["tuesday", "tue", "tues", "שלישי", "יום שלישי", "יום ג'", "יום ג", "ג'"],
    4: ["wednesday", "wed", "רביעי", "יום רביעי", "יום ד'", "יום ד", "ד'"],
    5: ["thursday", "thu", "thurs", "חמישי", "יום חמישי", "יום ה'", "יום ה", "ה'"],
    6: ["friday", "fri", "שישי", "יום שישי", "יום ו'", "יום ו", "ו'"],
}
_NAME_TO_DAY = {_norm(n): d for d, names in _DAY_NAMES.items() for n in names}
_WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def date_label(d: dt.date) -> str:
    return f"{d.isoformat()} ({_WEEKDAY[d.weekday()]})"


def resolve_date(data: InputData, text: str) -> dt.date:
    """An ISO date, d.m / d/m [/yyyy], or a day name (English or Hebrew), in the planning
    week only (Sunday to Friday of WeekStart)."""
    week = data.week_dates
    choices = [date_label(d) for d in week]
    q = _norm(text or "")

    if q in _NAME_TO_DAY:
        return data.date_for_day(_NAME_TO_DAY[q])

    d = None
    try:
        d = dt.date.fromisoformat(q)
    except ValueError:
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", q)
        if m:
            year = int(m.group(3)) if m.group(3) else data.week_start.year
            try:
                d = dt.date(year, int(m.group(2)), int(m.group(1)))   # Israeli order: day.month
            except ValueError:
                d = None
            if d and not m.group(3) and d not in week:
                # A week that crosses New Year: 30.12 / 2.1 may belong to the neighbouring year.
                for y in (year - 1, year + 1):
                    try:
                        alt = dt.date(y, d.month, d.day)
                    except ValueError:
                        continue
                    if alt in week:
                        d = alt
    if d is None:
        raise Unresolved("NOT_FOUND", f"'{text}' is not a date or a working day of the planning week.", choices)
    if d not in week:
        raise Unresolved("NOT_FOUND", f"{date_label(d)} is not in the planning week "
                                      f"({week[0].isoformat()} to {week[-1].isoformat()}, Sunday to Friday).", choices)
    return d


# ---------------------------------------------------------------- drafts
_LATEST = {"latest", "last", "newest", "the latest", "the last one", "אחרונה", "האחרונה", "הטיוטה האחרונה", "האחרון"}


def _draft_label(v: dict) -> str:
    cost = "-" if v["total_cost"] is None else f"{v['total_cost']:,.2f}"
    return (f"{v['schedule_version']} ({v['run_id']}, cost {cost}, shortage {v['total_shortage_slots']} half-hours, "
            f"created {v['created_at']:%Y-%m-%d %H:%M})")


def resolve_draft(store, text: str | None) -> str:
    """A schedule version (SV-0003), the run that made it (RUN-0003), 'latest', or nothing.
    Unnamed or 'latest' consider only DRAFTs of the active input. Unnamed with several
    drafts is AMBIGUOUS: the manager chooses."""
    active = store.active_input_version()
    drafts = store.list_schedule_versions(input_version=active, approval_status="DRAFT") if active else []
    labels = [_draft_label(v) for v in drafts]
    q = _norm(text or "")

    if q in ("", "draft", "the draft", "הטיוטה", "טיוטה"):
        if not drafts:
            raise Unresolved("NOT_FOUND", "There is no draft for the current input. Create a schedule first.", [])
        if len(drafts) > 1:
            raise Unresolved("AMBIGUOUS", f"There are {len(drafts)} drafts for the current input; which one?", labels)
        return drafts[0]["schedule_version"]
    if q in _LATEST:
        if not drafts:
            raise Unresolved("NOT_FOUND", "There is no draft for the current input. Create a schedule first.", [])
        return drafts[0]["schedule_version"]

    ref = q.upper()
    if re.fullmatch(r"SV-\d+", ref):
        if store.schedule_version_info(ref) is None:
            raise Unresolved("NOT_FOUND", f"Schedule version {ref} not found.", labels)
        return ref
    if re.fullmatch(r"RUN-\d+", ref):
        run = store.get_run(ref)
        if run is None:
            raise Unresolved("NOT_FOUND", f"Run {ref} not found.", labels)
        if run.schedule_version is None:
            raise Unresolved("NOT_FOUND", f"Run {ref} produced no schedule (status {run.status}).", labels)
        return run.schedule_version
    raise Unresolved("NOT_FOUND", f"'{text}' does not name a draft. Use a version such as SV-0003.", labels)
