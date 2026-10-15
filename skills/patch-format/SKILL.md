---
name: patch-format
description: Express every article edit as a minimal, versioned patch with a literal anchor and valid operation payload.
roles: [architect, researcher, writer, reviewer, validator]
max_tokens: 400
triggers: [patch, edit section, revise section, section update]
---
# Patch format

Return edits only as `Patch` objects for section ids named by the task. Set `base_version` to the section version supplied in context. Do not write to an article store or replace unrelated sections.

Use `replace_section` only for a first draft or an explicitly requested full replacement. Prefer `replace_text` with exact `old` and `new` strings, `insert_after` with an exact `anchor` and inserted `text`, or `delete_text` with exact `old` text. Include only fields allowed for the selected operation; every required payload must be non-empty. Keep operations small and ordered. If the anchor is unavailable or the base version is unknown, request clarification instead of guessing.
