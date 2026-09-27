"""Small inline-keyboard helpers shared across the setup flow and other bot handlers."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fitme.bot.callback_data import DeleteConfirm, HoldClear, SetupNav
from fitme.i18n import t


def grid(buttons: Sequence[InlineKeyboardButton], *, width: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(*buttons)
    builder.adjust(width)
    return builder.as_markup()


def with_extra_rows(
    markup: InlineKeyboardMarkup, *rows: Sequence[InlineKeyboardButton]
) -> InlineKeyboardMarkup:
    """Append one or more full-width rows (e.g. Back/Next) below an existing grid."""
    keyboard = [*markup.inline_keyboard, *(list(row) for row in rows)]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def back_row(step: str, lang: str, *, has_back: bool) -> list[InlineKeyboardButton]:
    if not has_back:
        return []
    return [
        InlineKeyboardButton(
            text=t("setup.common.back_button", lang),
            callback_data=SetupNav(step=step, action="back").pack(),
        )
    ]


def hold_clear_markup(hold_id: int, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("hold_clear.button", lang), callback_data=HoldClear(hold_id=hold_id).pack()
        )
    )
    return builder.as_markup()


def delete_prompt_markup(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("delete.button", lang), callback_data=DeleteConfirm(action="start").pack()
        ),
        InlineKeyboardButton(
            text=t("delete.cancel_button", lang),
            callback_data=DeleteConfirm(action="cancel").pack(),
        ),
    )
    builder.adjust(1)
    return builder.as_markup()
