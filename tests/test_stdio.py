"""The CLI and evals talk to ledger-mcp over stdio; check the stub works that way too."""

from __future__ import annotations

import sys
from pathlib import Path

from datagen.scenarios import build_case
from recon_copilot.mcp_client import connect_stdio


async def test_stub_over_stdio(tmp_path: Path) -> None:
    case, ds = build_case("wrong_reference", seed=9, case_id="X")
    path = tmp_path / "dataset.json"
    path.write_text(ds.model_dump_json())
    cmd = [sys.executable, "-m", "ledger_mcp_stub", "--dataset", str(path)]
    async with connect_stdio(cmd) as ledger:
        rid = case.exception.bank_record_ids[0]
        out = await ledger.call("get_bank_record", {"record_id": rid})
        assert not out.is_error
        assert out.data["record_id"] == rid
