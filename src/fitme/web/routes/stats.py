"""A§9.1 `/app/stats` and its `/api/stats/*.json` data endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, Response

from fitme.services import profile as profile_service
from fitme.services import stats as stats_service
from fitme.services.webauth import SessionState
from fitme.web.deps import exercise_display_names, get_db, get_settings, render, require_user

router = APIRouter()


@router.get("/app/stats")
async def stats_page(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    data = await stats_service.chart_data(db, session.user_id)
    return render(
        request,
        "stats.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        data=data,
        exercise_names=exercise_display_names(snapshot.language),
    )


@router.get("/api/stats/volume.json")
async def api_volume(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    data = await stats_service.chart_data(db, session.user_id)
    return JSONResponse([asdict(point) for point in data.weekly_volume])


@router.get("/api/stats/sessions.json")
async def api_sessions(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    data = await stats_service.chart_data(db, session.user_id)
    return JSONResponse([asdict(point) for point in data.sessions_per_week])


@router.get("/api/stats/loads.json")
async def api_loads(request: Request, session: SessionState = Depends(require_user)) -> Response:
    db = get_db(request)
    snapshot = await profile_service.get_snapshot(db, session.user_id)
    names = exercise_display_names(snapshot.language)
    data = await stats_service.chart_data(db, session.user_id)
    payload = {
        names.get(exercise_id, exercise_id): [asdict(point) for point in points]
        for exercise_id, points in data.load_over_time.items()
    }
    return JSONResponse(payload)


__all__ = ["router"]
