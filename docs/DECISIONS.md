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

## ADR-0006 — Cross-field contract rules live in model validators, not in the exported schema

- **Status:** accepted
- **Date:** 2026-10-23
- **Context:** The v1 audit (docs/CONTRACT-AUDIT.md) found payloads that passed JSON-Schema validation but contradicted the contract's own docstrings: duplicate issue ids, an entry both opening and closing an issue, a `continue` verdict with no target, a language field that is not a language. Expressing these in JSON Schema would mean sprawling `allOf`/`if-then` documents; leaving them to consumers would mean re-implementing them in every consumer.
- **Decision:** Field shapes, enums and numeric bounds stay in the schema; rules that span fields or need a regex over a semantic format live in Pydantic model/field validators. Every such rule gets a test in `backend/tests/test_contracts_validation.py`, and `docs/schemas/README.md` points at the audit so a schema-only reader knows where the rest of the contract lives. `json_schema_for` now declares `$schema` = draft 2020-12 instead of leaving the dialect implicit.
- **Alternatives:** encode every rule in JSON Schema (rejected: unreadable, duplicated logic, and Pydantic would be the source anyway); validate only at the API boundary (rejected: the ledger, the article store and provider adapters construct contracts too); relax the docstrings to match the loose validation (rejected: it would weaken the invariants the orchestrator is built on).
- **Consequences:** the wire format stays stable while the accepted value space matches what the models promise, and repair messages name the offending field. Cost: the exported schema is necessary but not sufficient, so the docs and the test-suite are load-bearing and must be updated with any new rule.

## ADR-0007 — Patch payloads are byte-exact

- **Status:** accepted
- **Date:** 2026-10-23
- **Context:** Every contract strips surrounding whitespace (`Contract.str_strip_whitespace`) because padding is noise in titles, summaries and prompts. `PatchOp` inherited that rule, but the applier matches `old`/`anchor` literally and writes `new`/`text` verbatim. The result was a silent corruption path (`new="b\n\n"` became `"b"`) and an inexpressible operation (inserting a space or a paragraph break), documented in a test as a known limitation awaiting a change request.
- **Decision:** `PatchOp` is the one contract that opts out of whitespace stripping. A patch payload is content: `" "`, `"\n\n"` and whitespace-only `old` values are valid, while `None` and `""` still mean "field missing". The applier stays literal — no normalisation, no collapsing, no retry with a trimmed needle.
- **Alternatives:** keep stripping and require agents to anchor before the gap (rejected: cannot express paragraph breaks, and the failure is silent); normalise the stored section body before matching (rejected: rewrites the artifact and invalidates every hash); add a whitespace-collapsing fallback to the applier (rejected: it already exists *only* to produce a repair hint, never to apply an untrusted match).
- **Consequences:** an agent can express any edit the applier can perform, and what it sends is exactly what lands in the article, which is what HARD RULE 3 and the hash-based versioning assume. Cost: `PatchOp` is a documented exception to a package-wide rule, so future contract changes must decide explicitly whether they are content or prose.
