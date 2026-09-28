# Prompt: export your training history for `fitme history import`

Paste everything below the line into the LLM chat that holds your program and training
log. Attach or paste your notes too. Save the TOML it returns as `my.import.toml`: the
`*.import.toml` pattern is gitignored, because the file is your health data. Then check it
with a dry run before importing:

```sh
make import-history FILE=my.import.toml DRY=1      # report only, writes nothing
make import-history FILE=my.import.toml            # import
docker compose exec -T fitme fitme history import - < my.import.toml   # Docker
```

The dry-run report lists every session or plan it would skip, and why. Fix those and run
it again; re-importing is safe because identical sessions are skipped. The exercise list at
the end was generated from the catalog. If the catalog changes, regenerate it with
`make catalog` or copy it from `src/fitme/catalog/exercises.toml`.

---

Convert my training program and my training log from this conversation into ONE TOML file
for an import tool. The tool is strict: an unknown key, a wrong exercise id or a malformed
value makes it skip the item, or reject the whole file. Follow these rules exactly and
**never invent data**. If something is unclear, leave it out and list it in the report at
the end.

## Output
1. One TOML file in a single ```toml code block.
2. After it, a short plain-text report with three parts:
   - entries you left out, and why;
   - exercises you couldn't map to the list below;
   - anything you were unsure about.

## File structure (only these keys exist)

```toml
[meta]
version = 1
timezone = "Europe/Lisbon"

[[plan]]                      # optional: the program I'm CURRENTLY following
name = "Сентябрьский блок"    # up to 40 characters

[[plan.schedule]]             # one entry per training day
weekday = 0                   # 0 = Monday ... 6 = Sunday
workout = "A"

[[plan.workout]]
key = "A"                     # short key, referenced by schedule.workout
title = "Присед + жим сидя"   # up to 40 characters

[[plan.workout.exercise]]     # one per exercise, in order
exercise = "barbell_back_squat"   # a catalog id from the list below, exactly
sets = 3
reps = [5, 5]                 # [min, max]; a range like 8–10 becomes [8, 10]
kg = 80                       # OR load = "bodyweight" (for "no kg" exercises)
rest_seconds = 120            # optional
note = "пауза внизу"          # optional, up to 200 characters

[[session]]                   # one per training ACTUALLY PERFORMED, oldest first
date = 2026-09-01             # the real date (YYYY-MM-DD); never guess one
workout = "A"                 # optional: which [[plan.workout]] key this training was, if known

