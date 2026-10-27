# Contract changelog

Every deliberate change to `backend/app/contracts/` — the CONTRACTS v1 wire format —
is recorded here. Contracts are frozen for the duration of a step
([rules/root.rules.md](../rules/root.rules.md), working agreement 4), so a change
arrives as a **CONTRACT-CHANGE-REQUEST** from a step's HANDOFF, is applied in one
batch, and lands together with:

1. the Pydantic model edit,
2. the tests that pin the new behaviour,
3. the regenerated `docs/schemas/` (`make schemas`; CI runs `make schemas-check`),
4. an entry in this file.

`schema_version` stays `1`: this batch is additive across the board (one attribute
relaxed, one union widened). Nothing that already validated stops validating, and no
field was renamed or removed. Consumers that *switch exhaustively* on `ChangeEntry.role`
must handle `'orchestrator'` — that is the one place where "additive" still means work.

---

## 2026-10-27 — PatchOp keeps whitespace; ChangeEntry accepts the orchestrator role

Two accepted CONTRACT-CHANGE-REQUESTs, applied as one batch:

| # | Contract | Change | Requested by |
| --- | --- | --- | --- |
| 1 | `PatchOp` | `str_strip_whitespace=False` (non-empty payload rules unchanged) | Step 3 HANDOFF (patch engine) |
| 2 | `ChangeEntry` | `role: AgentRole \| Literal['orchestrator']` + validators; `AgentResult` refuses orchestrator entries | Step 9a HANDOFF (loop controller, rollbacks) |

No other CONTRACT-CHANGE-REQUEST from the Step 1, 3, 5 or 9a HANDOFFs was accepted in
this batch. The remaining handoff notes are not contract changes and are deliberately
*not* applied here: the `budget_counters` table (`app/governor/budget.py`) and
cache-creation accounting (`app/core/pricing.py`) are storage and pricing concerns, and
the project/ownership model stays an opaque label on the run record
(`app/api/routers/runs.py`).

### 1. `PatchOp` opts out of whitespace stripping

- **Change.** `PatchOp` declares `model_config = ConfigDict(str_strip_whitespace=False)`.
  The base `Contract` keeps `str_strip_whitespace=True`; only this one model opts out.
  `extra="forbid"`, `str_min_length=1` and every rule in `PatchOp._check_payload`
  (required / forbidden payload fields per op verb, `replace_text` no-op) are unchanged,
  so an absent or empty payload is still invalid.
- **Reason.** Whitespace is *payload* in a patch, not formatting. Two shapes that agents
  genuinely need were impossible to express:

  - `insert_after` with a leading space — `{"anchor": "for quality.", "text": " We measure it."}`
    was silently stripped to `"We measure it."`, so the body gained
    `"for quality.We measure it."` and the agent had to anchor on the *previous* token
    to fake the gap. The applier is a literal splice and never adds a space itself, so
    the edit landed one character off — a mismatch between what the agent asked for and
    what the article received.
  - `delete_text` with a trailing newline — `{"old": "Delete this line.\n"}` lost its
    line break and left an empty paragraph behind, so "delete this line" could not
    actually delete a line.

  Stripping at the contract boundary also made the failure invisible: the op validated,
  the patch applied, and only the diff showed the drift.
- **Consequences.** Agents can express exact byte-level edits, so the drift class is
  gone and the `insert_after` workaround (anchoring before the gap) is no longer needed.
  A payload that is only whitespace (e.g. `text=" "`) now survives validation, which is
  a legitimate edit for `insert_after`; an *empty* payload is still rejected. The
  applier still normalises the final section body (`app.core.hashing.normalise_text`),
  so trailing blank lines cannot accumulate through patches.
- **Tests.** `backend/tests/test_article_patch.py`
  (`test_insert_after_can_carry_a_leading_space`,
  `test_delete_text_can_carry_a_trailing_newline`, and the updated splice test) and
  `backend/tests/test_contracts_validation.py`
  (`test_patch_op_keeps_payload_whitespace`,
  `test_patch_op_still_rejects_an_empty_payload`).
