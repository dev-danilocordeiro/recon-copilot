"""Command line: generate data, propose actions for exceptions.

recon-copilot generate --seed 7 --out data/day1        # a day's data, all scenarios
recon-copilot generate --golden --out evals/golden     # rebuild golden inputs
recon-copilot propose data/day1 --out proposals/       # read-only; never writes the ledger
recon-copilot approve proposals/EXC-0002.json --data-dir data/day1 --approver "Ana"
recon-copilot eval --token-ceiling 2000000 [--cases G001 G002] [--check]
recon-copilot baseline evals/reports/A.json evals/reports/B.json evals/reports/C.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import anthropic

from datagen.generate import generate_golden, generate_mixed
from evals.baseline import BASELINE_PATH, check, compute_baseline, load_baseline
from evals.run import GOLDEN_DIR, REPORTS_DIR, GoldenCase, load_golden, run_eval, write_report
from recon_copilot.agent.budget import TokenBudget
from recon_copilot.agent.llm import AnthropicLLM
from recon_copilot.agent.loop import Agent
from recon_copilot.approval import review_and_execute
from recon_copilot.audit import AuditLog
from recon_copilot.config import Settings, load_settings
from recon_copilot.domain import ReconException
from recon_copilot.mcp_client import LedgerClient, connect_stdio
from recon_copilot.records import run_to_dict


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


async def run_golden(
    settings: Settings, cases: list[GoldenCase], ceiling: int | None
) -> dict[str, Any]:
    llm = AnthropicLLM(anthropic.AsyncAnthropic(), fallbacks=settings.fallbacks)

    def connect(gc: GoldenCase) -> AbstractAsyncContextManager[LedgerClient]:
        return connect_stdio(settings.stub_command(str(gc.dataset_path)))

    return await run_eval(cases, settings, llm, connect, ceiling)


def cmd_eval(args: argparse.Namespace) -> int:
    settings = load_settings()
    ceiling = args.token_ceiling or settings.eval_token_ceiling
    if ceiling is None:
        print(
            "refusing to run without a token ceiling: set RECON_EVAL_TOKEN_CEILING "
            "or pass --token-ceiling"
        )
        return 2
    cases = load_golden(args.golden_dir, args.cases or None)
    report = asyncio.run(run_golden(settings, cases, ceiling))
    path = write_report(report, args.reports_dir)
    s = report["summary"]
    print(f"report: {path}")
    print(
        f"correct {s['correct']}/{s['cases']}, safety failures {s['safety_failures']}, "
        f"not run {s['not_run']}, cost {s['cost_usd']}"
    )
    problems = check(report, load_baseline())
    for p in problems:
        print(f"FAIL: {p}")
    if args.check and problems:
        return 1
    return 0 if s["complete"] else 3


def cmd_baseline(args: argparse.Namespace) -> int:
    reports = [json.loads(p.read_text()) for p in args.reports]
    baseline = compute_baseline(reports, [str(p) for p in args.reports])
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
    print(json.dumps(baseline, indent=2))
    return 0


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

    e = sub.add_parser("eval", help="run the golden set and write a report")
    e.add_argument("--cases", nargs="*", default=[], help="case IDs (default: all)")
    e.add_argument("--token-ceiling", type=int, default=None)
    e.add_argument("--check", action="store_true", help="exit 1 if the regression gate fails")
    e.add_argument("--golden-dir", type=Path, default=GOLDEN_DIR)
    e.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)

    b = sub.add_parser("baseline", help="set the accuracy baseline from full eval reports")
    b.add_argument("reports", type=Path, nargs="+")

    args = parser.parse_args(argv)
    if args.cmd == "eval":
        return cmd_eval(args)
    if args.cmd == "baseline":
        return cmd_baseline(args)
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
