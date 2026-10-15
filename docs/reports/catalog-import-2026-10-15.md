# Catalog import and retrieval: results (2026-10-15)

This report covers one working session. In it, exercise retrieval was added on top of the TOML
catalog (ADR 0007), and the rest of the local exercise reference library was brought into
the plannable catalog (ADR 0008). Everything listed here is committed and pushed to `main`,
and `make check` passes: lint, mypy, catalog check and 1,958 tests.

## Summary

| | Before | After |
|---|---:|---:|
| Catalog exercises | 190 | **1,098** |
| … of them `level = "advanced"` | 0 | 145 |
| Equipment values | 9 | 25 |
| Reference entries linked to the catalog (promoted or alias) | 197 | 1,257 |
| Reference entries still lookup-only | 1,127 | **67** |
| Search index vectors (`text-embedding-3-small`) | 0 | 1,098 |

The reference library has 1,324 entries. 984 of them are now catalog entries: 76 from the
first pass and 908 from this one. 273 are aliases of a catalog entry, and 67 stay
lookup-only, each with a recorded reason.

## 1. Retrieval over the catalog (ADR 0007)

- **The source of truth is unchanged.** Exercises live only in
  `src/fitme/catalog/exercises/<range>/<id>.toml`. Everything below is derived from it and
  can be rebuilt.
- **Search documents** (`catalog/search_docs.py`): deterministic en/ru text per exercise
  (names, search terms, pattern, kind, equipment, loaded areas, first instruction
  sentence), each with a SHA-256 fingerprint.
- **Embeddings** come from `make catalog-index`, run locally with `OPENAI_API_KEY`. It is
  incremental: only new or changed documents are embedded, and changing the model
  re-embeds everything. The results go into `src/fitme/catalog/search_index.sqlite`, which
  is committed and shipped in the wheel and the Docker image.
- **At startup** `fitme serve` imports the vectors whose fingerprints still match, and
  rebuilds the FTS5 table from the catalog (migration 0009: `catalog_search_fts`,
  `catalog_embeddings`). No provider is called at startup or per request; runtime query
  embedding is off by default (`FITME_EMBEDDINGS_QUERY`).
- **Ranking** (`services/exercise_search.py`): exact name, name prefix, FTS5 (with Russian
  prefix stems), body-region words → patterns, and semantic similarity, fused with
  Reciprocal Rank Fusion. It only ever ranks the set `available_exercises` already allowed
  (location, equipment, screening, experience). "Avoid an area" is a hard filter.
- **Substitutes** are ranked by structure: same pattern, then mechanics, then name, loaded
  areas and embedding similarity. Each is marked close or general. No working weight is
  carried over.
- **Plan generation** (`plan_generate.v4`): the model sees a balanced pool of about 40 to 50
  exercises with per-pattern quotas, varied equipment, the request's matches and every
  exercise with history, plus `exercise_facts` for each. The guards still judge against
  the full allowed set, and any retrieval error falls back to every allowed id.
- **Assistant** (`assistant.v6`): `find_exercises` now uses hybrid search, and the new
  `find_substitutes(exercise_id, avoid_areas)` ranks replacements. The swap itself still
  goes through `edit_plan`/`edit_today` and the guards.
- **CLI:** `fitme catalog index | reindex --force | search | substitutes`;
  `fitme catalog check` also reports the index status.

Checks against the real index:

| Query / request | Top results |
|---|---|
| `жим гантелей` | dumbbell_bench_press, dumbbell_floor_press, dumbbell_incline_bench_press |
| `румынская тяга` | barbell_romanian_deadlift, dumbbell_romanian_deadlift |
| `подтягивания` | pull_up, chin_up, machine_assisted_pull_up |
| `упражнение для спины` + avoid `lower_back` | machine_seated_row, machine_lat_pulldown, self_resisted_row, band_lat_pulldown |
| substitutes: barbell_bench_press with dumbbells + bench | dumbbell_bench_press (close, similarity 0.89), dumbbell_incline_bench_press, dumbbell_floor_press |
| substitutes: barbell_bent_over_row, avoid lower back | machine_seated_row, dumbbell_incline_row, self_resisted_row |

