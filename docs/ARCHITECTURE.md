# Architecture

ScholarLoop writes academic articles with a deterministic controller and a
planning-only LLM. The Planner never writes article text; five agents execute
tasks and return patches. Everything that costs tokens passes through the
governor, and everything that changes the article passes through the
append-only ledger.

## State machine

One run moves through these states. The controller is plain Python, so every
transition is reproducible and testable with `MockProvider`.

| State | What happens | Exit |
| --- | --- | --- |
| `INTAKE` | The `Brief` is validated; rubric sections and word ranges are resolved. | `PLANNING` |
| `PLANNING` | The Planner returns a `Plan` (todos plus rules), each task owned by exactly one role. | `EXECUTING` |
| `EXECUTING` | Ready tasks run (dependencies satisfied, up to `max_parallel`). Each writes a `ChangeEntry` and, usually, a `Patch`. | `VALIDATING` |
| `VALIDATING` | The validator runs the eight hard gates and scores the soft criteria, producing a `QualityReport`. | `DECIDING` |
| `DECIDING` | The loop controller combines Q, gates, spend and erosion signals into a `LoopDecision`. | `CHECKPOINT`, `REPLANNING`, `DONE` or `STOPPED` |
| `CHECKPOINT` | The digest (`claude.md`) is bounded and, past 80 % of its cap, compacted. | `PLANNING` or `DONE` |
| `REPLANNING` | A new `Plan` is requested from the current state and digest. | `EXECUTING` |
| `DONE` | Every gate passes, `Q >= q_min`, and the user has been offered the result. | terminal |
| `STOPPED` | Budget, error streak, erosion or the user ended the run. | terminal |

**One iteration** is: plan-or-reuse, execute ready tasks, validate, decide, and
checkpoint the digest. The planner may skip a new plan when the previous one
still has runnable tasks, which keeps a simple loop iteration cheap.

## Components

- `app/contracts/` — the wire format: Pydantic v2, `extra="forbid"`,
  `schema_version=1`, ULIDs, UTC timestamps. The bottom of the dependency graph;
  nothing here imports a higher layer.
- `app/core/` — settings, policy and rubric loaders, ids, hashing, redaction,
  logging and pricing. Config is loaded once and injected.
- `app/accounts/` — encrypted provider credentials, users and JWTs, per-role
  bindings and daily usage roll-ups. Keys are write-only and resolved per call.
- `app/providers/` — the `LLMProvider` protocol and its error family, live
  adapters behind `# VERIFY` markers, `MockProvider`, and the pool that picks an
  account per call with failover and a circuit breaker.
- `app/governor/` — nested budgets (account-month, run, iteration, call), token
  estimation with drift tracking, cache-friendly prompt assembly, telemetry with
  token ROI, and tier-to-model resolution.
- `app/memory/` — the append-only ledger and the bounded digest the Planner
  reads. The digest is the only history in a prompt.
- `app/article/` — sections, versions and hashes, and the patch applier that
  rejects stale `base_version` writes.
- `app/agents/` — the five executors (architect, researcher, writer, reviewer,
  validator). They return `AgentResult` JSON: patches, source cards, section
  state, and for the validator a `QualityReport`.
- `app/planner/` — the Planning-only Claude client: it returns a `Plan` or a
  `Verdict`, never prose for the article.
- `app/orchestrator/` — the deterministic loop controller, dependency
  scheduling, and the erosion/budget/error guards.
- `app/api/` — FastAPI surface: health, effective config, contracts, accounts,
  bindings, and later runs and events.

## Invariants

1. **No fabricated sources.** Missing or unknown data becomes
   `needs_user_input`; a source is cited only through a `SourceCard` id.
2. **No secrets above the account store.** Contracts have no plaintext key
   field; providers receive a key only for the call being resolved.
3. **Bounded prompts.** Rules, rubric excerpt and brief form a cached stable
   prefix; skill bodies and source cards the semi-stable layer; digest, task and
   section text the volatile layer. Over-large prompts trim digest detail, then
   optional skills, then source-card notes, and raise `ContextTooLarge` rather
   than truncate the task.
4. **Bounded history.** `summary` and `snippet` are at most 60 words, and the
   digest at most `digest_max_tokens`; older entries are compacted at 80 % of
   that cap.
5. **Append-only accounting.** Every call is a `ChangeEntry` plus a telemetry
   record; token totals from the provider, the budget manager and telemetry must
   agree.
6. **Deterministic control.** The controller's decisions are `LoopDecision`
   values with snake_case reasons and numeric evidence, so a run can be replayed
   and audited.
