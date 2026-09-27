"""`config/content.py::content_version` (A§4.8, IMPLEMENTATION_PLAN M3, round 2): a short
hash over the guard-relevant reference-data *and* guard-logic source files.

Every test here works against fake package directories (via monkeypatching
`importlib.resources.files`), so none of them ever touch the real shipped files — including
the locale-independence test, which the review round explicitly called out: it must not write
to the real `src/fitme/i18n/locales/`.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from fitme.config import content as content_module


def _make_dir(base: pathlib.Path, name: str, files: dict[str, str]) -> pathlib.Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return directory


@pytest.fixture
def fake_resources(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, pathlib.Path]]:
    """Fakes the five package roots `content_version` reads, each an ordinary directory under
    `tmp_path`. `root` stands in for the top-level `fitme` package: `root / "prompts"`
    doesn't exist yet, matching the real repo before M4."""
    catalog_dir = _make_dir(tmp_path, "catalog", {"exercises.toml": "exercise v1"})
    stop_words_dir = _make_dir(tmp_path, "stop_words", {"en.txt": "pain", "ru.txt": "боль"})
    guards_dir = _make_dir(
        tmp_path,
        "guards",
        {
            "ceiling.py": "def check_ceiling(): ...\n",
            "screening.py": "def exercise_allowed(): ...\n",
            # a subdirectory, to prove the walk recurses
            "stop_words/__init__.py": "def scan(): ...\n",
            # must be skipped even though it ends in .py
            "__pycache__/ceiling.cpython-313.py": "garbage",
        },
    )
    domain_dir = _make_dir(
        tmp_path,
        "domain",
        {
            "catalog.py": "class Exercise: ...\n",
            "enums.py": "class Equipment: ...\n",
            "models.py": "class Plan: ...\n",
        },
    )
    services_dir = _make_dir(tmp_path, "services", {"loads.py": "def next_load(): ...\n"})
    root_dir = tmp_path / "root"
    root_dir.mkdir()

    mapping = {
        content_module._CATALOG_PACKAGE: catalog_dir,
        content_module._STOP_WORDS_PACKAGE: stop_words_dir,
        content_module._GUARDS_PACKAGE: guards_dir,
        content_module._DOMAIN_PACKAGE: domain_dir,
        content_module._SERVICES_PACKAGE: services_dir,
        content_module._ROOT_PACKAGE: root_dir,
    }
    monkeypatch.setattr(content_module.importlib.resources, "files", mapping.__getitem__)
    yield {
        "catalog": catalog_dir,
        "stop_words": stop_words_dir,
        "guards": guards_dir,
        "domain": domain_dir,
        "services": services_dir,
        "root": root_dir,
    }


def test_content_version_is_12_hex_chars(fake_resources: dict[str, pathlib.Path]) -> None:
    version = content_module.content_version()
    assert len(version) == 12
    int(version, 16)  # raises ValueError if it isn't hex


def test_content_version_changes_when_the_catalog_file_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["catalog"] / "exercises.toml").write_text("exercise v2", encoding="utf-8")
    after = content_module.content_version()
    assert before != after


def test_content_version_changes_when_a_stop_word_file_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["stop_words"] / "en.txt").write_text("pain\ndizzy", encoding="utf-8")
    after = content_module.content_version()
    assert before != after


def test_content_version_changes_when_a_guard_py_file_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["guards"] / "ceiling.py").write_text(
        "def check_ceiling():\n    return True\n", encoding="utf-8"
    )
    after = content_module.content_version()
    assert before != after


def test_content_version_picks_up_a_nested_guard_py_file(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["guards"] / "stop_words" / "__init__.py").write_text(
        "def scan():\n    return None\n", encoding="utf-8"
    )
    after = content_module.content_version()
    assert before != after


def test_content_version_ignores_pycache(fake_resources: dict[str, pathlib.Path]) -> None:
    # __pycache__/ceiling.cpython-313.py exists in the fixture and must not be hashed: editing
    # it (or even deleting the whole directory) must not move the hash.
    before = content_module.content_version()
    import shutil

    shutil.rmtree(fake_resources["guards"] / "__pycache__")
    after = content_module.content_version()
    assert before == after


