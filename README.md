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

# Export a run (or a schedule version, SV-0001) in the output schema of spec sections 7-8
shiftopt export RUN-0001 Schedule.xlsx

# Approve a draft: the only operation that changes the active schedule
shiftopt approve SV-0001
```

## Chat client (MCP)

The manager works through an existing MCP chat client; the server runs on the same computer
over stdio and uses the same database as the command line.

```bash
shiftopt-mcp --db shiftopt.db --export-dir exports      # or: python -m shiftopt.mcp_server ...
```

Claude Desktop: Settings → Developer → Edit Config, then add (use absolute paths):

```json
{
  "mcpServers": {
    "yashiftoptimizer": {
      "command": "/absolute/path/to/.venv/bin/shiftopt-mcp",
      "args": ["--db", "/absolute/path/to/shiftopt.db", "--export-dir", "/absolute/path/to/exports"]
    }
  }
}
```

On Windows the command is `C:\path\to\.venv\Scripts\shiftopt-mcp.exe`. Restart the client after editing.
Tools so far: `get_system_status`, `import_input`, `create_schedule`, `get_run_status`, `resolve_reference`, `approve_draft`, `export_schedule` (more are added per story in sprint 3). A solve runs on a worker thread, so the server stays responsive.
Every number the assistant reports comes from a tool result; business errors come back as
`{"ok": false, "error": {"code", "message", "candidates"}}` so the assistant can ask the manager.

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
| `shiftopt/mcp_server` | MCP server (SDK 2.x `MCPServer`): typed tools, structured errors, stdio | Sprint 3 |
| `shiftopt/export` | Output workbook (RunSummary, Schedule, Coverage, EmployeeSummary, Instructions); metrics from the independent checker, values only | Sprint 3 |
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
