"""Locale loading and lookup (A§5, A§6, A§9.1, A§12).

Every user-visible string lives in `locales/<lang>.toml` (IMPLEMENTATION_PLAN's standing
rule: "Put no user-visible string in code"), loaded as nested TOML tables and flattened into
dotted keys: `[setup.language]` + `prompt = "..."` becomes the key `"setup.language.prompt"`.

`t()` is deliberately forgiving by default (a bot must not crash mid-conversation over a copy
bug) and strict on request, for tests and `fitme catalog check`: pass `strict=True` to raise
`MissingTranslationKeyError` instead of logging and returning the bare key.
"""

from __future__ import annotations

import importlib.resources
import logging
import string
import tomllib
from collections.abc import Mapping
from functools import cache

_LOCALES_PACKAGE = "fitme.i18n.locales"
_DEFAULT_LANG = "en"

_logger = logging.getLogger(__name__)


class MissingTranslationKeyError(KeyError):
    """`strict=True` and `key` doesn't exist for `lang` (after falling back to `en`)."""

    def __init__(self, key: str, lang: str) -> None:
        super().__init__(f"missing translation key {key!r} for language {lang!r}")
        self.key = key
        self.lang = lang


def _flatten(table: Mapping[str, object], prefix: str = "") -> dict[str, str]:
    """Turn nested TOML tables into `{"a.b.c": "value"}`. Every leaf must be a string: a
    locale file has no other business holding numbers, booleans or dates."""
    flat: dict[str, str] = {}
    for key, value in table.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            flat.update(_flatten(value, dotted))
        elif isinstance(value, str):
            flat[dotted] = value
        else:
            raise ValueError(f"locale key {dotted!r} is not a string or table: {value!r}")
    return flat


@cache
def _raw_langs() -> tuple[str, ...]:
    """Every language with a `locales/<lang>.toml` file, sorted, `en` first if present."""
    root = importlib.resources.files(_LOCALES_PACKAGE)
    langs = sorted(
        item.name.removesuffix(".toml")
        for item in root.iterdir()
        if item.is_file() and item.name.endswith(".toml")
    )
    return tuple(sorted(langs, key=lambda lang: (lang != _DEFAULT_LANG, lang)))


@cache
def _load_lang(lang: str) -> dict[str, str]:
    """The flattened key -> string table for one language. Raises `FileNotFoundError` (via
    `importlib.resources`) for a language with no locale file — callers only reach this with
    a language `_raw_langs()` already reported."""
    resource = importlib.resources.files(_LOCALES_PACKAGE) / f"{lang}.toml"
    data = tomllib.loads(resource.read_text(encoding="utf-8"))
    return _flatten(data)


def supported_languages() -> tuple[str, ...]:
    """Every language Fitme currently ships a locale file for (A§5.1: "Add more by adding a
    locale file")."""
    return _raw_langs()


def resolve_language(telegram_language_code: str | None) -> str:
    """Map a Telegram `from_user.language_code` (e.g. `"ru-RU"`, `"en"`, `"fr"`, `None`) to a
    supported locale, falling back to `en` for anything not shipped (A§5.1 step 1)."""
    if not telegram_language_code:
        return _DEFAULT_LANG
    primary = telegram_language_code.strip().lower().split("-", 1)[0].split("_", 1)[0]
    supported = supported_languages()
    return primary if primary in supported else _DEFAULT_LANG


def _format_field_names(text: str) -> frozenset[str]:
    """The `{name}` placeholders a `str.format`-style string uses, e.g. `{"area"}` for
    `"How did your {area} feel?"`. Ignores positional (`{}`/`{0}`) and literal `{{`/`}}`."""
    names: set[str] = set()
    for _literal, field_name, _spec, _conv in string.Formatter().parse(text):
        if field_name:
            names.add(field_name)
    return frozenset(names)


def t(key: str, lang: str, *, strict: bool = False, **params: object) -> str:
    """The localized string for `key` in `lang`, with `{param}` placeholders filled in from
    `params` (`str.format`-style).

    Falls back to `en` for an unsupported `lang` (A§5.1). A key missing from both `lang` and
    `en` is a bug, not a user-facing crash: by default (`strict=False`, the runtime default)
    it's logged and the bare `key` is returned so the conversation can continue; with
    `strict=True` (used by tests and `fitme catalog check`) it raises
    `MissingTranslationKeyError` instead.
    """
    resolved_lang = lang if lang in supported_languages() else _DEFAULT_LANG
    table = _load_lang(resolved_lang)
    text = table.get(key)
    if text is None and resolved_lang != _DEFAULT_LANG:
        text = _load_lang(_DEFAULT_LANG).get(key)
    if text is None:
        if strict:
            raise MissingTranslationKeyError(key, lang)
        _logger.warning("missing translation key %r for language %r", key, lang)
        return key
    return text.format(**params) if params else text


def keys(lang: str) -> frozenset[str]:
    """Every key defined for `lang` (no `en` fallback: used to compare locales against each
    other, not to look a string up)."""
    return frozenset(_load_lang(lang))


def format_params(key: str, lang: str) -> frozenset[str]:
    """The `{param}` names `key`'s string uses in `lang`. Raises `MissingTranslationKeyError`
    if `lang` has no such key (no silent fallback: this is a validation helper)."""
    table = _load_lang(lang)
    text = table.get(key)
    if text is None:
        raise MissingTranslationKeyError(key, lang)
    return _format_field_names(text)


def check_parity(langs: tuple[str, ...] | None = None) -> list[str]:
    """Every locale-consistency problem across `langs` (default: every supported language):
    a key present in one language and missing from another, or a key whose `{param}` names
    differ between languages. An empty result means the locales are in parity. Used by
    `fitme catalog check` and the locale-parity tests."""
    selected = langs if langs is not None else supported_languages()
    if len(selected) < 2:
        return []

    problems: list[str] = []
    per_lang_keys = {lang: keys(lang) for lang in selected}
    all_keys: set[str] = set()
    for key_set in per_lang_keys.values():
        all_keys |= key_set

    for key in sorted(all_keys):
        missing = [lang for lang in selected if key not in per_lang_keys[lang]]
        if missing:
            problems.append(f"{key!r} is missing from: {', '.join(missing)}")
            continue
        params_by_lang = {lang: format_params(key, lang) for lang in selected}
        first_lang = selected[0]
        first_params = params_by_lang[first_lang]
        for lang in selected[1:]:
            if params_by_lang[lang] != first_params:
                problems.append(
                    f"{key!r} has mismatched format params: "
                    f"{first_lang}={sorted(first_params)} vs {lang}={sorted(params_by_lang[lang])}"
                )
    return problems
