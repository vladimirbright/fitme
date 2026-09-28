# Import format: past trainings and plans

`fitme history import PATH [--dry-run]` reads one file of your past trainings and, optionally,
the plans you were following, and writes them into the training log (IMPLEMENTATION_PLAN
M11). `PATH` may be `-` to read standard input, which is how the Docker deployment gets a
file in:

```sh
fitme history import my.import.toml --dry-run          # local: report only, write nothing
fitme history import my.import.toml                    # local: import
docker compose exec -T fitme fitme history import - < my.import.toml   # Docker
```

The file is personal health data. Keep it outside the repository: `*.import.toml` and
`*.import.json` are gitignored, so name yours that way. The committed sample
`tests/fixtures/history_sample.toml` is synthetic and shows every field below.

## Format

TOML, or JSON with the same structure (an object per table, an array per `[[...]]` list;
dates as ISO strings). A `.json` path is read as JSON; anything else as TOML; on standard
input, text whose first character is `{` is JSON. Both parse with the standard library.

Every key not listed here is an error, so a typo in a key name never silently drops data.

### `[meta]`

```toml
[meta]
version = 1                 # required; the only version today
timezone = "Europe/Berlin"  # optional IANA name
```

`timezone` is used for dates without a time and for datetimes without an offset. When it
is absent, your profile's timezone is used, and UTC if there is none yet.

### `[aliases]`

```toml
[aliases]
"Squat" = "barbell_back_squat"
"DB bench" = "dumbbell_bench_press"
```

The explicit map from the names in your file to catalog exercise ids
(`src/fitme/catalog/exercises.toml`). A name is matched exactly (case, spaces and all). A
catalog id may also be used directly as an exercise name. **Nothing is guessed:** an
exercise name that is neither an alias nor a catalog id is reported, and every session or
plan that uses it is skipped whole, never partially, so that adding the alias later imports
it completely. An alias whose target is not a catalog id is an error.

### `[[plan]]` (optional, repeatable)

```toml
[[plan]]
name = "Old two-day"

[[plan.schedule]]
weekday = 0          # 0 = Monday ... 6 = Sunday
workout = "A"

[[plan.workout]]
key = "A"
title = "Lower + push"

[[plan.workout.exercise]]
exercise = "Squat"   # an alias or a catalog id
sets = 3
reps = [5, 8]        # [min, max]; a single number means min = max
kg = 75              # or: load = "bodyweight" / load = "calibration"
rest_seconds = 120   # optional, default 90
note = "pause at the bottom"   # optional
```

A plan is transcribed as written: one exercise per block, no supersets. It is judged exactly
like a pasted plan (M8b, `services.planning.judge_import`) **after** the file's sessions are
in, so the imported history is the reference: the load ceiling is the imported historical max
plus one increment, the schedule must match your profile's sessions per week, exercises must
fit your location, equipment and screening flags.

- A **structural** problem — an unknown or contraindicated exercise, equipment/location the
  plan doesn't fit, or a schedule that doesn't match your sessions per week — rejects that
  whole plan; it is reported and not saved. The others are saved in full or not at all.
- A **load** problem — a `kg` that breaks the weekly cap or the ceiling, or that has **no
  history** behind it at all — does not reject the plan. The load engine's value is used
  instead (a `calibration` load when there is no history yet), your declared number is kept
  as a display hint ("your plan says 80 kg, start at or below it and log what you used"), and
  the plan is saved with the substitution reported, e.g. `plan "…": barbell_back_squat 80 kg
  → 75 kg for now (your number kept as a hint)`.

A saved plan has passed a final `validate_plan` re-check (as confirming a pasted plan does)
and is a plan like any other (all plans are equal: it can be viewed, edited, revised, trained
from and made the default); the first one becomes your default plan when you have no default
yet. Saved plans get `plan_versions.origin = 'import'`. Importing a plan writes **no** load
change: the weekly cap starts counting from your first generated session onward. Plans need a
completed setup (`/start`); sessions do not.

### `[[session]]` (repeatable)

