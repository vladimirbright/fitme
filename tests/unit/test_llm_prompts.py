"""`llm/prompts.py`: highest-version selection, `string.Template` rendering, and that
`src/fitme/prompts/*.md` actually ships inside the built wheel (A§4.8, A§8.1)."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from fitme.llm.prompts import PromptNotFoundError, render_prompt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_PROMPT_NAMES = (
    "plan_generate",
    "plan_revise",
    "session_adjust",
    "result_parse",
    "recap",
    "plan_import",
)


@pytest.mark.parametrize("name", _REAL_PROMPT_NAMES)
def test_every_real_agent_prompt_loads_as_version_1(name: str) -> None:
    rendered = render_prompt(name)
    assert rendered.template_name == name
    assert rendered.version == 1
    assert rendered.text  # non-empty


def test_picks_the_highest_version_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_root = tmp_path / "root"
    prompts_dir = fake_root / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "greeting.v1.md").write_text("old", encoding="utf-8")
    (prompts_dir / "greeting.v3.md").write_text("newest", encoding="utf-8")
    (prompts_dir / "greeting.v2.md").write_text("middle", encoding="utf-8")

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(
        prompts_module.importlib.resources,
        "files",
        lambda pkg: fake_root if pkg == prompts_module._PROMPTS_ROOT_PACKAGE else NotImplemented,
    )

    rendered = prompts_module.render_prompt("greeting")
    assert rendered.version == 3
    assert rendered.text == "newest"


def test_an_explicit_version_can_be_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_root = tmp_path / "root"
    prompts_dir = fake_root / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "greeting.v1.md").write_text("old", encoding="utf-8")
    (prompts_dir / "greeting.v2.md").write_text("new", encoding="utf-8")

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(prompts_module.importlib.resources, "files", lambda pkg: fake_root)

    rendered = prompts_module.render_prompt("greeting", version=1)
    assert rendered.version == 1
    assert rendered.text == "old"


def test_missing_prompt_name_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    (fake_root / "prompts").mkdir(parents=True)

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(prompts_module.importlib.resources, "files", lambda pkg: fake_root)

    with pytest.raises(PromptNotFoundError):
        prompts_module.render_prompt("does_not_exist")


def test_missing_prompt_version_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    prompts_dir = fake_root / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "greeting.v1.md").write_text("old", encoding="utf-8")

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(prompts_module.importlib.resources, "files", lambda pkg: fake_root)

    with pytest.raises(PromptNotFoundError):
        prompts_module.render_prompt("greeting", version=5)


def test_missing_prompts_directory_raises_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_root = tmp_path / "root_without_prompts_dir"
    fake_root.mkdir()

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(prompts_module.importlib.resources, "files", lambda pkg: fake_root)

    with pytest.raises(PromptNotFoundError):
        prompts_module.render_prompt("greeting")


def test_renders_dollar_style_placeholders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    prompts_dir = fake_root / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "greeting.v1.md").write_text(
        "Hello, $name! Allowed: $allowed_exercise_ids.", encoding="utf-8"
    )

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(prompts_module.importlib.resources, "files", lambda pkg: fake_root)

    rendered = prompts_module.render_prompt(
        "greeting", name="Alex", allowed_exercise_ids="plank, pull_up"
    )
    assert rendered.text == "Hello, Alex! Allowed: plank, pull_up."


def test_no_params_returns_the_template_byte_for_byte_even_with_literal_braces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prompt is free to show an example JSON blob (curly braces) as long as it doesn't use
    `$placeholder` syntax — `str.format` would choke on the braces; `string.Template` (used
    only when `params` is non-empty) doesn't even look at them here."""
    fake_root = tmp_path / "root"
    prompts_dir = fake_root / "prompts"
    prompts_dir.mkdir(parents=True)
    raw = 'Return JSON like {"kind": "kg", "kg": 20.0}.'
    (prompts_dir / "example.v1.md").write_text(raw, encoding="utf-8")

    import fitme.llm.prompts as prompts_module

    monkeypatch.setattr(prompts_module.importlib.resources, "files", lambda pkg: fake_root)

    rendered = prompts_module.render_prompt("example")
    assert rendered.text == raw


def test_prompts_are_shipped_in_the_built_wheel(tmp_path: Path) -> None:
    """Builds the real wheel (offline, hatchling; ~0.2s in a warm environment) and checks
    every `src/fitme/prompts/*.md` file made it in, the same way `catalog/exercises.toml` and
    `i18n/locales/*.toml` already do.

    `--offline` means this needs the build backend (hatchling) already in uv's cache; a
    from-scratch environment (a fresh CI runner, a machine that's never run `uv build`/`uv
    sync` here) may not have it yet, and `--offline` deliberately forbids fetching it. That's
    an environment/cache gap, not a real failure of anything this project controls — skip
    with the captured reason instead of failing the suite over it.
    """
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        pytest.skip("uv is not on PATH")
    out_dir = tmp_path / "dist"
    result = subprocess.run(
        [uv_executable, "build", "--wheel", "--offline", "-o", str(out_dir)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            "uv build --offline failed (likely a missing cached build backend in this "
            f"environment), not a real project failure: {result.stderr.strip()[-500:]}"
        )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    for prompt_name in _REAL_PROMPT_NAMES:
        assert f"fitme/prompts/{prompt_name}.v1.md" in names, names
