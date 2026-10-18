"""The reference library is searchable only after website authentication."""

from __future__ import annotations

from pathlib import Path

from httpx import AsyncClient

from fitme.config.settings import Settings
from fitme.services.reference_exercises import reference_path
from tests.web.conftest import SentCode, login


async def test_library_requires_login_and_displays_local_toml(
    client: AsyncClient,
    user_id: int,
    sent_codes: list[SentCode],
    settings: Settings,
    tmp_path: Path,
) -> None:
    settings.db_path = tmp_path / "fitme.db"
    reference_path(settings.db_path).write_text(
        """planning_status = "reference_only"

[[exercise]]
id = "0001"
category = "back"
equipment = "dumbbell"
target = "lats"
muscle_group = "arms"
secondary_muscles = []
review_status = "safety_review_needed"

[exercise.names]
en = "Test row"
es = "Fila de prueba"
it = "Riga di prova"
tr = "Test hareketi"
ru = "Тестовое упражнение"
zh = "测试动作"

[exercise.name_sources]
en = "test"
es = "test"
it = "test"
tr = "test"
ru = "test"
zh = "test"

[exercise.instructions]
en = "Move slowly."
es = "Muévete despacio."
it = "Muoviti lentamente."
tr = "Yavaş hareket et."
ru = "Двигайтесь медленно."
zh = "缓慢移动。"
""",
        encoding="utf-8",
    )
    response = await client.get("/app/exercises", follow_redirects=False)
    assert response.status_code == 303
    await login(client, sent_codes)
    response = await client.get("/app/exercises?q=Test")
    assert response.status_code == 200
    assert "Test row" in response.text
    assert "1 exercises" in response.text
    detail = await client.get("/app/exercises/0001")
    assert detail.status_code == 200
    assert "Move slowly." in detail.text
    spanish = await client.get("/app/exercises/0001?language=es")
    assert spanish.status_code == 200
    assert 'value="es" selected' in spanish.text
    assert "Fila de prueba" in spanish.text
    assert (await client.get("/app/exercises/9999")).status_code == 404
