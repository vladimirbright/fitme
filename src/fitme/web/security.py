"""Cookie/CSRF/header plumbing (A§9.2). No SQL, no business logic — this is wiring only;
`services.webauth` holds the actual session and CSRF logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from fitme.config.settings import Settings, SettingsError

_SECURE_COOKIE_NAME = "__Host-fitme_session"
_DEV_COOKIE_NAME = "fitme_session"

# A§9.2 session lifetime: the absolute cap the cookie itself carries (the server-side
# `web_sessions.expires_at` is authoritative; the cookie's Max-Age is just a hint to the
# browser so it doesn't hold a cookie the server would refuse anyway).
_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

# The exact hostnames the dev exception covers (M9 review, "ALSO" #4): a bare
# `str.startswith("http://localhost")` also matches `http://localhost.evil.example` or
# `http://localhostx`, neither of which is actually localhost. Parsing the URL and comparing
# the hostname exactly closes that.
_DEV_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_dev_localhost(settings: Settings) -> bool:
    if not settings.dev:
        return False
    parts = urlsplit(settings.web_base_url)
    return parts.scheme == "http" and parts.hostname in _DEV_HOSTNAMES


def validate_web_config(settings: Settings) -> None:
    """A§9.1/A§9.2 startup check: `FITME_DEV` allows a non-Secure cookie on
    `http://localhost` only; every other configuration needs an `https://` base URL."""
    if not settings.web_base_url.startswith("https://") and not _is_dev_localhost(settings):
        raise SettingsError(
            "FITME_WEB_BASE_URL must start with https:// unless FITME_DEV=true and it points "
            f"at http://localhost (A§9.2). Got: {settings.web_base_url!r}"
        )


def cookie_is_secure(settings: Settings) -> bool:
    return not _is_dev_localhost(settings)


def session_cookie_name(settings: Settings) -> str:
    """`__Host-` cookies require `Secure`; the dev-localhost exception therefore also needs a
    plain name, since a browser silently refuses to set an `__Host-` cookie without it."""
    return _SECURE_COOKIE_NAME if cookie_is_secure(settings) else _DEV_COOKIE_NAME


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        session_cookie_name(settings),
        token,
        max_age=_COOKIE_MAX_AGE_SECONDS,
        path="/",
        secure=cookie_is_secure(settings),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        session_cookie_name(settings),
        path="/",
        secure=cookie_is_secure(settings),
        httponly=True,
        samesite="lax",
    )


_CSP = "default-src 'self'"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """A§9.2: CSP with no inline script, nosniff, same-origin referrer policy, no framing."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response


__all__ = [
    "SecurityHeadersMiddleware",
    "clear_session_cookie",
    "cookie_is_secure",
    "session_cookie_name",
    "set_session_cookie",
    "validate_web_config",
]
