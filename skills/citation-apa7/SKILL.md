---
name: citation-apa7
description: "APA 7 in-text markers and reference-list shape, applied to SourceCards only."
roles:
  - researcher
  - writer
  - validator
max_tokens: 400
triggers:
  - apa
  - apa7
  - citation
  - citations
  - references
  - in-text
---

## In-text

- Parenthetical: `(Smith, 2020)`. Two authors: `(Smith & Lee, 2020)`.
  Three or more: `(Smith et al., 2020)` from the first citation onward.
- Narrative: `Smith (2020)`; `Smith and Lee (2020)`; `Smith et al. (2020)`.
- Multiple works in one bracket are alphabetical and separated by semicolons:
  `(Lee, 2019; Smith, 2020)`.
- A direct quote needs a page or paragraph locator: `(Smith, 2020, p. 14)`.
  Quote only text the SourceCard snippet actually contains.
- Every marker resolves to a `SourceCard` id. If no card exists, do not cite it.

## Reference list

- Alphabetical by first author family name, then year; hanging indent.
- One entry per cited card, and no uncited entry (the both-ways gate).
- Shape: `Family, A. A., & Family, B. B. (Year). Title. Venue, volume(issue), pages. https://doi.org/...`
- Use the card's DOI when present, spelled `https://doi.org/<doi>`; otherwise the URL.
- Do not invent authors, volume, issue, pages or a DOI. Missing metadata stays
  missing and is reported as an issue.

## Checks

Markers match `Brief.citation_style` exactly. Citation count and reference count
agree in both directions.
