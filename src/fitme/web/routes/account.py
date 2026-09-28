"""A§9.1 `/app/account`: JSON export, typed-confirmation delete, active sessions with
revoke. Export/delete reuse `services.account` — the same code `fitme export`/`fitme delete`
and the bot's `/export`/`/delete` use (AGENTS.md §5: "export and delete from day one")."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from fitme.services import account as account_service
from fitme.services import profile as profile_service
from fitme.services import webauth
from fitme.services.webauth import SessionState
from fitme.web.deps import get_db, get_settings, redirect, render, require_user, verify_csrf_form
from fitme.web.security import clear_session_cookie

router = APIRouter(prefix="/app/account")

_DELETE_CONFIRMATION = "DELETE"


@router.get("")
async def account_page(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    sessions = await webauth.list_sessions(db, session.user_id)
    return render(
        request,
        "account.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        sessions=sessions,
        current_id_hash=session.id_hash,
    )


@router.get("/export")
async def export_data(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    data = await account_service.export_user(db, session.user_id)
    payload = json.dumps(data, indent=2, default=str)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="fitme-export.json"'},
    )


@router.post("/delete")
async def delete_account(
    request: Request,
    confirm: str = Form(...),
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    if confirm.strip() != _DELETE_CONFIRMATION:
        snapshot = await profile_service.get_snapshot(db, session.user_id)
        sessions = await webauth.list_sessions(db, session.user_id)
        return render(
            request,
            "account.html",
            lang=snapshot.language,
            settings=settings,
            session=session,
            sessions=sessions,
            current_id_hash=session.id_hash,
            delete_error=True,
        )
    await account_service.delete_user(db, session.user_id)
    response = redirect("/")
    clear_session_cookie(response, settings)
    return response


@router.post("/sessions/revoke")
async def revoke_session_route(
    request: Request,
    id_hash: str = Form(...),
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    revoked_own_session = id_hash == session.id_hash
    await webauth.revoke_session(db, session.user_id, id_hash)
    response = redirect("/app/account" if not revoked_own_session else "/")
    if revoked_own_session:
        clear_session_cookie(response, settings)
    return response


__all__ = ["router"]
