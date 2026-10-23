# CONTRACTS v1 audit

An audit of `backend/app/contracts/` against its own documentation, the consumers that
depend on it, and the HARD RULES. Every finding below was reproduced with a payload
before it was fixed.

**Scope rule.** The v1 *wire format* is unchanged: no field was added, renamed, removed
or made more/less required, and no type changed. What changed is the set of payloads that
can enter the system — every tightening here rejects something a docstring already said
was invalid. The only visible schema changes are `$schema` metadata (all 23 files), three
description strings, and the removal of `minLength: 1` from `LLMResponse.text` (a
*widening*, see C-08).

Verification: `make check` — 825 passed, 8 skipped; `make schemas-check` — 23 schemas
match the models; the new rules are covered in `backend/tests/test_contracts_validation.py`
(section "Hardening") and `backend/tests/test_contracts_roundtrip.py`.

---

## Findings

Severity is about what reaches the orchestrator if the hole stays open: **high** = silent
corruption of the article or the ledger, **medium** = a decision the loop cannot explain,
**low** = a trap for the next step.

| ID | Sev | Contract | Finding | Disposition |
| --- | --- | --- | --- | --- |
| C-01 | high | `PatchOp` | Payload text was whitespace-stripped (`Contract.str_strip_whitespace`), so `" "` and `"\n\n"` could not be expressed and `new="b\n\n"` silently became `"b"`. The applier matches and writes literally. | Fixed |
| C-02 | high | `SectionState` | `advanced()` used `model_copy(update=...)`, which skips validation: `version=0`, a malformed hash and `words=-1` all produced a "valid" state. | Fixed |
| C-03 | high | `Plan` | `missing_required_roles()` always returned `[]`: `role_distribution()` lists all five roles, so the set difference was empty by construction, and the controller could never see a plan missing the architect or validator. | Fixed |
| C-04 | medium | `LoopDecision` | Reason codes only rejected spaces/capitals; `"plateau-detected"`, `"123"`, `"trailing_"` and `"double__underscore"` were accepted, so the "stable snake_case codes" promise was not enforced. | Fixed |
| C-05 | medium | `Verdict` | `meets_minimum=false`, `recommendation=continue` and `missing=[]` was accepted — iterate forever without a target. Only the `meets_minimum=true` variant was caught. | Fixed |
| C-06 | medium | `Brief` | `language` was only length-bounded (2..35): `"hello world"`, `"!!"` and `"en_US"` were accepted as the language written into every prompt. | Fixed |
| C-07 | medium | `QualityReport`, `ChangeEntry` | Duplicate issue ids in one report, and one entry both opening and closing the same id, were accepted — ambiguous churn/open-issue state. | Fixed |
| C-08 | medium | `LLMResponse` | `text=""` failed contract validation *inside the adapter*, where no `ProviderError`/repair path catches it: a content-filtered or max-token-truncated completion raised `ValidationError` out of `provider.generate`. | Fixed (widen) |
| C-09 | low | `AgentResult` | Two `sections` entries for one section in one result were accepted; the store would apply whichever it saw last. | Fixed |
| C-10 | low | `Account` | The plaintext-key guard covered Anthropic/OpenAI/AWS-classic only; OpenRouter (`sk-or-`), Groq (`gsk_`), xAI (`xai-`), Google (`AIza`) and `ASIA` keys passed as "ciphertext". | Fixed |
| C-11 | low | `registry` | Exported schemas never declared `$schema`, although the docstring and ADR-0005 promise draft 2020-12; consumers had to infer the draft from `$defs`. | Fixed |
| C-12 | low | package, `enums`, `agent` | `REQUIRED_ROLES`/`STOP_ACTIONS` were not re-exported next to `GATE_NAMES`; `AccountProvider` claimed "only mock is implemented"; `AgentResult` claimed the reviewer may only set status and issues, contradicting `rules/agents/reviewer.rules.md`. | Fixed |
| C-13 | low | `agent` | Dead statement `_ = AgentRole` left in a validator. | Fixed |

### Evidence for C-01

The limitation was known and documented in the test that encoded it
(`backend/tests/test_article_patch.py`): *"`PatchOp` strips surrounding whitespace from
its fields … so an op cannot express a leading space. Agents anchor on the token *before*
the gap instead; see the CONTRACT-CHANGE-REQUEST in HANDOFF."* There is no HANDOFF file
in the repository, so the request had been lost. The fix makes the contract match the
applier (`body[:end] + payload + body[end:]`, literal `str.find`) and the guidance it
already gives agents ("Copy the exact bytes … whitespace included").

`PatchOp` is now the **only** contract that opts out of `str_strip_whitespace`. It is a
deliberate exception, recorded in ADR-0007, because patch payloads are the one place
where a space is content rather than formatting.

---

## Deliberate non-changes

These look like gaps and are not:

* **`ChangeEntry` has no timestamp.** The ledger is append-only and ordered, and
  `ChangeEntry.id` is a ULID: `ulid_timestamp_ms(entry.id)` recovers the creation time to
  the millisecond. A second field would be a second source of truth.
* **`AgentResult.sections` need not be listed in `entry.targets`; patches must be.** A
  patch mutates a section, so the ledger has to say which one (enforced). A returned
  `SectionState` is advisory reporting — the orchestrator (Step 8) decides whether it is
  applied, and forcing every reported state into `targets` would spend repair calls on a
  rule with no consumer yet.
