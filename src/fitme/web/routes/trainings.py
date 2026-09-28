"""A§9.1/A§9.4 training-log pages: the paginated list, one session's detail, and the
confirm-then-delete flow (single or bulk, works without JS)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from fitme.services import profile as profile_service
from fitme.services import training
from fitme.services.webauth import SessionState
from fitme.web.deps import (
    exercise_display_names,
    get_db,
    get_settings,
    redirect,
    render,
    require_user,
    safe_next,
    verify_csrf_form,
)

router = APIRouter(prefix="/app/trainings")

_PAGE_SIZE = 20


@router.get("")
async def list_trainings(
    request: Request, session: SessionState = Depends(require_user), page: int = 1
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    result = await training.list_sessions_page(db, session.user_id, page=page, page_size=_PAGE_SIZE)
    return render(
        request,
        "trainings_list.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        result=result,
        timezone=snapshot.timezone,
    )


@router.get("/{session_id}")
async def training_detail(
    request: Request, session_id: int, session: SessionState = Depends(require_user)
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    detail = await training.get_session_detail(db, session.user_id, session_id)
    if detail is None:
        return redirect("/app/trainings")
    return render(
        request,
        "training_detail.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        detail=detail,
        exercise_names=exercise_display_names(snapshot.language),
        timezone=snapshot.timezone,
    )


def _parse_ids(raw: list[str]) -> list[int]:
    ids: list[int] = []
    for value in raw:
        try:
            ids.append(int(value))
        except ValueError:
            continue
    return ids


@router.post("/delete/confirm")
async def delete_confirm(
    request: Request,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    form = await request.form()
    ids = _parse_ids([str(v) for v in form.getlist("ids")])
    raw_next = form.get("next")
    next_url = safe_next(raw_next if isinstance(raw_next, str) else None)
    snapshot = await profile_service.get_snapshot(db, session.user_id)

    sessions = []
    for session_id in ids:
        item = await training.get_session_detail(db, session.user_id, session_id)
        if item is not None:
            sessions.append(item)
    return render(
        request,
        "trainings_delete_confirm.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        ids=ids,
        sessions=sessions,
        next_url=next_url,
    )


@router.post("/delete")
async def delete_trainings(
    request: Request,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    form = await request.form()
    ids = _parse_ids([str(v) for v in form.getlist("ids")])
    raw_next = form.get("next")
    next_url = safe_next(raw_next if isinstance(raw_next, str) else None)
    result = await training.delete_sessions(db, session.user_id, ids)
    separator = "&" if "?" in next_url else "?"
    if result.status != training.DeleteSessionsStatus.OK:
        # M9 review ("ALSO" #7): a NOT_FOUND delete (a foreign or unknown id in the batch)
        # shows a message too, not a silent no-op redirect.
        return redirect(f"{next_url}{separator}error=not_found")
    # M9 review ("ALSO" #3): the redirect carries only a validated integer count, never
    # free text — `base.html` renders the actual (translated) message server-side.
    return redirect(f"{next_url}{separator}deleted={len(result.deleted)}")


__all__ = ["router"]
