"""`/stats` and `/system` (A§6.2, A§6.7). Both are read-only summaries built by
`services.stats`: no guard, no decision, no LLM call.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.i18n import t
from fitme.services import profile as profile_service
from fitme.services import stats as stats_service

_BYTES_PER_MIB = 1024 * 1024


def _format_db_size(size_bytes: int) -> str:
    return f"{size_bytes / _BYTES_PER_MIB:.1f} MiB"


def _format_change(delta: float, lang: str) -> str:
    if abs(delta) < 1e-9:
        return t("stats.top_lift_change_none", lang)
    key = "stats.top_lift_change_up" if delta > 0 else "stats.top_lift_change_down"
    return t(key, lang, delta=f"{delta:+.1f}" if delta > 0 else f"{delta:.1f}")


async def cmd_stats(message: Message, db: Database, settings: Settings, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    data = await stats_service.short_stats(db, user_id)

    lines = [
        t("stats.title", lang),
        "",
        t(
            "stats.sessions_line",
            lang,
            completed=data.weeks.completed,
            scheduled=data.weeks.scheduled,
        ),
        t("stats.volume_line", lang, volume=f"{data.volume_kg:g}"),
        "",
        t("stats.top_lifts_title", lang),
    ]
    if not data.top_lifts:
        lines.append(t("stats.no_top_lifts", lang))
    else:
        catalog_names = {
            exercise.id: exercise.names.get(lang, exercise.names.get("en", exercise.id))
            for exercise in load_catalog().exercise
        }
        for lift in data.top_lifts:
            name = catalog_names.get(lift.exercise_id, lift.exercise_id)
            lines.append(
                t(
                    "stats.top_lift_line",
                    lang,
                    name=name,
                    kg=f"{lift.current_kg:g}",
                    change=_format_change(lift.change_kg, lang),
                )
            )

    lines.append("")
    lines.append(t("stats.upcoming_title", lang))
    if not data.upcoming:
        lines.append(t("stats.no_upcoming", lang))
    else:
        for item in data.upcoming:
            lines.append(t("stats.upcoming_line", lang, date=item.date, workout=item.workout_key))

    lines.append("")
    lines.append(t("stats.website_line", lang, url=settings.web_base_url))
    await message.answer("\n".join(lines))


async def cmd_system(message: Message, db: Database, settings: Settings, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    data = await stats_service.system_stats(db, settings, user_id)

    def cost_text(cost: float | None) -> str:
        return (
            t("system.cost_known", lang, cost=f"{cost:.4f}")
            if cost is not None
            else t("system.cost_unknown", lang)
        )

    lines = [
        t("system.title", lang),
        "",
        t("system.db_size_line", lang, size=_format_db_size(data.db_size_bytes)),
        t("system.sessions_line", lang, count=data.sessions_logged),
        t("system.content_version_line", lang, version=data.content_version),
        "",
        t("system.llm_title", lang),
    ]
    for window_key, window in (
        ("system.window_today", data.llm_today),
        ("system.window_7d", data.llm_7d),
        ("system.window_30d", data.llm_30d),
        ("system.window_all", data.llm_all),
    ):
        lines.append(
            t(
                "system.llm_line",
                lang,
                window=t(window_key, lang),
                calls=window.calls,
                input_tokens=window.input_tokens,
                output_tokens=window.output_tokens,
                cost=cost_text(window.cost_usd),
            )
        )
    await message.answer("\n".join(lines))


def build_router() -> Router:
    router = Router(name="stats")
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_system, Command("system"))
    return router
