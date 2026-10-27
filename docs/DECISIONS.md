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

## ADR-0006 — Pin every prompt asset to the code that sends it

- **Status:** accepted
- **Date:** 2026-10-27
- **Context:** Prompt text is a deliverable *and* the largest fixed cost of a run: the planner system prompt is paid on every planner call. A prompt that exists only as a string constant is invisible in review, and a prompt that exists only as a document drifts from what is actually sent.
- **Decision:** The prompt sent is the Python constant (`app/planner/prompts.py` for the planner, `ContextBuilder` for the agents). `prompts/<role>/` holds a byte-for-byte copy, the short call templates and a golden answer, and `backend/tests/test_planner_prompts.py` fails when a copy, a template's instruction line, a token ceiling or a golden answer stops matching the code.
- **Alternatives:** load the prompt from the markdown file at runtime (rejected: a missing or edited file becomes a production failure, and the file is not type-checked); keep documentation only (rejected: it already drifted once - the planner directory did not exist); generate the markdown from the constant as a build step (rejected: one more artefact to keep out of review).
- **Consequences:** A prompt change is a code change with a documentation diff and a test that states its budget. Cost: two files must be updated together, and the test is the only thing enforcing it.

## ADR-0007 — The planner depends on a context-builder Protocol, not a concrete class

- **Status:** accepted
- **Date:** 2026-10-27
- **Context:** The planner needs prompt layout, the trim order and token accounting, all of which belong to the governor's `ContextBuilder` (a prerequisite step). Importing that class directly would make the planner's public types depend on an implementation it does not own, and would make "swap the builder" a refactor of the planner.
- **Decision:** `app/planner/context.py` declares `PlannerContextBuilder` - a `runtime_checkable` Protocol with the single `plan(task, run_state, *, max_input_tokens, max_output_tokens, json_schema) -> ContextPlan` method - and `PlannerContexts`/`PlannerPromptBuilder` are typed against it. The governor's `ContextBuilder` satisfies it structurally and no call site changed.
- **Alternatives:** import the concrete `ContextBuilder` (rejected: couples the planner to the governor's class and its constructor); copy the layout into the planner (rejected: two trim orders, one prompt, and a second place to get token accounting wrong); an abstract base class (rejected: forces the governor to inherit from the planner, which inverts the dependency).
- **Consequences:** A future builder - or a test double that returns a fixed `ContextPlan` - drops in without touching `service.py`, and `mypy --strict` checks the seam. Cost: the Protocol has to be kept in step with the builder's signature, which is checked at type-check time.

## ADR-0008 — Store projects in a table instead of deriving them from run records

- **Status:** accepted
- **Date:** 2026-10-27
- **Context:** The Step 10 OpenAPI spec (§Design notes, CONTRACT-CHANGE-REQUEST 7) could only derive a project from the distinct `project_id` values on run records, because CONTRACTS v1 has no `Project` document. That derivation cannot hold a display name, a description, the brief a project writes against, its policy overrides or a lifecycle status - and the console needs all five *before* the first run exists. It also makes "reserve this label" a spec-side fiction, and every list call a scan of every run.
- **Decision:** Add a `projects` table (Alembic `0002_projects`): ULID `id`, `owner_id`, `name`, `description`, `brief` (JSON, validated by the frozen `Brief` contract), `policy_overrides` (JSON, validated through `app.core.policies`), `status`, timestamps, a per-owner unique name and a `(owner_id, created_at, id)` index for keyset pagination. `/api/projects` exposes create/list/read/patch/delete. The projects → runs dependency is a `RunsQuery` Protocol (in-memory fake for tests, `StoreRunsQuery` over the persisted run records) so the API never boots an orchestrator to answer a project request.
- **Alternatives:** keep deriving projects from runs (rejected: no metadata, no pre-run reservation, O(all runs) per page); put the project fields on every run record (rejected: N copies of one brief, and a rename would rewrite history that the ledger says is immutable); add a `Project` document to CONTRACTS v1 (rejected: projects never cross an agent or provider boundary, so freezing a wire contract for them buys nothing).
- **Consequences:** A project is a first-class, editable object; its brief is validated once at write time by the same contract agents obey, and a run can name it by id. PATCH and DELETE are refused with 409 while a run of the project is still executing, so an edit cannot race an agent; a `CHECKPOINT` run does not block, because the loop is waiting for a human. Cost: `docs/openapi/openapi.yaml` describes the older derivation and is now out of date for the projects tag (logged in `docs/arena-log/step-11/spec-change-requests.md`, not silently edited), and `RunRecord.project_id` must be read as "the project's ULID, or a legacy slug".
