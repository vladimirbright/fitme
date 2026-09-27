# ADR 0001 — LLM harness: pydantic-ai

Status: accepted (2026-09-27)

## Context

The LLM does five narrow jobs: generate a plan, revise a plan, adjust a session, parse free
text results, and write a recap. Each job must return **structured output** that
deterministic guards can check (AGENTS.md §2). There is no retrieval, no long-running agent
and no tool-heavy orchestration. The project must stay readable for the person running it
(AGENTS.md §9).

## Options

| | pydantic-ai | LangChain / LangGraph | Raw provider SDK |
|---|---|---|---|
| Typed output | Native: `output_type=PlanProposal`, union types for refusal | Possible via parsers or structured-output wrappers, more layers | Manual JSON schema and validation |
| Validation retry | Built in (the model is re-asked on schema failure) | Available, more config | Hand-written |
| Provider switch | Model string (`anthropic:...`, `openai:...`, local OpenAI-compatible) | Yes | Rewrite |
| Token usage | `result.usage()` | Callbacks | Per SDK |
| Test doubles | `TestModel`, `FunctionModel` | Fake LLMs exist, heavier | Mock HTTP |
| Dependency weight | Small (slim + one provider extra) | Large, fast-moving API surface | Smallest |
| Async | Native | Yes | Yes |

## Decision

Use **pydantic-ai** (`pydantic-ai-slim[<provider>]`). The model comes from
`FITME_LLM_MODEL`.

## Consequences

- Guards stay **outside** the harness. pydantic-ai's schema-retry only checks shape. Safety
  checks run in `guards/` after the agent returns, and `guards/` never imports pydantic-ai.
  Swapping the harness later touches only `llm/`.
- Domain models (`domain/`) are shared by the LLM output, the DB JSON and the guards, with
  no mapping layer.
- Revisit this if the project ever needs multi-step tool orchestration. LangGraph would be
  the candidate then.