```toml
[[session]]
date = 2026-08-03                # a date, a datetime, or the same as an ISO string
title = "Lower + push"           # optional, display only; not stored

[[session.set]]
exercise = "Squat"
kg = 75
reps = 5
set_index = 1                    # optional: 1-based, per exercise, in file order by default
skipped = false                  # optional: true = prescribed but not performed
```

Each `[[session]]` becomes one `completed` training with its sets as `set_logs` rows
(`source = 'import'`). Planned equals actual: the planned load is the `kg` and the planned rep
range is `reps`–`reps`, so the load engine treats the imported load as the last prescription.
**An imported session is never a success and never a failure for the engine**: it holds at
the imported load (no increment is earned from it, no decrease is triggered by it), and
progression starts from the first session you log through the app. The ceiling does use the
imported historical max from the start. The report tells you how many imported sessions are
newer than your last app-logged training (those set the current working load) and, for each
dumbbell/kettlebell exercise, the highest imported load as "X kg each". `kg` is required on a
kg-loadable exercise and must be absent on one that takes no kg (push-ups, a run, a stretch).
`reps` is a whole number from 1 to 200. A `skipped = true` set keeps `kg`/`reps` as the plan
and has no result.

A date without a time is taken as noon in the file's timezone, so it shows on the right
calendar day everywhere. A datetime is used as given; without an offset it is read in the
file's timezone. Both `started_at` and `finished_at` are that one instant: no duration is
invented. Two sessions on the same date are fine; they keep the file's order.

Weights follow the catalog's conventions exactly:

- **barbell**: the total, bar included;
- **dumbbells and kettlebells** (`per_implement`): the weight of **one** implement;
- **one-hand implements** (`single_implement`): the weight of that implement;
- **machines**: the number on the stack.

## What is rejected

Every rejected item is reported with its position (`session #3 (2026-08-10)`, `plan
"Old two-day"`) and the reason; the rest of the file still imports. Reported and skipped:

- an unknown exercise name (see `[aliases]`);
- a `kg` that is not a finite positive number, or above the absolute bound
  (`guards.plausibility`: 300 kg for a total load, 60 kg per implement);
- a `kg` on a non-kg-loadable exercise, or a missing `kg` on a kg-loadable one;
- `reps` outside 1–200, a duplicate `set_index` for one exercise in one session;
- a session dated in the future;
- a plan with a structural problem (the failing rules are listed): an unknown or
  contraindicated exercise, equipment/location it doesn't fit, or a schedule that doesn't
  match your sessions per week. A plan whose only problem is a load — the weekly cap, the
  ceiling, or no history at all — is not rejected; see `[[plan]]` above.

A malformed file (bad TOML/JSON, a missing `[meta]`, an unknown key, an invalid timezone) is
an error: nothing at all is imported.

## Idempotency

Each session gets a content hash (its resolved timestamp plus every set) stored in
`workout_sessions.import_hash`. Importing the same file again reports every session as a
duplicate and writes nothing new. Sessions in the same file with identical content are
duplicates of each other too. Editing a session (a set, a weight, the date or the timezone)
changes its hash, so it imports as a new session next to the old one; delete the old one on
the website first. Plans are deduplicated by content too: a `[[plan]]` whose saved form is
identical to an already-imported plan version is skipped, not saved a second time.

## What the import writes

In one transaction per run (all or nothing; `--dry-run` writes nothing and prints the same
report):

- the sessions and their set rows. An imported session belongs to **no plan**
  (`workout_sessions.plan_version_id` is NULL, `workout_key = "import"`): it did not come
  from a stored plan, and no holder plan is created for it (migration 0007 removed the
  "Imported history" plan earlier versions used to create);
- the accepted `[[plan]]`s as `plans` + `plan_versions`;
- one `decision(kind = history_import)` with the counts, the file's SHA-256, the rejection
  reasons and the unknown exercise names (bounded: at most 30 names, 60 characters each;
  never the file's text otherwise), `load_changes = []`, plus a `decision_outcomes` row with
  what was actually written (the session ids, the saved, duplicate and rejected plans).

Imported sessions never trigger a recap or a progression, are never a session to resume, and
are exported and deleted like every other row (`fitme export`, `fitme delete`, the website's
training delete).
