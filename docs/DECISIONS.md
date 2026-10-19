# Architecture decision records

One record per decision that is expensive to reverse. Newest at the bottom.
Never edit a merged ADR: supersede it with a new one and link both ways.

## Template

```markdown
## ADR-000N — <decision, imperative>

- **Status:** proposed | accepted | superseded by ADR-000M
- **Date:** YYYY-MM-DD
- **Context:** the forces at play, in two or three sentences.
- **Decision:** what we will do.
- **Alternatives:** what we rejected, and why.
- **Consequences:** what this makes easy, what it makes hard, what it costs.
```

---

## ADR-0001 — Keep the orchestrator deterministic and the Planner planning-only

- **Status:** accepted
- **Date:** 2026-10-15
- **Context:** An LLM in the control path makes runs unreproducible, unbounded in cost and hard to debug: the same input can produce a different number of iterations, and every extra control decision is another paid call.
- **Decision:** The loop is plain Python. The Planner (Claude) returns a `Plan` and evaluates state to produce a `Verdict`; it never writes article text. Five agents execute tasks and return patches plus a `ChangeEntry`.
- **Alternatives:** a fully agentic "manager" LLM (rejected: non-reproducible, expensive, untestable without live calls); a fixed pipeline with no planning (rejected: cannot adapt to article type or user feedback).
- **Consequences:** every transition is unit-testable with `MockProvider`; budgets are enforceable; the Planner cannot silently edit the article. Cost: creative freedom moves to prompt and rubric design, and the controller's heuristics become load-bearing code that needs its own tests.

## ADR-0002 — Append-only ledger with a bounded digest as the only Planner history

- **Status:** accepted
- **Date:** 2026-10-15
- **Context:** Prompt cost grows with history; the Planner needs enough context to re-plan, but not the whole run.
- **Decision:** `ChangeEntry` rows are appended and never mutated. The Planner reads `claude.md`, a digest capped at `digest_max_tokens` (default 2,000): the last `recent_window` entries verbatim, older history compacted at `compact_trigger` (0.8) of the cap.
- **Alternatives:** pass the full ledger (rejected: cost grows linearly, and long context degrades instruction-following); summarise the article text (rejected: paraphrasing the artifact is lossy and hides provenance).
- **Consequences:** prompt size is a constant, so per-iteration cost is predictable; auditability is preserved because the ledger keeps everything. Cost: compaction can lose nuance, so summaries are capped at 60 words and the digest keeps ids and statuses it can be re-expanded from.

## ADR-0003 — Patch operations instead of section rewrites

- **Status:** accepted
- **Date:** 2026-10-15
- **Context:** Rewriting a section to change one sentence costs full output tokens, makes review diffs unreadable, and lets an agent silently delete work it was asked to preserve.
- **Decision:** Agents return `Patch{section_id, base_version, ops[]}` with `replace_text | insert_after | delete_text | replace_section`. Ops are literal-match, the applier rejects a patch whose `base_version` is stale, and `replace_section` is the escape hatch for first drafts.
- **Alternatives:** whole-file rewrites (rejected: token-expensive and unsafe under parallelism); line-number or regex patches (rejected: brittle against reformatting, and regex opens a footgun for untrusted model output).
- **Consequences:** cheap edits, readable diffs and safe parallelism; every patch is applied against a hash and version, so a stale write fails loudly. Cost: agents must express intent as deltas, and the applier needs careful tests.

## ADR-0004 — Accounts are resolved at call time, encrypted, never cached in a binding

- **Status:** accepted
- **Date:** 2026-10-15
- **Context:** Users add, delete and change API keys at any time, including mid-run. A cached credential or a provider instance holding an old key would either fail opaquely or, worse, keep spending on a revoked account.
- **Decision:** `Account` stores `key_ciphertext` only. `AgentBinding` holds `account_ids` plus a strategy (`dedicated | round_robin | failover`). The store is opened per call; `AccountPublic` exposes a 12-character fingerprint of the ciphertext, never key material.
- **Alternatives:** keys in environment variables (rejected: no rotation, no per-account budget, easy to leak into logs); provider instances cached per role (rejected: no mid-run account changes, stale credentials).
- **Consequences:** a disabled account takes effect immediately; per-account budgets are enforceable. Cost: one decrypt per call (cheap, and rate-limited by iteration caps) and a hard requirement that nothing above the store holds a key.

## ADR-0005 — Contracts are hand-written Pydantic v2, exported to JSON Schema

- **Status:** accepted
- **Date:** 2026-10-15
- **Context:** The same shapes cross the API, the ledger, provider calls and prompt schemas. Generating code from schemas gives weak runtime checks; generating schemas from models gives no compile-time check at all.
- **Decision:** One Pydantic v2 module per contract in `backend/app/contracts/`, `extra="forbid"`, `schema_version: Literal[1]` on documents, string ULIDs, UTC timestamps. `make schemas` exports JSON Schemas to `docs/schemas/`, and CI fails if the committed schemas drift from the models. Contract tests assert round-trip losslessness, unknown-field rejection and the registry's completeness.
- **Alternatives:** JSON Schema as the source of truth (rejected: loses Python typing, so `mypy --strict` cannot help); protobuf/OpenAPI-first (rejected: heavy for a four-person build, and prompts want plain JSON Schema).
- **Consequences:** one definition serves runtime validation, IDE autocompletion, documentation and the schemas handed to models. Cost: contract changes must be deliberate — a field rename is a breaking change that requires a CONTRACT-CHANGE-REQUEST and a re-export.

## ADR-0006 — Fix the loop-decision precedence; the Planner's verdict is advice

- **Status:** accepted
- **Date:** 2026-10-19
- **Context:** The controller sees three kinds of signal at once - human input, measured quality (gates, Q, erosion), and the Planner's opinion - and they routinely disagree: an acceptable article can be out of budget, a plateauing one can still have failing gates, and the Planner may ask for more polish after the minimum is met.
- **Decision:** A fixed precedence decides every iteration: `STOP_USER` > `ASK_USER` (`needs_user_input`) > `STOP_ERRORS` > acceptable quality (`OFFER_FINISH`: all gates pass, `Q >= q_min`, no high-severity issue) > `STOP_BUDGET` > `STOP_EROSION` > `CONTINUE`. An acceptable article is offered even when budget or erosion fires, with those codes recorded in `reasons`. The `Verdict` can delay an offer (missing minimum work) but never create one. One iteration is one full plan pass; parallel execution applies its patches serially in plan order, and `STOP_EROSION` restores the best-Q snapshot.
- **Alternatives:** letting the verdict overrule measurements (rejected: an LLM opinion must not finish an article with a failing gate); weighing detectors into one score (rejected: not auditable - `LoopDecision.evidence` must reconstruct why); stopping erosion even when quality is acceptable (rejected: destroys a deliverable the user could accept).
- **Consequences:** every run's ending is reconstructible from `decisions[]`, and threshold tuning stays policy work. Cost: a `continue` verdict after quality is met can keep the loop spending until budget or erosion ends it - bounded, by design, by the same precedence.

