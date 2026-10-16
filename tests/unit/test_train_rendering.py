"""The workout block keyboard: a YouTube search link per exercise comes first, built from the
catalog name only (no user data, no server-side call)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fitme.bot.train_rendering import block_markup, logged_item_text
from fitme.catalog import load_catalog
from fitme.domain.models import Block, Load, Prescription
from fitme.domain.results import SetResult
from fitme.i18n import t
from fitme.services.training import BlockView, LoggedItem


def _item(exercise_id: str, load: Load) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=3, reps_min=5, reps_max=8, load=load, rest_seconds=90
    )


def _view(block: Block) -> BlockView:
    return BlockView(
        session_id=1, index=0, total=3, workout_key="A", title="Full body", block=block
    )


def _query(url: str | None) -> str:
    assert url is not None
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "www.youtube.com", "/results")
    return parse_qs(parsed.query)["search_query"][0]


def test_the_search_link_comes_first_with_the_name_in_the_users_language() -> None:
    block = Block(kind="single", items=[_item("barbell_back_squat", Load(kind="kg", kg=60))])
    rows = block_markup(_view(block), catalog=load_catalog(), lang="ru").inline_keyboard
    first, second = rows[0][0], rows[1][0]
    assert first.text == t("workout.youtube_button", "ru")
    assert first.callback_data is None
    assert _query(first.url) == "Приседания со штангой на спине техника"
    assert second.text == t("workout.according_to_plan_button", "ru")


def test_a_superset_gets_one_named_link_per_exercise() -> None:
    block = Block(
        kind="superset",
        items=[
            _item("barbell_back_squat", Load(kind="kg", kg=60)),
            _item("pushup", Load(kind="bodyweight")),
        ],
    )
    rows = block_markup(_view(block), catalog=load_catalog(), lang="en").inline_keyboard
    links = [row[0] for row in rows if row[0].url is not None]
    assert [_query(link.url) for link in links] == [
        "Barbell back squat technique",
        "Push-up technique",
    ]
    assert links[0].text == t("workout.youtube_button_named", "en", name="Barbell back squat")
    assert rows[2][0].text == t("workout.according_to_plan_button", "en")


def test_a_calibration_block_still_offers_the_link() -> None:
    block = Block(kind="single", items=[_item("dumbbell_bench_press", Load(kind="calibration"))])
    rows = block_markup(_view(block), catalog=load_catalog(), lang="en").inline_keyboard
    assert rows[0][0].url is not None
    # No ✅ (nothing to confirm) and no ✏️ (ADR 0013): the weight is written as a message.
    assert [row[0].text for row in rows[1:]] == [
        t("workout.skip_button", "en"),
        t("workout.pain_button", "en"),
    ]


def test_the_catalog_search_term_replaces_a_descriptive_name() -> None:
    block = Block(kind="single", items=[_item("bodyweight_table_row", Load(kind="bodyweight"))])
    rows = block_markup(_view(block), catalog=load_catalog(), lang="ru").inline_keyboard
    assert _query(rows[0][0].url) == "австралийские подтягивания под столом"


def test_every_search_term_is_non_blank() -> None:
    for exercise in load_catalog().exercise:
        assert all(term.strip() for term in exercise.search.values()), exercise.id


def test_logged_sets_are_one_compact_line_per_exercise() -> None:
    """ADR 0013: what the assistant stored, always shown, one line each."""
    catalog = load_catalog()

    def line(exercise_id: str, *sets: SetResult, lang: str = "ru") -> str:
        return logged_item_text(
            LoggedItem(exercise_id, catalog.by_id(exercise_id), tuple(sets)), lang
        )

    squat = "barbell_back_squat"
    same = [SetResult(set_index=i, reps=9, load_kg=40.0) for i in (1, 2, 3, 4)]
    assert line(squat, *same, lang="en") == "✓ Barbell back squat: 4 × 9 @ 40 kg"
    mixed = [*same[:2], SetResult(set_index=3, reps=8, load_kg=40.0)]
    skipped = SetResult(set_index=4, skipped=True)
    assert line(squat, *mixed, skipped, lang="en") == "✓ Barbell back squat: 9, 9, 8, — @ 40 kg"
    heavier = [same[0], SetResult(set_index=2, reps=8, load_kg=42.5)]
    assert line(squat, *heavier, lang="en") == "✓ Barbell back squat: 9 @ 40 kg, 8 @ 42.5 kg"
    none = [SetResult(set_index=i, skipped=True) for i in (1, 2)]
    assert line(squat, *none).endswith(": пропущено")
    plank = [SetResult(set_index=i, seconds=120) for i in (1, 2, 3)]
    assert line("plank", *plank, lang="en") == "✓ Plank: 3 × 2 min"
    pair = [SetResult(set_index=1, reps=10, load_kg=12.5)]
    assert "12.5" in line("dumbbell_bench_press", *pair, lang="en")
