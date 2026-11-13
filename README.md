# YAShiftOptimizer

Weekly shift scheduling for a customer service department. The system reads the
week's data from Excel, minimizes understaffing first and cost second, repairs the
schedule after an absence, and lets the manager work in plain language through an
existing MCP chat client. All calculation and validation run in Python on the server.
the language model only understands requests and presents results.

Final project, B.Sc. Computer Science, Bar-Ilan University.

## Install

Python 3.11 or newer. The database defaults to `shiftopt.db` in the current folder; pass `--db PATH` before the command to use another one.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Run

```bash
# Import and validate an input workbook; on success it becomes the active input version
shiftopt import samples/Input_Small_10.xlsx

# Create a draft schedule for the active input within a time budget (seconds)
shiftopt solve --time-budget 60 --workers 8 --seed 0

# List stored runs, or show one in RunSummary form
shiftopt runs
shiftopt show-run RUN-0001

# Check any schedule workbook against an input with the independent validator
shiftopt validate samples/Input_Small_10.xlsx samples/Output_Small_10.xlsx
```

## Test

```bash
pytest -q
```

CI runs the same command on every push (`.github/workflows/ci.yml`).

## Layout

| Package | Responsibility | Status |
| --- | --- | --- |
| `shiftopt/domain.py` | Shared model: 30-minute slots, half-open intervals | Sprint 1 |
| `shiftopt/ingest` | Excel import, schema 1.0 checks, errors by sheet/row/column | Sprint 1 |
| `shiftopt/db` | Input tables, runs, schedule versions, assignments (later: locks, absences, audit log) | Sprints 1-2 |
| `shiftopt/validator` | Independent hard-rule checker and metrics; never imports `engine` | Sprint 1 |
| `shiftopt/engine` | CP-SAT model: shortage first, then cost, one time budget ([docs/model.md](docs/model.md)) | Sprint 2 |
| `shiftopt/service` | Runs and drafts (later: approval, repair) | Sprint 2 (partial) |
| `shiftopt/mcp_server` | MCP tools for the chat client | Sprint 3 |
| `shiftopt/export` | Output workbook (Schedule, Coverage, EmployeeSummary, RunSummary) | Sprint 3 |
| `shiftopt/baseline` | Greedy baseline for comparison | Sprint 5 |
| `samples/` | Course sample files (input schema 1.0, sample output) | |
| `docs/model.md` | Optimization model: variables, constraints, objective order, time budget | Sprint 2 |

## Conventions

- Times are minutes since midnight; intervals are half-open `[start, end)`.
- Coverage is per 30-minute slot; hourly demand applies to both halves of the hour.
- Day of week: 1 Sunday ... 6 Friday.
- Errors are reported, never silently dropped. Valid input with no possible schedule is
  `INFEASIBLE`, not `INVALID_INPUT`; running out of time is `NO_SOLUTION_FOUND`, never `INFEASIBLE`.
- Every schedule the engine returns is re-checked by the independent validator before it is
  saved as a draft. A new run never changes the active schedule.
- Money is stored in whole cents.
