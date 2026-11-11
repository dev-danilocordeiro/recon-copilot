# recon-copilot

An agent that helps a finance team triage bank reconciliation exceptions. Built as a
delivery for a fictitious client (Ipê Payments). All data is synthetic.

Given an exception the matcher couldn't resolve (an unmatched entry, an amount mismatch or a
duplicate), the agent looks up the ledger through an MCP server, picks a cause from a fixed
taxonomy and proposes one action with the evidence attached. **It never changes the
ledger.** Writes happen only through `recon-copilot approve`, which needs a person to type
`yes` for that exact proposal.

> **Status: no real model runs yet.** Everything below runs and is tested without an API
> key: 73 tests, ruff, and mypy `--strict`. The agent has not been evaluated against a
> model, so there are no accuracy, cost or latency figures anywhere in this repo. See
> [docs/failure-modes.md](docs/failure-modes.md).

## Docs

| | |
|---|---|
| [docs/discovery.md](docs/discovery.md) | Problem, questions for the client, scope, out of scope |
| [docs/plan.md](docs/plan.md) | Technical plan and decisions |
| [docs/failure-modes.md](docs/failure-modes.md) | Errors observed (eval and development), unvarnished |
| [docs/rollout.md](docs/rollout.md) | Shadow rollout, go and rollback criteria |

## How it works

```
exception ──► agent loop (Anthropic SDK, hand-written) ──► proposal JSON (cause, action, targets, evidence)
                  │  ▲                                               │
       read-only  │  │ results                       human types yes │
                  ▼  │                                               ▼
               ledger-mcp (6 read tools)                 executor ──► ledger-mcp write tools
                                                              └──► audit.jsonl
```

- **Loop:** `src/recon_copilot/agent/loop.py`. Messages API with tool use and structured outputs for the final answer. No agent framework.
- **Tool allowlist:** only the six read tools reach the model. A call to anything else is refused in the loop and never reaches the server.
- **Evidence check:** a proposal that targets an ID the agent never looked up is downgraded to `escalate_to_human`.
- **Token ceiling:** checked *before* each call (`count_tokens` + `max_tokens` ≤ remaining), so a run can't exceed it.
- **Approval:** `executor.py` needs an `Approval` bound to the proposal's SHA-256 digest. Each approval works once, and an edited proposal file is refused. The agent package doesn't import the executor; a test checks this.

## Setup

Python 3.12 via [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
```

The model is configured with environment variables:

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Needed for `propose` and `eval` only |
| `RECON_MODEL` | `claude-opus-5-5` | Any Messages API model ID |
| `RECON_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |
| `RECON_MAX_TOKENS` | `16000` | Per call |
| `RECON_MAX_TURNS` | `8` | Per exception |
| `RECON_FALLBACKS` | `auto` | Server-side refusal fallback where the model supports it; `off` to disable |
| `RECON_EVAL_TOKEN_CEILING` | (none) | Required for `eval`: a token cap for the whole run |
| `LEDGER_MCP_COMMAND` | local stub | Command that starts the real `ledger-mcp` server |

## Commands

```bash
# Synthetic data: ledger, bank statements, return files, exceptions, answer key
uv run recon-copilot generate --seed 7 --out data/day1

# Agent proposals (read-only)
uv run recon-copilot propose data/day1 --out proposals/

# Review one proposal and apply it if you type yes (interactive only)
uv run recon-copilot approve proposals/EXC-0002.json --data-dir data/day1 --approver "Your Name"

# Golden set: 40 cases, report in evals/reports/
uv run recon-copilot eval --token-ceiling 3000000

# After three full runs, set the regression baseline used by CI
uv run recon-copilot baseline evals/reports/A.json evals/reports/B.json evals/reports/C.json
```

The local stub keeps the ledger in memory per process. `approve` applies the write to the
stub and records it in `audit.jsonl`, but the change disappears when the process exits.
A real `ledger-mcp` would persist it.

## Evals

- `evals/golden/`: 40 frozen cases: 24 across the 8 causes, 6 ambiguous, 6 near-misses, 4 prompt injections. Each case has its own dataset and answer key. They were generated with fixed seeds and reviewed case by case (the review found and fixed two problems; see failure-modes.md 2.2 and 2.3). The review was done by the AI assistant that built the repo, so a human spot-check is still worth doing.
- `evals/graders.py`: code graders, no LLM judge. A case is correct when the cause is accepted and the action is accepted with the right target IDs and the exact amount. Safety failures are reported separately and never averaged into accuracy.
- Report: per case, expected vs got, grader results, tokens, cost (from real `usage` × `pricing.toml`; `null` if a rate is missing), latency, turns, and request IDs.
- CI: `lint-type-test` on every push. `eval-regression` runs nightly, on demand, and on PRs touching the agent, data or evals, when the `ANTHROPIC_API_KEY` secret and the `RECON_EVAL_TOKEN_CEILING` variable are set. It fails on an accuracy drop below `evals/baseline.json`, on any safety failure, or on an incomplete run. Until a baseline is set, only the safety and completeness checks apply.

## Layout

```
src/recon_copilot/   agent loop, config, domain, executor, approval, audit, CLI
ledger_mcp_stub/     MCP server (stdio) over a synthetic dataset
datagen/             synthetic data + planted scenarios + answer key
evals/               golden set, graders, runner, baseline gate, reports
tests/               no network needed
```
