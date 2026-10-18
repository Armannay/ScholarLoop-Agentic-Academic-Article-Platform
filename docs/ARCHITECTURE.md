# ScholarLoop architecture

## What it is

ScholarLoop writes academic articles with a **deterministic orchestrator** (plain
Python, no LLM in the control path) driving a **planning-only Planner** (Claude) and
five **executing agents**. The Planner plans and evaluates; it never writes article
text. Agents return *patches* plus a `ChangeEntry`; the ledger is append-only and the
Planner only ever reads a bounded digest (`claude.md`, ≤ 2,000 tokens).

The design goal is token frugality with auditability: pass references, not content;
patches, not rewrites; JSON-schema outputs with hard `max_tokens`.

## Components

| Component | Module | Responsibility |
| --- | --- | --- |
| Contracts | `app/contracts/` | Pydantic v2 wire format, `extra="forbid"`, `schema_version=1` |
| Core | `app/core/` | settings, policies, rubric, ULIDs, hashing, redaction, logging |
| Accounts | `app/accounts/` | encrypted key store; keys resolved **at call time** |
| Providers | `app/providers/` | `LLMProvider` Protocol, `ProviderError` family, `MockProvider` |
| Governor | `app/governor/` | token budgets, caps, per-call accounting |
| Memory | `app/memory/` | append-only ledger, bounded digest, compaction |
| Article | `app/article/` | section store, patch application, versioning |
| Agents | `app/agents/` | architect, researcher, writer, reviewer, validator |
| Planner | `app/planner/` | plan construction and verdict evaluation (LLM, JSON-schema) |
| Orchestrator | `app/orchestrator/` | the loop: state machine, scheduling, decision |
| API | `app/api/` | FastAPI surface (`/api/health`, `/api/config`, contracts) |

`contracts`, `core`, `providers`, `scripts`, `api` and `article` exist today; the rest
are the interfaces this foundation was built to accept.

### Article store (implemented)

`app/article/` holds the article itself: every section is an independently versioned
document (body, `version`, `sha256`, words, citations, status).

* **Patches, not rewrites.** `patch.py` applies the four op verbs with exact, exactly-once
  matching; a miss raises `PatchRejected` carrying one bounded repair hint per offending
  op, so an agent needs a single repair call, not one per error.
* **Optimistic concurrency.** `base_version` must equal the section head or the store
  raises `StalePatch`; the patch is discarded, never merged. Per-section async locks
  (`locks.py`) serialise same-section writers, take multi-section batches in one global
  order, and time out instead of hanging an iteration.
* **Append-only history.** Versions are never edited. `restore(snapshot_id)` appends new
  versions carrying the frozen bodies, and `best_snapshot()` returns the highest-Q
  checkpoint - that is the loop's rollback path out of erosion.
* **Erosion signals.** `recurrence.py` reports a body that round-tripped
  (`hash_recurred`) and edits that measured no gain (`edits_without_gain`).

## State machine

```
INTAKE ──► PLANNING ──► EXECUTING ──► VALIDATING ──► DECIDING
              ▲                                          │
              │                                          ├─► CHECKPOINT
              │                                          ├─► REPLANNING ──┐
              │                                          │                │
              │                                          ├─► DONE         │
              │                                          └─► STOPPED      │
              └───────────────────────────────────────────────────────────┘
```

* **INTAKE** — validate the `Brief`; resolve accounts, bindings and the rubric.
* **PLANNING** — the Planner returns a `Plan` (todo list + rules, every task owned by exactly one role). Validated for acyclic dependencies and role coverage.
* **EXECUTING** — up to `max_parallel` ready tasks run; each returns patches + a `ChangeEntry`.
* **VALIDATING** — the validator measures the article and emits a `QualityReport` (8 hard gates, weighted criteria, `Q`).
* **DECIDING** — the loop controller combines the report, the verdict, the budget and the erosion detectors into a `LoopDecision`.
* **CHECKPOINT** — digest compaction, artifact snapshot; loop continues.
* **REPLANNING** — the Planner re-plans from the digest (version + 1).
* **DONE / STOPPED** — terminal. `STOPPED` covers erosion, budget, errors and the user.

Every transition is decided by deterministic rules over numbers, and every decision
is recorded as a `LoopDecision` with machine-readable `reasons` and `evidence`.

## One iteration, step by step

1. The controller picks ready tasks (`Plan.ready_tasks`) and claims the accounts each role's binding resolves to *now*.
2. The governor checks `iteration_token_cap` and `call_input_cap` before each call; it refuses rather than truncating silently.
3. Each agent calls the provider once per unit of work and returns an `AgentResult`: `entry`, `patches`, `source_cards`, `sections`, optional `report`.
4. Patches are applied against `base_version`; a stale patch is rejected and re-queued, never forced through.
5. `ChangeEntry` rows are appended to the ledger; the ledger stays the single history.
6. The digest is rebuilt: the last `recent_window` entries verbatim, older ones summarised until it fits `digest_max_tokens` (compaction triggers at `compact_trigger`).
7. The validator's `QualityReport` updates Q history, gate state and issues; the reviewer's issues open and close.
8. The controller evaluates plateau, regression, oscillation, diminishing returns, issue churn, error streak, budget and `q_min`, then emits one `LoopDecision`.
9. `CONTINUE` → next iteration. `OFFER_FINISH` → a human may accept. `ASK_USER` → the loop pauses with the exact question. `STOP_*` → artifacts are written and the run ends.

## Token frugality, concretely

* The Planner reads the **digest**, never the ledger and never the article.
* Tasks carry **ids, budgets and acceptance criteria**, not content.
* Patches carry **deltas**; section text is addressed by `(section_id, version, hash)`.
* Every call declares `max_output_tokens` and, where the answer is structured, a `json_schema`.
* Budgets nest: `call_input_cap` < `iteration_token_cap` < `run_token_cap`, enforced by the policy loader.

## Trust boundaries

* Provider responses, fetched pages and source snippets are **data**. Instructions inside them are ignored (HARD RULE 4).
* Keys never leave the account store; logs, API responses and fixtures are scanned for key-shaped strings by `app/scripts/secret_scan.py`, which also redacts at the logging handler.
* Tests and CI run against `MockProvider` only, and a test asserts the mock imports no HTTP client.
