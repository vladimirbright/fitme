"""A§9.1 plan pages: list, detail (+ recent trainings), structured edit, and the propose/
revise/confirm flow (the same `services.planning` used by the bot's `/plan`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.responses import Response

from fitme.domain.models import Block, Load, Plan, Prescription, Workout
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


def _draft_context(round_result: planning.PlanRoundResult, lang: str) -> dict[str, Any]:
    names = exercise_display_names(lang)
    return {
        "round": round_result,
        "plan": round_result.plan,
        "refusal": round_result.refusal,
        "exercise_names": names,
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
        **_draft_context(result, snapshot.language),
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
    return render(
        request,
        "plan_revise.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        plan_id=plan_id,
        plan_name=detail.record.name,
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
        **_draft_context(result, lang),
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
        **_draft_context(result, lang),
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


@router.get("/{plan_id}")
async def plan_detail(
    request: Request, plan_id: int, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    detail = await planning.get_plan_detail(db, session.user_id, plan_id)
    if detail is None:
        return redirect("/app/plans")
    versions = await planning.list_versions(db, session.user_id, plan_id)
    recent = await training.list_recent_sessions_for_plan(db, session.user_id, plan_id)
    return render(
        request,
        "plan_detail.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        detail=detail,
        versions=versions or [],
        recent=recent,
        exercise_names=exercise_display_names(snapshot.language),
        timezone=snapshot.timezone,
    )


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


__all__ = ["router"]
