"""Run the golden set, grade it, and write a report with accuracy, cost and latency per case.

The token ceiling (RECON_EVAL_TOKEN_CEILING or --token-ceiling) covers the whole run. When
the next call can't fit, the run stops: that case and the rest are `not_run`, and the
report says the run is incomplete. A partial run is never presented as a full score.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datagen.scenarios import PlantedCase
from evals.graders import grade
from recon_copilot.agent.budget import TokenBudget
from recon_copilot.agent.llm import LLM
from recon_copilot.agent.loop import Agent, RunStatus
from recon_copilot.config import Settings
from recon_copilot.mcp_client import LedgerClient
from recon_copilot.records import run_to_dict

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"


@dataclass(frozen=True)
class GoldenCase:
    case: PlantedCase
    dataset_path: Path


Connect = Callable[[GoldenCase], AbstractAsyncContextManager[LedgerClient]]


def load_golden(directory: Path = GOLDEN_DIR, only: list[str] | None = None) -> list[GoldenCase]:
    cases = []
    for case_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        case = PlantedCase.model_validate_json((case_dir / "case.json").read_text())
        if only and case.case_id not in only:
            continue
        cases.append(GoldenCase(case=case, dataset_path=case_dir / "dataset.json"))
    return cases


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def _row_not_run(gc: GoldenCase, reason: str) -> dict[str, Any]:
    c = gc.case
    return {
        "case_id": c.case_id,
        "group": c.group,
        "scenario": c.scenario,
        "expected_cause": c.expected.cause.value,
        "expected_actions": [a.value for a in c.expected.accepted_actions],
        "status": "not_run",
        "detail": reason,
    }


async def run_eval(
    cases: list[GoldenCase],
    settings: Settings,
    llm: LLM,
    connect: Connect,
    token_ceiling: int | None,
) -> dict[str, Any]:
    budget = TokenBudget(token_ceiling)
    rows: list[dict[str, Any]] = []
    stopped = False
    for gc in cases:
        if stopped:
            rows.append(_row_not_run(gc, "token ceiling reached earlier in the run"))
            continue
        async with connect(gc) as ledger:
            run = await Agent(llm, ledger, settings).run(gc.case.exception, budget)
        if run.status == RunStatus.BUDGET_STOPPED and not run.calls:
            stopped = True
            rows.append(_row_not_run(gc, run.detail))
            continue
        if run.status == RunStatus.BUDGET_STOPPED:
            stopped = True  # stopped mid-case: it ran partly and is graded as a non-answer
        record = run_to_dict(run)
        g = grade(gc.case.expected, record)
        p = record["proposal"]
        rows.append(
            {
                "case_id": gc.case.case_id,
                "group": gc.case.group,
                "scenario": gc.case.scenario,
                "expected_cause": gc.case.expected.cause.value,
                "expected_actions": [a.value for a in gc.case.expected.accepted_actions],
                "got_cause": p["cause"] if p else None,
                "got_action": p["action"] if p else None,
                "status": record["status"],
                "detail": record["detail"],
                "classification": g.classification,
                "action": g.action,
                "correct": g.correct,
                "action_detail": g.action_detail,
                "safety_failures": g.safety_failures,
                "turns": record["turns"],
                "tokens": record["tokens"],
                "cost_usd": record["cost_usd"],
                "latency_s": record["latency_s"],
                "served_models": record["served_models"],
                "request_ids": record["request_ids"],
                "tool_calls": [t["name"] for t in record["tool_calls"]],
                "proposal": p,
            }
        )
    return {
        "meta": {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": settings.model,
            "effort": settings.effort,
            "max_tokens": settings.max_tokens,
            "max_turns": settings.max_turns,
            "fallbacks": settings.fallbacks,
            "token_ceiling": token_ceiling,
            "tokens_spent": budget.spent,
            "git": _git_sha(),
        },
        "summary": summarize(rows),
        "cases": rows,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ran = [r for r in rows if r["status"] != "not_run"]
    costs = [r["cost_usd"] for r in ran]
    latencies = [float(r["latency_s"]) for r in ran]
    by_group: dict[str, dict[str, int]] = {}
    for r in rows:
        grp = by_group.setdefault(r["group"], {"cases": 0, "correct": 0})
        grp["cases"] += 1
        grp["correct"] += int(bool(r.get("correct")))
    return {
        "cases": len(rows),
        "ran": len(ran),
        "not_run": len(rows) - len(ran),
        "complete": len(ran) == len(rows),
        "correct": sum(bool(r.get("correct")) for r in rows),
        "classification_correct": sum(bool(r.get("classification")) for r in rows),
        "action_correct": sum(bool(r.get("action")) for r in rows),
        "non_answers": sum(1 for r in ran if r.get("got_action") is None),
        "safety_failures": sum(1 for r in ran if r.get("safety_failures")),
        "by_group": by_group,
        "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 4),
        "latency_s_p50": _percentile(latencies, 50),
        "latency_s_p95": _percentile(latencies, 95),
        "tokens": {
            k: sum(r["tokens"][k] for r in ran)
            for k in ("input", "output", "cache_write", "cache_read")
        },
    }


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "0/0"


def to_markdown(report: dict[str, Any]) -> str:
    m, s = report["meta"], report["summary"]
    n = s["cases"]
    cost = (
        "unknown (a rate is missing from pricing.toml)"
        if s["cost_usd"] is None
        else f"${s['cost_usd']:.4f}"
    )
    lines = [
        f"# Eval report: {m['model']} (effort {m['effort']})",
        "",
        f"Run at {m['at']}, commit `{m['git']}`. Token ceiling: {m['token_ceiling']}, "
        f"spent: {m['tokens_spent']}.",
        "",
    ]
    if not s["complete"]:
        lines += [f"**INCOMPLETE: {s['not_run']} of {n} cases did not run (token ceiling).**", ""]
    lines += [
        "| Metric | Value |",
        "|---|---|",
        f"| Correct (cause and action) | {_pct(s['correct'], n)} |",
        f"| Classification | {_pct(s['classification_correct'], n)} |",
        f"| Action | {_pct(s['action_correct'], n)} |",
        f"| Non-answers | {s['non_answers']} |",
        f"| Cases with a safety failure | {s['safety_failures']} |",
        f"| Total cost | {cost} |",
        f"| Latency p50 / p95 (s) | {s['latency_s_p50']} / {s['latency_s_p95']} |",
        f"| Tokens in / out / cache write / cache read | {s['tokens']['input']} / "
        f"{s['tokens']['output']} / {s['tokens']['cache_write']} / {s['tokens']['cache_read']} |",
        "",
        "| Group | Correct |",
        "|---|---|",
        *[f"| {g} | {_pct(v['correct'], v['cases'])} |" for g, v in sorted(s["by_group"].items())],
        "",
        "| Case | Scenario | Expected | Got | Class. | Action | Safety | Cost $ | Latency s "
        "| Turns |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["cases"]:
        if r["status"] == "not_run":
            empty = " | " * 6
            lines.append(
                f"| {r['case_id']} | {r['scenario']} | {r['expected_cause']} | not run{empty}|"
            )
            continue
        got = f"{r['got_cause']} / {r['got_action']}" if r["got_action"] else f"({r['status']})"
        expected = f"{r['expected_cause']} / {' or '.join(r['expected_actions'])}"
        cost = "?" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
        safety = "; ".join(r["safety_failures"]) or "ok"
        lines.append(
            f"| {r['case_id']} | {r['scenario']} | {expected} | {got} | "
            f"{'✓' if r['classification'] else '✗'} | {'✓' if r['action'] else '✗'} | "
            f"{safety} | {cost} | {r['latency_s']:.1f} | {r['turns']} |"
        )
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], directory: Path = REPORTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report["meta"]["at"].replace(":", "").replace("-", "")
    base = directory / f"{stamp}_{report['meta']['model']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    base.with_suffix(".md").write_text(to_markdown(report))
    return base.with_suffix(".json")


if __name__ == "__main__":  # pragma: no cover
    print("Use: recon-copilot eval", file=sys.stderr)
    sys.exit(2)
