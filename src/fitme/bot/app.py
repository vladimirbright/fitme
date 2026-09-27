"""Assembles the aiogram `Dispatcher` (A§6): `OwnerGateMiddleware` first (on
`update.outer_middleware`, which wraps every event type — messages and callback queries
alike), then every router, most specific first, with the free-text catch-all last."""

from __future__ import annotations

from aiogram import Dispatcher

from fitme.bot.handlers import account, activation, free_text, setup, start, stubs
from fitme.bot.middleware import OwnerGateMiddleware, StopWordCommandArgsMiddleware
from fitme.config.settings import Settings
from fitme.db.connection import Database


def build_dispatcher(db: Database, settings: Settings) -> Dispatcher:
    dp = Dispatcher(db=db, settings=settings, pending_deletes=set())
    dp.update.outer_middleware(OwnerGateMiddleware(db, settings))
    # Inner middleware on the root `message` observer: covers every router's command
    # handlers (A§6.3 "scan all owner free text", including a command's own arguments).
    dp.message.middleware(StopWordCommandArgsMiddleware(db))

    dp.include_router(activation.build_router())
    dp.include_router(setup.build_router())
    dp.include_router(start.build_router())
    dp.include_router(account.build_router())
    dp.include_router(stubs.build_router())
    dp.include_router(free_text.build_router())

    return dp