Plan pools with the request "upper body, chest and back", for under 6 months vs over 5
years of training:

| Location | Allowed (novice / experienced) | Pool | Advanced in pool | Patterns covered |
|---|---|---:|---|---:|
| public_gym | 947 / 1,094 | 42 | 0 / 5 | 10 |
| home (dumbbells, bench, bar) | 411 / 471 | 44 | 0 / 7 | 10 |
| apartment, no equipment | 181 / 212 | 40 | 0 / 5 | 10 |
| outdoor | 190 / 238 | 40–41 | 0 / 9 | 10 |

An independent Opus review of the retrieval change found one blocker: the index file was
caught by `*.sqlite` in `.gitignore` and `.dockerignore`. That is fixed with an explicit
exception and a wheel artifact. Its other findings were fixed too: test isolation from real
keys, scrub and allowed-set tests, the `--force` recovery path, and negated requests no
longer boosting the excluded pattern.

## 2. Importing the reference library (ADR 0008)

**Code changes, made first:**
- **Equipment:** 16 new values: `ez_bar`, `trap_bar`, `smith_machine`, `medicine_ball`,
  `stability_ball`, `bosu_ball`, `foam_roller`, `ab_wheel`, `weight_plate`, `dip_bars`,
  `plyo_box`, `jump_rope`, `battle_rope`, `sled`, `tire`, `sledgehammer`, with en/ru labels.
  The gym-only ones are left out of the home questionnaire.
- **Level:** `level = "advanced"` plus the guard `guards.experience` (rule
  `plan.experience_level`). It is also applied in `available_exercises`: advanced
  exercises need 2+ years of training, and unknown experience fails closed. Tested in
  `tests/guards/test_experience.py`.

**Process:**
- **Batches:** the 1,127 remaining entries went to 24 batches. Each had a Sonnet writer
  followed by an adversarial Opus reviewer.
- **Instructions:** written fresh in en/ru, with no source or web text copied (largest
  similarity ratio to the source text: 0.56).
- **Web search:** 32 searches, used to confirm what unfamiliar movements are (Thibaudeau
  kayak row, elbow lift, towel rows, and others).
- **Cross-batch merge:** resolved 7 id collisions and 3 same-name duplicates as aliases.
- **Final audit:** an independent Opus check of 182 entries (70 random, plus every
  non-standard 20 kg barbell start and every name suggesting an advanced move) plus
  mechanical checks over all 908. It found 19 issues, all fixed:
  - medicine-ball slams and the stiff-leg good morning made advanced;
  - five barbell accessory moves start at calibration instead of the empty bar;
  - missing hernia, ankle and shoulder/elbow contraindications added;
  - equipment and locations corrected on four entries;
  - two Russian names and one setup description fixed.
- **New catalog test:** every listed location must be able to have the exercise's
  equipment. It also caught two old entries: `band_lat_pulldown` at outdoor and
  `machine_upright_bike` at home, which could never have been offered there. Both are fixed.

### Per batch (after review, before the cross-batch merge)

