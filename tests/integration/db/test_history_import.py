"""`services/history_import.py` (IMPLEMENTATION_PLAN M11) against a real temp DB: the sample
fixture imports cleanly, a dry run writes nothing, a re-import is a no-op, an unknown exercise
is reported, the load engine then progresses from the imported 75 kg (not calibration), the
ceiling uses the imported max, implausible and future rows are rejected, the import writes no
`load_changes` and leaves the weekly cap untouched, imported sessions never produce a recap
or an active session, imported sessions belong to no plan (no holder plan is created,
migration 0007), plans import with `origin = 'import'` and validate against the imported
history (a plan over the ceiling is reported and not saved; a no-history kg becomes
calibration with the declared hint), export includes and delete removes the rows, JSON is
accepted, and the engine reads chronology rather than insert order."""

from __future__ import annotations

import json
import re
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_planning_service import _settings

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.controllers.training import (
    finish_workout_session,
    insert_set_log,
    insert_workout_session,
)
from fitme.db.selectors.decisions import (
    applied_to_kg_by_exercise,
    list_decision_outcomes,
    list_decisions_for_user,
    recent_increase_deltas_by_exercise,
)
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.db.selectors.training import (
    get_active_workout_session,
    historical_max_by_exercise,
    list_set_logs_for_session,
    list_workout_sessions_for_user,
    recent_session_outcomes,
)
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS
from fitme.domain.models import Plan
from fitme.guards.plan import validate_plan
from fitme.services import planning, recap
from fitme.services.account import delete_user, export_user
from fitme.services.history_import import (
    IMPORT_WORKOUT_KEY,
    ImportFormatError,
    ImportReport,
    NoUserError,
    import_history,
)
from fitme.services.loads import next_load_for_exercise

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "history_sample.toml"
_SQUAT = "barbell_back_squat"
_EPOCH = "1970-01-01T00:00:00.000000Z"


def fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


async def seed_gym_profile(db: Database, user_id: int, *, sessions_per_week: int = 2) -> None:
    """A complete public-gym profile (every red flag "no", no area flags): the fixture's
    plan uses a machine, which only a gym location offers."""
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="2y_5y",
            barbell_experience="yes",
            preferences=["weight_training"],
            location="public_gym",
            equipment=[],
            sessions_per_week=sessions_per_week,
            session_minutes=60,
            focus="strength",
            completed_at=clock.now(),
        )
        for flag in (*RED_FLAGS, *AREA_FLAGS):
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )


async def run_import(
    db: Database, text: str, *, fmt: str = "toml", dry_run: bool = False
) -> ImportReport:
    return await import_history(db, _settings(), text, fmt=fmt, source="test", dry_run=dry_run)


async def seed_plan_version(db: Database, user_id: int, *, name: str = "P") -> int:
    """A stored plan version for the app-logged sessions some tests add next to the imported
    ones (an imported session itself belongs to no plan, migration 0007)."""
    async with db.transaction() as conn:
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_confirm",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
            load_changes=[],
        )
        plan_id = await insert_plan(
            conn, user_id=user_id, name=name, is_default=False, status="active"
        )
        return await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": name, "schedule": [], "workouts": []},
            origin="llm",
            decision_id=decision_id,
        )


def toml_with(**sections: str) -> str:
    """A minimal file: `[meta]` + the fixture's aliases + whatever sections are given."""
    body = "\n".join(sections.values())
    return (
        '[meta]\nversion = 1\ntimezone = "Europe/Berlin"\n\n[aliases]\n'
        '"Squat" = "barbell_back_squat"\n"DB bench" = "dumbbell_bench_press"\n'
        '"Push-up" = "pushup"\n\n' + body
    )


def session_toml(day: str, *sets: tuple[str, float | None, int]) -> str:
    lines = [f"[[session]]\ndate = {day}"]
    for exercise, kg, reps in sets:
        lines.append(f'[[session.set]]\nexercise = "{exercise}"\nreps = {reps}')
        if kg is not None:
            lines.append(f"kg = {kg:g}")
    return "\n".join(lines) + "\n"


