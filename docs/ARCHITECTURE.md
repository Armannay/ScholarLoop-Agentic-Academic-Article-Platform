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
| Governor | `app/governor/` | nested budgets, estimation, prompt assembly, telemetry, model tiers |
| Memory | `app/memory/` | append-only ledger, bounded digest, compaction |
| Article | `app/article/` | section store, patch application, versioning |
| Agents | `app/agents/` | generic runtime, context builder, registry, the five role agents |
| Skills | `app/skills/` | lazy `SKILL.md` loader, deterministic selection, lint |
| Tools | `app/tools/` | scoped registry: role/project grants, untrusted bounded results |
| Planner | `app/planner/` | plan construction and verdict evaluation (LLM, JSON-schema) |
| Orchestrator | `app/orchestrator/` | the loop: state machine, scheduling, decision |
| API | `app/api/` | FastAPI surface (health, config, contracts, auth, accounts, runs + SSE) |

All of the packages above exist today, including `planner` (Step 8) and
`orchestrator` (Step 9): intake, the twelve-state run machine with
file-persisted run records, the dependency-aware scheduler, the pure loop
controller with its erosion detectors, and the completion checkpoint flow.

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
### Agent runtime (implemented)

`app/agents/base.py` is the single execution path every role agent inherits:
`ContextBuilder.build` (rules, skill index, selected skills, authorised tool schemas and
task references) -> `Governor.precheck` -> one JSON-schema `provider.generate` ->
`AgentResult` and patch-scope validation -> at most one repair (validation errors and the
previous output, never the original context) -> measured `entry.tokens`. A provider error
is never converted into a repair. `agents/registry.py` maps roles to agents, resolves the
`AgentBinding` at call time (hot-swap safe) and refuses unknown or disabled roles.
`skills/loader.py` keeps bodies out of the prompt until selected (at most two,
deterministic, budgeted), and `tools/base.py` denies by default, narrows per project,
validates arguments and returns size-capped untrusted results.

### Role agents (implemented)

`app/agents/roles/` builds the five roles on that runtime. Each answers with two documents
in one JSON object - the frozen `AgentResult` plus the role document v1 has no field for -
constrained by one schema, then checked deterministically: rubric
ids and word targets, no card the search did not return, one section with supplied
citations inside ±10 % words, dirty sections only and no patch, and gates from tools with
`Q` from reviewer scores. Text arrives through an injected provider; scores travel through
a hash-keyed scoreboard, so they carry forward only while the text is unchanged.

The account store
(`app/accounts/`) holds provider keys encrypted at rest (AES-GCM envelope,
`APP_MASTER_KEY`), resolves them at call time through `app/providers/pool.py`, and
ships live adapters for Anthropic (Claude Console keys for the Planner) and
OpenAI-compatible services (OpenRouter et al.).

### Governor (implemented)

`app/governor/` is where token spend stops being a guess.

* **Nested budgets** (`budget.py`): `account-month -> run -> iteration -> call`. A call
  *reserves* its estimate before it runs and settles the provider's real usage after;
  a reservation counts towards the ceiling, so concurrent calls cannot both spend the
  last thousand tokens. Crossing 80 % of any level warns once, reaching 100 % refuses
  the next call with `BudgetExceeded(level, remaining)`. Counters are atomic behind
  `BudgetStore`: in-memory for one process, an atomically replaced JSON document to
  survive a restart (a SQL store is the multi-worker path).
* **Estimation** (`estimate.py`): the provider's `count_tokens` when it answers,
  otherwise `chars / 4` plus a 15 % margin. Every estimate can be reconciled against
  the prompt usage the provider reported, exposing signed bias, mean absolute error
  and the margin that would have covered the samples.
* **Context assembly** (`context_builder.py`): stable prefix (root rules, project
  rules, role rules, rubric excerpt, skill index, brief; `cache=true`) -> semi-stable
  (selected skill bodies, the source cards the task names) -> volatile (digest slice,
  task, section text). Project rules are appended, never merged over, and a guard
  rejects any attempt to remove a mandatory root rule. The prefix is byte-identical
  across calls of one role in a run, which is what makes prompt caching pay. When the
  prompt does not fit it trims digest detail, then optional skills, then source-card
  notes, and finally raises `ContextTooLarge` - the task and the section being edited
  are never truncated.
* **Telemetry** (`telemetry.py`): one record per call, tokens by role and iteration,
  cache-hit ratio, and token ROI (Q gained per 10k tokens) for the loop's diminishing
  returns check.
* **Tiers** (`tiers.py`): `policies.model_tiers` names a class (planner strong,
  validator and compactor cheap), the provider table names the model id, and an
  `AgentBinding.model` overrides both.

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
