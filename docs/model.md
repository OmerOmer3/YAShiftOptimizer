# Optimization model

Draft of deliverable 2 in spec section 10: decision variables, constraints, objective
order, solution method, time-budget handling and implementation decisions.
Code: `shiftopt/engine/model.py` (model) and `shiftopt/engine/solver.py` (staged solve).

## 1. Data

| Symbol | Meaning | Source |
| --- | --- | --- |
| E | employees | Employees sheet |
| T | shift templates; each has a day d(t), start, end, paid minutes p(t), cost c(t) | Shifts sheet |
| U(e) | days employee e declared unavailable (at most two) | UnavailableDay1/2 |
| S(d) | 30-minute slots of day d that appear in Demand (two per demand hour) | Demand sheet |
| r(d, s) | required employees in slot s of day d (the hourly value applies to both halves) | RequiredEmployees |
| B(t) | legal break starts of template t: start + 60 <= b and b + 30 <= end - 60, on 30-minute steps | BreakRules |
| k(t) | number of breaks: 1 for 4 h, 2 for 6 h and 8 h | BreakRules |

The import already guarantees that every template lies fully inside the active hours of
its day (spec section 6), so the model never has to forbid a missing hour separately.

## 2. Decision variables

* `x[e, t]` in {0, 1}: employee e works template t. Created only when d(t) is not in U(e),
  so unavailable days cannot be violated.
* `b[e, t, s]` in {0, 1}: a break of that shift starts at minute s, for s in B(t).
* `short[d, s]` integer in [0, r(d, s)]: missing employees in slot s, equal to
  max(0, required - active) (see the Coverage row below).
* `gap[d, s]` integer: required minus active employees in slot s (may be negative);
  an auxiliary variable for the exact shortage.

Sizes: Input_Small_10 has 233 shift and 1,836 break variables; Input_Large_50 has
1,166 and 9,192; both have 116 coverage slots.

## 3. Constraints (spec sections 1-2)