def plan_toml(name: str, squat_kg: float, *, weekdays: tuple[int, ...] = (0, 3)) -> str:
    schedule = "".join(
        f'[[plan.schedule]]\nweekday = {weekday}\nworkout = "A"\n' for weekday in weekdays
    )
    return (
        f'[[plan]]\nname = "{name}"\n{schedule}'
        '[[plan.workout]]\nkey = "A"\ntitle = "A"\n'
        '[[plan.workout.exercise]]\nexercise = "Squat"\nsets = 3\nreps = [5, 8]\n'
        f"kg = {squat_kg:g}\n"
    )


async def _counts(db: Database, user_id: int) -> dict[str, int]:
    async with db.read() as conn:
        sessions = await list_workout_sessions_for_user(conn, user_id)
        plans = await list_plans_for_user(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
        set_rows = 0
        for session in sessions:
            set_rows += len(await list_set_logs_for_session(conn, session.id))
    return {
        "sessions": len(sessions),
        "set_logs": set_rows,
        "plans": len(plans),
        "decisions": len(decisions),
    }


# --- The accept list -----------------------------------------------------------------------------


async def test_sample_fixture_imports_cleanly(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)

    report = await run_import(db, fixture_text())

    assert report.sessions_new == 3
    assert report.sessions_duplicate == 0
    assert report.rejected == ()
    assert report.unknown_exercises == ()
    assert [plan.name for plan in report.plans_saved] == ["Old two-day"]
    assert report.decision_id is not None

    async with db.read() as conn:
        sessions = await list_workout_sessions_for_user(conn, user_id)
        plans = await list_plans_for_user(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
        outcomes = await list_decision_outcomes(conn, report.decision_id)
        rows = [
            row for session in sessions for row in await list_set_logs_for_session(conn, session.id)
        ]
        history_max = await historical_max_by_exercise(conn, user_id)
    assert len(sessions) == 3
    for session in sessions:
        assert session.status == "completed"
        assert session.import_hash is not None
        assert session.plan_version_id is None  # belongs to no plan: no holder plan exists
        assert session.workout_key == IMPORT_WORKOUT_KEY
        assert session.started_at == session.finished_at
    # Oldest first in the file; timestamps are the file's dates (noon Berlin -> 10:00Z).
    by_time = sorted(session.finished_at or "" for session in sessions)
    assert by_time[0] == "2026-08-03T10:00:00.000000Z"
    assert by_time[2] == "2026-08-10T16:30:00.000000Z"  # the datetime, Berlin summer time
    assert len(rows) == 18
    assert all(row.source == "import" for row in rows)
    assert all(not row.skipped for row in rows)
    for row in rows:  # planned = actual, so the engine reads the load as the prescription
        assert row.planned_load_kg == row.actual_load_kg
        assert row.planned_reps_min == row.planned_reps_max == row.actual_reps
    assert history_max[_SQUAT] == 75.0
    assert history_max["dumbbell_bench_press"] == 20.0
    assert history_max["machine_leg_press"] == 120.0
    assert "pushup" not in history_max

    # The only plan the import created is the file's own `[[plan]]`; there is no holder.
    assert [plan.name for plan in plans] == ["Old two-day"]
    imported_plan = plans[0]
    assert imported_plan.status == "active" and imported_plan.is_default
    async with db.read() as conn:
        versions = await list_plan_versions(conn, imported_plan.id)
    assert [version.origin for version in versions] == ["import"]
    body = Plan.model_validate(versions[0].body)
    assert [day.weekday for day in body.schedule] == [0, 3]
    squat = body.workouts[0].blocks[0].items[0]
    assert squat.exercise_id == _SQUAT and squat.load.kg == 75.0  # history exists: kept as kg

    kinds = [decision.kind for decision in decisions]
    assert kinds.count("history_import") == 1
    decision = next(item for item in decisions if item.kind == "history_import")
    assert decision.load_changes == []
    assert decision.user_report is not None
    assert decision.user_report["sessions"] == {
        "declared": 3,
        "new": 3,
        "duplicate": 0,
        "rejected": 0,
    }
    assert decision.user_report["file_sha256"] == report.file_sha256
    assert "Squat" not in json.dumps(decision.user_report)  # no file text in the record
    assert len(outcomes) == 1
    assert "holder_plan_version_id" not in outcomes[0].outcome
    assert outcomes[0].outcome["session_ids"] == list(report.session_ids)


async def test_dry_run_writes_nothing(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)

    report = await run_import(db, fixture_text(), dry_run=True)

    assert report.dry_run and report.decision_id is None
    assert report.sessions_new == 3
    assert [plan.name for plan in report.plans_saved] == ["Old two-day"]
    assert report.rejected == ()
    assert await _counts(db, user_id) == {"sessions": 0, "set_logs": 0, "plans": 0, "decisions": 0}


async def test_reimport_is_a_no_op(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())
    before = await _counts(db, user_id)

    report = await run_import(db, fixture_text())

    assert report.sessions_new == 0
    assert report.sessions_duplicate == 3
    assert report.plans_saved == ()
    assert report.plans_duplicate == 1
    assert report.rejected == ()
    after = await _counts(db, user_id)
    assert after == {**before, "decisions": before["decisions"] + 1}  # only the run's record


async def test_unknown_exercise_is_reported_and_its_session_skipped(
    db: Database, user_id: int
) -> None:
    text = toml_with(
        a=session_toml("2026-08-01", ("Squat", 60, 5)),
        b=session_toml("2026-08-02", ("Hack squat", 80, 8), ("Squat", 60, 5)),
    )

    report = await run_import(db, text)

    assert report.sessions_new == 1
    assert report.unknown_exercises == ("Hack squat",)
    assert len(report.rejected) == 1
    assert report.rejected[0].ref == "session #2"
    assert "unknown exercise 'Hack squat'" in report.rejected[0].reason
    assert (await _counts(db, user_id))["sessions"] == 1


async def test_next_load_after_import_proposes_from_the_imported_75(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())
    squat = load_catalog().by_id(_SQUAT)
    assert squat is not None

    async def engine_kg() -> tuple[float | None, str]:
        decision = await next_load_for_exercise(
            db,
            user_id=user_id,
            exercise=squat,
            checkins={},
            flagged_areas=frozenset(),
            cap_kg=2.5,
            since_7d=_EPOCH,
        )
        assert decision.load.kind == "kg"  # from the imported history, not calibration
        return decision.load.kg, decision.kind.value

    # An imported session is never a success: the engine holds at the imported 75, it does
    # not add an increment from it. Progression starts from the first app-logged session.
    assert await engine_kg() == (75.0, "hold")

    version_id = await seed_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version_id,
            workout_key="A",
            status="in_progress",
        )
        for index in (1, 2, 3):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=_SQUAT,
                set_index=index,
                planned_load_kg=75.0,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=75.0,
                actual_reps=8,
                rpe=None,
                source="button",
            )
        await finish_workout_session(conn, session_id, status="completed")

    # One app-logged success at 75 earns the increment (within the ceiling 75 + 2.5).
    assert await engine_kg() == (77.5, "increase")


