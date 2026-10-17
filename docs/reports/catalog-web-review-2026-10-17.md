# Catalog web review — 2026-10-17

Every exercise in `src/fitme/catalog/exercises/` (1,107 before this pass, 1,105 after) was
checked against web sources: ExRx, ACE, NASM, Bodybuilding.com, Muscle & Strength, StrengthLog,
Fitness Volt and similar. The checks covered name vs. movement, the complete equipment list,
locations, loaded areas and their contraindications, level, and the en/ru instructions. Eight
model reviewers each took one batch, and a lead reviewer then checked their edits. No human has
reviewed this pass yet.

Rule followed: a change was applied only when it keeps the catalog the same or stricter (adds
equipment, contraindications or `advanced`; corrects a name or cue). Anything that would loosen
availability was left as an open item below unless the owner decided it.

## Decisions taken

- **Equipment added** (`Equipment`, en/ru locales): `towel`, `dip_belt`, `suspension_straps`,
  `wrist_roller`. The entries that need one now list it, and towel-only moves left
  `apartment_no_equipment`.
- **Low fixed bar** (owner): exercises that need a bar at hip/waist height now use
  `["barbell", "rack"]`, with an empty barbell resting on the safety arms. This covers
  `bar_korean_dip`, `bodyweight_bent_knee_inverted_row`, `low_bar_inverted_row`,
  `straight_bar_dip` and `weight_plate_straight_bar_dip`, and drops `outdoor` from them. A
  doorway pull-up bar alone no longer selects them.
- **Duplicates removed** (owner): `band_straight_leg_deadlift` (= `band_stiff_leg_deadlift`)
  and `barbell_reverse_grip_incline_bench_press` (= `barbell_incline_reverse_grip_press`). No
  plan version or set log referenced them.
- **Stability-ball level** (owner): keep the per-exercise calls from this pass. One-arm
  dumbbell work on the ball and the supine loaded variants already marked `advanced` stay
  advanced. The two-arm supine ball press, fly, hammer press and pullover and the cable-on-ball
  entries stay general.
- **Assisted machines**: `kg_loadable = false`, because their kg is assistance, not load.

## Notable corrections

- Wrong movement described: `barbell_bench_rollout` (the bar sits on the floor and you kneel
  on the bench; now `advanced`), `side_push_up`, `dumbbell_bench_supported_one_arm_curl`,
  `dumbbell_incline_bench_t_raise`, `mobility_seated_calf_stretch`,
  `mobility_seated_wide_angle_fold`, `dumbbell_twisting_bench_press` (wrist rotation),
  `kettlebell_turkish_get_up` and the kettlebell windmills (foot position).
- Misleading names: `mobility_iron_cross_stretch` → "Lying straight-leg rotation";
  `mobility_back_chest_stretch` → "Clasped-hands upper-back stretch";
  `drop_push_up` → "Knee-drop push-up"; `kettlebell_two_arm_clean` → "two-handed";
  `mobility_reverse_clasped_toe_touch`; ru names for `barbell_incline_shoulder_raise`,
  `bodyweight_v_sit`, `jump_rope_skipping`, `mobility_shoulder_band_dislocate`.
- Instructions that offered unlisted gear ("bar or rings", "block or step", "preacher pad or
  incline bench", "wall or rack") now name only the listed equipment. Calf raises that need a
  step stand on a weight plate or plyo box, which is now in their equipment.
- Stricter gating: missing `lower_back`/`hernia` on loaded lunges and split squats; `knee`
  on kneeling and lunge stretches; `elbow_wrist` on front squats; `neck` on
  `band_twisting_overhead_press`; `hernia` on the rower and ski erg; extra `advanced` on
  unstable-surface push-ups, one-arm ball presses and `medicine_ball_close_grip_push_up`.
- `cable_wood_chop` now leads with `core_stability` (controlled cues, so effort is set by
  reps in reserve, not speed). `stance_footwork_drill` is `specific = true`.

## Open items (not applied)

- **Owner decisions**:
  - `smith_machine_hack_squat` describes a feet-forward back squat; some sources mean a bar
    held behind the legs.
  - `bodyweight_janda_sit_up`: a real Janda sit-up needs a partner or calf pads; ours is a
    plain half sit-up. Rename or drop.
  - `barbell_standing_twist`: ours holds the bar in front of the chest; the standard version
    has it on the back.
  - `bodyweight_side_lying_curl` has no real resistance.
- **Equipment the enum can't express**:
  - decline-capable bench for `dumbbell_decline_*` at `home_equipment`;
  - table for `bodyweight_table_row`;
  - landmine for `barbell_landmine_lateral_raise`;
  - parallel handles for outdoor pull-ups.
- **Loosening (needs a yes)**: `jump_rope_skipping` → add `outdoor`.
- **Possible tightening**:
  - `advanced` for `bodyweight_three_bench_dip`, `bosu_ball_push_up`, `stability_ball_pull_in`
    and `dumbbell_forward_lunge_triceps_extension`;
  - `lower_back` on `machine_narrow_grip_seated_row` and
    `machine_alternating_narrow_grip_seated_row`;
  - `knee` on `mobility_cat_cow` and `mobility_thoracic_rotation`;
  - `machine_calf_raise` doesn't say seated or standing.
- **Wording**:
  - `dumbbell_hinge_rotation_reach` search term "spell caster" names a different move;
  - `cable_pro_lat_bar_pulldown` ru name;
  - `bodyweight_astride_jump` and `bodyweight_hip_hinge` ru wording.

## Not confirmed on the web

No reputable source was found for these names or variants. Each one is internally coherent and
gated conservatively, so it was left unchanged:

`arm_extended_push_up`, `band_kneeling_hip_thrust`, `bodyweight_supine_elbow_lift`,
`cable_rear_drive`, `cable_one_arm_fly_stability_ball`, `cable_twisting_pull`,
`cable_standing_lift`, `cable_rotating_grip_row`, `cable_kayak_row`, the four
`cable_*decline*` entries (pulley height), `dip_bars_crossed_leg_dip`, `doorframe_squat_row`,
`dumbbell_around_the_world`, `dumbbell_lying_elbow_press`, `dumbbell_prone_one_arm_rear_raise`,
`dumbbell_reverse_grip_incline_bench_one_arm_row`,
`dumbbell_seated_reverse_grip_overhead_triceps_extension`, `foam_roller_hip_roll`,
`foam_roller_seated_front_raise`, `hindu_push_up`, `kettlebell_bent_press`,
`kettlebell_lunge_pass_through`, `weight_plate_plank`, `weight_plate_wrist_roller`,
`weight_plate_lunge_with_front_raise`, `mobility_all_fours_quad_stretch`,
`one_arm_wall_push_hold`, `mobility_squat_knee_hug`, `mobility_side_lying_adductor_stretch`,
`machine_alternating_leg_press`, `self_resisted_pulldown`, `self_resisted_row`,
`stability_ball_hug`, `stability_ball_hip_flexor_stretch`.
