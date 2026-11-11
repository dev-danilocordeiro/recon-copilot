"""Write synthetic datasets to disk: ledger, bank statement, return files, exceptions, answer key.

Return file layout (fixed width, CNAB240-*like*, simplified; not any bank's real layout):

  H  bank(3) file_id(30) covers(8, YYYYMMDD) delivered(8)                     header
  D  record_id(10) settled(8) amount_cents(15, zero padded) reference(20)
     product(6) payer_name(40)                                                detail
  T  count(6) total_cents(18)                                                 trailer
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from datagen.scenarios import GOLDEN_PLAN, SCENARIOS, PlantedCase, build_case, build_mixed
from recon_copilot.domain import BankRecord, Dataset, ReturnFile


def return_file_text(f: ReturnFile, records: list[BankRecord]) -> str:
    lines = [
        f"H{f.bank_code:<3}{f.file_id:<30}{f.covers_settlement_on:%Y%m%d}{f.delivered_on:%Y%m%d}"
    ]
    total = 0
    for r in sorted(records, key=lambda r: r.record_id):
        total += r.amount_cents
        lines.append(
            f"D{r.record_id:<10}{r.settled_on:%Y%m%d}{r.amount_cents:015d}"
            f"{r.reference[:20]:<20}{r.product:<6}{r.payer_name[:40]:<40}"
        )
    lines.append(f"T{len(records):06d}{total:018d}")
    return "\n".join(lines) + "\n"


def write_dataset(ds: Dataset, cases: list[PlantedCase], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "dataset.json").write_text(ds.model_dump_json(indent=2) + "\n")

    by_file: dict[str, list[BankRecord]] = defaultdict(list)
    for r in ds.bank_records:
        by_file[r.file_id].append(r)
    returns = out / "return_files"
    returns.mkdir(exist_ok=True)
    for f in ds.return_files:
        (returns / f"{f.file_id}.txt").write_text(return_file_text(f, by_file[f.file_id]))

    by_bank: dict[str, list[BankRecord]] = defaultdict(list)
    for r in ds.bank_records:
        by_bank[r.bank_code].append(r)
    for bank, recs in sorted(by_bank.items()):
        with (out / f"statement_{bank}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "description", "amount_cents", "reference", "record_id"])
            for r in sorted(recs, key=lambda r: (r.settled_on, r.record_id)):
                w.writerow(
                    [
                        r.settled_on.isoformat(),
                        f"{r.product.upper()} {r.payer_name}",
                        r.amount_cents,
                        r.reference,
                        r.record_id,
                    ]
                )

    with (out / "exceptions.jsonl").open("w") as fh:
        for c in cases:
            fh.write(c.exception.model_dump_json() + "\n")
    with (out / "answer_key.jsonl").open("w") as fh:
        for c in cases:
            fh.write(
                json.dumps(
                    {
                        "case_id": c.case_id,
                        "exception_id": c.exception.exception_id,
                        "scenario": c.scenario,
                        "group": c.group,
                        "expected": c.expected.model_dump(mode="json"),
                    }
                )
                + "\n"
            )


def generate_mixed(seed: int, out: Path) -> list[PlantedCase]:
    """A day's worth of data: every scenario planted once in one dataset."""
    cases, ds = build_mixed(seed, list(SCENARIOS))
    write_dataset(ds, cases, out)
    return cases


def generate_golden(out: Path, base_seed: int = 1000) -> list[PlantedCase]:
    """One directory per golden case, each with its own small dataset."""
    out.mkdir(parents=True, exist_ok=True)
    cases: list[PlantedCase] = []
    n = 0
    for scenario, count in GOLDEN_PLAN:
        for _ in range(count):
            n += 1
            case_id = f"G{n:03d}"
            case, ds = build_case(scenario, seed=base_seed + n, case_id=case_id)
            case_dir = out / case_id
            case_dir.mkdir(exist_ok=True)
            (case_dir / "dataset.json").write_text(ds.model_dump_json(indent=2) + "\n")
            (case_dir / "case.json").write_text(case.model_dump_json(indent=2) + "\n")
            cases.append(case)
    return cases
