"""A§9.1 plan pages: list, detail (+ recent trainings), structured edit, and the propose/
revise/confirm flow (the same `services.planning` used by the bot's `/plan`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.responses import Response

from fitme import i18n
from fitme.db.connection import Database
from fitme.domain.models import NAME_MAX_LENGTH, Block, Load, Plan, Prescription, Workout
from fitme.services import plan_edit, planning, training
from fitme.services import profile as profile_service
from fitme.services.safety import scan_and_maybe_halt
from fitme.services.webauth import SessionState
from fitme.web.deps import (
    exercise_display_names,
    get_db,
    get_llm,
    get_settings,
    redirect,
    render,
    require_user,
    verify_csrf_form,
)

router = APIRouter(prefix="/app/plans")


@router.get("")
async def list_plans(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    plans = await planning.list_plans(db, session.user_id)
    return render(
        request,
        "plans_list.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        plans=plans,
    )


@router.get("/new")
async def new_plan_form(
    request: Request, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    return render(
        request, "plan_new.html", lang=snapshot.language, settings=settings, session=session
    )


Trail = list[tuple[str, str]]


def _plans_trail(lang: str) -> Trail:
    return [(i18n.t("web.nav.plans", lang), "/app/plans")]


def _new_plan_trail(lang: str) -> Trail:
    """Plans › New plan: the parent trail of a draft generated from scratch."""
    return [*_plans_trail(lang), (i18n.t("web.plans.new_crumb", lang), "/app/plans/new")]


def _revise_trail(detail: planning.PlanDetail, lang: str) -> Trail:
    """Plans › {plan} › Revise: the parent trail of a draft revising an existing plan."""
    href = f"/app/plans/{detail.record.id}"
    return [
        *_plans_trail(lang),
        (detail.record.name, href),
        (i18n.t("web.plans.revise_crumb", lang), f"{href}/revise"),
    ]


def _delete_trail(detail: planning.PlanDetail, lang: str) -> Trail:
    """Plans › {plan} › Delete: the delete-confirm page's breadcrumb trail."""
    href = f"/app/plans/{detail.record.id}"
    return [
        *_plans_trail(lang),
        (detail.record.name, href),
        (i18n.t("web.plans.delete_crumb", lang), f"{href}/delete"),
    ]


async def _draft_context(
    db: Database, user_id: int, round_result: planning.PlanRoundResult, lang: str
) -> dict[str, Any]:
    """The draft page's context, with the breadcrumb trail of where the draft came from: a
    revision of an existing plan (`round_result.plan_id`) or a new plan."""
    trail = _new_plan_trail(lang)
    if round_result.plan_id is not None:
        detail = await planning.get_plan_detail(db, user_id, round_result.plan_id)
        if detail is not None:
            trail = _revise_trail(detail, lang)
    return {
        "round": round_result,
        "plan": round_result.plan,
        "refusal": round_result.refusal,
        "exercise_names": exercise_display_names(lang),
        "draft_trail": trail,
    }


