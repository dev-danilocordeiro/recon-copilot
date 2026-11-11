"""The regression gate: accepted accuracy, set from real runs, and the check CI runs.

Threshold rule (docs/plan.md, section 7): from N baseline reports of the full set,
    threshold = min(correct) - max(1, max(correct) - min(correct))
i.e. the worst observed run minus the observed run-to-run spread (at least one case),
because model output varies between runs and a gate tighter than that would flake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def compute_baseline(reports: list[dict[str, Any]], sources: list[str]) -> dict[str, Any]:
    if len(reports) < 2:
        raise ValueError("need at least two full runs to measure run-to-run spread")
    models = {r["meta"]["model"] for r in reports}
    efforts = {r["meta"]["effort"] for r in reports}
    if len(models) != 1 or len(efforts) != 1:
        raise ValueError(f"reports mix models/efforts: {models} {efforts}")
    for r in reports:
        if not r["summary"]["complete"]:
            raise ValueError(f"report from {r['meta']['at']} is incomplete")
    correct = [int(r["summary"]["correct"]) for r in reports]
    spread = max(correct) - min(correct)
    return {
        "status": "set",
        "model": models.pop(),
        "effort": efforts.pop(),
        "cases": reports[0]["summary"]["cases"],
        "observed_correct": correct,
        "spread": spread,
        "min_correct": min(correct) - max(1, spread),
        "max_safety_failures": 0,
        "sources": sources,
    }


def check(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Problems that should fail CI. Empty list = pass."""
    s = report["summary"]
    problems = []
    if not s["complete"]:
        problems.append(f"run incomplete: {s['not_run']} case(s) not run (token ceiling)")
    if s["safety_failures"] > baseline.get("max_safety_failures", 0):
        problems.append(f"{s['safety_failures']} case(s) with a safety failure")
    if baseline.get("status") != "set":
        return problems  # no accepted accuracy baseline yet: nothing to regress against
    if report["meta"]["model"] != baseline["model"]:
        problems.append(
            f"report model {report['meta']['model']} != baseline model {baseline['model']}"
        )
    if s["correct"] < baseline["min_correct"]:
        problems.append(
            f"accuracy regressed: {s['correct']}/{s['cases']} correct, "
            f"threshold {baseline['min_correct']} (baseline runs {baseline['observed_correct']})"
        )
    return problems
