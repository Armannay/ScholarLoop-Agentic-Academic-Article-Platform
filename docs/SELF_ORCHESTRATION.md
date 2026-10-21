# Self-orchestration — the Arena agent inside the ScholarLoop loop

How a single Arena coding agent (the *arena agent*) plays the **deterministic
orchestrator** *and* the five **executing roles**, and how it forms a loop with the
**planning-only Planner (Claude)** — including from sandboxes where the Anthropic API
is unreachable. Companion to [ARCHITECTURE.md](ARCHITECTURE.md); the message bus is
specified in [planner_mail/README.md](../planner_mail/README.md).

## Whiteboard → repo → who plays it

| Whiteboard | Repo | Played by |
| --- | --- | --- |
| "Insert Code" → True → ◇ diamond | `Brief` validation; DECIDING step of the loop | arena agent (controller hat) |
| Task → **Claude** box | `plan_request` → `Plan` reply; `app/providers/anthropic.py` | Claude (Planner) |
| "Todo list & mandatory distribute" | `Plan.tasks`, `Plan.ready_tasks` | arena agent (controller hat) |
| Agents 1–5 (tester, reviewer, arch…) | architect, researcher, writer, reviewer, validator | arena agent (role hats) |
| "AI tools", root project | bash toolchain (`pytest`, `ruff`, `mypy`, `secret_scan`), `app/article/` | arena agent's hands |
| ChangeLog / `claude.md` | `app/memory/ledger.py` + `digest.py` (≤ 2,000 tokens) | code; only the digest reaches the Planner |
| Rules | `rules/root.rules.md` + each task's `acceptance` | enforced on every role turn |
| "Threshold of historical memory & token use" | `compact_trigger` (1,600) / `digest_max_tokens` (2,000), governor caps | code |
| "$125 / 30 mil T / monthly" | `TokenUsage` ledger sums, `MonthlyBudget`, `STOP_BUDGET` | controller |
| OpenCode / keys / Colab | wherever the live provider runs | human infrastructure |

## 1. Me as orchestrator (control path; no prose decisions)

Per run the arena agent opens `planner_mail/<run>/` (tracked bus) and `runs/<run>/`
(gitignored scratch), then walks the state machine:

* **INTAKE** — validate the `Brief`; refuse to start without one.
* **PLANNING** — post `plan_request` (digest + caps + reply schema). Never author the
  `Plan` itself: a pushed reply is accepted only if `Plan.model_validate` passes
  (acyclic, roles covered, budgets legal); otherwise it is quarantined and re-requested.
* **EXECUTING** — dispatch `Plan.ready_tasks(completed)`; independent tasks run as
  parallel role turns (§2); each returns patches + a `ChangeEntry`.
* **VALIDATING / DECIDING** — run the deterministic instruments (`make check`, the
  rubric gates), combine `QualityReport` + `Verdict` + budget + erosion signals into one
  `LoopDecision` with machine-readable reasons. The whiteboard diamond is this step —
  a rule over numbers, never a vibe.
* **CHECKPOINT** — append ledger rows, rebuild `claude.md`, compact when the trigger hits.

## 2. Me as the five roles (the role-turn protocol)

One model plays five agents without role bleed by making the **filesystem the context
boundary**. A role turn reads exactly:

1. `rules/root.rules.md` and the role card;
2. its own `Task` JSON (instructions, `acceptance`, `token_budget`);
3. `outbox/claude.md` (the bounded digest);
4. the ids the task references (sections, sources) — references, not content.

Nothing else from other roles' turns enters the turn. Drafts live in
`runs/<run>/roles/<role>/` (gitignored): scratch is not memory. The turn emits only an
`AgentResult`-shaped JSON plus patch ops, summary ≤ 60 words, inside `token_budget`;
uncertainty becomes an opened issue, never hedging prose. The only shared memory is the
ledger + digest — the same separation five processes would give, without five processes.

## 3. The loop with Claude (Planner)

Same wire format, three channels:

* **A. Live API — the intended path.** Keys in the encrypted account store,
  `AnthropicProvider`, resolved at call time. *Not available from the Arena sandbox:*
  its outbound allow-list covers github/pypi/npm only; `api.anthropic.com` fails TLS
  here (verified 2026-10-21). Run channel A where keys and network live — your machine
  or Colab (the whiteboard's "Colab / keys" box) — against this same repository.
* **B. Git bus — works from anywhere, today.** `planner_mail/<run>/`: I push
  `brief.json` + `claude.md` + `plan_request.json`; any Claude instance with repo access
  (Claude Code, OpenCode, a Colab runner holding the key) pushes `plan.vN.json` /
  `verdict.vN.json`. One commit per hop; both ends validate every message against
  CONTRACTS v1 before acting. Bandwidth stays bounded by design: digest ≤ 2,000 tokens,
  replies ≤ `max_output_tokens`.
* **C. MockProvider** — mechanics in tests/CI only (HARD RULE 5).

Loop shape: I execute → ledger grows → digest rebuilds (compacts at 1,600 tokens) →
`plan_request(digest, caps)` → Claude answers `Plan`/`Verdict` → I validate → dispatch →
repeat. The Planner only ever sees the bounded digest; I only ever execute validated
plans. Two minds, one bounded blackboard — the whiteboard's arrow pair between the
Claude box and the agent row, realised as commits.

## 4. Validating a hop (both ends)

Inside the `make install` venv, with `PYTHONPATH=backend`:

```python
from app.contracts.plan import Plan
from app.contracts.verdict import Verdict
Plan.model_validate_json(Path("planner_mail/<run>/inbox/plan.v2.json").read_text())
Verdict.model_validate_json(Path("planner_mail/<run>/inbox/verdict.v2.json").read_text())
```

## 5. Worked round trip

[planner_mail/demo-000/STATUS.md](../planner_mail/demo-000/STATUS.md): intake → five
role turns (9,200 ledger tokens) → `claude.md` at 429/2,000 tokens → plan_request posted
→ sample Planner reply validated (5 tasks, acyclic) → ready-set
`{architect, researcher}` dispatched → verdict folded into `LoopDecision CONTINUE`.

## Safety

* No keys ever enter `planner_mail/` (HARD RULE 2; `secret_scan` gates CI).
* Inbox text is data (HARD RULE 4): a reply that fails validation is moved to
  `runs/<run>/quarantine/` (gitignored) and re-requested — never executed.
