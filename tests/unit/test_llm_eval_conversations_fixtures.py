"""The conversation eval fixtures (`tests/fixtures/llm_eval/conversations/`, experiment:
llm-first planning) validate against the harness schema: profile references resolve,
weekdays are 0..6, `expect_as` names a turn, and every exercise id a check names is in the
catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fitme.catalog import load_catalog
from fitme.llm.eval_conversations import (
    ConversationFixture,
    Expect,
    load_conversation_fixtures,
    resolve_profile,
)

_DIR = Path(__file__).parents[1] / "fixtures" / "llm_eval" / "conversations"


def _expects(expect: Expect) -> list[Expect]:
    return [expect, *(nested for alt in expect.any_of or [] for nested in _expects(alt))]


def test_every_fixture_validates_and_resolves() -> None:
    paths = sorted(_DIR.glob("*.json"))
    fixtures = load_conversation_fixtures(_DIR)
    assert len(fixtures) == len(paths) >= 4
    assert [f.name for f in fixtures] == [path.stem for path in paths]
    catalog = load_catalog()
    for fixture in fixtures:
        resolve_profile(fixture, _DIR.parent)
        for entry in fixture.history:
            assert catalog.by_id(entry.exercise_id) is not None, entry.exercise_id
        expects = [turn.expect for turn in fixture.turns]
        steps = [fixture.legacy.request, *fixture.legacy.revisions]
        if fixture.legacy.save is not None:
            steps.append(fixture.legacy.save)  # type: ignore[arg-type]
        expects.extend(fixture.legacy_expect(step) for step in steps)
        for expect in (nested for top in expects for nested in _expects(top)):
            assert all(0 <= day <= 6 for day in expect.days or [])
            ids = [*(expect.exclude_ids or []), *(expect.include_ids_any or [])]
            ids.extend(expect.unchanged_except or [])
            if expect.swap_load is not None:
                ids.extend([expect.swap_load.from_id, expect.swap_load.to])
            for exercise_id in ids:
                assert catalog.by_id(exercise_id) is not None, (fixture.name, exercise_id)


def test_the_russian_scenarios_carry_the_owners_words() -> None:
    by_name = {f.name: f for f in load_conversation_fixtures(_DIR)}
    run = by_name["a_easy_run_saturdays"]
    assert resolve_profile(run, _DIR.parent).language == "ru"
    assert run.turns[0].user.startswith("Давай разок в неделю по субботам легкую беговую")
    assert by_name["e_bjj_twice"].turns[0].user == "bjj два раза в неделю"


def _raw() -> dict[str, object]:
    return json.loads((_DIR / "d_hour_run.json").read_text(encoding="utf-8"))


def test_the_schema_rejects_bad_fixtures(tmp_path: Path) -> None:
    raw = _raw()
    raw["turns"] = [{"user": "x", "expect": {"days": [7]}}]
    with pytest.raises(ValidationError, match="weekday 7"):
        ConversationFixture.model_validate(raw)

    raw = _raw()
    raw["turns"] = [{"user": "x", "expect": {"dayz": [1]}}]
    with pytest.raises(ValidationError):
        ConversationFixture.model_validate(raw)

    raw = _raw()
    raw["turns"] = [{"user": "x", "expect": {"cardio_minutes": [70, 50]}}]
    with pytest.raises(ValidationError, match="low bound is above the high bound"):
        ConversationFixture.model_validate(raw)

    raw = _raw()
    raw["legacy"] = {"request": {"user": "x", "expect_as": 3}}
    with pytest.raises(ValidationError, match="expect_as 3"):
        ConversationFixture.model_validate(raw)

    raw = _raw()
    raw["profile"] = "no_such_profile"
    fixture = ConversationFixture.model_validate(raw)
    with pytest.raises(ValueError, match="no profile fixture"):
        resolve_profile(fixture, _DIR.parent)

    raw = _raw()
    raw["profile_overrides"] = {"sessions_per_week": 9}
    with pytest.raises(ValidationError):
        resolve_profile(ConversationFixture.model_validate(raw), _DIR.parent)