def test_content_version_changes_when_domain_catalog_py_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["domain"] / "catalog.py").write_text(
        "class Exercise:\n    pass\n", encoding="utf-8"
    )
    after = content_module.content_version()
    assert before != after


def test_content_version_changes_when_domain_enums_py_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["domain"] / "enums.py").write_text(
        "class Equipment:\n    pass\n", encoding="utf-8"
    )
    after = content_module.content_version()
    assert before != after


def test_content_version_changes_when_domain_models_py_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    (fake_resources["domain"] / "models.py").write_text("class Plan:\n    pass\n", encoding="utf-8")
    after = content_module.content_version()
    assert before != after


def test_content_version_changes_when_services_loads_py_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    """A§4.8's M4-round update: `services/loads.py` decides every kg value a guard ever
    checks, so editing it (even in this fake, disconnected tree) must move the hash."""
    before = content_module.content_version()
    (fake_resources["services"] / "loads.py").write_text(
        "def next_load():\n    pass\n", encoding="utf-8"
    )
    after = content_module.content_version()
    assert before != after


def test_content_version_changes_when_content_moves_between_files(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    """Moving the same total content between two files must change the hash: each file
    contributes `path + length + bytes`, so it isn't just a hash of the concatenated bytes
    (which content redistribution could otherwise leave unchanged)."""
    (fake_resources["guards"] / "ceiling.py").write_text("AAA", encoding="utf-8")
    (fake_resources["guards"] / "screening.py").write_text("BBB", encoding="utf-8")
    before = content_module.content_version()

    (fake_resources["guards"] / "ceiling.py").write_text("AAABBB", encoding="utf-8")
    (fake_resources["guards"] / "screening.py").write_text("", encoding="utf-8")
    after = content_module.content_version()

    assert before != after


def test_content_version_is_stable_when_nothing_changes(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    assert content_module.content_version() == content_module.content_version()


def test_content_version_ignores_a_missing_prompts_directory(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    # root_dir/prompts doesn't exist in the fixture; must not raise.
    assert content_module.content_version()


def test_content_version_picks_up_a_new_prompt_file(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    before = content_module.content_version()
    prompts_dir = fake_resources["root"] / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "plan_generate.v1.md").write_text("prompt text", encoding="utf-8")
    after = content_module.content_version()
    assert before != after


def test_no_hashed_path_starts_with_i18n(fake_resources: dict[str, pathlib.Path]) -> None:
    """Locales are excluded from the hash (A§4.8): they don't affect what the guards allow.
    Structural, against the real `_hashed_files()` walk (via the fake resource roots this
    fixture wires up) rather than against a copy of the real `src/fitme/i18n/locales/` — no
    locale package is even in the mapping `_hashed_files()` reads from, so this asserts the
    absence directly instead of tautologically re-deriving "locales aren't hashed" from a
    fixture built to exclude them."""
    paths = [path for path, _item in content_module._hashed_files()]
    assert paths, "expected the fake fixture to produce at least one hashed path"
    assert not any(path.startswith("i18n/") for path in paths), paths


def test_every_hashed_path_matches_the_a4_8_allow_list(
    fake_resources: dict[str, pathlib.Path],
) -> None:
    """A§4.8 names exactly six kinds of guard-relevant content: `catalog/exercises.toml`,
    `guards/stop_words/*.txt`, `prompts/*.md`, `guards/**/*.py`, the three `domain/*.py`
    logic files, and `services/loads.py`. Every path `_hashed_files()` yields must fall into
    one of those — a stray path (e.g. a locale file, or something under a package this
    function was never told to walk) would be a silent scope-creep bug."""
    allowed_exact = {
        "catalog/exercises.toml",
        "domain/catalog.py",
        "domain/enums.py",
        "domain/models.py",
        "services/loads.py",
    }
    for path, _item in content_module._hashed_files():
        if path in allowed_exact:
            continue
        is_guard_file = path.startswith("guards/") and path.endswith((".py", ".txt"))
        is_prompt_file = path.startswith("prompts/") and path.endswith(".md")
        assert is_guard_file or is_prompt_file, f"{path!r} is not in the A§4.8 allow list"