| Batch | Input | Promoted | Advanced | Aliased | Kept out | Web searches |
|---|---:|---:|---:|---:|---:|---:|
| back_1 | 44 | 34 | 6 | 3 | 7 | 2 |
| back_2 | 44 | 40 | 0 | 2 | 2 | 3 |
| back_3 | 44 | 34 | 6 | 7 | 3 | 2 |
| back_4 | 43 | 35 | 9 | 1 | 7 | 0 |
| chest_1 | 47 | 42 | 6 | 1 | 4 | 1 |
| chest_2 | 47 | 39 | 7 | 7 | 1 | 1 |
| chest_3 | 45 | 36 | 10 | 6 | 3 | 2 |
| misc_1 | 51 | 38 | 2 | 11 | 2 | 0 |
| misc_2 | 51 | 40 | 5 | 10 | 1 | 1 |
| shoulders_1 | 40 | 31 | 1 | 4 | 5 | 0 |
| shoulders_2 | 40 | 33 | 2 | 7 | 0 | 1 |
| shoulders_3 | 38 | 27 | 9 | 7 | 5 | 1 |
| upper_arms_1 | 52 | 35 | 4 | 9 | 8 | 3 |
| upper_arms_2 | 52 | 45 | 3 | 6 | 1 | 1 |
| upper_arms_3 | 52 | 46 | 0 | 6 | 0 | 3 |
| upper_arms_4 | 52 | 40 | 7 | 12 | 0 | 2 |
| upper_arms_5 | 50 | 41 | 11 | 5 | 4 | 4 |
| upper_legs_1 | 48 | 37 | 13 | 9 | 2 | 0 |
| upper_legs_2 | 48 | 43 | 11 | 3 | 2 | 0 |
| upper_legs_3 | 48 | 44 | 7 | 3 | 1 | 2 |
| upper_legs_4 | 48 | 42 | 7 | 5 | 1 | 1 |
| waist_1 | 48 | 36 | 4 | 8 | 4 | 1 |
| waist_2 | 48 | 41 | 14 | 5 | 2 | 0 |
| waist_3 | 47 | 39 | 5 | 6 | 2 | 1 |

Totals after the merge: **908 promoted** (145 advanced), **152 aliases**, **67 kept out**.

### Equipment use across the catalog

| Equipment | Exercises |
|---|---:|
| `bench` | 277 |
| `dumbbells` | 240 |
| `cable` | 138 |
| `barbell` | 104 |
| `machine` | 86 |
| `stability_ball` | 70 |
| `resistance_bands` | 68 |
| `rack` | 45 |
| `smith_machine` | 44 |
| `pull_up_bar` | 41 |
| `kettlebell` | 38 |
| `weight_plate` | 27 |
| `ez_bar` | 20 |
| `medicine_ball` | 10 |
| `dip_bars` | 8 |
| `foam_roller` | 8 |
| `ab_wheel` | 4 |
| `bosu_ball` | 3 |
| `plyo_box` | 2 |
| `jump_rope` | 2 |
| `tire` | 2 |
| `battle_rope` | 1 |
| `sledgehammer` | 1 |
| `trap_bar` | 1 |

### Advanced exercises (145)

