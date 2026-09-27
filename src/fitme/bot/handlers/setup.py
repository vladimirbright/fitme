"""The setup questionnaire (A§5.1) and `/profile` (A§6.2).

Every step's answer is written to the database as soon as it's known and
`setup_progress.step` is updated to match (A§5.1: "answers are saved step by step"), so a
restart mid-setup resumes exactly here — nothing here relies on aiogram FSM memory.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from fitme.bot import rendering
from fitme.bot.callback_data import ProfileFix, SetupChoice, SetupNav, SetupToggle
from fitme.db.connection import Database
from fitme.domain.enums import ScreeningFlag
from fitme.i18n import t
from fitme.services import profile as profile_service
from fitme.services.profile import ProfileSnapshot

_ChatTarget = CallbackQuery | Message


def _initial_selection(step: str, snapshot: ProfileSnapshot) -> list[str]:
    if step == "preferences":
        return list(snapshot.profile.preferences) if snapshot.profile else []
    if step == "equipment":
        return list(snapshot.profile.equipment) if snapshot.profile else []
    if step == "screening_areas":
        active = {f.flag for f in snapshot.flags if f.value == "yes"}
        return [v for v in rendering.AREA_CHOICES if v in active]
    return []


def _current_value(step: str, snapshot: ProfileSnapshot) -> str | None:
    profile = snapshot.profile
    if step == "language":
        return snapshot.language
    if profile is None:
        return None
    mapping = {
        "age": profile.age_bucket,
        "weight": profile.weight_bucket,
        "experience_duration": profile.experience,
        "experience_barbell": profile.barbell_experience,
        "focus": profile.focus,
        "location": profile.location,
        "frequency": str(profile.sessions_per_week) if profile.sessions_per_week else None,
        "session_length": str(profile.session_minutes) if profile.session_minutes else None,
    }
    return mapping.get(step)


def _valid_choice(step: str, value: str) -> bool:
    if step in profile_service.STEP_TO_RED_FLAG or step == "screening_clearance":
        return value in ("yes", "no")
    return value in rendering.choices_for(step)


async def _render(
    step: str, db: Database, user_id: int, snapshot: ProfileSnapshot
) -> tuple[str, InlineKeyboardMarkup]:
    lang = snapshot.language
    ctx = await profile_service.build_step_context(db, user_id)
    has_back = profile_service.prev_step(step, ctx) is not None

    if step == "summary":
        text = rendering.render_summary_text(
            lang,
            language=snapshot.language,
            timezone=snapshot.timezone,
            profile=snapshot.profile,
            flags=snapshot.flags,
            notes=snapshot.notes,
            title_key="setup.summary.title",
        )
        text = f"{text}\n\n{t('disclosure.ai', lang)}"
        markup = rendering.summary_markup(lang, confirm_button_key="setup.summary.confirm_button")
        return text, markup

    prompt = rendering.prompt_text(step, lang)
    if step == "timezone":
        return prompt, rendering.timezone_markup(lang, has_back=has_back)
    if step in rendering.MULTI_SELECT_STEPS:
        data = await profile_service.get_progress_data(db, user_id)
        selected = data.get("selected")
        if selected is None:
            selected = _initial_selection(step, snapshot)
            await profile_service.set_step(db, user_id, step, data={"selected": selected})
        markup = rendering.multi_select_markup(
            step,
            lang,
            selected=selected,  # type: ignore[arg-type]
            has_back=has_back,
            extra_none_button=step == "screening_areas",
        )
        return prompt, markup
    if step == "screening_other":
        markup = rendering.free_text_markup(
            step, lang, has_back=has_back, skip_button_key="setup.screening.other_skip_button"
        )
        return prompt, markup
    if step in profile_service.STEP_TO_RED_FLAG or step == "screening_clearance":
        return prompt, rendering.yes_no_markup(step, lang, has_back=has_back)

    current = _current_value(step, snapshot)
    return prompt, rendering.single_choice_markup(step, lang, has_back=has_back, current=current)


async def show_step(target: _ChatTarget, db: Database, user_id: int, step: str) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    text, markup = await _render(step, db, user_id, snapshot)
    if step != "summary":
        text = f"{text}\n\n{t('setup.common.cancel_hint', snapshot.language)}"
    if isinstance(target, CallbackQuery):
        message = target.message
        assert message is not None
        try:
            await message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        except TypeError:  # pragma: no cover - defensive, e.g. an inaccessible message
            await message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def advance_step(target: _ChatTarget, db: Database, user_id: int, current_step: str) -> None:
    ctx = await profile_service.build_step_context(db, user_id)
    nxt = profile_service.next_step(current_step, ctx) or "summary"
    await profile_service.set_step(db, user_id, nxt)
    await show_step(target, db, user_id, nxt)


async def _apply_choice(db: Database, user_id: int, step: str, value: str) -> None:
    if step == "language":
        await profile_service.set_language(db, user_id, value)
    elif step == "timezone":
        await profile_service.set_timezone(db, user_id, value)
    elif step == "age":
        await profile_service.update_profile_fields(db, user_id, age_bucket=value)
    elif step == "weight":
        await profile_service.update_profile_fields(db, user_id, weight_bucket=value)
    elif step == "experience_duration":
        await profile_service.update_profile_fields(db, user_id, experience=value)
    elif step == "experience_barbell":
        await profile_service.update_profile_fields(db, user_id, barbell_experience=value)
    elif step == "focus":
        await profile_service.update_profile_fields(db, user_id, focus=value)
    elif step == "location":
        await profile_service.update_profile_fields(db, user_id, location=value)
    elif step == "frequency":
        await profile_service.update_profile_fields(db, user_id, sessions_per_week=int(value))
    elif step == "session_length":
        await profile_service.update_profile_fields(db, user_id, session_minutes=int(value))
    elif step in profile_service.STEP_TO_RED_FLAG:
        flag = profile_service.STEP_TO_RED_FLAG[step]
        await profile_service.set_screening_flag(db, user_id, flag, value)
    elif step == "screening_clearance":
        await profile_service.set_clearance(db, user_id, value)
    else:  # pragma: no cover - defensive
        raise ValueError(f"step {step!r} has no single-choice handler")


async def _commit_multi_select(db: Database, user_id: int, step: str, selected: list[str]) -> None:
    if step == "preferences":
        await profile_service.update_profile_fields(db, user_id, preferences=list(selected))
    elif step == "equipment":
        await profile_service.update_profile_fields(db, user_id, equipment=list(selected))
    elif step == "screening_areas":
        flags = {ScreeningFlag(v) for v in selected}
        await profile_service.set_areas(db, user_id, flags)


async def _toast_stale(query: CallbackQuery, lang: str) -> None:
    await query.answer(t("errors.stale_callback", lang), show_alert=False)


async def on_setup_choice(
    query: CallbackQuery, callback_data: SetupChoice, db: Database, user_id: int
) -> None:
    step = callback_data.step
    current_step = await profile_service.get_step(db, user_id)
    snapshot = await profile_service.get_snapshot(db, user_id)
    if current_step != step or not _valid_choice(step, callback_data.value):
        await _toast_stale(query, snapshot.language)
        return
    await _apply_choice(db, user_id, step, callback_data.value)
    await advance_step(query, db, user_id, step)
    await query.answer()


async def on_setup_toggle(
    query: CallbackQuery, callback_data: SetupToggle, db: Database, user_id: int
) -> None:
    step = callback_data.step
    current_step = await profile_service.get_step(db, user_id)
    snapshot = await profile_service.get_snapshot(db, user_id)
    if current_step != step or callback_data.value not in rendering.choices_for(step):
        await _toast_stale(query, snapshot.language)
        return
    data = await profile_service.get_progress_data(db, user_id)
    raw_selected = data.get("selected", [])
    selected: set[str] = set(raw_selected) if isinstance(raw_selected, list) else set()
    selected.symmetric_difference_update({callback_data.value})
    ordered = [v for v in rendering.choices_for(step) if v in selected]
    await profile_service.set_step(db, user_id, step, data={"selected": ordered})
    await show_step(query, db, user_id, step)
    await query.answer()


async def on_setup_nav(
    query: CallbackQuery, callback_data: SetupNav, db: Database, user_id: int
) -> None:
    step = callback_data.step
    action = callback_data.action
    current_step = await profile_service.get_step(db, user_id)
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    if current_step != step:
        await _toast_stale(query, lang)
        return

    if action == "manual":
        if step != "timezone":
            await _toast_stale(query, lang)
            return
        await profile_service.set_step(db, user_id, step, data={"awaiting_manual_timezone": True})
        message = query.message
        assert message is not None
        await message.edit_text(  # type: ignore[union-attr]
            f"{t('setup.timezone.manual_prompt', lang)}\n\n{t('setup.common.cancel_hint', lang)}"
        )
        await query.answer()
        return

    if action == "back":
        ctx = await profile_service.build_step_context(db, user_id)
        prev = profile_service.prev_step(step, ctx)
        if prev is None:
            await query.answer()
            return
        await profile_service.set_step(db, user_id, prev)
        await show_step(query, db, user_id, prev)
        await query.answer()
        return

    if action == "next":
        if step == "summary":
            await profile_service.complete_setup(db, user_id)
            message = query.message
            assert message is not None
            await message.edit_text(t("start.bound_with_profile", lang))  # type: ignore[union-attr]
            await query.answer()
            return
        if step == "screening_other":
            # Skip (B2): never downgrades an already-"yes" `other_unlisted` (belt-and-
            # braces against a stale/racing Skip undoing a note); always advances.
            nxt = await profile_service.skip_other_note(db, user_id)
            await show_step(query, db, user_id, nxt)
            await query.answer()
            return
        if step in rendering.MULTI_SELECT_STEPS:
            data = await profile_service.get_progress_data(db, user_id)
            await _commit_multi_select(db, user_id, step, data.get("selected", []))  # type: ignore[arg-type]
        await advance_step(query, db, user_id, step)
        await query.answer()
        return

    await _toast_stale(query, lang)  # pragma: no cover - unknown action, defensive


# --- Free text that belongs to the setup flow (manual timezone) -----------------------------
#
# `screening_other` is handled directly in `bot/handlers/free_text.py`, *before* the stop-word
# scan (B2/A§6.6): the note must be persisted (and the step advanced past) even when it also
# halts, so it isn't special-cased here.


async def handle_setup_free_text(message: Message, db: Database, user_id: int, text: str) -> bool:
    """Returns True if `text` was consumed by an active setup step, False otherwise (the
    caller then falls back to the generic "use the menu" hint)."""
    step = await profile_service.get_step(db, user_id)
    if step != "timezone":
        return False
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language

    data = await profile_service.get_progress_data(db, user_id)
    if not data.get("awaiting_manual_timezone"):
        return False
    try:
        ZoneInfo(text.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        await message.answer(t("setup.timezone.invalid", lang))
        return True
    await _apply_choice(db, user_id, step, text.strip())
    await advance_step(message, db, user_id, step)
    return True


# --- /profile ---------------------------------------------------------------------------------


async def cmd_profile(message: Message, db: Database, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    if snapshot.profile is None or snapshot.profile.completed_at is None:
        await message.answer(t("profile.empty", snapshot.language))
        return
    text = rendering.render_summary_text(
        snapshot.language,
        language=snapshot.language,
        timezone=snapshot.timezone,
        profile=snapshot.profile,
        flags=snapshot.flags,
        notes=snapshot.notes,
        title_key="profile.title",
    )
    text = f"{text}\n\n{t('profile.fix_hint', snapshot.language)}"
    await message.answer(text, reply_markup=rendering.fix_buttons_markup(snapshot.language))


async def on_profile_fix(
    query: CallbackQuery, callback_data: ProfileFix, db: Database, user_id: int
) -> None:
    field = callback_data.field
    snapshot = await profile_service.get_snapshot(db, user_id)
    step = profile_service.FIELD_TO_STEP.get(field)
    if step is None:
        await _toast_stale(query, snapshot.language)
        return
    if field == "screening":
        # A deliberate re-run may legitimately clear a previous "other" note back to "no"
        # via Skip; `skip_other_note`'s belt-and-braces rule only refuses to do that for a
        # stale/racing press, so give this intentional re-run a clean slate first (B2).
        await profile_service.reset_other_unlisted(db, user_id)
    await profile_service.set_step(db, user_id, step)
    await show_step(query, db, user_id, step)
    await query.answer()


def build_router() -> Router:
    """A fresh `Router` with every setup/`profile` handler registered. A function, not a
    module-level singleton: an aiogram `Router` instance can only ever be attached to one
    `Dispatcher` (`fitme serve` builds exactly one; tests build a fresh one per case)."""
    router = Router(name="setup")
    router.message.register(cmd_profile, Command("profile"))
    router.callback_query.register(on_setup_choice, SetupChoice.filter())
    router.callback_query.register(on_setup_toggle, SetupToggle.filter())
    router.callback_query.register(on_setup_nav, SetupNav.filter())
    router.callback_query.register(on_profile_fix, ProfileFix.filter())
    return router
