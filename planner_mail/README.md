# planner_mail — the git bus between the arena agent and the Planner

Tracked mailbox for the loop described in
[docs/SELF_ORCHESTRATION.md](../docs/SELF_ORCHESTRATION.md). Git is the transport:
one commit per hop, both ends validate every message against CONTRACTS v1.

```
planner_mail/<run>/
  outbox/brief.json         Brief, contract-validated at intake
  outbox/claude.md          bounded digest, <= digest_max_tokens (default 2,000)
  outbox/plan_request.json  envelope: digest pointer, caps, reply schema + reply path
  inbox/plan.vN.json        Planner reply; validated as Plan before any dispatch
  inbox/verdict.vN.json     Planner evaluation; validated as Verdict before DECIDING
  STATUS.md                 append-only transcript of hops and token accounting
```

Rules:

1. Validate on write and on read. An inbox file that fails validation moves to
   `runs/<run>/quarantine/` (gitignored) and the request is re-posted; it is never acted on.
2. Size caps: `claude.md` ≤ `digest_max_tokens`; envelope ≤ `call_input_cap`; replies ≤
   the `max_output_tokens` stated in the request.
3. One commit per hop, message `planner-mail(<run>): <kind> v<n>`.
4. No secrets, ever (HARD RULE 2); `app/scripts/secret_scan.py` gates CI.
5. `runs/` stays gitignored: scratch, drafts and quarantine never travel on the bus.