`ab_wheel_plank_rollout`, `ab_wheel_standing_rollout`, `archer_pull_up`, `archer_push_up`, `arm_extended_push_up`, `assisted_nordic_hamstring_curl`, `back_lever`, `band_alternating_v_up`, `band_jack_knife_sit_up`, `band_v_up`, `bar_korean_dip`, `barbell_bent_arm_pullover`, `barbell_clean_and_press`, `barbell_frankenstein_squat`, `barbell_incline_reverse_grip_press`, `barbell_jefferson_squat`, `barbell_kneeling_squat`, `barbell_landmine_rotation`, `barbell_lateral_lunge`, `barbell_lying_triceps_extension`, `barbell_lying_triceps_extension_behind_head`, `barbell_one_arm_bent_over_row`, `barbell_one_arm_deadlift`, `barbell_overhead_squat`, `barbell_power_clean`, `barbell_pullover`, `barbell_pullover_to_press`, `barbell_reverse_close_grip_bench_press`, `barbell_reverse_grip_decline_bench_press`, `barbell_reverse_grip_incline_bench_press`, `barbell_single_leg_deadlift`, `barbell_single_leg_squat`, `barbell_snatch_pull`, `barbell_squat_jerk`, `barbell_standing_overhead_triceps_extension`, `barbell_standing_rollout`, `barbell_step_up`, `barbell_thruster`, `barbell_wide_reverse_grip_bench_press`, `barbell_zercher_squat`, `bird_dog_push_up`, `bodyweight_astride_jump`, `bodyweight_backward_jump`, `bodyweight_floor_l_sit`, `bodyweight_forward_jump`, `bodyweight_frog_planche`, `bodyweight_front_lever_raise`, `bodyweight_full_planche`, `bodyweight_half_squat_jump`, `bodyweight_jump_squat`, `bodyweight_jump_squat_feet_together`, `bodyweight_jumping_lunge`, `bodyweight_leg_wipers`, `bodyweight_one_arm_bench_dip`, `bodyweight_pistol_squat`, `bodyweight_planche_lean`, `bodyweight_scissor_jump`, `bodyweight_sissy_squat`, `bodyweight_skater_hop`, `bodyweight_sprint_intervals`, `bodyweight_star_jump`, `bodyweight_straddle_planche`, `bodyweight_v_sit`, `bodyweight_v_up`, `clap_push_up`, `dip_bars_wide_grip_chest_dip`, `dumbbell_alternating_curl_foot_on_stability_ball`, `dumbbell_clean`, `dumbbell_curl_stork_stance`, `dumbbell_one_arm_snatch`, `dumbbell_one_leg_stability_ball_fly`, `dumbbell_push_press`, `dumbbell_single_arm_overhead_carry`, `dumbbell_single_leg_deadlift`, `dumbbell_single_leg_deadlift_box_support`, `dumbbell_single_leg_squat`, `dumbbell_stability_ball_pullover_hip_extension`, `dumbbell_stability_ball_seated_curl_one_leg_raised`, `dumbbell_stability_ball_supine_triceps_extension`, `dumbbell_step_up_with_curl`, `dumbbell_stork_stance_triceps_kickback`, `ez_bar_decline_close_grip_press`, `ez_bar_decline_triceps_extension`, `ez_bar_lying_triceps_extension_behind_head`, `ez_bar_stability_ball_french_press`, `full_planche_push_up`, `hanging_pike`, `hanging_twisting_straight_leg_raise`, `hindu_push_up`, `incline_plyometric_push_up`, `incline_reverse_grip_push_up`, `kettlebell_advanced_windmill`, `kettlebell_bent_press`, `kettlebell_bottoms_up_hang_clean`, `kettlebell_double_alternating_hang_clean`, `kettlebell_double_jerk`, `kettlebell_double_push_press`, `kettlebell_double_snatch`, `kettlebell_double_windmill`, `kettlebell_hang_clean`, `kettlebell_one_arm_clean_and_jerk`, `kettlebell_one_arm_jerk`, `kettlebell_one_arm_push_press`, `kettlebell_one_arm_snatch`, `kettlebell_pistol_squat`, `kettlebell_renegade_row`, `kettlebell_sumo_high_pull`, `kettlebell_thruster`, `kettlebell_turkish_get_up`, `kettlebell_two_arm_clean`, `kettlebell_windmill`, `l_sit_pull_up`, `machine_glute_ham_raise`, `medicine_ball_squat_overhead_throw`, `medicine_ball_three_point_chest_throw`, `muscle_up`, `one_arm_chin_up`, `plyo_box_single_leg_landing_jump`, `plyo_push_up`, `plyo_wide_narrow_push_up`, `pull_up_bar_front_lever`, `pull_up_bar_gorilla_chin`, `side_push_up`, `side_to_side_chin_up`, `single_arm_push_up`, `skin_the_cat`, `smith_machine_leg_press`, `stability_ball_pike_push_up`, `stability_ball_weighted_back_extension`, `stalder_press`, `sternum_pull_up`, `straight_bar_dip`, `tire_flip`, `wall_handstand_hold`, `wall_handstand_push_up`, `weight_plate_bench_dip`, `weight_plate_dip`, `weight_plate_sissy_squat`, `weight_plate_straight_bar_dip`, `weight_plate_three_bench_dip`, `weighted_close_grip_chin_up`, `weighted_hanging_leg_raise`, `weighted_muscle_up`, `weighted_one_arm_pull_up`, `weighted_pull_up`