* **`Plan` construction does not reject a plan with missing roles.** Only the graph is
  validated (ids, dependencies, acyclicity, per-task budget bounds). Role coverage is a
  runtime precondition: a plan is legitimately partial while it is being replanned, and
  the controller refuses to execute one whose `missing_required_roles()` is non-empty.
  ARCHITECTURE's PLANNING step now says exactly that instead of implying construction
  enforces coverage.
* **`QualityReport.Q` is not recomputed from `scores`.** The weights live in
  `config/rubric.yaml`; a contract cannot import the config layer (`core.rubric` imports
  contracts, so the dependency would be circular). The validator/API layer checks the
  arithmetic against the loaded rubric.
* **`TokenUsage.in` + `cached` double counting.** Verified normalised at the adapter
  boundary: Anthropic adds `cache_creation_input_tokens` to `in` and `cache_read` to
  `cached`; the OpenAI-compatible adapter subtracts `cached_tokens` from
  `prompt_tokens`. `total` is the true volume and nothing is counted twice.
* **`Brief`/`Verdict` free lists remain unbounded.** Capping them would reject inputs
  that are valid today; that is a wire change, so it is raised as CCR-01 rather than
  applied.

---

## CONTRACT-CHANGE-REQUESTs

The working agreement (README §4, `rules/root.rules.md` §6) says a contract change is
requested, not edited. Changes in this audit are **applied** because the user scoped this
step to `backend/app/contracts/`; everything that would change the *schema-visible* wire
format is listed here for the next owner.

### CCR-01 — Bound every free-text field

* **Status:** proposed
* **Fields:** `PatchOp.new`, `PatchOp.text` (≤ 40,000 chars ≈ a 6k-word section);
  `Task.acceptance[]` (≤ 300 chars); `Brief.provided_data[]` (≤ 4,000 chars, ≤ 50
  entries); `Brief.must_include[]`/`must_avoid[]` (≤ 40 entries);
  `Verdict.missing[]`/`risks[]` (≤ 300 chars).
* **Why:** the contracts are the last choke point before these strings enter a prompt or
  the article store. Today `replace_section.text` accepts an entire article in one patch,
  which contradicts HARD RULE 3 ("patches, not rewrites") even though the governor caps
  output tokens.
* **Impact:** `maxLength`/`maxItems` in six schemas; a previously accepted oversized
  payload becomes a repair instead of a spend. No consumer changes.
* **Recommendation:** apply with the Step 7 prompt work, so the limits can be tuned
  against real section sizes rather than guessed.

### CCR-02 — Give `LoopDecision.evidence` a JSON value type

* **Status:** proposed
* **Change:** `dict[str, Any]` → `dict[str, JSONValue]`, where
  `JSONValue = str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]`.
* **Why:** `Any` admits datetimes, enums and objects that `model_dump_json` may coerce
  differently from `model_dump`. The field's own docstring promises "numbers behind the
  call"; the schema should say so.
* **Impact:** schemas gain an `anyOf`; a decision carrying a non-JSON value (a bug) now
  fails at construction. The controller and API are unaffected.
* **Recommendation:** apply before the loop controller writes its first decision.

### CCR-03 — Constrain the account fingerprint

* **Status:** proposed
* **Change:** `AccountPublic.key_fingerprint` gains `pattern: ^[0-9A-F]{12}$`.
* **Why:** the field is "first 12 hex chars of sha256(ciphertext)" but accepts any
  12-character string, so a UI cannot distinguish a fingerprint from a placeholder.
* **Impact:** one pattern in `AccountPublic.json`; `key_fingerprint()` already complies.
* **Recommendation:** apply with the next accounts change (low risk, no consumer edits).

### CCR-04 — Bind `QualityReport.scores` keys to the rubric

* **Status:** proposed (no contract change recommended)
* **Issue:** a typo'd criteria key (`coherance`) is accepted, scores nothing, and `Q`
  still claims to be a weighted average.
* **Options:** (a) freeze the nine criteria names in contracts like `GATE_NAMES`; (b)
  validate against `config/rubric.yaml` in `app.core.rubric` / the API; (c)
  dependency-inject the rubric into the report.
* **Recommendation:** (b) now — the rubric is data and gates already drift-check against
  it (`backend/tests/test_rubric.py`). Revisit (a) only if criteria become part of v2.

### CCR-05 — Reject `0`/`1` for `QualityReport.gates`

* **Status:** proposed (behaviour only, no schema change)
* **Change:** a `mode="before"` validator requiring real booleans.
* **Why:** Pydantic's lax bool coercion turns `1`/`0` into pass/fail silently; a model
  that emits numbers is guessing at a gate result.
* **Recommendation:** apply with the validator role (Step 7), where the cost of a repair
  is understood.

---

## How to re-run the audit

```bash
make check           # ruff + mypy --strict + skill-lint + pytest (825 passed)
make schemas-check   # the committed schemas match the models
.venv/bin/python -m pytest backend/tests/test_contracts_validation.py -q
.venv/bin/python -c "from app.contracts import *; print(len(CONTRACT_NAMES))"  # 23
```

Adding a contract still requires a fixture in `backend/tests/factories.py`; the
round-trip suite fails loudly when one is missing. Adding an enum member is still a
contract change: consumers switch exhaustively.
