# demo-000 — one round trip of the arena<->claude loop

Channel: **git bus** (`planner_mail/`), because this sandbox's network allow-list
reaches github/pypi/npm only; `api.anthropic.com` is unreachable from here
(verified 2026-10-21). The Planner half below is a **sample** reply, validated
against CONTRACTS v1 exactly as a real pushed reply would be.

- **S1 intake** — Brief validated against CONTRACTS v1 -> outbox/brief.json
- **S2 executing** — arena agent played all five roles; 5 ChangeEntry rows appended; ledger total 9200 tokens
- **S3 checkpoint** — claude.md rendered: 429/2000 tokens (21% of cap), compact_at=1600, verbatim window=3
- **S4 outbox** — plan_request.json posted; wire size 128 tokens + digest 429; channel = git (api.anthropic.com unreachable in sandbox)
- **S5 inbox** — plan v2 validated (5 tasks, acyclic, roles covered); ready now: ['architect:Re-cut section bounds for related work', 'researcher:Close provenance gaps on two claims']
- **S6 verdict** — verdict validated: meets_minimum=False, recommendation=continue; controller (me) combines it with budget + gates -> LoopDecision CONTINUE
- **S7 accounting** — iteration spend in=6100 out=3100
  total=9200; digest utilisation 21%;
  compaction triggers at 1600 tokens (board: 'threshold of
  historical memory and token use').

Next hop: arena/orchestrator dispatches `architect:Re-cut section bounds for related work` and `researcher:Close provenance gaps on two claims` in parallel,
appends their ChangeEntry rows, rebuilds claude.md, posts plan_request for iteration 2.