@router.post("/new")
async def propose_plan(
    request: Request,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    llm = get_llm(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    result = await planning.propose_new_plan(db, llm, session.user_id)
    return render(
        request,
        "plan_draft.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        **await _draft_context(db, session.user_id, result, snapshot.language),
    )


@router.get("/{plan_id}/revise")
async def revise_plan_form(
    request: Request, plan_id: int, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")
    # The plan being revised is shown below the form (its latest version), through the same
    # partial as the detail and draft pages.
    return render(
        request,
        "plan_revise.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        detail=detail,
    )


async def _halted_response(request: Request, session: SessionState, lang: str) -> Response:
    settings = get_settings(request)
    return render(
        request, "halted.html", lang=lang, settings=settings, session=session, status_code=200
    )


@router.post("/{plan_id}/revise")
async def revise_plan(
    request: Request,
    plan_id: int,
    request_text: str = Form(...),
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    llm = get_llm(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    lang = snapshot.language
    halted = await scan_and_maybe_halt(db, user_id=session.user_id, lang=lang, text=request_text)
    if halted is not None:
        return await _halted_response(request, session, lang)
    try:
        result = await planning.revise_plan(
            db, llm, session.user_id, planning.PlanBase(plan_id=plan_id), request_text
        )
    except planning.PlanNotFoundError:
        return redirect("/app/plans")
    return render(
        request,
        "plan_draft.html",
        lang=lang,
        settings=settings,
        session=session,
        **await _draft_context(db, session.user_id, result, lang),
    )


@router.post("/draft/revise")
async def revise_draft(
    request: Request,
    decision_id: int = Form(...),
    request_text: str = Form(...),
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    llm = get_llm(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    lang = snapshot.language
    halted = await scan_and_maybe_halt(db, user_id=session.user_id, lang=lang, text=request_text)
    if halted is not None:
        return await _halted_response(request, session, lang)
    try:
        result = await planning.revise_plan(
            db, llm, session.user_id, planning.DraftBase(decision_id=decision_id), request_text
        )
    except planning.PlanNotFoundError:
        return redirect("/app/plans/new")
    return render(
        request,
        "plan_draft.html",
        lang=lang,
        settings=settings,
        session=session,
        **await _draft_context(db, session.user_id, result, lang),
    )


@router.post("/draft/confirm")
async def confirm_draft(
    request: Request,
    decision_id: int = Form(...),
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    result = await planning.confirm_plan(db, settings, session.user_id, decision_id)
    if result.status in (planning.ConfirmStatus.SAVED, planning.ConfirmStatus.ALREADY_SAVED):
        return redirect(f"/app/plans/{result.plan_id}")
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    lang = snapshot.language
    return render(
        request,
        "plan_confirm_failed.html",
        lang=lang,
        settings=settings,
        session=session,
        status=result.status,
        refusal=result.refusal,
    )


@router.post("/draft/cancel")
async def cancel_draft(
    session: SessionState = Depends(require_user), _csrf: None = Depends(verify_csrf_form)
) -> Response:
    return redirect("/app/plans")


async def _render_detail(
    request: Request,
    session: SessionState,
    detail: planning.PlanDetail,
    *,
    rename_error: str | None = None,
    status_code: int = 200,
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    plan_id = detail.record.id
    versions = await planning.list_versions(db, session.user_id, plan_id)
    recent = await training.list_recent_sessions_for_plan(db, session.user_id, plan_id)
    return render(
        request,
        "plan_detail.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        status_code=status_code,
        detail=detail,
        versions=versions or [],
        recent=recent,
        exercise_names=exercise_display_names(snapshot.language),
        timezone=snapshot.timezone,
        rename_error=rename_error,
    )


@router.get("/{plan_id}")
async def plan_detail(
    request: Request, plan_id: int, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")
    return await _render_detail(request, session, detail)


_RENAME_ERROR_KEYS = {
    planning.RenameStatus.EMPTY: "web.plans.rename_empty",
    planning.RenameStatus.TOO_LONG: "web.plans.rename_too_long",
    planning.RenameStatus.FORBIDDEN: "web.plans.rename_forbidden",
}


@router.post("/{plan_id}/rename")
async def rename_plan(
    request: Request,
    plan_id: int,
    name: str = Form(""),
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    """The plan detail page's rename form: the same `services.planning.rename_plan` the bot
    uses (trim, 1–60 characters, wording check, ownership). A rejected name re-shows the
    detail page with the reason; nothing is saved."""
    db = get_db(request)
    result = await planning.rename_plan(db, session.user_id, plan_id, name)
    if result.status == planning.RenameStatus.OK:
        return redirect(f"/app/plans/{plan_id}")
    if result.status == planning.RenameStatus.NOT_FOUND:
        return redirect("/app/plans")
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    message = i18n.t(
        _RENAME_ERROR_KEYS[result.status],
        snapshot.language,
        max=NAME_MAX_LENGTH,
        term=result.term or "",
    )
    return await _render_detail(request, session, detail, rename_error=message, status_code=400)


# --- Structured edit (A§9.1) ---------------------------------------------------------------------


def _field_error(prefix: str, exc: ValidationError) -> list[str]:
    return [
        f"{prefix}{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    ]


def _parse_int(form: FormData, key: str, default: int, errors: list[str]) -> int:
    """A malformed integer (non-numeric, empty) is a form error, never a crash (M9 review fix
    B1) — `int("999")`/`int("abc")` etc. are both handled here, before anything reaches a
    pydantic model; the *range* checks (e.g. `sets` 1..10) are still enforced by `Prescription`
    itself right after."""
    raw = form.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw))
    except ValueError:
        errors.append(f"{key}: {raw!r} is not a whole number")
        return default


def _parse_prescription(
    item: Prescription, prefix: str, form: FormData
) -> tuple[Prescription | None, list[str]]:
    """One prescription, rebuilt from the submitted fields and validated through
    `Prescription`'s own constructor (M9 review fix B1) — never `model_copy(update=...)`,
    which skips validation entirely and would silently accept `sets=999`, `reps_min >
    reps_max`, a negative `rest_seconds`, a bogus `load_kind`, or a non-numeric/non-finite kg.
    Returns `(None, [errors])` on any parse or validation failure; nothing is ever raised up
    to the route (a malformed value is a form error, not a 500)."""
    errors: list[str] = []
    exercise_id = form.get(f"{prefix}exercise", item.exercise_id)
    sets = _parse_int(form, f"{prefix}sets", item.sets, errors)
    reps_min = _parse_int(form, f"{prefix}reps_min", item.reps_min, errors)
    reps_max = _parse_int(form, f"{prefix}reps_max", item.reps_max, errors)
    rest_seconds = _parse_int(form, f"{prefix}rest", item.rest_seconds, errors)
    load_kind_raw = form.get(f"{prefix}load_kind", item.load.kind)
    load_kind = str(load_kind_raw)

    load_kg: float | None
    if load_kind == "kg":
        raw_kg = form.get(f"{prefix}load_kg")
        if raw_kg is None or raw_kg == "":
            load_kg = item.load.kg
        else:
            try:
                load_kg = float(str(raw_kg))
            except ValueError:
                errors.append(f"{prefix}load_kg: {raw_kg!r} is not a number")
                load_kg = None
    else:
        load_kg = None

    if errors:
        return None, errors

    try:
        load = Load(kind=load_kind, kg=load_kg)
    except ValidationError as exc:
        return None, _field_error(f"{prefix}load_", exc)

    try:
        prescription = Prescription(
            exercise_id=str(exercise_id),
            sets=sets,
            reps_min=reps_min,
            reps_max=reps_max,
            load=load,
            rest_seconds=rest_seconds,
            note=item.note,
            declared_kg=item.declared_kg,
        )
    except ValidationError as exc:
        return None, _field_error(prefix, exc)

    return prescription, []


def parse_edit_form(base: Plan, form: FormData) -> tuple[Plan | None, list[str]]:
    """Rebuild `base`'s structure (workouts/blocks kept as-is) with each prescription's
    editable fields overwritten from the submitted form — never adding or removing a workout,
    block or prescription (A§9.1: "structured form editing", values only). Every level
    (`Prescription`, `Block`, `Workout`, `Plan`) is constructed through its own validating
    constructor, never `model_copy(update=...)` (M9 review fix B1). Returns `(None, errors)`
    on any failure — the caller re-shows the form with those errors, nothing is saved, and
    nothing 500s."""
    errors: list[str] = []
    workouts: list[Workout] = []
    for wi, workout in enumerate(base.workouts):
        blocks: list[Block] = []
        for bi, block in enumerate(workout.blocks):
            items: list[Prescription] = []
            for ii, item in enumerate(block.items):
                prefix = f"w{wi}_b{bi}_i{ii}_"
                prescription, item_errors = _parse_prescription(item, prefix, form)
                errors.extend(item_errors)
                if prescription is not None:
                    items.append(prescription)
            if errors:
                continue
            try:
                blocks.append(Block(kind=block.kind, items=items))
            except ValidationError as exc:
                errors.extend(_field_error(f"w{wi}_b{bi}: ", exc))
        if errors:
            continue
        try:
            workouts.append(Workout(key=workout.key, title=workout.title, blocks=blocks))
        except ValidationError as exc:
            errors.extend(_field_error(f"w{wi}: ", exc))
    if errors:
        return None, errors
    try:
        return Plan(name=base.name, schedule=list(base.schedule), workouts=workouts), []
    except ValidationError as exc:
        return None, _field_error("", exc)


@router.get("/{plan_id}/edit")
async def edit_plan_form(
    request: Request, plan_id: int, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")
    allowed = await plan_edit.allowed_exercises_for_edit(db, session.user_id)
    return render(
        request,
        "plan_edit.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        detail=detail,
        plan=detail.plan,
        exercise_names=exercise_display_names(snapshot.language),
        allowed_exercises=allowed or [],
        warnings={},
        errors=[],
    )


@router.post("/{plan_id}/edit")
async def edit_plan_submit(
    request: Request,
    plan_id: int,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")

    form_data = await request.form()
    edited, parse_errors = parse_edit_form(detail.plan, form_data)
    confirmed = form_data.get("confirm_over_cap") == "on"
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    allowed = await plan_edit.allowed_exercises_for_edit(db, session.user_id)

    if edited is None:
        # A parse/validation failure (M9 review fix B1): re-show the *last known-good* plan
        # with the errors listed, never a 500 and never a save.
        return render(
            request,
            "plan_edit.html",
            lang=snapshot.language,
            settings=settings,
            session=session,
            detail=detail,
            plan=detail.plan,
            exercise_names=exercise_display_names(snapshot.language),
            allowed_exercises=allowed or [],
            warnings={},
            errors=parse_errors,
            status_code=400,
        )

    result = await plan_edit.save_edit(
        db, settings, session.user_id, plan_id, edited, confirmed=confirmed
    )
    if result.ok:
        return redirect(f"/app/plans/{plan_id}")

    return render(
        request,
        "plan_edit.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        detail=detail,
        plan=edited,
        exercise_names=exercise_display_names(snapshot.language),
        allowed_exercises=allowed or [],
        warnings=result.warnings or {},
        errors=result.errors or [],
    )


# --- Set default (A§9.1: the website had no way to do this; the bot's /plan already does) ----


def _default_next(candidate: str | None, plan_id: int) -> str:
    """A tight whitelist for the "Make default" form's `next` (A§9.4-style): only the plans
    list or this plan's own detail page, never an arbitrary `/app/...` path."""
    if candidate in (f"/app/plans/{plan_id}", "/app/plans"):
        assert candidate is not None
        return candidate
    return "/app/plans"


@router.post("/{plan_id}/default")
async def set_default_plan_route(
    request: Request,
    plan_id: int,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    """Make `plan_id` the one default (`services.planning.set_default`, the same call the
    bot's `/plan` "Set default" button makes). Redirects back to wherever the form was
    submitted from (the plans list or this plan's detail page), with an allow-listed flash
    query code (`base.html`'s `deleted=`/`error=` pattern)."""
    db = get_db(request)
    form = await request.form()
    raw_next = form.get("next")
    next_url = _default_next(raw_next if isinstance(raw_next, str) else None, plan_id)
    ok = await planning.set_default(db, session.user_id, plan_id)
    separator = "&" if "?" in next_url else "?"
    if not ok:
        return redirect(f"{next_url}{separator}error=not_found")
    return redirect(f"{next_url}{separator}plan_default=1")


# --- Delete (A§4.3: "any plan can be deleted") ------------------------------------------------


@router.get("/{plan_id}/delete")
async def delete_plan_confirm(
    request: Request, plan_id: int, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")
    return render(
        request,
        "plan_delete_confirm.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        detail=detail,
        trail=_delete_trail(detail, snapshot.language),
    )


@router.post("/{plan_id}/delete")
async def delete_plan_submit(
    request: Request,
    plan_id: int,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    result = await planning.delete_plan(db, session.user_id, plan_id)
    if result.status != planning.DeleteStatus.OK:
        return redirect("/app/plans?error=not_found")
    return redirect("/app/plans?plan_deleted=1")


__all__ = ["router"]
