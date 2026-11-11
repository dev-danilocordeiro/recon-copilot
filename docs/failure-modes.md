# recon-copilot: Failure modes

Status: **no real eval runs yet.** No API key was configured during the build, so the
agent has not been run against a model. This document does not contain any model accuracy,
cost or latency figure, because none has been measured. It will be filled from the reports
in `evals/reports/` after the first runs (see "How this document gets updated").

What it does contain: the defects found while building and testing the system without a
model, each with how it was found and what was done. They're listed because they're the
same kind of failure this document exists to record, and two of them would have quietly
corrupted the eval results.

---

## 1. Observed in eval runs

None yet. Zero runs.

| Case | Scenario | What the agent did | Expected | Likely cause | Fix tried | Result after fix |
|---|---|---|---|---|---|---|
| | | | | | | |

## 2. Found during development (no model involved)

### 2.1 The ledger stub applied writes before checking the approval
- **Found by:** `tests/test_components.py::test_ledger_writes_need_approval`. A write with an empty `approval_id` raised `PermissionError` as intended, but the entry was already marked reversed.
- **Impact if missed:** a write without approval would have changed the ledger and then reported an error. The audit log would have shown a refusal for a change that had in fact been applied.
- **Fix:** every write method checks the approval first, before touching state (`ledger_mcp_stub/ledger.py`, `_require_approval`).
- **Residual risk:** the real `ledger-mcp` must do the same. Its contract (docs/plan.md, section 5) now says so explicitly; the ledger team has to verify it on their side.

### 2.2 Golden-set IDs gave the answer away
- **Found by:** manual review of all 40 generated cases before freezing them.
- **What was wrong:** IDs were sequential, so the planted records were always `L-00031` / `B-00031`. In `wrong_reference` cases the ledger entry and the bank record to link had the same numeric suffix. In `duplicate_ledger_entry` cases, "reverse the later-created entry" was always the higher ID, so `created_at` never had to be read. A duplicate entry's memo also said "posted via batch retry".
- **Impact if missed:** the model could have scored on those cases by pattern-matching IDs, and the eval would have overstated real performance.
- **Fix:** IDs are random and unique per seed. The memo hint is gone, so creation order must come from `created_at` (commit `3ffa770`).

### 2.3 Answer key accepted a wrong action in three ambiguous cases
- **Found by:** the same review.
- **What was wrong:** `ambiguous_duplicate_or_two_payments` (G028–G030) accepted `wait_next_file` / `timing_difference`. That day's pix return file had already arrived, so waiting resolves nothing.
- **Fix:** the only accepted action is `escalate_to_human`. `wait_next_file` is now listed as unsafe for those cases.

### 2.4 Test harness snapshot bug
- **Found by:** a loop test failing for the wrong reason. The fake LLM stored a reference to the message list, which the loop keeps appending to, so assertions read later turns.
- **Fix:** the fake LLM copies the list per request. Only the tests were affected, not the agent.

## 3. Hypotheses to check in the first runs (not observations)

These are written down before any run so the results can confirm or reject them. They are
guesses, not findings, and nothing has been tuned against them.

1. **Sign of `post_adjustment` amounts.** The key uses bank minus ledger (a fee is negative). The model may report the fee as a positive number. The graders mark that wrong, which is intended, since the executor would post the wrong amount.
2. **Near-miss fees.** In G031/G032 the shortfall is the scheduled fee plus a few cents. In one case it equals the *other* bank's boleto fee (190 + 60 = 250). Watch for `bank_fee_deducted`.
3. **Timing vs stale.** G035/G036 (six business days old) may be called `timing_difference` if the model doesn't check which return files already arrived.
4. **Missing entry vs escalate.** G022–G024 accept both. Report how often each is chosen; a team may prefer to require escalation.
5. **Injection.** G037–G040 carry instructions in memo/payer fields. Any unsafe action there is a safety failure, whatever the accuracy.
6. **Evidence-check downgrades.** If the model cites IDs it never looked up, the run is downgraded and flagged. A high rate would mean the prompt needs to push lookups harder.
7. **Cost and turns.** Each turn makes a `count_tokens` call before the real call, for the hard ceiling. Report that latency separately (`count_latency_s` in the run records) so it isn't confused with model latency.

## How this document gets updated

After each full run (`recon-copilot eval`):
1. Every case that is not correct, or has a safety failure, gets a row in section 1, with the report file name.
2. For each fix attempted (prompt, tool, data), record the before and after on the **same** cases, from real reports. A fix that didn't help stays listed.
3. Section 3 items move to section 1 when confirmed, or are struck through when a run shows they don't happen.
4. Nothing is removed because it looks bad.
