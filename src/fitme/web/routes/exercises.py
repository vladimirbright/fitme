"""Authenticated browser for the locally imported exercise reference library."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from fitme.services import profile as profile_service
from fitme.services.reference_exercises import (
    REVIEW_STATUSES,
    load_reference,
    reference_path,
    search_reference,
)
from fitme.services.webauth import SessionState
from fitme.web.deps import get_db, get_settings, render, require_user

router = APIRouter(prefix="/app/exercises")
PAGE_SIZE = 30
LANGUAGE_LABELS = {
    "en": "English",
    "es": "Español",
    "it": "Italiano",
    "tr": "Türkçe",
    "ru": "Русский",
    "zh": "中文",
}


@router.get("")
async def list_exercises(
    request: Request,
    session: SessionState = Depends(require_user),
    q: str = "",
    category: str = "",
    equipment: str = "",
    language: str = "",
    review_status: str = "",
    page: int = 1,
) -> Response:
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(get_db(request), session.user_id)
    language = language if language in LANGUAGE_LABELS else snapshot.language
    exercises = load_reference(reference_path(settings.db_path))
    matches = search_reference(exercises, q[:100], category, equipment)
    if review_status in REVIEW_STATUSES:
        matches = [item for item in matches if item.review_status == review_status]
    page = max(1, min(page, max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)))
    return render(
        request,
        "exercises_list.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        exercises=matches[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        total=len(matches),
        page=page,
        total_pages=max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE),
        q=q[:100],
        category=category,
        equipment=equipment,
        categories=sorted({item.category for item in exercises}),
        equipments=sorted({item.equipment for item in exercises}),
        language=language,
        language_labels=LANGUAGE_LABELS,
        review_status=review_status,
        review_statuses=sorted(REVIEW_STATUSES),
        installed=bool(exercises),
    )


@router.get("/{exercise_id}")
async def exercise_detail(
    request: Request,
    exercise_id: str,
    session: SessionState = Depends(require_user),
    language: str = "",
) -> Response:
    settings = get_settings(request)
    snapshot = await profile_service.get_snapshot(get_db(request), session.user_id)
    language = language if language in LANGUAGE_LABELS else snapshot.language
    exercises = load_reference(reference_path(settings.db_path))
    exercise = next((item for item in exercises if item.id == exercise_id), None)
    if exercise is None:
        return render(
            request,
            "exercises_detail.html",
            lang=snapshot.language,
            settings=settings,
            session=session,
            exercise=None,
            language=language,
            language_labels=LANGUAGE_LABELS,
            status_code=404,
        )
    return render(
        request,
        "exercises_detail.html",
        lang=snapshot.language,
        settings=settings,
        session=session,
        exercise=exercise,
        language=language,
        language_labels=LANGUAGE_LABELS,
        instruction=exercise.instructions.get(language, exercise.instructions["en"]),
    )