### Still lookup-only (67)

These remain in the local reference TOML with `triage = "lookup_only"` and the reason below.

| Ref id | Exercise | Reason |
|---|---|---|
| 0020 | balance board | equipment: balance board has no Fitme equipment value and is an unstable standing surface; no safe mapping |
| 2404 | ez-bar biceps curl (with arm blaster) | equipment: needs an arm blaster, which has no Fitme Equipment value (a plan could prescribe it to someone without one) |
| 3303 | flag | equipment: vertical pole (human flag) - no matching Fitme equipment value; also advanced gymnastics |
| 3315 | full maltese | equipment: gymnastic rings (maltese); source text describes a standing forward lean, so the movement cannot be reliably identified |
| 0609 | london bridge | equipment: climbing rope (London bridge is a bodyweight pull on a rope hung from a high anchor); no Fitme equipment value fits |
| 0677 | ring dips | equipment: gymnastic rings have no Fitme Equipment value (also a high-skill unstable dip) |
| 0680 | rope climb | equipment: climbing rope |
| 0805 | suspended abdominal fallout | equipment: suspension trainer has no Fitme Equipment value |
| 0806 | suspended push-up | equipment: suspension trainer (straps/rings) is not a Fitme equipment value |
| 0808 | suspended row | equipment: suspension straps (TRX-type) are not a Fitme equipment value; a pull_up_bar alone does not cover the setup |
| 1716 | assisted seated pectoralis major stretch with stability ball | instructions: contradictory (sits on a stability ball while holding another ball to the chest; 'assisted' implies an unspecified partner); web sources describe several unrelated setups, so the intended movement cannot be determined |
| 3167 | bodyweight squatting row (with towel) | instructions: squat-to-row with a towel held in the hands and no anchor or resistance source; movement cannot be defined |
| 3158 | bodyweight standing close-grip row | instructions: standing close-grip row pulling a bar or handles with no stated anchor; movement unspecified |
| 3161 | bodyweight standing one arm row (with towel) | instructions: one-arm towel row with no anchor or resistance source; load basis undefined |
| 3166 | bodyweight standing row | instructions: standing row pulling a bar or handles with no stated anchor; movement unspecified |
| 3165 | bodyweight standing row (with towel) | instructions: towel row with no anchor or resistance source; load basis undefined |
| 1638 | cable pulldown bicep curl | instructions: bar from the highest pulley is 'curled' down toward the thighs with elbows at the sides; reads as a pushdown, not a recognizable biceps curl, and a web search found no description of it |
| 0458 | floor fly (with barbell) | instructions: a single rigid barbell cannot be opened out to the sides, so the described fly is incoherent; the web sources disagree about the setup (two bars as handles, a bench barbell fly, a supine floor version), and an unclear loaded bar over the face is not safe to prescribe; dumbbell_flat_fly and barbell_bench_press already cover the movement |
| 0543 | kettlebell pirate supper legs | instructions: name 'pirate supper legs' is not a recognised movement; web search found only a mislabelled page, source text is a hinged one-arm press with no leg work |
| 1401 | muscle-up (on vertical bar) | instructions: vertical pole/bar muscle-up; a vertical bar is not a Fitme equipment item and the movement cannot be set up on a pull-up bar |
| 1773 | one arm towel row | instructions: towel held in one hand with no anchor; load and setup unclear, towel is not a Fitme equipment item |
| 3641 | weighted kneeling step with swing | instructions: kneeling step with a swing; source describes a straight-arm kneeling overhead swing with an unspecified weight, name and text contradict; no standard exercise matches |
| 0854 | weighted standing hand squeeze | instructions: weighted standing hand squeeze describes squeezing hands together while holding a weight in each hand; the movement and the implement are not clear, and a web search did not settle what is actually gripped or squeezed |
| 0010 | assisted hanging knee raise with throw down | safety: explosive throw-down of the legs from a hang (ballistic leg drop); hanging_knee_raise covers the safe version |
| 0012 | assisted lying leg raise with lateral throw down | safety: ballistic lateral throw-down of straight legs |
| 0013 | assisted lying leg raise with throw down | safety: ballistic throw-down of straight legs |
| 0034 | barbell decline bent arm pullover | safety: decline-bench barbell pullover with a loaded bar over the face and no safety catch; the flat barbell pullover is promoted instead |
| 0035 | barbell decline close grip to skull press | safety: decline bench with head below the feet and an unracked bar lowered to the forehead; the flat lying version is already promoted |
| 1255 | barbell decline pullover | safety: barbell lowered in an arc behind the head over the face on a decline bench with no safety catch; dumbbell_pullover is the safer standard version |
| 0037 | barbell decline wide-grip pullover | safety: decline-bench wide-grip barbell pullover with a loaded bar over the face and no safety catch; the flat barbell pullover is promoted instead |
| 1370 | barbell floor calf raise | safety: standing with the balls of the feet on a barbell lying on the floor; the bar can roll |
| 0040 | barbell front raise and pullover | safety: barbell lowered behind the head with straight arms (behind-the-neck loading, bar over face) |
| 0045 | barbell guillotine bench press | safety: guillotine press lowers the bar to the neck/throat |
| 0052 | barbell jm bench press | safety: the real JM press lowers the bar toward the throat/chin; source text only describes a plain bench press already in the catalog |
| 0053 | barbell jump squat | safety: loaded barbell jump squat, high-impact ballistic jump under a bar on the back |
| 0056 | barbell lying close-grip triceps extension | safety: same underhand-grip barbell lowered to the forehead as 1721 (palms up), with no safety catch; the standard overhand skull crusher (0060) is promoted |
| 0065 | barbell one arm floor press | safety: one-hand barbell press over the chest from the floor; the bar can tip with no catch |
| 0067 | barbell one arm snatch | safety: one-handed ballistic snatch of a full-length barbell caught overhead with no possible safety catch (bar can tip toward the head); the dumbbell and kettlebell one-arm snatches cover the movement |
| 1721 | barbell reverse grip skullcrusher | safety: barbell lowered toward the face with an underhand grip, which is less secure, and no possible safety catch; the standard overhand skull crusher (0060) and the behind-the-head version (1720) are promoted |
| 0086 | barbell seated behind head military press | safety: bar pressed from behind the neck/head (behind-the-neck loading) |
| 0087 | barbell seated bradford rocky press | safety: Bradford press takes the bar behind the head (behind-the-neck loading); source describes only a front press |
| 1718 | barbell seated close grip behind neck triceps extension | safety: behind-the-neck loading, bar lowered behind the head seated with a close grip |
| 0092 | barbell seated overhead triceps extension | safety: barbell seated overhead extension lowers the bar behind the neck with no catch; cable and dumbbell overhead versions exist |
| 0100 | barbell skier | safety: loaded jump with a barbell (jump shrug/skier) |
| 2798 | barbell squat jump step rear lunge | safety: barbell squat jump into a rear lunge is a loaded jump with a bar on the back, ballistic and high impact |
| 0105 | barbell standing bradford press | safety: Bradford press takes the bar behind the head (behind-the-neck loading); safer front press already in catalog |
| 0109 | barbell standing overhead triceps extension | safety: barbell standing overhead extension lowers the bar behind the neck with no catch; cable and dumbbell overhead versions exist |
| 0174 | cable judo flip | safety: power-style loaded rotation finishing with the handle flipped over the shoulder; sources disagree on direction and finish, and cable_standing_lift covers the controlled low-to-high diagonal |
| 0205 | cable rear pulldown | safety: pulldown behind the neck loads the shoulders and neck in an extreme position; a front pulldown is already in the catalog |
| 1325 | cable wide grip rear pulldown behind neck | safety: wide-grip pulldown behind the neck loads the shoulders and neck in an extreme position; a front pulldown is already in the catalog |
| 1201 | dumbbell burpee | safety: dumbbell burpee with push-up on the dumbbells, explosive stand and overhead press; loaded jumping complex with rolling implements |
| 0371 | dumbbell plyo squat | safety: dumbbell plyometric jump squat is a loaded jump |
| 0450 | ez barbell jm bench press | safety: JM press lowers a loaded bar toward the chin and throat with no catch, and the safer ez_bar_close_grip_bench_press is promoted |
| 2298 | inverted row on bench | safety: rowing up toward the edge of a free-standing bench pulls more than the bench weighs, so it can lift or tip onto the face; low_bar_inverted_row and smith_machine_inverted_row cover the movement on a fixed bar |
| 0545 | kettlebell plyo push-up | safety: explosive push-up landing back onto kettlebell handles; the bells can tip under the hands on landing at any experience level |
| 0558 | kipping muscle up | safety: kipping muscle-up (swinging momentum on a bar) |
| 1420 | kneeling jump squat | safety: barbell jump squat from a kneeling start with a bar across the upper back; loaded jump with no safe catch. |
| 1312 | medicine ball chest push with run release | safety: chest throw released onto the floor and then chased at a sprint to catch it; a loose rolling ball plus a sprint across a shared floor is a trip/collision hazard, and the source text contradicts itself (rolls vs caught before landing) |
| 1750 | medicine ball supine chest throw | safety: ballistic supine chest throw sends a loaded ball vertically over the face to be caught, with no safety catch |
| 0670 | rear pull-up | safety: behind-the-neck pull-up (bar pulled to the back of the neck) |
| 0678 | rocky pull-up pulldown | safety: the Rocky pull-up alternates a front pull-up with a behind-the-neck pull-up |
| 0747 | smith behind neck press | safety: behind-the-neck press (bar lowered to the back of the neck) |
| 0769 | smith sprint lunge | safety: the rear foot rests on a Smith bar that slides on its rails, an unstable platform under load; the safer rear-foot-on-bench smith_machine_split_squat is promoted |
| 0772 | smith standing behind head military press | safety: behind-the-neck press (bar lowered behind the head) |
| 0788 | standing behind neck press | safety: behind-the-neck press with a barbell |
| 3314 | straddle maltese | safety: straddle maltese is an elite ring-support strength hold with extreme shoulder and elbow load; also needs gymnastic rings, which have no Fitme Equipment value |
| 1367 | wide grip rear pull-up | safety: behind-the-neck pull-up with wide grip |

## Commits

- `2ed305b` Catalog retrieval: hybrid search, substitutes and plan candidate pools
- `3ad0077` Extended equipment and an experience gate for advanced exercises
- the import commit that adds this report: 908 exercise files, the rebuilt search index, and the audit fixes

## Known limitations and follow-ups

- **Review status:** every catalog entry here was written and reviewed by models, not by a
  person. The en/ru text and the safety fields deserve a human pass. The 145 advanced
  entries and the barbell lying lifts come first.
- **Advanced level:** the gate is all-or-nothing at 2 years of training. A finer level, or
  a per-user opt-in, is open.
- **Reference language review:** the reference TOML's es/it/tr/zh text is still a machine
  draft and is not used by the app.
- **Lexical search:** it can't read negation ("without a bench"). Runtime query embedding
  (`FITME_EMBEDDINGS_QUERY=true`) helps but costs one provider call per query; it is off by
  default.
- **Catalog edits need a re-index:** after editing exercises, run `make catalog-index` and
  commit `search_index.sqlite`. `test_catalog_index.py` fails until the index matches.
- **Restart to pick up the catalog:** `fitme serve` loads the catalog and index at
  startup, so restart the app or rebuild the image after pulling.
