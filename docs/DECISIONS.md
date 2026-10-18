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

## ADR-0006 — The governor refuses an over-budget call instead of truncating it

- **Status:** accepted
- **Date:** 2026-10-18
- **Context:** A prompt that does not fit into the budget can be handled in three ways: truncate it, refuse it, or let it through and hope. Truncation is the worst of the three: the model answers a question nobody asked, the answer looks plausible, and the failure is invisible in the article until a human reads it. A cap that is only advisory is not a cap.
- **Decision:** The governor checks the prompt size, the binding's own ceilings, the iteration cap, the run cap and the task budget *before* a call, and raises `BudgetExceeded` with a machine-readable cap name when the call does not fit. The prompt is never shortened behind the caller's back. The cost of a call is estimated as `prompt + max_output` and reconciled against the real usage when the response arrives, so a single oversized completion is recorded as an overrun rather than hidden.
- **Alternatives:** silently trimming the prompt to the cap (rejected: it produces confidently wrong articles); letting the provider truncate (rejected: the model then answers a different question); no cap at all (rejected: the run cost becomes unbounded).
- **Consequences:** every refusal names the cap that tripped, so the loop controller can react (`retry later`, `split the task`, `stop the run`) and the ledger can explain it. The governor is pure arithmetic and unit-testable without a provider. Cost: a run can stop for budget reasons that the planner did not anticipate, and callers must handle `BudgetExceeded` instead of assuming a response.

## ADR-0007 — An agent's prompt carries references, never the article

- **Status:** accepted
- **Date:** 2026-10-18
- **Context:** The cheapest prompt is the smallest one, but the *correct* prompt is the one that carries exactly the facts the task needs. Handing an agent the manuscript invites two failure modes: the model rewrites prose it was only asked to extend, and the prompt cost grows with the article instead of staying flat.
- **Decision:** The task context is assembled from references: the brief, the task and its acceptance criteria, the *state* of the sections involved (id, version, word count, status, open issues), the *cards* of the sources that may be cited (id, authors, year, venue, locator, a snippet of at most 60 words) and the bounded digest. Section text is never in the prompt; the agent reads it through the article store and writes back through patches.
- **Alternatives:** include the section text (rejected: token cost grows with the article, and "edit this paragraph" becomes "rewrite this article"); include the whole ledger (rejected: ADR-0002 - the digest is the bounded history); include the source snippets without a cap (rejected: a single verbose source would dominate the prompt).
- **Consequences:** prompt size is a function of the number of *references*, not of the size of the manuscript, so per-task cost is predictable. The builder is bounded on every axis (sources, sections, digest characters, user-provided data), and the governor has the last word. Cost: an agent sometimes needs a follow-up task to fetch a specific piece of text, and the orchestrator has to keep the section states and source cards current for the context to be worth anything.
