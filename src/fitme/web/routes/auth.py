"""A§9.1 `/`, `/auth/request-code`, `/auth/verify`, `/auth/logout`.

The unauthenticated endpoints (`request-code`, `verify`) carry no CSRF token: there is no
session yet to bind one to, and both are already rate-limited/attempt-limited at the service
layer (`services.webauth`). Every other POST route in this app requires one
(`web.deps.verify_csrf_form`).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from fitme.services import webauth
from fitme.services.webauth import SessionState
from fitme.web.deps import (
    client_ip,
    get_db,
    get_send_code,
    get_session,
    get_settings,
    get_verify_rate_limiter,
    login_page_language,
    redirect,
    render,
    require_user,
    verify_csrf_form,
)
from fitme.web.security import clear_session_cookie, session_cookie_name, set_session_cookie

router = APIRouter()


@router.get("/")
async def login_page(request: Request) -> Response:
    session = await get_session(request)
    if session is not None:
        return redirect("/app/plans")
    settings = get_settings(request)
    lang = login_page_language(request)
    return render(request, "login.html", lang=lang, settings=settings, session=None)


@router.get("/app")
async def app_root(session: SessionState = Depends(require_user)) -> Response:
    return redirect("/app/plans")


@router.post("/auth/request-code")
async def request_code(request: Request, background_tasks: BackgroundTasks) -> Response:
    db = get_db(request)
    send_code = get_send_code(request)
    lang = login_page_language(request)
    settings = get_settings(request)
    # A§9.1: the response is identical whether or not an account is bound (and, here, whether
    # or not the request was rate-limited) — always the same "check Telegram" message. Doing
    # the actual work (the rate-limit check, the DB write, the Telegram call) as a background
    # task (M9 review, "ALSO" #2) means the response itself is sent immediately, with no
    # timing difference between "there's an account to notify" and "there isn't" — the route
    # does no work of its own before answering, so there's nothing left to time.
    background_tasks.add_task(webauth.request_login_code, db, send_code=send_code)
    return render(
        request,
        "login.html",
        lang=lang,
        settings=settings,
        session=None,
        code_sent=True,
    )


@router.post("/auth/verify")
async def verify_code(request: Request, code: str = Form(...)) -> Response:
    # M9 review ("ALSO" #1): a per-client-IP throttle, ahead of (and independent from) the
    # per-code attempt counter — a fresh `/auth/request-code` mints a new code with its own
    # counter, but this one keeps counting across codes.
    limiter = get_verify_rate_limiter(request)
    if limiter.hit(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many attempts, try again later")

    db = get_db(request)
    settings = get_settings(request)
    lang = login_page_language(request)
    result = await webauth.verify_login_code(db, code=code)
    if result.status != webauth.VerifyStatus.OK or result.session_token is None:
        return render(
            request,
            "login.html",
            lang=lang,
            settings=settings,
            session=None,
            code_sent=True,
            invalid_code=True,
        )
    response = redirect("/app/plans")
    set_session_cookie(response, settings, result.session_token)
    return response


@router.post("/auth/logout")
async def logout(
    request: Request,
    session: SessionState = Depends(require_user),
    _csrf: None = Depends(verify_csrf_form),
) -> Response:
    settings = get_settings(request)
    db = get_db(request)
    token = request.cookies.get(session_cookie_name(settings))
    if token is not None:
        await webauth.logout(db, token)
    response: RedirectResponse = redirect("/")
    clear_session_cookie(response, settings)
    return response


__all__ = ["router"]
