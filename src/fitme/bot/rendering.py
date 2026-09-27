"""Pure text/keyboard builders for the setup questionnaire and `/profile` (A§5.1, A§6.2).

Nothing here touches the database: callers (`bot/handlers/setup.py`) pass in whatever
already-loaded state a step needs (the current multi-select selection, the profile snapshot
for the summary). Keeping this pure makes it trivial to unit-test the choice lists and label
lookups without a database or a mocked bot.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fitme.bot.callback_data import ProfileFix, SetupChoice, SetupNav, SetupToggle
from fitme.bot.keyboards import back_row, with_extra_rows
from fitme.db.records import ProfileRecord, ScreeningFlagRecord, ScreeningNoteRecord
from fitme.domain.catalog import HOME_SELECTABLE_EQUIPMENT
from fitme.domain.enums import (
    AREA_FLAGS,
    RED_FLAGS,
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
from fitme.i18n import supported_languages, t

# A shared, language-independent list of common IANA zones (A§5.1 step 2). Kept small and the
# same for every interface language, rather than a per-language curated list, to keep the
# maintenance surface (and the risk of the two lists drifting) down; "type it" always covers
# anything not on the list.
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Moscow",
    "Europe/Kyiv",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Dubai",
    "Asia/Almaty",
    "Asia/Singapore",
)

_LANGUAGE_LABELS: dict[str, str] = {"en": "English", "ru": "Русский"}

FREQUENCY_CHOICES: tuple[str, ...] = tuple(str(n) for n in range(1, 7))
SESSION_LENGTH_CHOICES: tuple[str, ...] = ("30", "45", "60", "90")

_AREA_FLAG_ORDER: tuple[ScreeningFlag, ...] = tuple(f for f in ScreeningFlag if f in AREA_FLAGS)
AREA_CHOICES: tuple[str, ...] = tuple(f.value for f in _AREA_FLAG_ORDER)
_HOME_EQUIPMENT_ORDER: tuple[Equipment, ...] = tuple(
    e for e in Equipment if e in HOME_SELECTABLE_EQUIPMENT
)
EQUIPMENT_CHOICES: tuple[str, ...] = tuple(e.value for e in _HOME_EQUIPMENT_ORDER)

# Steps whose choices come straight from an `[enum.X]` locale table keyed by the enum value.
_ENUM_TABLE_STEPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "age": ("age_bucket", tuple(v.value for v in AgeBucket)),
    "weight": ("weight_bucket", tuple(v.value for v in WeightBucket)),
    "experience_duration": ("experience", tuple(v.value for v in Experience)),
    "experience_barbell": ("barbell_experience", tuple(v.value for v in BarbellExperience)),
    "preferences": ("preference", tuple(v.value for v in Preference)),
    "focus": ("focus", tuple(v.value for v in Focus)),
    "location": ("location", tuple(v.value for v in Location)),
    "equipment": ("equipment", EQUIPMENT_CHOICES),
    "screening_areas": ("screening_area_flag", AREA_CHOICES),
}

MULTI_SELECT_STEPS: frozenset[str] = frozenset({"preferences", "equipment", "screening_areas"})
_GRID_WIDTH: dict[str, int] = {"weight": 3, "equipment": 2, "preferences": 2}


def choice_label(step: str, value: str, lang: str) -> str:
    if step == "language":
        return _LANGUAGE_LABELS.get(value, value)
    if step == "timezone":
        return value
    if step == "frequency":
        return t("profile.frequency_value", lang, n=value)
    if step == "session_length":
        return t("setup.session_length.minutes_suffix", lang, n=value)
    table, _values = _ENUM_TABLE_STEPS[step]
    return t(f"enum.{table}.{value}", lang)


def choices_for(step: str) -> tuple[str, ...]:
    if step == "language":
        return supported_languages()
    if step == "timezone":
        return COMMON_TIMEZONES
    if step == "frequency":
        return FREQUENCY_CHOICES
    if step == "session_length":
        return SESSION_LENGTH_CHOICES
    return _ENUM_TABLE_STEPS[step][1]


def _choice_button(
    step: str, value: str, lang: str, *, selected: bool = False
) -> InlineKeyboardButton:
    label = choice_label(step, value, lang)
    text = f"✅ {label}" if selected else label
    packed = (
        SetupToggle(step=step, value=value).pack()
        if step in MULTI_SELECT_STEPS
        else SetupChoice(step=step, value=value).pack()
    )
    return InlineKeyboardButton(text=text, callback_data=packed)


def single_choice_markup(
    step: str, lang: str, *, has_back: bool, current: str | None = None
) -> InlineKeyboardMarkup:
    width = _GRID_WIDTH.get(step, 1)
    builder = InlineKeyboardBuilder()
    for value in choices_for(step):
        builder.add(_choice_button(step, value, lang, selected=value == current))
    builder.adjust(width)
    markup = builder.as_markup()
    return with_extra_rows(markup, back_row(step, lang, has_back=has_back))


def multi_select_markup(
    step: str, lang: str, *, selected: Sequence[str], has_back: bool, extra_none_button: bool
) -> InlineKeyboardMarkup:
    width = _GRID_WIDTH.get(step, 1)
    builder = InlineKeyboardBuilder()
    for value in choices_for(step):
        builder.add(_choice_button(step, value, lang, selected=value in selected))
    builder.adjust(width)
    markup = builder.as_markup()
    extra_rows: list[list[InlineKeyboardButton]] = []
    if extra_none_button:
        extra_rows.append(
            [
                InlineKeyboardButton(
                    text=t("setup.screening.areas_none_option", lang),
                    callback_data=SetupNav(step=step, action="next").pack(),
                )
            ]
        )
    extra_rows.append(
        [
            InlineKeyboardButton(
                text=t("setup.common.next_button", lang),
                callback_data=SetupNav(step=step, action="next").pack(),
            )
        ]
    )
    extra_rows.append(back_row(step, lang, has_back=has_back))
    return with_extra_rows(markup, *[row for row in extra_rows if row])


def yes_no_markup(step: str, lang: str, *, has_back: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("setup.common.yes_button", lang),
            callback_data=SetupChoice(step=step, value="yes").pack(),
        ),
        InlineKeyboardButton(
            text=t("setup.common.no_button", lang),
            callback_data=SetupChoice(step=step, value="no").pack(),
        ),
    )
    builder.adjust(2)
    markup = builder.as_markup()
    return with_extra_rows(markup, back_row(step, lang, has_back=has_back))


def timezone_markup(lang: str, *, has_back: bool) -> InlineKeyboardMarkup:
    markup = single_choice_markup("timezone", lang, has_back=False)
    manual_row = [
        InlineKeyboardButton(
            text=t("setup.timezone.manual_button", lang),
            callback_data=SetupNav(step="timezone", action="manual").pack(),
        )
    ]
    return with_extra_rows(markup, manual_row, back_row("timezone", lang, has_back=has_back))


def free_text_markup(
    step: str, lang: str, *, has_back: bool, skip_button_key: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t(skip_button_key, lang),
            callback_data=SetupNav(step=step, action="next").pack(),
        )
    )
    markup = builder.as_markup()
    return with_extra_rows(markup, back_row(step, lang, has_back=has_back))


_PROMPT_KEYS: dict[str, str] = {
    "language": "setup.language.prompt",
    "timezone": "setup.timezone.prompt",
    "age": "setup.age.prompt",
    "weight": "setup.weight.prompt",
    "experience_duration": "setup.experience.duration_prompt",
    "experience_barbell": "setup.experience.barbell_prompt",
    "screening_areas": "setup.screening.areas_prompt",
    "screening_other": "setup.screening.other_prompt",
    "screening_clearance": "setup.screening.clearance_prompt",
    "preferences": "setup.preferences.prompt",
    "focus": "setup.focus.prompt",
    "location": "setup.location.prompt",
    "equipment": "setup.equipment.prompt",
    "frequency": "setup.frequency.prompt",
    "session_length": "setup.session_length.prompt",
}


def prompt_text(step: str, lang: str) -> str:
    if step in _PROMPT_KEYS:
        return t(_PROMPT_KEYS[step], lang)
    flag = ScreeningFlag(step.removeprefix("screening_"))
    return t(f"enum.screening_red_flag.{flag.value}", lang)


# --- Profile / summary rendering -------------------------------------------------------------

_FIELD_ORDER: tuple[str, ...] = (
    "language",
    "timezone",
    "age",
    "weight",
    "experience",
    "barbell_experience",
    "screening",
    "preferences",
    "focus",
    "location",
    "equipment",
    "frequency",
    "session_length",
)


def _field_value_text(
    field: str,
    lang: str,
    *,
    language: str,
    timezone: str | None,
    profile: ProfileRecord | None,
    flags: Sequence[ScreeningFlagRecord],
) -> str:
    not_set = t("profile.not_set", lang)
    if field == "language":
        return _LANGUAGE_LABELS.get(language, language)
    if field == "timezone":
        return timezone or not_set
    if profile is None:
        return not_set
    if field == "age":
        return choice_label("age", profile.age_bucket, lang) if profile.age_bucket else not_set
    if field == "weight":
        return (
            choice_label("weight", profile.weight_bucket, lang)
            if profile.weight_bucket
            else not_set
        )
    if field == "experience":
        return (
            choice_label("experience_duration", profile.experience, lang)
            if profile.experience
            else not_set
        )
    if field == "barbell_experience":
        return (
            choice_label("experience_barbell", profile.barbell_experience, lang)
            if profile.barbell_experience
            else not_set
        )
    if field == "screening":
        answered = sum(1 for f in flags if f.flag in {rf.value for rf in RED_FLAGS})
        return t("profile.screening_status", lang, answered=answered, total=len(RED_FLAGS))
    if field == "preferences":
        values = profile.preferences
        return (
            ", ".join(choice_label("preferences", v, lang) for v in values) if values else not_set
        )
    if field == "focus":
        return choice_label("focus", profile.focus, lang) if profile.focus else not_set
    if field == "location":
        return choice_label("location", profile.location, lang) if profile.location else not_set
    if field == "equipment":
        values = profile.equipment
        return ", ".join(choice_label("equipment", v, lang) for v in values) if values else not_set
    if field == "frequency":
        return (
            t("profile.frequency_value", lang, n=profile.sessions_per_week)
            if profile.sessions_per_week
            else not_set
        )
    if field == "session_length":
        return (
            t("setup.session_length.minutes_suffix", lang, n=profile.session_minutes)
            if profile.session_minutes
            else not_set
        )
    raise ValueError(f"unknown profile field {field!r}")  # pragma: no cover - defensive


def render_summary_text(
    lang: str,
    *,
    language: str,
    timezone: str | None,
    profile: ProfileRecord | None,
    flags: Sequence[ScreeningFlagRecord],
    notes: Sequence[ScreeningNoteRecord],
    title_key: str,
) -> str:
    lines = [t(title_key, lang), ""]
    for field in _FIELD_ORDER:
        label = t(f"profile.field.{field}", lang)
        value = _field_value_text(
            field, lang, language=language, timezone=timezone, profile=profile, flags=flags
        )
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def fix_buttons_markup(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for field in _FIELD_ORDER:
        builder.add(
            InlineKeyboardButton(
                text=t(f"profile.field.{field}", lang),
                callback_data=ProfileFix(field=field).pack(),
            )
        )
    builder.adjust(2)
    return builder.as_markup()


def summary_markup(lang: str, *, confirm_button_key: str) -> InlineKeyboardMarkup:
    fix = fix_buttons_markup(lang)
    confirm_row = [
        InlineKeyboardButton(
            text=t(confirm_button_key, lang),
            callback_data=SetupNav(step="summary", action="next").pack(),
        )
    ]
    return with_extra_rows(fix, confirm_row, back_row("summary", lang, has_back=True))
