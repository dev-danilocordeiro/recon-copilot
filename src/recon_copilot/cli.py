"""Command line: generate data, propose actions for exceptions.

recon-copilot generate --seed 7 --out data/day1        # a day's data, all scenarios
recon-copilot generate --golden --out evals/golden     # rebuild golden inputs
recon-copilot propose data/day1 --out proposals/       # read-only; never writes the ledger
recon-copilot approve proposals/EXC-0002.json --data-dir data/day1 --approver "Ana"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anthropic

from datagen.generate import generate_golden, generate_mixed
from recon_copilot.agent.budget import TokenBudget
from recon_copilot.agent.llm import AnthropicLLM
from recon_copilot.agent.loop import Agent, AgentRun
from recon_copilot.approval import review_and_execute
from recon_copilot.audit import AuditLog
from recon_copilot.config import Settings, load_settings
from recon_copilot.domain import ReconException
from recon_copilot.mcp_client import connect_stdio
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


def read_exceptions(path: Path) -> list[ReconException]:
    return [
        ReconException.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


async def propose(
    data_dir: Path, out: Path, settings: Settings, exceptions: list[ReconException]
) -> int:
    budget = TokenBudget(settings.eval_token_ceiling)
    llm = AnthropicLLM(anthropic.AsyncAnthropic(), fallbacks=settings.fallbacks)
    command = settings.stub_command(str(data_dir / "dataset.json"))
    async with connect_stdio(command) as ledger:
        agent = Agent(llm, ledger, settings)
        for exc in exceptions:
            run = await agent.run(exc, budget)
            record = run_to_dict(run)
            (out / f"{exc.exception_id}.json").write_text(json.dumps(record, indent=2) + "\n")
            p = run.proposal
            summary = f"{p.cause.value} -> {p.action.value}" if p else run.detail
            print(f"{exc.exception_id}  {run.status.value:<15} {summary}")
    return 0


async def approve(record: dict[str, Any], data_dir: Path, approver: str, audit_path: Path) -> int:
    command = load_settings().stub_command(str(data_dir / "dataset.json"))
    async with connect_stdio(command) as ledger:
        outcome = await review_and_execute(
            record, ledger, AuditLog(audit_path), approver=approver, ask=input, say=print
        )
    print(f"decision: {outcome.decision} {outcome.detail}".rstrip())
    return 0 if outcome.decision in ("approved", "rejected") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recon-copilot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic data")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--golden", action="store_true", help="build golden-set inputs instead")

    p = sub.add_parser("propose", help="run the agent on exceptions (read-only)")
    p.add_argument("data_dir", type=Path, help="directory with dataset.json, exceptions.jsonl")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--only", nargs="*", default=[], help="exception IDs to run")

    a = sub.add_parser("approve", help="review one proposal and apply it if you approve")
    a.add_argument("record", type=Path, help="a run record written by `propose`")
    a.add_argument("--data-dir", type=Path, required=True)
    a.add_argument("--approver", required=True, help="your name, recorded in the audit log")
    a.add_argument("--audit", type=Path, default=Path("audit.jsonl"))

    args = parser.parse_args(argv)
    if args.cmd == "generate":
        cases = generate_golden(args.out) if args.golden else generate_mixed(args.seed, args.out)
        print(f"wrote {len(cases)} cases to {args.out}")
        return 0
    if args.cmd == "propose":
        exceptions = read_exceptions(args.data_dir / "exceptions.jsonl")
        if args.only:
            exceptions = [e for e in exceptions if e.exception_id in args.only]
        args.out.mkdir(parents=True, exist_ok=True)
        return asyncio.run(propose(args.data_dir, args.out, load_settings(), exceptions))
    if args.cmd == "approve":
        if not sys.stdin.isatty():
            print("approve needs an interactive terminal; there is no unattended mode")
            return 1
        record = json.loads(args.record.read_text())
        return asyncio.run(approve(record, args.data_dir, args.approver, args.audit))
    return 2


if __name__ == "__main__":
    sys.exit(main())
