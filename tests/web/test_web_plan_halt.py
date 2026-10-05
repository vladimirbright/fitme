"""A§6.3/A§7.4: the stop-word scan runs on the web textarea exactly like the bot's, *before*
any LLM call — a hit halts and the model is never invoked."""

from __future__ import annotations

import json

from conftest import SentCode, get_csrf_token, login
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_web_plans import make_plan, seed_confirmed_plan, seed_profile

from fitme.db.connection import Database
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.training import list_open_health_holds
from fitme.llm.agents import plan_generate_agent
from fitme.services.llm_runtime import LlmRuntime


async def test_stop_word_in_revise_textarea_halts_without_calling_the_llm(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))

    await login(client, sent_codes)
    page = await client.get(f"/app/plans/{plan_id}/revise")
    token = get_csrf_token(page.text)

    response = await client.post(
        f"/app/plans/{plan_id}/revise",
        data={"csrf_token": token, "request_text": "my chest hurts today, sharp pain"},
    )
    assert response.status_code == 200
    assert "halt" in response.text.lower() or "stop" in response.text.lower()

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert holds  # a health_hold was opened
    assert any(d.kind == "session_halt" for d in decisions)
    assert not any(d.kind in ("plan_revise", "plan_generate") for d in decisions)


async def test_new_plan_form_asks_for_guidance_only_when_plans_exist(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    await login(client, sent_codes)
    page = await client.get("/app/plans/new")
    assert 'name="guidance"' not in page.text

    await seed_confirmed_plan(db, user_id, make_plan(60.0))
    page = await client.get("/app/plans/new")
    assert 'name="guidance"' in page.text


async def test_stop_word_in_new_plan_guidance_halts_without_calling_the_llm(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await login(client, sent_codes)
    page = await client.get("/app/plans/new")
    token = get_csrf_token(page.text)

    response = await client.post(
        "/app/plans/new",
        data={"csrf_token": token, "guidance": "lighter please, sharp pain in my knee"},
    )
    assert response.status_code == 200
    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert holds
    assert not any(d.kind == "plan_generate" for d in decisions)


async def test_new_plan_guidance_reaches_the_generator(
    app: FastAPI, client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        prompts.extend(str(p.content) for p in request.parts if p.part_kind == "user-prompt")
        tool = next(t.name for t in info.output_tools if t.name.endswith("_Plan"))
        plan = make_plan(60.0)
        plan.name = "Gym plan"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=plan.model_dump(mode="json"))]
        )

    settings = app.state.llm.settings
    app.state.llm = LlmRuntime(
        settings=settings,
        agent_factories={
            "plan_generate": lambda model: plan_generate_agent(
                FunctionModel(respond, model_name=str(model))
            )
        },
    )
    await seed_profile(db, user_id)
    await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await login(client, sent_codes)
    token = get_csrf_token((await client.get("/app/plans/new")).text)

    response = await client.post(
        "/app/plans/new", data={"csrf_token": token, "guidance": "a gym version"}
    )
    assert response.status_code == 200
    payload = json.loads(prompts[-1])
    assert payload["user_request"] == "a gym version"
    assert len(payload["existing_plans"]) == 1