async def test_ceiling_uses_the_imported_max(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())

    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    inputs = planning.build_inputs(load_catalog(), snapshot, _settings(), user_id)
    assert inputs.ctx.history_max_kg[_SQUAT] == 75.0

    over = Plan.model_validate(
        {
            "name": "Over",
            "schedule": [{"weekday": 0, "workout_key": "A"}, {"weekday": 3, "workout_key": "A"}],
            "workouts": [
                {
                    "key": "A",
                    "title": "A",
                    "blocks": [
                        {
                            "kind": "single",
                            "items": [
                                {
                                    "exercise_id": _SQUAT,
                                    "sets": 3,
                                    "reps_min": 5,
                                    "reps_max": 8,
                                    "load": {"kind": "kg", "kg": 80.0},  # 75 + 2.5 is the ceiling
                                    "rest_seconds": 90,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    failures = {
        verdict.rule: verdict.detail
        for verdict in validate_plan(over, inputs.ctx)
        if not verdict.ok
    }
    # 80 is over the ceiling (75 + 2.5) and, as a +5 over the reference, over the weekly cap.
    assert set(failures) == {"ceiling.historical_max", "progression.weekly_cap"}
    assert "75" in failures["ceiling.historical_max"]


async def test_plan_over_the_imported_ceiling_is_reported_and_not_saved(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())
    before = await _counts(db, user_id)

    report = await run_import(db, toml_with(p=plan_toml("Too heavy", 80.0)))

    assert report.plans_saved == ()
    assert len(report.rejected) == 1
    assert report.rejected[0].ref == "plan 'Too heavy'"
    assert "ceiling.historical_max" in report.rejected[0].reason
    assert (await _counts(db, user_id))["plans"] == before["plans"]
    # One increment above the imported max is within the ceiling and the weekly cap.
    report = await run_import(db, toml_with(p=plan_toml("One step up", 77.5)))
    assert [plan.name for plan in report.plans_saved] == ["One step up"]


async def test_implausible_and_future_rows_are_rejected(db: Database, user_id: int) -> None:
    tomorrow = (clock.now() + timedelta(days=1)).date().isoformat()
    text = toml_with(
        a=session_toml("2026-08-01", ("Squat", 400, 5)),  # above the 300 kg total bound
        b=session_toml("2026-08-02", ("DB bench", 70, 5)),  # above the 60 kg per-implement bound
        c=session_toml("2026-08-03", ("Push-up", 20, 10)),  # kg on a non-kg-loadable exercise
        d=session_toml("2026-08-04", ("Squat", None, 5)),  # kg missing on a kg-loadable one
        e=session_toml("2026-08-05", ("Squat", 60, 0)),  # reps out of range
        f=session_toml("2026-08-06", ("Squat", -5, 5)),  # not positive
        g=session_toml(tomorrow, ("Squat", 60, 5)),  # in the future
        h=session_toml("2026-08-07", ("Squat", 60, 5)),  # the one good session
    )

    report = await run_import(db, text)

    assert report.sessions_new == 1
    assert len(report.rejected) == 7
    reasons = {item.ref: item.reason for item in report.rejected}
    assert "absolute bound of 300 kg" in reasons["session #1"]
    assert "absolute bound of 60 kg" in reasons["session #2"]
    assert "takes no kg load" in reasons["session #3"]
    assert "kg is required" in reasons["session #4"]
    assert "reps must be" in reasons["session #5"]
    assert "not a finite positive number" in reasons["session #6"]
    assert "future" in reasons["session #7"]
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    fired_rules = {item["rule"] for item in decisions[0].guards_fired}
    assert fired_rules == {"plausibility.parsed_load"}


async def test_non_finite_values_are_rejected(db: Database, user_id: int) -> None:
    payload: dict[str, Any] = {
        "meta": {"version": 1},
        "aliases": {"Squat": _SQUAT},
        "session": [
            {"date": "2026-08-01", "set": [{"exercise": "Squat", "kg": float("nan"), "reps": 5}]},
            {"date": "2026-08-02", "set": [{"exercise": "Squat", "kg": float("inf"), "reps": 5}]},
        ],
    }
    report = await run_import(db, json.dumps(payload), fmt="json")
    assert report.sessions_new == 0
    assert len(report.rejected) == 2


async def test_import_writes_no_load_changes_and_leaves_the_weekly_cap_free(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())

    async with db.read() as conn:
        increases = await recent_increase_deltas_by_exercise(conn, user_id, since=_EPOCH)
        applied = await applied_to_kg_by_exercise(conn, user_id, since=_EPOCH)
        snapshot = await planning.read_snapshot(conn, user_id)
    assert increases == {}
    assert applied == {}
    inputs = planning.build_inputs(load_catalog(), snapshot, _settings(), user_id)
    assert inputs.ctx.increases_7d == {}
    assert inputs.ctx.applied_to_kg_7d == {}
    # A +2.5 on the squat right after the import is a first, allowed increase this week.
    plan = Plan.model_validate(
        {
            "name": "Next",
            "schedule": [{"weekday": 0, "workout_key": "A"}, {"weekday": 3, "workout_key": "A"}],
            "workouts": [
                {
                    "key": "A",
                    "title": "A",
                    "blocks": [
                        {
                            "kind": "single",
                            "items": [
                                {
                                    "exercise_id": _SQUAT,
                                    "sets": 3,
                                    "reps_min": 5,
                                    "reps_max": 8,
                                    "load": {"kind": "kg", "kg": 77.5},
                                    "rest_seconds": 90,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    verdicts = validate_plan(plan, inputs.ctx)
    assert all(verdict.ok for verdict in verdicts), [v for v in verdicts if not v.ok]
    assert "progression.weekly_cap" in {verdict.rule for verdict in verdicts}


async def test_imported_sessions_produce_no_recap_and_no_active_session(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())

    assert await recap.pending_recap_session(db, user_id) is None
    async with db.read() as conn:
        assert await get_active_workout_session(conn, user_id) is None


async def test_no_history_kg_in_a_plan_becomes_calibration_with_the_declared_hint(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    text = toml_with(
        p=(
            '[[plan]]\nname = "Deadlift day"\n'
            '[[plan.schedule]]\nweekday = 0\nworkout = "A"\n'
            '[[plan.schedule]]\nweekday = 3\nworkout = "A"\n'
            '[[plan.workout]]\nkey = "A"\ntitle = "A"\n'
            '[[plan.workout.exercise]]\nexercise = "barbell_deadlift"\nsets = 3\n'
            "reps = [5, 8]\nkg = 100\n"
        )
    )

    report = await run_import(db, text)

    assert [plan.name for plan in report.plans_saved] == ["Deadlift day"]
    async with db.read() as conn:
        versions = await list_plan_versions(conn, report.plans_saved[0].plan_id)
    prescription = Plan.model_validate(versions[0].body).workouts[0].blocks[0].items[0]
    assert prescription.load.kind == "calibration"
    assert prescription.declared_kg == 100.0


async def test_plans_are_skipped_without_a_complete_setup(db: Database, user_id: int) -> None:
    text = toml_with(s=session_toml("2026-08-01", ("Squat", 60, 5)), p=plan_toml("Old", 60.0))

    report = await run_import(db, text)

    assert report.sessions_new == 1
    assert report.plans_saved == ()
    assert len(report.rejected) == 1
    assert "complete setup" in report.rejected[0].reason


async def test_export_includes_and_delete_removes_the_imported_rows(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    await run_import(db, fixture_text())

    exported = await export_user(db, user_id)
    imported_sessions = [row for row in exported["workout_sessions"] if row["import_hash"]]
    assert len(imported_sessions) == 3
    assert sum(1 for row in exported["set_logs"] if row["source"] == "import") == 18
    assert any(row["kind"] == "history_import" for row in exported["decisions"])

    await delete_user(db, user_id)

    async with db.read() as conn:
        assert await list_workout_sessions_for_user(conn, user_id) == []
        assert await historical_max_by_exercise(conn, user_id) == {}


async def test_json_is_accepted_with_the_same_structure(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)

    def plain(value: object) -> object:
        if isinstance(value, dict):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [plain(item) for item in value]
        if isinstance(value, datetime | date):
            return value.isoformat()
        return value

    document = plain(tomllib.loads(fixture_text()))
    report = await run_import(db, json.dumps(document), fmt="json")

    assert report.sessions_new == 3
    assert [plan.name for plan in report.plans_saved] == ["Old two-day"]
    # The same content in either syntax hashes to the same sessions.
    again = await run_import(db, fixture_text())
    assert again.sessions_duplicate == 3


async def test_engine_reads_chronology_not_insert_order(db: Database, user_id: int) -> None:
    """An older, heavier history imported *after* a real session must not become "the last
    session": the engine progresses from the real 60 kg prescription, while the ceiling's
    historical max does see the imported 100 kg."""
    await seed_gym_profile(db, user_id)
    version_id = await seed_plan_version(db, user_id)
    # Import a session first, then log a real session today.
    await run_import(db, toml_with(s=session_toml("2025-01-10", ("Squat", 100, 5))))
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=version_id, workout_key="A", status="in_progress"
        )
        for index in (1, 2, 3):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=_SQUAT,
                set_index=index,
                planned_load_kg=60.0,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=60.0,
                actual_reps=8,
                rpe=None,
                source="button",
            )
        await finish_workout_session(conn, session_id, status="completed")
    # Now import an even older session, written last.
    await run_import(db, toml_with(s=session_toml("2024-06-01", ("Squat", 90, 5))))

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, _SQUAT, limit=3)
        history_max = await historical_max_by_exercise(conn, user_id)
    assert [outcome.planned_load_kg for outcome in outcomes] == [60.0, 100.0, 90.0]
    assert history_max[_SQUAT] == 100.0
    squat = load_catalog().by_id(_SQUAT)
    assert squat is not None
    decision = await next_load_for_exercise(
        db,
        user_id=user_id,
        exercise=squat,
        checkins={},
        flagged_areas=frozenset(),
        cap_kg=2.5,
        since_7d=_EPOCH,
    )
    assert decision.load.kg == 62.5  # from the real 60, not from the imported 100


# --- Errors ---------------------------------------------------------------------------------------


async def test_malformed_file_imports_nothing(db: Database, user_id: int) -> None:
    for text, message in (
        ("[meta\nversion = 1", "not valid TOML"),
        ('[aliases]\n"x" = "barbell_back_squat"', "[meta] is required"),
        ("[meta]\nversion = 2", "version must be 1"),
        ('[meta]\nversion = 1\ntimezone = "Mars/Olympus"', "not a known IANA name"),
        ('[meta]\nversion = 1\n[aliases]\n"x" = "not_an_exercise"', "not a catalog exercise id"),
        ("[meta]\nversion = 1\n[[sessions]]\ndate = 2026-01-01", "unknown key(s) sessions"),
    ):
        with pytest.raises(ImportFormatError, match=re.escape(message)):
            await run_import(db, text)
    assert await _counts(db, user_id) == {"sessions": 0, "set_logs": 0, "plans": 0, "decisions": 0}


async def test_import_before_activation_raises(db: Database) -> None:
    with pytest.raises(NoUserError):
        await run_import(db, fixture_text())


# --- Coordinator additions: recap/check-in staleness and the report lines ---------------------


async def _app_logged_session_with_start_and_checkin(db: Database, user_id: int) -> tuple[int, int]:
    """A completed session run through the loop (it has a `start` decision) with one open
    check-in, so a pending recap exists and the check-in can be answered."""
    from fitme.db.controllers.training import insert_checkin
    from fitme.domain.enums import DecisionKind
    from fitme.services.training import EVENT_START

    await run_import(db, toml_with(s=session_toml("2025-01-10", ("Squat", 60, 5))))
    version_id = await seed_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version_id,
            workout_key="A",
            status="in_progress",
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id=_SQUAT,
            set_index=1,
            planned_load_kg=60.0,
            planned_reps_min=5,
            planned_reps_max=8,
            actual_load_kg=60.0,
            actual_reps=8,
            rpe=None,
            source="button",
        )
        await finish_workout_session(conn, session_id, status="completed")
        await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.SESSION_ADJUST.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report={"session_id": session_id, "event": EVENT_START},
            proposal=None,
            guards_fired=[],
        )
        checkin_id = await insert_checkin(
            conn, user_id=user_id, session_id=session_id, question_key="area:knee"
        )
    return session_id, checkin_id


async def test_import_after_a_real_session_keeps_its_pending_recap(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    session_id, _ = await _app_logged_session_with_start_and_checkin(db, user_id)
    assert await recap.pending_recap_session(db, user_id) == session_id

    # Imported afterwards (higher ids), dated both before and after the real session.
    yesterday = (clock.now() - timedelta(days=1)).date().isoformat()
    report = await run_import(
        db,
        toml_with(
            a=session_toml("2024-06-01", ("Squat", 50, 5)),
            b=session_toml(yesterday, ("Squat", 55, 5)),
        ),
    )
    assert report.sessions_new == 2

    assert await recap.pending_recap_session(db, user_id) == session_id


async def test_import_after_a_real_session_does_not_make_its_checkin_stale(
    db: Database, user_id: int
) -> None:
    from fitme.domain.enums import CheckinAnswer
    from fitme.services.training import Status

    await seed_gym_profile(db, user_id)
    _, checkin_id = await _app_logged_session_with_start_and_checkin(db, user_id)
    yesterday = (clock.now() - timedelta(days=1)).date().isoformat()
    await run_import(db, toml_with(b=session_toml(yesterday, ("Squat", 55, 5))))

    result = await recap.answer_checkin(db, user_id, checkin_id, CheckinAnswer.FINE)

    assert result.status == Status.OK
    async with db.read() as conn:
        from fitme.db.selectors.training import get_checkin

        checkin = await get_checkin(conn, checkin_id)
    assert checkin is not None and checkin.answer == "fine"


async def test_report_shows_per_implement_max_and_newer_than_last_logged(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)

    dry = await run_import(db, fixture_text(), dry_run=True)
    assert dry.per_implement_max == (("dumbbell_bench_press", 20.0),)
    assert dry.newer_than_last_logged == 3  # nothing logged through the app yet: all of them

    real = await run_import(db, fixture_text())
    assert real.per_implement_max == (("dumbbell_bench_press", 20.0),)
    assert real.newer_than_last_logged == 3

    # A session logged through the app today: the August fixture sessions are all older.
    version_id = await seed_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version_id,
            workout_key="A",
            status="in_progress",
        )
        await finish_workout_session(conn, session_id, status="completed")
    later = await run_import(
        db, toml_with(a=session_toml("2026-08-20", ("DB bench", 22.5, 8), ("Squat", 70, 5)))
    )
    assert later.sessions_new == 1
    assert later.per_implement_max == (("dumbbell_bench_press", 22.5),)
    assert later.newer_than_last_logged == 0


async def test_imported_sessions_never_trigger_a_decrease(db: Database, user_id: int) -> None:
    """Two imported sessions "below reps_min" cannot exist (planned reps = actual reps), and
    even a skipped imported set is neither a success nor a failure: the engine holds."""
    await seed_gym_profile(db, user_id)
    text = toml_with(
        a=(
            "[[session]]\ndate = 2026-08-01\n"
            '[[session.set]]\nexercise = "Squat"\nkg = 70\nreps = 5\nskipped = true\n'
        ),
        b=(
            "[[session]]\ndate = 2026-08-04\n"
            '[[session.set]]\nexercise = "Squat"\nkg = 70\nreps = 5\nskipped = true\n'
        ),
    )
    report = await run_import(db, text)
    assert report.sessions_new == 2

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, _SQUAT)
    assert [(o.hit_reps_max, o.below_reps_min, o.planned_load_kg) for o in outcomes] == [
        (False, False, 70.0),
        (False, False, 70.0),
    ]
