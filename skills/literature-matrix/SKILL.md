---
name: literature-matrix
description: "Organise sources into a comparison matrix before writing related work."
roles:
  - architect
  - researcher
  - reviewer
max_tokens: 400
triggers:
  - literature
  - related work
  - matrix
  - source comparison
  - evidence map
---

## Build the matrix first

One row per `SourceCard` id. Columns:

| id | question | method / design | sample or data | finding | limitation |

- Fill a cell only from the card's title, venue, year and snippet. Never infer a
  sample size, effect or limitation that the card does not state.
- A cell you cannot fill is `unknown`, not a guess. Open an issue instead.
- Keep the snippet's meaning; do not strengthen a hedged finding into a claim.

## Read the matrix, then write

- Group rows that share a question and a method; name the group in one phrase.
- Within a group, say where the findings agree and where they conflict, and what
  differs (data, population, measure) that could explain the conflict.
- A column that is empty across a group is a gap the article can name.
- Cite by card id; the reference list is generated from cards, never hand-written.

## Checks

Every claim in Related Work traces to at least one row. Every row cited exists as
a card. No row was created to fill a column. Conflicts are reported, not smoothed
over.
