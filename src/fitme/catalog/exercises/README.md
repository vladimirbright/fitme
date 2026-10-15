# Fitme exercise catalog

The system may only prescribe exercises listed here (A§4.4). Each exercise is one TOML file,
`<range>/<id>.toml`, whose top-level keys are one `fitme.domain.catalog.Exercise`. The range
folder is chosen by the id's first letter: `a-c`, `d-f`, `g-i`, `j-l`, `m-o`, `p-r`, `s-u`,
`v-x`, `y-z` (ADR 0006). `fitme.catalog.load_catalog()` refuses a file whose name isn't its
id, a file in the wrong folder, and anything else in this directory except this README.

After editing, run `make catalog` (`fitme catalog check`). Every file here is part of
`content_version`.

## Field notes

- `pattern`: the movement pattern the exercise trains (`fitme.domain.enums.ExercisePattern`).
  Used by the "every location has a workable full-body set" check.
- `start`: a conservative calibration start: an empty barbell (20 kg), the lightest
  dumbbell/kettlebell (2-4 kg), or bodyweight/calibration. Never a load someone might already
  have to work up to.
- `load_unit`: what a prescribed `kg` means (`fitme.domain.catalog.LoadUnit`): `total` (a
  barbell, a machine stack; unused for bodyweight/band/cardio), `per_implement` (two
  dumbbells or kettlebells, kg each) or `single_implement` (one dumbbell/kettlebell). Required
  for every dumbbell/kettlebell exercise; the model refuses a missing one.
- `kg_loadable`: whether a kg load may be prescribed at all. Derived by the model when omitted
  (false for cardio, conditioning, mobility, bodyweight-start and band-only moves); set it
  explicitly only to override, with a comment saying why.
- `contraindicated_by`: current-injury-area flags (and `hernia`) the exercise is removed for.
  Chosen conservatively: any meaningful spinal loading is contraindicated by
  `lower_back_injury_current` and `hernia`; overhead/pressing work by
  `shoulder_injury_current` and `elbow_wrist_injury_current`; deep knee flexion under load by
  `knee_injury_current`.
- `search` (optional): the term people actually search for, per language, when `names` is
  descriptive rather than the common name. Only used for the workout's YouTube search link.
- `instructions`: short, neutral execution cues (AGENTS.md §3), with no medical, therapeutic
  or diagnostic language.
