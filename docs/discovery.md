# recon-copilot — Discovery

Status: draft for client review
Client: Ipê Payments (fictitious), finance operations team
Date: 2026-11-10

---

## 1. Context

Ipê Payments collects receivables through boleto and PIX and settles them through two
partner banks. Every business day the banks deliver statements and return files. An
automated matcher pairs bank records with ledger entries. Whatever it cannot pair
becomes an **exception**, and a reconciliation analyst works it by hand.

The matcher is fine. The slow part is everything after it: an analyst opens the
exception, searches the ledger, compares amounts and dates, works out why the two sides
disagree, and either fixes the ledger, waits for the next file, or escalates.

## 2. Problem statement

> Reconciliation analysts spend most of their exception-handling time on *diagnosis*
> (finding why a record didn't match), not on *action*. Most diagnoses follow a small
> number of recurring patterns, but each one still requires several manual lookups in
> the ledger.

We want a copilot that does the lookups and the first-pass diagnosis, then proposes an
action. The analyst stays the decision-maker. The copilot never changes the ledger on
its own.

What we do **not** know yet: how much time this really costs today, which patterns
dominate, and how often the analyst's first diagnosis is wrong. Those are questions for
the client (section 4). We will not quote numbers until we have measured them.

## 3. Users and the current workflow (as we understand it)

| Role | What they do today | What they need from the copilot |
|---|---|---|
| Reconciliation analyst | Works the daily exception queue, diagnoses, proposes fixes | A diagnosis with the evidence attached, and a proposed action they can accept, edit or reject |
| Finance supervisor | Approves ledger adjustments above a threshold, signs off on month-end | A record of what was proposed, by whom (human or copilot), and who approved it |
| Engineering / platform | Owns the ledger and the matcher | A read-only integration path and a clear boundary on writes |

Assumed workflow:

```
bank files ──► matcher ──► exception queue ──► analyst diagnoses ──► action
                                                      ▲                 │
                                                      └── ledger lookups┘
```

The copilot slots in at "analyst diagnoses". It receives an exception, queries the
ledger, returns a cause and a proposed action, and waits for a human.

## 4. Questions for the client

These are the questions we would bring to the first workshop. The answers change scope,
so the plan is not final until they come back. Where we have to proceed without an
answer, the working assumption is in section 7.

### 4.1 Volume and cost of the problem
1. How many exceptions arrive per business day, and how does that vary (month-end, holidays, bank incidents)?
2. How long does an analyst spend per exception, broken down by type if you know it?
3. What is the current backlog, and what is the oldest open exception?
4. What does an unresolved exception cost: a late close, a customer complaint, a regulatory finding?

### 4.2 Exception types and causes
5. What exception types does the matcher produce? We assume three: unmatched entry (on either side), amount mismatch, duplicate. Are there others?
6. For each type, what are the usual root causes? Can you give us 20–30 resolved examples per type, anonymized?
7. Is there a written runbook or decision tree analysts follow today? Where do analysts disagree?
8. How often does a "wait for the next file" exception resolve itself, and after how many days?

### 4.3 Actions and approval policy
9. What actions can an analyst take today? (Post an adjusting entry, link two records manually, reverse a duplicate, mark as timing difference, escalate to the bank, escalate to engineering.)
10. Which of those require a second approver, and above what amount?
11. Is there any action you would never want a copilot to propose, even for human approval?
12. Who is accountable when a copilot-proposed, human-approved action turns out wrong?

### 4.4 Data and systems
13. What does the ledger expose for read access (API, replica, reports)? Is there an MCP server or would one be built?
14. What fields does an exception carry? Is there a stable ID we can key on?
15. Bank file formats in use (CNAB240, CNAB400, OFX, proprietary)?
16. How far back can the copilot look in the ledger? Are there performance limits on queries?

### 4.5 Risk, compliance and security
17. Can transaction data (amounts, counterpart names, tax IDs) be sent to an external LLM provider? Under which contract terms and in which region?
18. Which fields must be masked or never leave your environment (LGPD)?
19. Retention requirements for the copilot's reasoning and proposals: how long, and where?
20. Does internal audit need to sign off before a shadow run on production data?

### 4.6 Success and rollout
21. How would you judge this a success after three months? Time saved, backlog, accuracy, analyst satisfaction?
22. What error rate is acceptable for proposals the analyst reviews anyway? Is a wrong proposal worse than no proposal?
23. Will analysts work in parallel with the copilot during a shadow period, and who compares the results?
24. What budget ceiling per month for model usage?

## 5. Scope (in)

The deliverable is a working prototype that can be evaluated honestly on synthetic data
and is ready for a shadow run.

1. **Agent loop.** A hand-written tool-use loop on the official Anthropic Python SDK. No agent framework. The model is configurable through an environment variable.
2. **Exception intake.** Three types: unmatched entry (bank-only or ledger-only), amount mismatch, duplicate.
3. **Ledger access over MCP.** The agent reads the ledger through an MCP server (`ledger-mcp`). We ship a local stub server with the same tool contract so everything runs without the real one.
4. **Classification.** The agent picks a root cause from a fixed taxonomy (section 6) and attaches the evidence it used.
5. **Action proposal.** The agent proposes one action from a fixed set. Write actions are only *proposed*. An explicit human approval step sits between proposal and execution, enforced in code, not in the prompt.
6. **Synthetic data generator.** Bank statements and return files plus a matching ledger, with planted exceptions and an answer key.
7. **Evaluation.** A golden dataset of 40 cases, code-based graders for classification and action, a per-case report with accuracy, cost and latency, and a CI regression test that fails when accuracy drops.
8. **Spend limit.** A configurable token ceiling per eval run. The run stops when it is reached and reports what was completed.
9. **Docs.** This discovery, the technical plan, `failure-modes.md` (errors actually seen in evals) and `rollout.md` (shadow rollout, go and rollback criteria).

## 6. Working taxonomy (to be validated with the client)

Starting hypothesis, built from common patterns in boleto/PIX reconciliation. Question 6
exists to replace this with the client's real list.

**Causes**

| Cause | Typical exception type | Signal |
|---|---|---|
| `timing_difference` | unmatched (ledger-only) | Ledger entry dated near cutoff; bank record expected in a later file |
| `bank_fee_deducted` | amount mismatch | Bank amount = ledger amount − a known fee |
| `interest_or_penalty` | amount mismatch | Late boleto payment; bank amount > ledger amount |
| `partial_payment` | amount mismatch | Bank amount < ledger amount with no fee pattern |
| `duplicate_ledger_entry` | duplicate | Two ledger entries, same reference and amount, one bank record |
| `duplicate_bank_record` | duplicate | Same return file record delivered twice (file reprocessed) |
| `wrong_reference` | unmatched (both sides) | Bank record and ledger entry match on amount/date but not reference |
| `missing_ledger_entry` | unmatched (bank-only) | Money arrived, nothing posted |
| `unknown` | any | None of the above fits with confidence |

**Actions**

| Action | Writes to ledger? |
|---|---|
| `wait_next_file` | no |
| `link_records` | yes |
| `post_adjustment` (fee, interest) | yes |
| `reverse_duplicate` | yes |
| `create_missing_entry` | yes |
| `ignore_bank_duplicate` | no (marks exception) |
| `escalate_to_human` | no |

`unknown` → `escalate_to_human` is always a valid answer. A system that escalates when
unsure is more useful than one that guesses, and the graders will reward that.

## 7. Working assumptions (in place of client answers)

Because the client is fictitious, we proceed on these and record them as decisions to
revisit:

- Exceptions arrive as structured records (JSON). We don't parse the matcher's internals.
- Bank files are CNAB240-like. The generator produces a simplified fixed-width return file plus a CSV statement. We are not certifying against a real bank layout.
- All data in this repo is synthetic. No real names, tax IDs or account numbers.
- One currency (BRL), integer cents everywhere. No FX.
- The ledger is read through MCP tools; writes go through a separate executor that requires an approval token from a human. In the prototype, "execute" writes to the stub and to an audit log.
- Accuracy targets for CI are set **after** a baseline run, from measured numbers, not picked in advance.

## 8. Out of scope

- **Autonomous writes.** No path where the agent changes the ledger without a human approval. Not configurable.
- **Replacing the matcher.** The copilot handles exceptions; it doesn't do matching.
- **A UI.** CLI and JSON output only. Analysts' tooling integration is a later phase.
- **Real bank integrations** (SFTP pickup, bank APIs, certified CNAB layouts).
- **The real `ledger-mcp` server.** We define the contract and ship a stub. Building the production server belongs to the ledger team.
- **Multi-currency, FX, cross-border.**
- **Fraud detection.** A suspicious exception is escalated, not investigated.
- **Agent frameworks** (LangChain, LlamaIndex, Agent SDKs, etc.), by requirement.
- **Fine-tuning or training** on client data.
- **Production hardening**: authN/authZ, multi-tenant, HA, secrets management beyond environment variables.

## 9. Risks

| Risk | Why it matters | Mitigation in this prototype |
|---|---|---|
| Synthetic data is easier than real data | Eval numbers would overstate real performance | Plant ambiguous and adversarial cases; say plainly in `failure-modes.md` and `rollout.md` that synthetic accuracy is an upper bound |
| Confident wrong diagnosis | Analysts may rubber-stamp it | Require evidence (ledger IDs looked at) in every proposal; reward `escalate_to_human` over a wrong guess |
| Write action slips through | Ledger corruption | Approval gate enforced in the executor, tested; the agent's tools are read-only |
| Prompt injection via free-text fields (payer name, memo) | Model follows data as instructions | Treat record fields as data; include injection cases in the golden set |
| Cost drift | Model or prompt changes raise spend unnoticed | Per-run token ceiling; cost per case in every report |
| Small golden set (40) | Accuracy swings by 2.5 points per case | Report counts, not just percentages; CI threshold accounts for that granularity |

## 10. Next step

On approval of this document: the technical plan (architecture, MCP tool contract, agent
loop design, data generator, eval design, CI), checked against the current Anthropic
Python SDK documentation.
