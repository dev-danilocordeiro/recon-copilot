"""Append-only JSONL audit log: proposals shown, human decisions, write results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def used_approval_ids(self) -> set[str]:
        return {
            str(e["approval_id"])
            for e in self.events()
            if e["event"] in ("write_applied", "write_failed") and "approval_id" in e
        }