| Rule | Constraint |
| --- | --- |
| One shift a day | for each e and day d: sum of x[e, t] over templates of d <= 1 |
| 4 to 5 shifts | 4 <= sum over t of x[e, t] <= 5 (the lower bound is dropped only for employees exempt after an absence, spec section 3) |
| 40 paid hours | sum over t of p(t) x[e, t] <= 2400 minutes (breaks are paid, so p(t) is the full duration) |
| Break count | sum over s of b[e, t, s] = k(t) x[e, t] (no breaks without the shift) |
| Break spacing | for two starts s < s' of the same shift with s' - s < 30 + 60: b[e, t, s] + b[e, t, s'] <= 1 (at least one hour of work between breaks; also prevents overlap) |
| Break position | only starts in B(t) exist, so the first-break and last-break rules hold by construction |
| Coverage | gap[d, s] = r(d, s) - (sum of x[e, t] over shifts covering s - sum of b[e, t', s'] over breaks covering s); short[d, s] = max(0, gap[d, s]) with AddMaxEquality |

Surplus is not modelled: it never offsets a shortage in another slot (spec section 2).

The shortage is exact, not only a lower bound (Jira SCRUM-112). With `short >= gap` alone, a
stage-1 run stopped by the time budget could leave slack in some `short` variables, so the
objective value was higher than the real shortage of the returned schedule (found in
SCRUM-106: engine 440, independent checker 385 on a slower computer).

## 4. Objective order and solution method

Google OR-Tools CP-SAT, solved lexicographically in two stages on the same model:

1. **Coverage.** Minimize `sum short[d, s]` (total missing half-hours).
2. **Cost.** Add `sum short[d, s] <= best value of stage 1`, hint the stage-1 solution, and
   minimize `sum c(t) x[e, t]`. Costs are multiplied by 100 so the objective is an exact
   integer (costs have at most two decimals).

No weighted sum is used, so a cost saving can never buy shortage (spec section 2). The
alternative `M * shortage + cost` was rejected (Jira SCRUM-74): it is only correct if M is
large enough for every input, and it yields one proof flag instead of two.

Break times are decision variables, so the solver places breaks where they cost the least
coverage, and two employees on the same template can break at different times (Jira SCRUM-75).

## 5. Time budget, statuses and proof flags (spec sections 2 and 9)

* One wall-clock budget starts when `solve()` is entered, so model building counts.
  Stage 1 receives all of it; stage 2 receives whatever is left (skipped below 0.05 s).
* If stage 1 stops on the time limit with a solution, stage 2 still runs, bounded by the
  best shortage found; the run is FEASIBLE and `CoverageOptimalityProven = FALSE`.
* `CostOptimalityProven` is TRUE only if stage 2 proved its optimum **and** stage 1 had
  proved its own: a cost optimum under an unproven shortage bound is not the true
  lexicographic optimum.

| CP-SAT outcome | Status |
| --- | --- |
| both stages OPTIMAL | OPTIMAL |
| a schedule, not every stage proven | FEASIBLE |
| stage 1 INFEASIBLE | INFEASIBLE, with reasons such as "E001 can work on only 1 day(s)" |
| stage 1 UNKNOWN (time ran out, no schedule) | NO_SOLUTION_FOUND, never INFEASIBLE |
| MODEL_INVALID or an exception while building | ERROR |
| a schedule rejected by the independent checker | ERROR, VALIDATION_FAILED, no draft saved |
| engine totals differ from the independent checker | ERROR, METRICS_MISMATCH, no draft saved |

**Reported totals.** The run reports the shortage and cost of the schedule the engine
returns, computed from that schedule, not the objective value of a stage. As a safety net,
the run service compares these totals with the independent checker's: if they differ, the
run is ERROR with reason `METRICS_MISMATCH` and no draft is saved (Jira SCRUM-106, SCRUM-112).

`FirstFeasibleSeconds` is taken from a solution callback in stage 1 and is empty when no
schedule is found. `OptimizationSeconds` (in the run service) covers model building,
solving and the independent check, and excludes import, export and the chat client.

Every run records the solver version, number of workers, random seed, Python version,
platform and CPU count (spec section 9).

## 6. First measurements

Machine: 2 x Intel Xeon @ 2.10 GHz (container), OR-Tools 9.15.6755, 2 workers, seed 0.

| Input | Budget | Status | Shortage (half-hours) | Cost | First feasible | Proven |
| --- | --- | --- | --- | --- | --- | --- |
| Input_Small_10 | 30 s | OPTIMAL | 0 | 11,510 | 0.45 s | coverage and cost, in 0.9 s |
| Input_Large_50 | 60 s | FEASIBLE | 24 | 57,900 (bound 57,730) | 1.24 s | coverage in 3.8 s; cost not proven |
| Input_Infeasible | 10 s | INFEASIBLE | - | - | - | infeasibility in 0.01 s |

The course sample for Input_Small_10 costs 11,540 and is not claimed optimal; the engine
proves 11,510 optimal.

### Input_Large_50 on our own computers (14.11.2026)

Both machines: Windows 11, 12 logical CPUs, OR-Tools 9.15.6755, seed 0.

| Machine | Workers | Budget | Status | Cost | Cost proven | First feasible | Total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Omer | 2 | 60 s | FEASIBLE | 57,900 | no | 2.43 s | 60.08 s |
| Omer | 4 | 60 s | FEASIBLE | 57,925 | no | 2.33 s | 60.16 s |
| Omer | 8 | 60 s | FEASIBLE | 57,975 | no | 2.49 s | 60.42 s |
| Omer | 8 | 120 s | OPTIMAL | 57,900 | yes | 3.14 s | 75.80 s |
| Ofri | 2 | 60 s | FEASIBLE | 57,900 | no | 4.64 s | 60.05 s |
| Ofri | 4 | 60 s | FEASIBLE | 57,900 | no | 3.56 s | 60.10 s |
| Ofri | 8 | 60 s | OPTIMAL | 57,900 | yes | 2.06 s | 42.32 s |
| Ofri | 8 | 120 s | OPTIMAL | 57,900 | yes | 2.89 s | 39.60 s |

The shortage was 24 half-hours and proven optimal in every run. **57,900 is the optimal
cost of Input_Large_50**, proven three times on two machines, always with 8 workers. The
proof took about 40 s on one machine and about 76 s on the other under the same settings,
so parallel search time varies between machines and runs. With 2 or 4 workers the optimal
cost is usually found within 60 s but not proven. "Total" includes the independent check
after the solver stops, so it can exceed the budget by a fraction of a second.

The full experiment report with the greedy baseline and several seeds per setting is
sprint 5 (Jira SCRUM-46).

## 7. Open points

* Proving the cost optimum on Input_Large_50 takes about 40-76 s with 8 workers on our
  machines, so a 60 s budget may end FEASIBLE. Keep 8 workers as the default. Symmetry
  breaking between employees with the same unavailable days was tried and did not help
  (Jira SCRUM-82).

## 8. Repair after an absence (spec section 3)

The repair reuses the weekly model of sections 2-3 and the same independent check. It
starts from the active schedule (`service/absence.py: repair_start`): the absent shifts are
removed (even if locked), the absent employees are passed in `min_shifts_exempt` (that
week only; 5 shifts and 40 hours still hold), and the current time decides which shifts
have started (`shiftopt/clock.py`, settable for demos because the sample week is past).

**Fixed parts.** Started, completed and locked shifts keep their template and breaks:
`x[e, t] = 1` and their `b[e, t, s] = 1`. The absent employee gets no shift on the absent
day: every `x[e, t]` of that day is 0. Past shifts stay in the model, so every weekly quota
counts them.

**Step 2, single replacement** (`engine/repair.py: single_replacement`, Jira SCRUM-127).
Every other kept shift also stays on its template; for each removed shift exactly one
employee who is free that day takes the same template; no other `x` may change. Stage 1
minimizes shortage; stage 2 keeps it and minimizes moved breaks, `sum (1 - b[e, t, s0])`
over the original breaks of future unlocked shifts. Every candidate changes two pairs per
absence and costs the same, so changes and cost are optimal by construction; the proof
covers the single-replacement scope only (spec section 9).

**Step 3, re-optimization** (`engine/repair.py: reoptimize`), only if step 2 is infeasible,
with the rest of the same time budget. Future unlocked shifts may change; no shift may be
added on a template that has already started. A change is an (employee, date) pair whose
template differs from the active schedule:

| Active schedule has, for (e, d) | changed[e, d] |
| --- | --- |
| template t0 | 1 - x[e, t0] (removed, or swapped to another template of d: counted once) |
| no shift | sum of x[e, t] over the templates of d (added) |

Break-only moves do not change `x`, so they are not counted (they are listed in the diff).
Three lexicographic stages share the budget: minimize shortage; then minimize
`sum changed` with shortage bounded; then minimize cost with both bounded.
`StabilityOptimalityProven` is true only if the changes stage and the shortage stage were
proven; `CostOptimalityProven` only if all three were.

**Tie-break: fewest moved breaks** (Jira SCRUM-128). After the three spec objectives, a
fourth stage keeps shortage, changes and cost at their values and minimizes moved breaks of
the future unlocked shifts that keep their template: `sum (x[e, t0] - b[e, t0, s0])` over
their original breaks (1 exactly when the shift stays and the break does not), the same
measure as stage 2 of the single replacement. Break moves are allowed and are not changes
(spec section 3), so this changes no reported objective and claims no proof flag; it only
keeps the break-move list in the comparison short. Measured on Input_Small_10 (absence on
Wednesday, the same active schedule): shortage 2, 3 changes and cost 11,370 with and without
the stage; moved breaks 21 without it, 0 with it. If no repair is found (INFEASIBLE,
or NO_SOLUTION_FOUND when time runs out), no draft is saved, the active schedule stays and
the absence stays open.

**After solving.** The repair draft is re-checked by the independent checker, its totals
must match the checker's, and every locked shift must be kept; only then is it saved as a
REPAIR draft whose base is the active schedule. It becomes active only on explicit
approval, which also refuses any draft that gives an absent employee a shift that day.

First measurement (Input_Small_10, absence on Wednesday, now Monday 07:00, 2 workers):
no single replacement exists (the employees free on any day are exactly those unavailable
on it); the re-optimization found 3 changes and shortage 2, all three stages proven, in
about 0.5 s.
