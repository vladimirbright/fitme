# ADR 0014 — Doing more than prescribed earns an increase

Status: accepted (2026-10-16). Amends A§7.3; builds on ADR 0012.

## Context

Double progression (A§7.3) raised a load only when every prescribed set reached the top of
the rep range. Since ADR 0012 the log also holds sets done beyond the prescription, but the
engine ignored them, and a heavier load than prescribed only raised the historical max
(B3). The owner's view: an extra set, or a heavier weight, is a plain sign the load can go
up.

## Decision

- A completed session **exceeded the prescription** for an exercise when every prescribed
  set was performed at or above its prescribed kg load with at least `reps_min` reps, and
  - an extra set was performed at or above that load with at least `reps_min` reps, or
  - every prescribed set was performed at a heavier load than prescribed.
- The engine treats it like `hit_reps_max`: **one** `increment_kg` from the **prescribed**
  load (B3 stands: never a jump to the weight the user lifted), gated by
  `increase_allowed` (silence is not consent), `check_weekly_increment` and
  `check_ceiling`. A failed gate is a hold, as before. The reason reads "did more than
  prescribed".
- Not without a prescribed kg load (after calibration the engine holds at the logged load),
  never from imported history, and never for a non-kg exercise (no kg to raise).
- `hit_reps_max` and `below_reps_min` still judge the prescribed sets only.

## Consequences

- "сделал три подхода из двух" at the prescribed weight raises the next prescription by one
  increment, subject to the weekly cap and the ceiling.
- A heavier weight on one set only, or an extra set below the rep minimum, changes nothing.
- No guard is loosened: the increment size, the weekly cap, the ceiling and the check-in
  rule are the same; only what earns the attempt at an increase is wider.
