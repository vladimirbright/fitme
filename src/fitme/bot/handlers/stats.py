"""`/stats` and `/system` (A§6.2, A§6.7). Both are read-only summaries built by
`services.stats`: no guard, no decision, no LLM call.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

from aiogram import Router
from aiogram.enums import ParseMode
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


def _short_model(model: str) -> str:
    """ "anthropic:claude-sonnet-5" → "sonnet-5": a table column, as narrow as it can be."""
    name = model.partition(":")[2] or model
    return name.removeprefix("claude-")


def _cost_cell(usage: stats_service.LlmUsage | None) -> str:
    if usage is None or usage.calls == 0:
        return "–"
    if usage.cost_usd is None:
        return "?"
    if 0 < usage.cost_usd < 0.005:
        return "<0.01"
    return f"{usage.cost_usd:.2f}"


def _calls_cell(usage: stats_service.LlmUsage | None) -> str:
    return "–" if usage is None or usage.calls == 0 else str(usage.calls)


def usage_table(
    rows: list[tuple[str, stats_service.LlmWindowStats]],
    models: list[str],
    cell: Callable[[stats_service.LlmUsage | None], str],
    total_label: str,
) -> str:
    """A monospace table: one row per window, one column per model, the total last. Columns
    are right-aligned to their widest cell."""
    header = ["", *(_short_model(model) for model in models), total_label]
    body: list[list[str]] = []
    for label, window in rows:
        by_model = {item.model: item.usage for item in window.by_model}
        body.append([label, *(cell(by_model.get(model)) for model in models), cell(window.total)])
    widths = [max(len(line[i]) for line in [header, *body]) for i in range(len(header))]

    def render(line: list[str]) -> str:
        first = line[0].ljust(widths[0])
        rest = (value.rjust(width) for value, width in zip(line[1:], widths[1:], strict=True))
        return "  ".join([first, *rest]).rstrip()

    return "\n".join(render(line) for line in [header, *body])


async def cmd_system(message: Message, db: Database, settings: Settings, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    data = await stats_service.system_stats(db, settings, user_id)

    rows = [
        (t("system.row_today", lang), data.llm_today),
        (t("system.row_week", lang), data.llm_week),
        (t("system.row_month", lang), data.llm_month),
        (t("system.row_all", lang), data.llm_all),
    ]
    # Every model ever called, the most expensive first (`by_model`'s order, all time).
    models = [item.model for item in data.llm_all.by_model]
    total = t("system.total_column", lang)
    lines = [
        escape(t("system.title", lang)),
        "",
        escape(t("system.db_size_line", lang, size=_format_db_size(data.db_size_bytes))),
        escape(t("system.sessions_line", lang, count=data.sessions_logged)),
        escape(t("system.content_version_line", lang, version=data.content_version)),
        "",
        f"<b>{escape(t('system.cost_title', lang))}</b>",
        f"<pre>{escape(usage_table(rows, models, _cost_cell, total))}</pre>",
        f"<b>{escape(t('system.calls_title', lang))}</b>",
        f"<pre>{escape(usage_table(rows, models, _calls_cell, total))}</pre>",
    ]
    if any(item.usage.cost_usd is None for item in data.llm_all.by_model):
        lines.append(escape(t("system.unpriced_note", lang)))
    if data.llm_all.total.estimated:
        lines.append(escape(t("system.estimate_note", lang)))
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


def build_router() -> Router:
    router = Router(name="stats")
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_system, Command("system"))
    return router
