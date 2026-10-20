---
name: citation-ieee
description: "IEEE numeric citations, first-appearance numbering and reference-list shape."
roles:
  - researcher
  - writer
  - validator
max_tokens: 400
triggers:
  - ieee
  - citation
  - citations
  - references
  - numeric
---

## In-text

- Bracket numerals in order of first appearance: `[1]`, `[2]`.
- Several at once: `[1], [3]` for separate citations, `[1]-[3]` for a consecutive
  range. Never reorder numerals inside a bracket.
- The same source keeps its number for the whole article; the first mention fixes it.
- Place the bracket before the punctuation: `... as shown in [2].`
- A quotation still needs a page locator: `[2, p. 41]`, and must match the card.
- Every numeral resolves to a `SourceCard` id. If no card exists, do not cite it.

## Reference list

- Ascending numeral order, not alphabetical; every entry has exactly one number.
- Shape: `[1] A. A. Family and B. B. Family, "Title," Venue, vol. 3, no. 2, pp. 10-21, 2020, doi: 10.xxxx/yyyy.`
- Use the card's DOI when present; otherwise the URL.
- Do not invent authors, volume, issue, pages or a DOI. Report missing metadata as
  an issue instead of filling it in.

## Checks

Numbers are contiguous from `[1]` and follow first appearance in the body. Body
numerals and reference entries agree in both directions.
