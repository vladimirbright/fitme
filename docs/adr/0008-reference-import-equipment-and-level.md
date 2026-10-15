# ADR 0008 — Importing the reference library: extended equipment and an advanced level

Status: accepted (2026-10-15). Changes A§4.2 (`Equipment`), A§4.4 (catalog fields) and A§7
(a new per-prescription guard rule).

## Context

The first triage of the local exercise reference library (1,324 entries) left 1,127 entries
as lookup-only. Most were set aside because the catalog's equipment list was too short (EZ
bar, Smith machine, stability ball, …), because they were close variants of existing
entries, or because they are advanced moves that a beginner shouldn't be offered. The owner
asked to bring the rest into the plannable catalog.

## Decision

- **Equipment.** `Equipment` gains `ez_bar`, `trap_bar`, `smith_machine`, `medicine_ball`,
  `stability_ball`, `bosu_ball`, `foam_roller`, `ab_wheel`, `weight_plate`, `dip_bars`,
  `plyo_box`, `jump_rope`, `battle_rope`, `sled`, `tire` and `sledgehammer`.
  `GYM_ONLY_EQUIPMENT` (machine, cable, Smith machine, sled, battle rope, tire, sledgehammer)
  is left out of the home questionnaire. Gyms are still assumed to have everything.
  Plate-loaded implements round to 2.5 kg by default, and every kg-carrying implement counts
  as kg-loadable.
- **Level.** A catalog exercise may say `level = "advanced"`. `guards.experience` (rule
  `plan.experience_level`) blocks it for anyone with less than two years of training, and
  `available_exercises` applies the same check, so it never reaches the model's list either.
  Unknown experience fails closed. The guard only removes options.
- **Kept out.** An exercise stays lookup-only, with its reason in the reference TOML, when it
  is hazardous for an unsupervised app at any level: behind-the-neck loading, a bar lowered
  to the throat, standing on a rolling object, loaded jumps and ballistic throw-downs, and the
  like.
- **New entries** follow the existing catalog rules: en/ru text written fresh (web search was
  used only to confirm what a movement is), complete equipment, a conservative start, and
  contraindications covering every loaded area. True duplicates become aliases.

## Result

908 new catalog entries were imported (145 of them `advanced`), so the catalog went from 190
to 1,098 exercises. Another 152 reference entries became aliases, and 67 stay lookup-only (44
for safety, 13 for unclear instructions, 10 for equipment such as climbing ropes). Each batch
had a writer agent and an adversarial reviewer agent. The full list is in
`docs/reports/catalog-import-2026-10-15.md`.

## Consequences

- The setup questionnaire's equipment list is longer. Profiles saved before this keep their
  answers; nobody gets equipment they didn't choose.
- `content_version` changes (enums, catalog, guards).
- The search index must be rebuilt after the import (`make catalog-index`).
