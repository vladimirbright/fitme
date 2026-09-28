"""Shared FastAPI dependencies: the process-wide objects (`app.state`), the session
dependency, CSRF verification, template rendering and safe-redirect helpers.

Every route module imports from here rather than reaching into `request.app.state` itself,
so the wiring stays in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse, Response

from fitme import i18n
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.services import webauth
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.webauth import SendCode, SessionState
from fitme.web.ratelimit import SlidingWindowLimiter
from fitme.web.security import session_cookie_name

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _local_dt_filter(value: str | None, timezone: str | None, default: str = "—") -> str:
    """A Jinja filter: an ISO UTC `fitme.clock` timestamp -> `YYYY-MM-DD HH:MM` in `timezone`
    (the user's own, A§9.1's "in the user's timezone"), or `default` (an em dash — a fallback
    label, M9 review "ALSO" #7, rather than an empty, invisible link) for `None` — a draft or
    `confirmed` session has no `started_at` yet, and a session still `in_progress` has no
    `finished_at`."""
    if not value:
        return default
    from zoneinfo import ZoneInfo

    from fitme import clock

    tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    return clock.parse_timestamp(value).astimezone(tz).strftime("%Y-%m-%d %H:%M")


templates.env.filters["local_dt"] = _local_dt_filter


class NotAuthenticated(Exception):
    """Raised by `require_user` for a missing/expired session. An exception handler
    (`web.app`) turns this into a redirect to `/` — A§9.2 "`/app/*` ... redirect without a
    session", never a bare 401 a browser can't act on."""


def get_db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_llm(request: Request) -> LlmRuntime:
    return request.app.state.llm  # type: ignore[no-any-return]


def get_send_code(request: Request) -> SendCode:
    return request.app.state.send_code  # type: ignore[no-any-return]


def get_verify_rate_limiter(request: Request) -> SlidingWindowLimiter:
    return request.app.state.verify_rate_limiter  # type: ignore[no-any-return]


def client_ip(request: Request) -> str:
    """The rate-limit key for `/auth/verify` (M9 review, "ALSO" #1): the client's address, or
    a fixed placeholder when Starlette has none (e.g. some test transports) — every such
    request then shares one bucket, which only makes the limiter *more* eager, never less."""
    return request.client.host if request.client is not None else "unknown"


async def get_session(request: Request) -> SessionState | None:
    settings = get_settings(request)
    token = request.cookies.get(session_cookie_name(settings))
    if token is None:
        return None
    return await webauth.resolve_session(get_db(request), token)


async def require_user(request: Request) -> SessionState:
    session = await get_session(request)
    if session is None:
        raise NotAuthenticated()
    return session


async def verify_csrf_form(request: Request, session: SessionState = Depends(require_user)) -> None:
    """A POST-only dependency (A§9.2): every state-changing form carries a `csrf_token` field,
    checked against an HMAC bound to the session. Missing or wrong -> 403."""
    from fastapi import HTTPException

    settings = get_settings(request)
    form = await request.form()
    submitted = form.get("csrf_token")
    if not webauth.verify_csrf(
        settings, session.id_hash, submitted if isinstance(submitted, str) else None
    ):
        raise HTTPException(status_code=403, detail="invalid or missing csrf token")


def _accept_language_primary(header: str | None) -> str | None:
    """The highest-priority language tag's primary subtag from an `Accept-Language` header
    (A§9.2: "On the login page, use `Accept-Language`"), e.g. `"en-US,en;q=0.9,ru;q=0.8"` ->
    `"en"`. `None` for a missing/empty header."""
    if not header:
        return None
    first = header.split(",", 1)[0].split(";", 1)[0].strip()
    return first or None


def login_page_language(request: Request) -> str:
    return i18n.resolve_language(_accept_language_primary(request.headers.get("accept-language")))


def render(
    request: Request,
    name: str,
    *,
    lang: str,
    settings: Settings,
    session: SessionState | None = None,
    status_code: int = 200,
    **context: Any,
) -> Response:
    def translate(key: str, **kwargs: Any) -> str:
        return i18n.t(key, lang, **kwargs)

    csrf: str | None = None
    if session is not None:
        csrf = webauth.csrf_token(settings.secret_key.get_secret_value(), session.id_hash)
    ctx: dict[str, Any] = {
        "request": request,
        "t": translate,
        "lang": lang,
        "source_url": settings.source_url,
        "csrf_token": csrf,
        **context,
    }
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def exercise_display_names(lang: str) -> dict[str, str]:
    """`{exercise_id: display name}` for the whole catalog, in `lang` (falling back to `en`) —
    templates never see a bare catalog id."""
    catalog = load_catalog()
    return {
        exercise.id: exercise.names.get(lang, exercise.names.get("en", exercise.id))
        for exercise in catalog.exercise
    }


def safe_next(candidate: str | None, *, default: str = "/app/plans") -> str:
    """A `next` redirect target is only ever an internal `/app/...` path (A§9.4): never an
    absolute URL, a scheme-relative `//host` one, or anything else that could redirect off
    this site."""
    if (
        candidate is not None
        and candidate.startswith("/app")
        and not candidate.startswith("//")
        and "\\" not in candidate
    ):
        return candidate
    return default


def redirect(url: str, *, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status_code)


__all__ = [
    "NotAuthenticated",
    "client_ip",
    "exercise_display_names",
    "get_db",
    "get_llm",
    "get_send_code",
    "get_session",
    "get_settings",
    "get_verify_rate_limiter",
    "login_page_language",
    "redirect",
    "render",
    "require_user",
    "safe_next",
    "templates",
    "verify_csrf_form",
]
