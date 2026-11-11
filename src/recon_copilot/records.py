"""Serialisable record of one agent run (what `propose` writes and the evals grade)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from recon_copilot.agent.loop import AgentRun
from recon_copilot.pricing import cost_usd


def run_to_dict(run: AgentRun) -> dict[str, Any]:
    usages = run.usages
    return {
        "exception_id": run.exception_id,
        "status": run.status.value,
        "detail": run.detail,
        "proposal": run.proposal.model_dump(mode="json") if run.proposal else None,
        "proposal_digest": run.proposal.digest() if run.proposal else None,
        "downgraded": run.downgraded,
        "downgrade_reason": run.downgrade_reason,
        "turns": len(run.calls),
        "served_models": sorted({c.served_model for c in run.calls}),
        "tokens": {
            "input": sum(u.input_tokens for u in usages),
            "output": sum(u.output_tokens for u in usages),
            "cache_write": sum(u.cache_creation_input_tokens for u in usages),
            "cache_read": sum(u.cache_read_input_tokens for u in usages),
        },
        "cost_usd": cost_usd(usages),
        "latency_s": round(run.latency_s, 3),
        "tool_calls": [asdict(t) for t in run.tool_calls],
        "refused_tool_calls": run.refused_tool_calls,
        "request_ids": [c.request_id for c in run.calls],
    }
