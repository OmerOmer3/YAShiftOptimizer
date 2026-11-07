"""Excel input import (spec section 5-6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shiftopt.db.store import Store
from shiftopt.ingest.excel_reader import ImportResult, InputIssue, read_input


@dataclass
class ImportOutcome:
    status: str  # "OK" or "INPUT_INVALID"
    input_version: str | None
    errors: list[InputIssue] = field(default_factory=list)


def import_workbook(store: Store, path: str | Path) -> ImportOutcome:
    """Read, validate and store a workbook. On INPUT_INVALID nothing is stored and
    the previously active input version stays active (SCRUM-21)."""
    res = read_input(path)
    if not res.ok:
        return ImportOutcome("INPUT_INVALID", None, res.errors)
    return ImportOutcome("OK", store.save_input_version(res.data), [])


__all__ = ["import_workbook", "read_input", "ImportOutcome", "ImportResult", "InputIssue"]
