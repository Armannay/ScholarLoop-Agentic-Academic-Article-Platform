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
| Providers | `app/providers/` | `LLMProvider` Protocol, `ProviderError` family, `MockProvider`, provider pool |
| Governor | `app/governor/` | token budgets, caps, per-call accounting |
| Memory | `app/memory/` | append-only ledger, bounded digest, compaction |
| Article | `app/article/` | section store, patch application, versioning |
| Agents | `app/agents/` | architect, researcher, writer, reviewer, validator |
| Planner | `app/planner/` | plan construction and verdict evaluation (LLM, JSON-schema) |
| Orchestrator | `app/orchestrator/` | the loop: state machine, scheduling, decision |
| API | `app/api/` | FastAPI surface (`/api/health`, `/api/config`, `/api/accounts`, contracts) |

`contracts`, `core`, `providers`, `accounts`, `governor`, `scripts` and `api` exist
today. The rest are the interfaces this foundation was built to accept.

## The runtime core (what exists between the contracts and the agents)

Three services sit between "the user changed an account" and "a model answered".
They are deliberately small and deliberately boring, because everything expensive
downstream depends on them being predictable.

```
account store ──► binding registry ──► provider pool ──► provider
      (ciphertext)      (role → ids)      (call time)     (one call)
                            ▲                 │
                            │                 ▼
                     agent binding        governor
                                      (precheck / charge)
```

* **Account store** (`app/accounts/`) - a key is accepted as plaintext only at the
  boundary and is stored as an AES-GCM ciphertext. `reveal()` is the single method
  in the codebase that returns a plaintext key, and the account's *fingerprint*
  (12 hex characters of the ciphertext) is what the UI shows. Rotating a key
  changes the fingerprint, which is how an operator proves a rotation happened.
* **Binding registry** (`app/providers/pool.py`) - role → ordered account ids plus
  a strategy (`dedicated | round_robin | failover`). A binding holds ids, never
  accounts, because accounts come and go while a run is in flight.
* **Provider pool** (`app/providers/pool.py`) - resolves the binding, then the
  accounts, *at call time*; walks the candidates when a call fails for a provider
  reason; hands the decrypted key to the provider builder; and charges what was
  spent.
* **Governor** (`app/governor/`) - checks `call_input_cap`, the binding's own
  ceilings, `iteration_token_cap`, `run_token_cap` and (when the run state carries
  it) the task budget *before* a call. It refuses; it never truncates a prompt.
* **Task context** (`app/agents/context.py`) - assembles the prompt for one task
  from the brief, the task, section *states*, source *cards* and the digest. The
  article itself is never in the prompt.


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
