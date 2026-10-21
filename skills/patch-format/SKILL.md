---
name: patch-format
description: "The four patch ops, their payloads, and how to avoid a rejected patch."
roles:
  - architect
  - researcher
  - writer
  - reviewer
  - validator
max_tokens: 400
triggers:
  - patch
  - edit
  - replace
  - insert
  - delete
  - base version
  - section update
---

## Ops

- `replace_section`: whole new body in `text`. Only for an empty or draft section,
  and only for a first draft. Never carries `old`, `new` or `anchor`.
- `replace_text`: `old` and `new`. `old` is literal and must occur exactly once.
- `insert_after`: `anchor` and `text`. The anchor is literal and must occur once.
- `delete_text`: `old` only. It must occur exactly once; no `new`, `text` or `anchor`.

## Rules that prevent rejection

- `base_version` must equal the section version you were shown. Never patch a
  version you have not seen; re-read the state instead of guessing.
- Patch only the sections in your task's `section_ids`. Another agent owns the rest.
- Prefer several small ops over one rewrite: cheaper to review, cheaper to repair.
- Copy `old` and `anchor` from the current body verbatim, including punctuation.
  Do not paraphrase the anchor or normalise its spacing.
- A patch targets one section. Two patches for one section in one result is an error.
- Empty `ops` is invalid; `replace_text` with `old == new` is a no-op and invalid.

## Output

Return patches inside the `AgentResult` JSON, with every patched section also listed
in `entry.targets`. Do not paste the section body into the summary.
