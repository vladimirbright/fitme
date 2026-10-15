# Prompt: review the exercise reference and update Fitme's TOMLs

Give this prompt to a repository-aware coding LLM working in this checkout. The local
`fitme-reference-exercises.toml` is gitignored, so attach it if the LLM cannot read the
workspace. Do not paste personal training data or `.env` into the chat.

---

Review the local exercise reference and incorporate verified results into Fitme's existing
TOMLs. Work in `/Users/vladimirbright/Projects/fitme`. Read `AGENTS.md` first; its safety,
language, and project-posture rules are binding.

## Files and starting facts

- `fitme-reference-exercises.toml` is the local, text-only reference. It has 1,324 records,
  each with names and instructions in `en`, `es`, `it`, `tr`, `ru`, and `zh`. It is gitignored.
  Spanish, Italian, Turkish, and 1,301 Russian names are machine drafts. The source
  instructions have not passed Fitme safety review.
- `src/fitme/catalog/exercises/` is the **only** catalog the plan generator may use: one
  file per exercise, `<range>/<id>.toml` (ADR 0006; field notes in its `README.md`). It
  currently has 190 reviewed entries. Its schema is defined by
  `src/fitme/domain/catalog.py`, and its allowed enum values are in
  `src/fitme/domain/enums.py`. Read nearby entries before editing it.
- The reference marks 23 name-only possible catalog matches, 146 entries needing equipment
  review, and 1,155 needing safety review. Twelve records have a duplicate English source
  name. These markers identify work; they do not prove that entries are equivalent or safe.
- A first triage pass (2026-10-15) gave every entry `triage = "alias" | "promoted" |
  "lookup_only"`, with `catalog_id`, `related_catalog_id`, `lookup_only_reasons` and
  `pending_language_review`. Its en/ru review was model-only. Treat these fields as a starting
  point to re-check, not as settled decisions.
- The source repository is `https://github.com/tool-source/base-exercises-dataset`; Chinese
  names came from `https://github.com/muxuezzz/exercises-dataset-zh-cn`. Keep the source
  data local until its redistribution terms have been resolved.

## Work through every reference entry

1. Review each source ID in manageable batches. Track completed IDs so that every one of
   the 1,324 receives a decision. Preserve the four-digit source ID and provenance. Do not
   infer equivalence from an English-name match alone. Compare the movement, setup,
   equipment, target, and instruction text with the proposed catalog match. Check the 12
   duplicate-name records as separate variants.
2. Correct clear source metadata errors in the reference TOML: category, target, muscle
   group, secondary muscles, equipment, and exercise name. If a correction changes meaning,
   preserve the original wording in a TOML comment and say why it changed. Equipment labels
   often omit a bench, rack, or pull-up bar; read the instructions for required gear.
3. Review names and instruction text in all six languages for each exercise. Check grip,
   angle, limb count, range, equipment, and movement type; a sit-up must not become a squat.
   Fix mistranslations and unnatural terminology directly in the TOML. Keep
   `name_sources` accurate: label a translation reviewed only after real language review.
   If you cannot verify a language confidently, leave its draft provenance and list it as
   pending. Do not claim that six populated fields mean six reviewed translations.
4. Match true equivalents to existing catalog entries. Keep the existing canonical catalog
   ID and its reviewed English/Russian text when correct. Add a reviewed `es`, `it`, `tr`,
   or `zh` name and instruction to that same entry only after checking that the translation
   describes the catalog's actual variant and its safety cues. Do not create a second
   plannable entry for an alias.
5. For a genuinely new exercise, add it as a new file in `src/fitme/catalog/exercises/` only when
   you can independently justify **every** required plan field: `kind`, `pattern`, complete
   `equipment`, `load_unit`, `locations`, `loads_areas`, `contraindicated_by`,
   `increment_kg`, conservative `start`, and reviewed `instructions`. Give a newly promoted
   entry `en` and `ru` `names` and `instructions`, the app's two languages. Check `kg_loadable` and
   `load_step_kg` semantics, especially for dumbbells, kettlebells, bands, bodyweight,
   cardio, and machines. Use a stable new ID that does not collide with an existing one.
   Never derive injury flags or safe loading from the source's target muscle alone.
6. Keep uncertain exercises in the reference TOML as lookup-only. Record the exact missing
   or disputed field in a TOML comment near the entry. Do not fill safety fields with
   plausible guesses merely to satisfy schema validation. The reference's
   `planning_status = "reference_only"` must remain in place; a separate reviewed catalog
   entry is what makes a movement available to plans.

## Safety and content checks

- Follow `AGENTS.md` §2 and §9. A new catalog entry must fit the existing screening and
  load guards; no new exercise may bypass the progression cap, historical maximum,
  unanswered check-in block, or stop-word halt. `loads_areas` must have corresponding
  `contraindicated_by` coverage under the catalog check.
- Keep instructions short, neutral, and specific. Remove unsupported health outcomes,
  disease claims, or unsafe cues. Do not copy a source instruction into the planning
  catalog just because its grammar is good.
- Do not add media, media IDs, URLs to assets, import scripts, generated JSON, or a second
  exercise database. Edit the current TOMLs directly. Do not alter `src/fitme/i18n/locales`
  merely to add exercise translations; those files hold interface copy.
- Preserve unrelated user edits. Do not unignore or commit the local reference TOML without
  a separate decision on the source data's redistribution terms.

## Verify and report

After each catalog batch, parse both TOMLs, check unique IDs and all six language keys,
then run `.venv/bin/fitme catalog check`. Run the catalog tests and the full test suite
before reporting completion. If a safety rule needs code changes, put the guard in its
dedicated module and add a test proving it blocks the bad case.

Report: IDs reviewed, existing entries updated, new entries added, translations verified
by language, source corrections, entries still lookup-only with reasons, and the exact
validation results. Continue through all 1,324 IDs; if work remains, give the last
completed ID and a concrete continuation point without calling the whole review complete.
