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
* `short[d, s]` integer in [0, r(d, s)]: missing employees in slot s.

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
| Coverage | short[d, s] >= r(d, s) - (sum of x[e, t] over shifts covering s - sum of b[e, t', s'] over breaks covering s) |

Surplus is not modelled: it never offsets a shortage in another slot (spec section 2).

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
proves 11,510 optimal. The full experiment report with the greedy baseline is sprint 5.

## 7. Open points

* Cost is not proven on Input_Large_50 within 60 s on 2 workers. To try: more workers on
  the evaluation machine, and symmetry breaking between employees with the same
  unavailable days.
* Repair (sprint 4) reuses this model: past and locked shifts become fixed variables, a
  stage minimizing changed (employee, date) pairs is inserted between coverage and cost,
  and the absent employee is passed in `min_shifts_exempt`.
