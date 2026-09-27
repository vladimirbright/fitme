"""`llm/context.py` (A§8.2): `scrub()` and the pseudonymized `UserContext` builder.

`test_build_user_context_never_carries_forbidden_values` is *the* A§8.2 pseudonymization
test: it builds a context from a fixture standing in for what a real caller would have on
hand — including `telegram_user_id`, `chat_id`, a display name, and `screening_notes` text —
and asserts none of those values appear anywhere in the rendered (JSON) prompt content.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fitme.domain.enums import (
    AgeBucket,
    BarbellExperience,
    Equipment,
    Experience,
    Focus,
    Location,
    Preference,
    ScreeningFlag,
    WeightBucket,
)
from fitme.llm.context import (
    ExerciseHistorySummary,
    FlagSummary,
    UserContext,
    build_user_context,
    scrub,
)


class TestScrub:
    def test_strips_an_at_handle(self) -> None:
        assert scrub("ping @johndoe123 about this") == "ping [redacted] about this"

    def test_strips_an_email(self) -> None:
        assert scrub("reach me at john.doe@example.com please") == ("reach me at [redacted] please")

    def test_strips_a_url(self) -> None:
        assert scrub("see https://example.com/path?x=1 for details") == "see [redacted] for details"
        assert scrub("see www.example.com for details") == "see [redacted] for details"

    def test_strips_a_phone_number(self) -> None:
        assert scrub("call me at +1 415-555-0132 tonight") == "call me at [redacted] tonight"
        assert scrub("call 4155550132 tonight") == "call [redacted] tonight"

    def test_leaves_ordinary_training_text_alone(self) -> None:
        text = "did 3x8 at 42.5 kg, last set felt heavy"
        assert scrub(text) == text

    def test_does_not_flag_short_numbers_like_sets_and_reps(self) -> None:
        text = "3x8 at 42.5, then 5x5"
        assert scrub(text) == text

    def test_handles_all_four_categories_together(self) -> None:
        text = "hey @coachbot email me john@example.com or visit https://x.io or call 4155550132"
        result = scrub(text)
        assert "@coachbot" not in result
        assert "john@example.com" not in result
        assert "https://x.io" not in result
        assert "4155550132" not in result

    # --- B1: links/handles/emails scrub() previously missed ---

    def test_strips_a_bare_telegram_link(self) -> None:
        assert scrub("t.me/ivan_petrov") == "[redacted]"
        assert scrub("telegram.me/ivan_petrov") == "[redacted]"

    def test_strips_a_telegram_deep_link(self) -> None:
        assert scrub("tg://resolve?domain=ivan_petrov") == "[redacted]"

    def test_strips_a_scheme_less_social_link(self) -> None:
        assert scrub("vk.com/id12345") == "[redacted]"
        assert scrub("instagram.com/ivan") == "[redacted]"

    def test_strips_a_scheme_less_bare_domain_with_a_path(self) -> None:
        assert scrub("ivanov.ru/profile") == "[redacted]"

    def test_strips_a_cyrillic_email(self) -> None:
        assert scrub("иван@почта.рф") == "[redacted]"

    def test_strips_a_cyrillic_handle(self) -> None:
        assert scrub("@иван") == "[redacted]"

    # --- B1: false positives that must not happen ---

    def test_does_not_strip_a_long_rep_scheme(self) -> None:
        assert scrub("12 12 10 10 8 8 6 6") == "12 12 10 10 8 8 6 6"

    def test_does_not_strip_a_repeated_set_count(self) -> None:
        assert scrub("sets 5 5 5 5 5 5 5 5") == "sets 5 5 5 5 5 5 5 5"

    def test_does_not_strip_a_counting_sequence(self) -> None:
        assert scrub("1 2 3 4 5 6 7 8") == "1 2 3 4 5 6 7 8"

    def test_does_not_strip_an_iso_date(self) -> None:
        assert scrub("2024-05-01 session") == "2024-05-01 session"

    def test_does_not_strip_a_comma_separated_result_with_a_decimal_load(self) -> None:
        assert scrub("8,8,6 at 42.5") == "8,8,6 at 42.5"

    def test_does_not_strip_an_at_sign_used_for_a_working_load(self) -> None:
        assert scrub("3x10 @ 60") == "3x10 @ 60"

    # --- Russian/parenthesized phone shapes (must redact) ---

    def test_strips_a_prefixed_parenthesized_ru_phone(self) -> None:
        assert scrub("8 (916) 123-45-67") == "[redacted]"

    def test_strips_a_parenthesized_ru_phone_with_no_prefix(self) -> None:
        assert scrub("(916) 123-45-67") == "[redacted]"

    def test_strips_a_hyphenated_ru_phone(self) -> None:
        assert scrub("8-916-123-45-67") == "[redacted]"

    def test_strips_a_space_separated_ru_phone_with_no_prefix(self) -> None:
        assert scrub("916 123 45 67") == "[redacted]"

    def test_strips_an_international_phone_with_spaces(self) -> None:
        assert scrub("+7 916 1234567") == "[redacted]"
        assert scrub("+44 20 7946 0958") == "[redacted]"

    def test_strips_a_bare_digit_run_phone(self) -> None:
        assert scrub("89161234567") == "[redacted]"

    def test_strips_a_hyphenated_international_phone(self) -> None:
        assert scrub("+1-415-555-0132") == "[redacted]"

    # --- Load ramps and rep sequences must survive (the phone rework must not catch these) ---

    def test_does_not_strip_a_load_ramp_of_three_digit_numbers(self) -> None:
        assert scrub("100 100 100 kg") == "100 100 100 kg"
        assert scrub("squat 140 150 160 170") == "squat 140 150 160 170"

    def test_does_not_strip_a_hyphenated_load_ramp(self) -> None:
        assert scrub("warmup 20-40-60-80 then 100x5") == "warmup 20-40-60-80 then 100x5"

    def test_does_not_strip_a_comma_separated_rep_list(self) -> None:
        assert scrub("3 sets: 10, 10, 8 @ 42.5kg") == "3 sets: 10, 10, 8 @ 42.5kg"

    def test_does_not_strip_russian_set_by_rep_by_weight_notation(self) -> None:
        assert scrub("жим 60х8х3") == "жим 60х8х3"
        assert scrub("жим 102,5 кг на 3") == "жим 102,5 кг на 3"

    # --- Bare domains: known TLD (path optional), or any TLD-shaped suffix with a path ---

    def test_strips_a_cyrillic_tld_domain_with_no_path(self) -> None:
        assert scrub("иванов.рф") == "[redacted]"

    def test_does_not_strip_ordinary_punctuation_that_looks_like_a_domain(self) -> None:
        assert scrub("did 3x10.Next time more") == "did 3x10.Next time more"
        assert scrub("rest.Then squats") == "rest.Then squats"
        assert scrub("ok.done") == "ok.done"
        assert scrub("Mr.Olympia style") == "Mr.Olympia style"


_RED_FLAGS = (
    ScreeningFlag.HEART_CONDITION,
    ScreeningFlag.CHEST_DISCOMFORT,
    ScreeningFlag.DIZZINESS_FAINTING,
    ScreeningFlag.HIGH_BLOOD_PRESSURE,
    ScreeningFlag.RECENT_SURGERY,
    ScreeningFlag.PREGNANT,
    ScreeningFlag.OTHER_CONDITION_LIMITS,
)


def _sample_flags() -> list[FlagSummary]:
    return [FlagSummary(flag=flag, value="no") for flag in _RED_FLAGS]


def _sample_context() -> UserContext:
    return build_user_context(
        user_id=47,
        language="en",
        age_bucket=AgeBucket.AGE_30_39,
        weight_bucket=WeightBucket.KG_80_89,
        experience=Experience.M6_TO_2Y,
        barbell_experience=BarbellExperience.SOME,
        preferences=[Preference.WEIGHT_TRAINING, Preference.FULL_BODY],
        focus=Focus.STRENGTH,
        location=Location.PUBLIC_GYM,
        equipment=[Equipment.BARBELL, Equipment.RACK],
        sessions_per_week=3,
        session_minutes=60,
        flags=_sample_flags(),
        allowed_exercise_ids=["barbell_back_squat", "barbell_bench_press"],
        history=[
            ExerciseHistorySummary(
                exercise_id="barbell_back_squat",
                last_working_load_kg=80.0,
                last_reps=5,
                historical_max_kg=82.5,
            )
        ],
    )


async def test_the_actual_model_messages_never_carry_forbidden_values() -> None:
    """A§8.2 / B2: this is *the* pseudonymization test. It doesn't just inspect the rendered
    JSON in isolation — it runs a real agent (`plan_revise_agent`) against a `FunctionModel`
    and captures the literal text pydantic-ai sends as the user prompt, standing in for what
    would go out over the wire to a real provider.

    `telegram_user_id`, `chat_id`, a display name and `screening_notes` are never passed to
    `build_user_context`/`render_user_prompt` at all — there's no parameter for any of them
    (structurally impossible; `test_user_context_structurally_cannot_carry_a_telegram_id`
    below covers the model-level half of that guarantee) — so a fixture standing in for a
    real caller's on-hand data (all four, plus a phone number and a `t.me` link) never even
    reaches this function. What real user-typed free text *can* carry is embedded in
    `request` instead: a phone number and a `t.me` link, which `scrub()` must actually catch
    on the way out. The test also asserts the stored `llm_input` (`rendered.payload`) equals
    exactly what was sent — B2's "one rendering path" guarantee.
    """
    import contextlib

    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from fitme.llm.agents import plan_revise_agent
    from fitme.llm.context import render_user_prompt

    # Stands in for what a real caller (a service, later milestones) has on hand from
    # `telegram_accounts`/`users`/`screening_notes` — deliberately never passed to
    # `build_user_context` or `render_user_prompt` below; there's no parameter for any of it.
    forbidden_telegram_user_id = 918273645
    forbidden_chat_id = -100123456789
    forbidden_name = "Zbigniew Wozniakowski-Fitzgerald"
    forbidden_notes = "afraid of needles, prefers morning sessions"

    # What scrub() is actually specified to catch (B1): a phone number and a bare social
    # link, both embedded in realistic free text the user actually typed.
    forbidden_phone = "415-555-0132"
    forbidden_link = "t.me/zbyszek_w"
    request_text = (
        f"call me at {forbidden_phone} or message {forbidden_link} — "
        "please drop the lunges from my plan."
    )

    context = _sample_context()
    rendered = render_user_prompt(context, request=request_text)

    captured: dict[str, str | None] = {}

    def capture(messages: list[ModelRequest], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if part.part_kind == "user-prompt":
                captured["user_prompt"] = str(part.content)
        return ModelResponse(parts=[TextPart(content="{}")])

    built = plan_revise_agent(FunctionModel(capture))
    with contextlib.suppress(Exception):
        await built.agent.run(rendered.text)

    sent = captured.get("user_prompt")
    assert sent is not None
    assert sent == rendered.text

    for forbidden in (
        str(forbidden_telegram_user_id),
        str(forbidden_chat_id),
        forbidden_name,
        forbidden_notes,
        forbidden_phone,
        forbidden_link,
    ):
        assert forbidden not in sent, forbidden
    assert "telegram_user_id" not in sent.lower()
    assert "chat_id" not in sent.lower()
    assert "screening_notes" not in sent.lower()

    # The stored llm_input (what a caller would write to decisions.llm_input) equals exactly
    # what was sent — B2's "one rendering path" guarantee.
    assert json.loads(sent) == rendered.payload

    # Positive check: the pseudonymized fields that *are* allowed made it through.
    assert rendered.payload["context"]["user_id"] == 47
    assert "barbell_back_squat" in sent


def test_user_context_structurally_cannot_carry_a_telegram_id() -> None:
    """`UserContext`'s `extra="forbid"` config means even a caller that tries to smuggle a
    forbidden key in via `model_validate` (not through `build_user_context`, which has no
    parameter for it at all) gets a validation error instead of a silently-accepted key."""
    base = build_user_context(
        user_id=1,
        language="en",
        age_bucket=AgeBucket.AGE_30_39,
        weight_bucket=WeightBucket.KG_80_89,
        experience=Experience.NONE,
        barbell_experience=BarbellExperience.NO,
        preferences=[],
        focus=Focus.GENERAL_FITNESS,
        location=Location.OUTDOOR,
        equipment=[],
        sessions_per_week=2,
        session_minutes=30,
        flags=_sample_flags(),
        allowed_exercise_ids=[],
    ).model_dump(mode="json")
    base["telegram_user_id"] = 12345

    with pytest.raises(ValidationError):
        UserContext.model_validate(base)


def test_build_user_context_accepts_a_mapping_or_a_sequence_for_history() -> None:
    entry = ExerciseHistorySummary(exercise_id="plank", historical_max_kg=None)
    from_sequence = build_user_context(
        user_id=1,
        language="en",
        age_bucket=AgeBucket.AGE_30_39,
        weight_bucket=WeightBucket.KG_80_89,
        experience=Experience.NONE,
        barbell_experience=BarbellExperience.NO,
        preferences=[],
        focus=Focus.GENERAL_FITNESS,
        location=Location.OUTDOOR,
        equipment=[],
        sessions_per_week=2,
        session_minutes=30,
        flags=_sample_flags(),
        allowed_exercise_ids=["plank"],
        history=[entry],
    )
    from_mapping = build_user_context(
        user_id=1,
        language="en",
        age_bucket=AgeBucket.AGE_30_39,
        weight_bucket=WeightBucket.KG_80_89,
        experience=Experience.NONE,
        barbell_experience=BarbellExperience.NO,
        preferences=[],
        focus=Focus.GENERAL_FITNESS,
        location=Location.OUTDOOR,
        equipment=[],
        sessions_per_week=2,
        session_minutes=30,
        flags=_sample_flags(),
        allowed_exercise_ids=["plank"],
        history={"plank": entry},
    )
    assert from_sequence.history == from_mapping.history == [entry]


def test_no_history_for_an_exercise_is_a_normal_empty_list() -> None:
    context = build_user_context(
        user_id=1,
        language="en",
        age_bucket=AgeBucket.AGE_30_39,
        weight_bucket=WeightBucket.KG_80_89,
        experience=Experience.NONE,
        barbell_experience=BarbellExperience.NO,
        preferences=[],
        focus=Focus.GENERAL_FITNESS,
        location=Location.OUTDOOR,
        equipment=[],
        sessions_per_week=2,
        session_minutes=30,
        flags=_sample_flags(),
        allowed_exercise_ids=[],
    )
    assert context.history == []


def test_render_user_prompt_with_full_context_includes_allowed_ids_and_history() -> None:
    from fitme.llm.context import render_user_prompt

    context = _sample_context()
    rendered = render_user_prompt(context)
    assert "allowed_exercise_ids" in rendered.payload["context"]
    assert "history" in rendered.payload["context"]
    assert "language" not in rendered.payload  # nested inside "context", not top-level


def test_render_user_prompt_for_result_parse_omits_allowed_ids_and_history() -> None:
    """A§8.2 per-agent minimization: `result_parse` gets only the planned block, the result
    text and the language — never the allowed-ids list or history, because its caller never
    passes a `context` at all."""
    from fitme.domain.models import Block, Load, Prescription
    from fitme.llm.context import render_user_prompt

    block = Block(
        kind="single",
        items=[
            Prescription(
                exercise_id="barbell_back_squat",
                sets=3,
                reps_min=5,
                reps_max=8,
                load=Load(kind="kg", kg=60.0),
                rest_seconds=120,
            )
        ],
    )
    rendered = render_user_prompt(language="en", planned_block=block, result_text="did 8,8,6")

    assert rendered.payload == {
        "language": "en",
        "planned_block": block.model_dump(mode="json"),
        "result_text": "did 8,8,6",
    }
    assert "context" not in rendered.payload
    assert "allowed_exercise_ids" not in json.dumps(rendered.payload)
    assert "history" not in json.dumps(rendered.payload)


def test_render_user_prompt_requires_context_or_language() -> None:
    from fitme.llm.context import render_user_prompt

    with pytest.raises(ValueError, match="context or an explicit language"):
        render_user_prompt()


def test_render_user_prompt_carries_guard_feedback_only_when_given() -> None:
    from fitme.llm.context import render_user_prompt

    context = _sample_context()
    first = render_user_prompt(context)
    assert "guard_feedback" not in first.payload
    retry = render_user_prompt(
        context, guard_feedback=["unicorn_press is not a catalog exercise", "schedule has 1 day(s)"]
    )
    assert retry.payload["guard_feedback"] == [
        "unicorn_press is not a catalog exercise",
        "schedule has 1 day(s)",
    ]
    assert json.loads(retry.text) == retry.payload


def test_render_user_prompt_carries_load_units_for_result_parse() -> None:
    """A§4.4: `result_parse` is told what each planned exercise's kg means."""
    from fitme.domain.models import Block, Load, Prescription
    from fitme.llm.context import render_user_prompt

    block = Block(
        kind="single",
        items=[
            Prescription(
                exercise_id="dumbbell_bench_press",
                sets=3,
                reps_min=8,
                reps_max=12,
                load=Load(kind="kg", kg=12.5),
                rest_seconds=90,
            )
        ],
    )
    rendered = render_user_prompt(
        language="en",
        planned_block=block,
        result_text="12,12,10",
        load_units={"dumbbell_bench_press": "per_implement"},
    )
    assert rendered.payload["load_units"] == {"dumbbell_bench_press": "per_implement"}
    assert json.loads(rendered.text) == rendered.payload
    assert "load_units" not in render_user_prompt(language="en", planned_block=block).payload