[[session.set]]               # one entry per WORKING set (no warm-up sets)
exercise = "barbell_back_squat"
kg = 80
reps = 5
```

## Rules
- **Exercise ids:** use ONLY ids from the list below, character for character. Pick the
  closest real match: a narrow-grip pulldown with a V-handle is
  `cable_close_grip_pulldown`, and a seated incline curl is `dumbbell_incline_curl`. If
  nothing matches, leave that exercise out and name it in the report. Never make up an id.
- **Weights (kg):**
  - barbell: the total weight, bar included;
  - dumbbells and kettlebells marked "kg each" below: the weight of ONE implement, never
    the sum of both;
  - "kg one": the single implement's weight;
  - machines and cables: the number on the stack.
- **kg vs no kg:** exercises marked "no kg" must have NO `kg` key: in plans use
  `load = "bodyweight"`, in sessions give only `reps`. Exercises marked with kg must always
  have `kg` in sessions.
- **One number per set:**
  - if a set lists different weights per set ("10 / 10 / 12"), write one `[[session.set]]`
    per set with its own weight;
  - in a plan, use the lowest weight and mention the others in `note`;
  - a range like "35–40 kg" becomes the lower bound;
  - `reps` is a whole number from 1 to 200.
- **Timed work:**
  - planks and similar holds: `reps` = the number of seconds;
  - cardio (treadmill, bike, rower, SkiErg): leave it out of sessions entirely; you may keep
    it in the plan as a no-kg exercise with the minutes in `note`.
- **Warm-ups:** leave warm-up sets out. Log only working sets.
- **Sessions:** include only trainings that were actually done, with a known date. If only
  the week is known, or you'd have to guess what was done, leave the session out and say
  so. A planned-but-skipped set can be written with `skipped = true` (it still needs its
  planned `kg` and `reps`).
- **Plan:** include only my current program. The number of distinct training days in
  `schedule` must equal how many times a week I train. One exercise per entry, and no
  supersets: list superset exercises one after another and mention the superset in `note`.
- **Linking a session to the plan:** if you can tell which `[[plan.workout]]` a session was
  (its exercises match that workout, or its title/day says so), set `workout = "<key>"` on
  that `[[session]]`, using the same key as `[[plan.schedule]].workout`/`[[plan.workout]].key`.
  If the program includes more than one `[[plan]]`, also set `plan = "<plan name>"` on the
  session to say which plan's workout you mean. Leave both out if you're not sure — the tool
  can still work it out from the date when the schedule makes it unambiguous, and a wrong
  guess is worse than none.
- **No personal data:** no names, no health notes, no chat handles. Program notes about
  technique are fine in `note`.
- **Check before answering:**
  - every exercise id is in the list;
  - every "no kg" exercise has no kg;
  - weights for "each" exercises are for one dumbbell;
  - dates are real and not in the future;
  - every session's `workout`, if set, is a key that actually exists in `[[plan.workout]]`;
  - the TOML parses.

## Exercise list (`id | English / Russian name | load`)

"kg total" = the total or stack weight, "kg each" = the weight of one of two implements,
"kg one" = one implement, "no kg" = bodyweight or timed (never give kg).

```
ankle_circles | Ankle circles / Круговые движения стопой | no kg
band_assisted_pull_up | Band-assisted pull-up / Подтягивания с резиновой лентой | no kg
band_external_rotation | Band external rotation / Наружные ротации с резинкой | no kg
band_good_morning | Band good morning / Наклоны с эспандером | no kg
band_lat_pulldown | Band lat pulldown / Тяга эспандера сверху | no kg
band_overhead_press | Band overhead press / Жим эспандера вверх | no kg
band_pull_apart | Band pull-apart / Разведение эспандера перед собой | no kg
band_squat | Band squat / Приседания с эспандером | no kg
barbell_back_squat | Barbell back squat / Приседания со штангой на спине | kg total
barbell_bench_press | Barbell bench press / Жим штанги лёжа | kg total
barbell_bent_over_row | Barbell bent-over row / Тяга штанги в наклоне | kg total
barbell_deadlift | Barbell deadlift / Становая тяга со штангой | kg total
barbell_deficit_deadlift | Deficit deadlift / Становая тяга с дефицита | kg total
barbell_front_squat | Barbell front squat / Фронтальные приседания со штангой | kg total
barbell_good_morning | Barbell good morning / Наклоны со штангой на спине | kg total
barbell_hip_thrust | Barbell hip thrust / Подъём таза со штангой | kg total
barbell_overhead_press | Barbell overhead press / Жим штанги стоя | kg total
barbell_romanian_deadlift | Barbell Romanian deadlift / Румынская тяга со штангой | kg total
bird_dog | Bird dog / Вытягивание разноимённых руки и ноги | no kg
bodyweight_burpee | Burpee / Бёрпи | no kg
bodyweight_calf_raise | Calf raise / Подъём на носки | no kg
bodyweight_dead_bug | Dead bug / Мёртвый жук | no kg
bodyweight_glute_bridge | Glute bridge / Ягодичный мостик | no kg
bodyweight_hip_hinge | Bodyweight hip hinge / Наклоны с отведением таза | no kg
bodyweight_jumping_jacks | Jumping jacks / Прыжки «звёздочка» | no kg
bodyweight_reverse_lunge | Bodyweight reverse lunge / Обратные выпады с собственным весом | no kg
bodyweight_single_leg_deadlift | Bodyweight single-leg deadlift / Становая тяга на одной ноге с собственным весом | no kg
bodyweight_split_squat | Bodyweight split squat / Приседания в разножке | no kg
bodyweight_squat | Bodyweight squat / Приседания с собственным весом | no kg
bodyweight_table_row | Bodyweight table row / Тяга к столу с собственным весом | no kg
brisk_walk | Brisk walk / Ходьба в бодром темпе | no kg
cable_biceps_curl | Cable biceps curl / Сгибание рук на нижнем блоке | kg total
cable_chest_fly | Cable chest fly / Сведение рук в кроссовере | kg total
cable_close_grip_pulldown | Close-grip lat pulldown (V-handle) / Тяга верхнего блока узким хватом | kg total
cable_face_pull | Cable face pull / Тяга каната к лицу | kg total
cable_neutral_grip_pulldown | Neutral-grip lat pulldown / Тяга верхнего блока нейтральным хватом | kg total
cable_rotation | Cable rotation / Ротации на блоке | kg total
cable_triceps_pressdown | Cable triceps pressdown / Разгибание рук на верхнем блоке | kg total
cable_wide_grip_pulldown | Wide-grip lat pulldown / Тяга верхнего блока широким хватом | kg total
cable_wood_chop | Cable wood chop / Диагональная тяга блока | kg total
chair_squat | Chair squat / Приседания до стула | no kg
chin_up | Chin-up / Подтягивания обратным хватом | no kg
decline_push_up | Decline push-up / Отжимания с ногами на возвышении | no kg
doorframe_row | Doorframe row / Тяга у дверного проёма | no kg
dumbbell_bench_press | Dumbbell bench press / Жим гантелей лёжа | kg each
dumbbell_biceps_curl | Dumbbell biceps curl / Сгибания рук с гантелями на бицепс | kg each
dumbbell_bulgarian_split_squat | Dumbbell Bulgarian split squat / Болгарские выпады с гантелями | kg each
dumbbell_flat_fly | Flat dumbbell fly / Разводка гантелей лёжа | kg each
dumbbell_floor_press | Dumbbell floor press / Жим гантелей с пола | kg each
dumbbell_goblet_squat | Dumbbell goblet squat / Приседания с гантелью у груди | kg one
dumbbell_incline_bench_press | Incline dumbbell press / Жим гантелей на наклонной скамье | kg each
dumbbell_incline_curl | Seated incline dumbbell curl / Сгибания на бицепс сидя на наклонной скамье | kg each
dumbbell_incline_rear_delt_raise | Chest-supported incline rear delt raise / Разведения на заднюю дельту лёжа на наклонной скамье | kg each
dumbbell_lateral_raise | Dumbbell lateral raise / Разведение гантелей в стороны | kg each
dumbbell_reverse_lunge | Dumbbell reverse lunge / Выпады назад с гантелями | kg each
dumbbell_romanian_deadlift | Dumbbell Romanian deadlift / Румынская тяга с гантелями | kg each
dumbbell_row | Dumbbell row / Тяга гантели в наклоне | kg one
dumbbell_seated_french_press | Seated dumbbell French press / Французский жим сидя с гантелью | kg one
dumbbell_seated_rear_delt_raise | Seated bent-over rear delt raise / Разведения на заднюю дельту в наклоне сидя | kg each
dumbbell_seated_shoulder_press | Seated dumbbell shoulder press / Жим гантелей сидя | kg each
dumbbell_shoulder_press | Dumbbell shoulder press / Жим гантелей сидя или стоя | kg each
dumbbell_step_up | Dumbbell step-up / Зашагивание с гантелями | kg each
dumbbell_suitcase_carry | Dumbbell suitcase carry / Ходьба с гантелью в одной руке | kg one
dumbbell_triceps_extension | Dumbbell triceps extension / Разгибания рук с гантелью на трицепс | kg one
easy_jog | Easy jog / Лёгкий бег | no kg
hanging_leg_raise | Hanging leg raise / Подъём ног в висе | no kg
incline_push_up | Incline push-up / Отжимания от опоры | no kg
kettlebell_deadlift | Kettlebell deadlift / Становая тяга с гирей | kg one
kettlebell_farmer_carry | Kettlebell farmer carry / Ходьба с гирями в руках | kg each
kettlebell_goblet_squat | Kettlebell goblet squat / Приседания с гирей у груди | kg one
kettlebell_one_arm_row | One-arm kettlebell row / Тяга гири одной рукой | kg one
kettlebell_overhead_press | Kettlebell overhead press / Жим гири над головой | kg one
kettlebell_russian_twist | Kettlebell Russian twist / Русские скручивания с гирей | kg one
kettlebell_swing | Kettlebell swing / Мах гирей | kg one
low_bar_inverted_row | Low bar inverted row / Тяга в упоре на низкой перекладине | no kg
machine_assisted_pull_up | Assisted pull-up machine / Подтягивание в гравитроне | kg total
machine_calf_raise | Machine calf raise / Подъём на носки в тренажёре | kg total
machine_chest_press | Machine chest press / Жим от груди в тренажёре | kg total
machine_incline_treadmill_walk | Incline treadmill walk / Ходьба в горку на дорожке | no kg
machine_lat_pulldown | Lat pulldown / Тяга верхнего блока | kg total
machine_leg_extension | Leg extension / Разгибание ног сидя | kg total
machine_leg_press | Machine leg press / Жим ногами в тренажёре | kg total
machine_pec_deck | Pec deck (machine fly) / Сведения в тренажёре «бабочка» | kg total
machine_rower | Rowing machine / Гребной тренажёр | no kg
machine_seated_leg_curl | Seated leg curl / Сгибание ног сидя | kg total
machine_seated_row | Machine seated row / Тяга сидя в тренажёре | kg total
machine_shoulder_press | Machine shoulder press / Жим вверх в тренажёре | kg total
machine_skierg | SkiErg / Лыжный тренажёр (SkiErg) | no kg
machine_upright_bike | Upright bike / Велотренажёр | no kg
march_in_place | March in place / Шаги на месте | no kg
mobility_ankle_dorsiflexion | Ankle dorsiflexion drill / Упражнение на подвижность голеностопа | no kg
mobility_cat_cow | Cat-cow / Кошка-корова | no kg
mobility_hip_flexor_stretch | Hip flexor stretch / Растяжка сгибателей бедра | no kg
mobility_shoulder_band_dislocate | Band shoulder dislocate / Растяжка плеч с эспандером | no kg
mobility_thoracic_rotation | Thoracic rotation / Ротация грудного отдела позвоночника | no kg
mountain_climbers | Mountain climbers / Скалолаз | no kg
open_book_rotation | Side-lying torso rotation / Поворот корпуса лёжа на боку | no kg
outdoor_interval_jog | Outdoor interval jog / Интервальный бег на улице | no kg
pike_pushup | Pike push-up / Отжимания в упоре согнувшись | no kg
plank | Plank / Планка | no kg
pull_up | Pull-up / Подтягивания | no kg
pushup | Push-up / Отжимания от пола | no kg
resistance_band_chest_press | Resistance band chest press / Жим от груди с эспандером | no kg
resistance_band_row | Resistance band row / Тяга эспандера к поясу | no kg
self_resisted_pulldown | Self-resisted pulldown / Тяга вниз с сопротивлением другой рукой | no kg
self_resisted_row | Self-resisted row / Тяга с сопротивлением другой рукой | no kg
shoulder_circles | Shoulder circles / Круговые движения плечами | no kg
side_plank | Side plank / Боковая планка | no kg
single_leg_glute_bridge | Single-leg glute bridge / Ягодичный мостик на одной ноге | no kg
single_leg_sit_to_stand | Single-leg sit-to-stand / Вставание со стула на одной ноге | no kg
stair_climb | Stair climbing / Подъём по лестнице | no kg
standing_hip_circles | Standing hip circles / Круговые движения тазом стоя | no kg
step_jacks | Step jacks / Шаги в стороны с подъёмом рук | no kg
wall_push_up | Wall push-up / Отжимания от стены | no kg
```