- **Schemas.** `docs/schemas/PatchOp.json`, `docs/schemas/Patch.json` (description only).

### 2. `ChangeEntry.role` also names the orchestrator

- **Change.** `ChangeEntry.role` is now `EntryRole = AgentRole | Literal["orchestrator"]`
  (`ORCHESTRATOR_ROLE` in `app/contracts/enums.py`). The model gains:
  - a `mode="before"` field validator that maps a name onto the five `AgentRole` members
    and keeps the literal `'orchestrator'`, and that names the full allowed set on
    failure (the interesting mistake is `'planner'`, which plans and evaluates but never
    writes the ledger);
  - a model validator: an orchestrator entry must not carry `patch_ref` — a rollback or
    emergency action changes state and never authors article text, so a patch pointer
    would misattribute authorship (HARD RULE 3);
  - the `role_name`, `is_orchestrator` and `agent_role` accessors, so callers never have
    to guess whether `role` is an enum member or the literal.
  `AgentRole` itself stays closed at five members: `Task.role` must remain an agent, so
  widening the enum would have let a `Plan` assign work to a non-agent.
- **Reason.** The loop has actions of its own that belong in the append-only ledger —
  rolling back to the best snapshot, an emergency stop or repair. Without a role for
  them, Step 9a's `StopOption.ROLLBACK` path could only stay invisible in the history or
  be recorded under an agent's role, which misattributes work and corrupts the
  per-role token and outcome accounting the digest reports.
- **`AgentResult` rule.** `AgentResult` is by definition authored by an agent, so its
  validator now rejects `role='orchestrator'` outright ("agents cannot emit orchestrator
  entries"). Only the orchestrator writes that role, and only for its own work. The
  constrained output schema shown to models (`AGENT_RESULT_SCHEMA`) narrows
  `ChangeEntry.role` back to the five-member `AgentRole` enum, so an agent is never
  offered a value that would cost it a repair call.
- **Consequences.**
  - Consumers that switch exhaustively on `entry.role`/`CompactedBatch.roles` must handle
    `'orchestrator'`. Every in-tree consumer now goes through `ChangeEntry.role_name`
    (ledger filters and totals, digest lines, compactor render, run usage summary) or
    through `ChangeEntry.agent_role` where an `AgentRole` is genuinely required (source
    verification, which only the validator may perform).
  - `InMemoryLedger.completed_task_ids()` ignores orchestrator entries: a rollback can be
    recorded as `ok` while still naming a task id, and a task the loop merely reported on
    has not been executed. Marking it complete would skip real work in the scheduler.
  - `CompactedBatch.roles` is `list[EntryRole]`, so older orchestrator entries compact
    and stay addressable instead of raising inside deterministic compaction.
- **Tests.** `backend/tests/test_contracts_validation.py`
  (`test_change_entry_accepts_the_orchestrator_role`,
  `test_change_entry_normalises_agent_roles_to_the_enum`,
  `test_change_entry_rejects_unknown_roles_and_names_the_allowed_set`,
  `test_orchestrator_entries_never_carry_a_patch_ref`,
  `test_agent_result_rejects_an_orchestrator_entry`), `backend/tests/test_ledger.py`
  (`test_orchestrator_entries_are_queryable_but_never_complete_a_task`),
  `backend/tests/test_compactor.py`
  (`test_deterministic_compact_keeps_orchestrator_entries_addressable`) and
  `backend/tests/test_agent_runtime.py`
  (`test_output_schema_does_not_offer_the_orchestrator_role`).
- **Schemas.** `docs/schemas/ChangeEntry.json`, `docs/schemas/AgentResult.json`
  (`role` becomes `anyOf [{"$ref": "#/$defs/AgentRole"}, {"const": "orchestrator"}]`).

### Verification

```bash
make schemas        # regenerate docs/schemas/ (committed with this change)
make schemas-check  # CI gate: fails when the committed schemas drift
make check          # ruff + mypy --strict + skill-lint + pytest
```
