"""A§7.2 guard layering: an LLM safety signal can only *add* a halt/refusal on top of a
deterministic guard's verdict. It can never remove one a deterministic guard already
produced (AGENTS.md §2's guards are invariants; a model's opinion doesn't get a veto)."""

from __future__ import annotations

from fitme.domain.guard_types import GuardVerdict

_RULE = "layering.llm_safety_signal"


def combine_with_llm_signal(verdict: GuardVerdict, llm_safety_signal: bool) -> GuardVerdict:
    """Combine one deterministic guard verdict with an LLM-reported `safety_signal`.

    - If `verdict.ok` is already `False`, it is returned unchanged: `llm_safety_signal=False`
      can never clear a deterministic block.
    - If `verdict.ok` is `True` and `llm_safety_signal` is `True`, the combined verdict
      blocks: the LLM's signal added a halt the deterministic guard didn't itself find.
    - If `verdict.ok` is `True` and `llm_safety_signal` is `False`, the deterministic verdict
      is returned unchanged: nothing to add.
    """
    if not verdict.ok:
        return verdict
    if llm_safety_signal:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"LLM safety signal added a halt on top of a passing '{verdict.rule}' verdict",
        )
    return verdict
