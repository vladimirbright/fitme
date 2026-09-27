"""Loads versioned prompt templates from `src/fitme/prompts/<name>.v<N>.md` (A§4.8, A§8.1).

Prompts are plain files, not inline strings (AGENTS.md §9), shipped as package data the same
way `catalog/exercises.toml` and `i18n/locales/*.toml` already are (proven by
`config/content.py`'s own hashing walk), so `src/fitme/prompts/` needs no `__init__.py` —
`importlib.resources.files("fitme") / "prompts"` reaches it as a plain subdirectory of the
`fitme` package, and hatchling's `packages = ["src/fitme"]` includes every file under it in
the wheel (a wheel-inclusion test builds the real wheel and checks for these files).

Rendering uses `string.Template` (`$name`, boring stdlib), not `str.format`: a prompt file is
free to contain literal `{`/`}` (e.g. an example JSON blob shown to the model) without that
looking like a format placeholder. `render_prompt` with no `params` skips substitution
entirely, so a v1 prompt with no placeholders at all — every prompt shipped so far — is
returned byte-for-byte.
"""

from __future__ import annotations

import importlib.resources
import re
import string
from dataclasses import dataclass
from importlib.resources.abc import Traversable

_PROMPTS_ROOT_PACKAGE = "fitme"
_PROMPTS_SUBDIR = "prompts"
_FILENAME_RE = re.compile(r"^(?P<name>[a-z][a-z_]*)\.v(?P<version>[1-9]\d*)\.md$")


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """What `decisions.prompt_template`/`decisions.prompt_version` store (A§4.3, A§8.1),
    plus the rendered text actually sent to the model."""

    template_name: str
    version: int
    text: str


class PromptNotFoundError(FileNotFoundError):
    """No prompt file matches the requested `name` (and `version`, if given)."""


def _prompts_root() -> Traversable:
    return importlib.resources.files(_PROMPTS_ROOT_PACKAGE) / _PROMPTS_SUBDIR


def _available_versions(name: str) -> dict[int, Traversable]:
    root = _prompts_root()
    if not root.is_dir():
        return {}
    versions: dict[int, Traversable] = {}
    for item in root.iterdir():
        if not item.is_file():
            continue
        match = _FILENAME_RE.match(item.name)
        if match is not None and match.group("name") == name:
            versions[int(match.group("version"))] = item
    return versions


def render_prompt(
    template_name: str, *, version: int | None = None, **params: object
) -> RenderedPrompt:
    """The rendered text of prompt `template_name`, plus its resolved version. Picks the
    highest available version when `version` is omitted (A§4.8). `**params` fill
    `$placeholder`-style names via `string.Template.substitute` — every placeholder the
    template uses must have a matching keyword argument, or this raises `KeyError`, the same
    fail-fast contract as a missing i18n format param (`fitme.i18n.t`).

    The loader's own parameters are named `template_name`/`version` rather than the more
    obvious `name` deliberately: a prompt is free to use `$name` as one of its own
    placeholders (e.g. a future prompt version rendering a user's display context) without
    colliding with `**params`.
    """
    versions = _available_versions(template_name)
    if not versions:
        raise PromptNotFoundError(
            f"no prompt template named {template_name!r} in {_PROMPTS_SUBDIR}/"
        )
    chosen = max(versions) if version is None else version
    item = versions.get(chosen)
    if item is None:
        raise PromptNotFoundError(
            f"prompt {template_name!r} has no version {chosen}; available: {sorted(versions)}"
        )
    raw_text = item.read_text(encoding="utf-8")
    text = string.Template(raw_text).substitute(**params) if params else raw_text
    return RenderedPrompt(template_name=template_name, version=chosen, text=text)
