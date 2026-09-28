"""The FastAPI website (A§9), mounted into `fitme serve` alongside the bot (A§3).

`create_app` builds a stateless app object: the database, settings, LLM runtime and the
"send this OTP" callback all live on `app.state`, read back by `web.deps`. Nothing here talks
to SQLite directly (A§2.1: web routes call `services/` only) — this module is wiring, not
business logic.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.webauth import SendCode
from fitme.web.deps import NotAuthenticated
from fitme.web.ratelimit import SlidingWindowLimiter
from fitme.web.routes import account, auth, plans, stats, trainings
from fitme.web.security import SecurityHeadersMiddleware, validate_web_config

_STATIC_DIR = Path(__file__).parent / "static"

# M9 review ("ALSO" #1): a per-client-IP throttle on `/auth/verify`, ahead of (and
# independent from) the per-code attempt counter (`services.webauth`, 5 attempts per code) —
# this one survives a fresh `/auth/request-code` call, which mints a new code with its own
# attempt counter.
_VERIFY_RATE_LIMIT_WINDOW = timedelta(minutes=15)
_VERIFY_RATE_LIMIT_MAX = 10


def create_app(db: Database, settings: Settings, llm: LlmRuntime, send_code: SendCode) -> FastAPI:
    validate_web_config(settings)

    app = FastAPI(title="Fitme", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.settings = settings
    app.state.llm = llm
    app.state.send_code = send_code
    app.state.verify_rate_limiter = SlidingWindowLimiter(
        _VERIFY_RATE_LIMIT_WINDOW, _VERIFY_RATE_LIMIT_MAX
    )

    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
        return RedirectResponse(url="/", status_code=303)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(auth.router)
    app.include_router(plans.router)
    app.include_router(trainings.router)
    app.include_router(stats.router)
    app.include_router(account.router)

    return app


__all__ = ["create_app"]
