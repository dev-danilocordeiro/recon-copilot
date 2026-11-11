# recon-copilot: Shadow rollout plan

Status: proposal for the client. No production data has been used; every number below is
a **proposed criterion to agree with the finance team**, not a measured result.

## 1. What "shadow" means here

The copilot runs beside the current manual process on the same exceptions, and changes
nothing:

```
matcher ──► exception queue ──► analyst works it as today ──► analyst's decision (system of record)
                   │
                   └──► recon-copilot propose (read-only) ──► proposal stored, not shown to the analyst
                                                                  │
                         daily comparison job ◄───────────────────┘
```

- `propose` only. The executor and `approve` are **not deployed** during the shadow phase. The `ledger-mcp` credentials it gets are read-only.
- Analysts don't see the proposals during the blind phase, so they aren't anchored by them.
- Each day, a comparison job joins proposals with what analysts actually did and records agreement per exception type and cause.

## 2. Phases

| Phase | What runs | Who sees proposals | Writes | Minimum length |
|---|---|---|---|---|
| 0. Pre-flight | Golden set on the production model and prompt; a replay of recent resolved exceptions, anonymized | Project team | none | until the go criteria for phase 1 are met |
| 1. Blind shadow | `propose` on every new exception | Project team only | none | 4 weeks **and** at least 300 exceptions, including a month-end close |
| 2. Assisted | Proposals shown to analysts next to the exception; the analyst still does the work by hand | Analysts | none by the copilot | 4 weeks |
| 3. Approve-to-apply | `approve` enabled for a subset of actions (start with `ignore_bank_duplicate` and `link_records`) | Analysts | only through `approve`, one human approval per write | open-ended, reviewed monthly |

No phase allows unattended writes. That isn't a setting: it isn't built (docs/discovery.md, section 8).

## 3. Before phase 0

- **Data handling:** the client confirms that amounts, names and tax IDs may be sent to the model provider, under which contract terms and in which region (discovery questions 17–20). Without that answer, phase 0 runs on synthetic data only.
- **Baseline:** three full golden-set runs on the chosen model, with `evals/baseline.json` set from them (`recon-copilot baseline ...`). The CI regression job is then active.
- **Spend ceiling:** a per-day token ceiling for shadow runs, agreed with finance and derived from the measured cost per case in those baseline runs.
- **Real ledger-mcp:** the ledger team confirms its tools match the contract, including that every write tool refuses calls without an approval ID before changing anything (see failure-modes.md, 2.1).

## 4. Go criteria (proposed, to agree with the client)

To move from phase 1 to phase 2, all of these, measured on real exceptions from phase 1:

| Criterion | Proposed bar | Why |
|---|---|---|
| Safety failures (write proposed where the analyst escalated or did nothing; any non-allowlisted tool attempt; any injection followed) | **0** over the whole phase | One wrong write proposal shown to a rushed analyst is the main risk |
| Agreement with the analyst on cause | ≥ agreed bar, per exception type, reported with counts | Per type, because a high overall number can hide a type the copilot gets wrong |
| Agreement on action (with the same targets and amount) | ≥ agreed bar | The action is what an analyst would accept |
| Escalation rate | Reported, no bar | Escalating too often makes it useless, too rarely makes it dangerous. Track it and discuss |
| Cost per exception | ≤ agreed budget, from measured usage | A ceiling exists per run; this checks the average |
| Golden-set regression | CI green on every prompt or model change during the phase | Changes during the shadow phase must not silently lower quality |

Where the copilot and the analyst disagree, a supervisor reviews a sample and labels who was
right. Agreement is not accuracy: the analyst is sometimes wrong. The labelled sample is the
nearest thing to accuracy available, and it feeds new golden cases.

From phase 2 to phase 3: phase 1 criteria still hold, analysts report the proposals save
time (a short survey plus time-per-exception from the queue tool, if it records it), and
internal audit signs off on the `approve` flow and the audit log.

## 5. Rollback criteria

Any of these returns the system to the previous phase (phase 3 to 2 means `approve` is
switched off). The first one also means an incident review:

1. **Any write applied through `approve` that a supervisor later finds wrong.** Disable `approve`, review the audit log for that approval, and add the case to the golden set.
2. Any safety failure in phases 2–3.
3. Agreement on any exception type falling below its agreed bar over a rolling two-week window.
4. The CI regression job failing on the deployed prompt and model, or the provider retiring or changing the model.
5. Cost per exception above budget for three consecutive days.
6. `ledger-mcp` contract changes that weren't re-verified.

Rollback is cheap by design: the copilot owns no state the ledger depends on. Turning it
off means stopping the `propose` job and, in phase 3, removing the `approve` command.

## 6. What synthetic results do and don't tell us

The 40-case golden set is synthetic. It checks that the agent handles the planted patterns,
refuses to act on ambiguous cases, and ignores injected text. Real exceptions are messier:
partial references, foreign payers, bank-specific quirks and causes outside the taxonomy.
**Treat golden-set accuracy as an upper bound on real accuracy.** Phase 1 exists to measure
the real number before anyone relies on it.
