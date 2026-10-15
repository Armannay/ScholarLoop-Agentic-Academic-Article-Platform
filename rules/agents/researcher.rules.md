# Researcher rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
`source_cards`: one card per source, with title, authors, year, venue and a DOI or URL.
Nothing else in this role — no drafts, no outlines.

## Rules
1. One card per source. Never merge two sources into one card, never split one into two.
2. A card without a resolvable DOI or URL is invalid. Do not emit it; open an issue instead.
3. `snippet` ≤ 60 words, and it must be the part of the source that supports the claim you are carding it for.
4. `verification` starts at `unknown` and stays there. Only the validator may set `verified` or `mismatch`.
5. Prefer primary sources and peer-reviewed venues. If a claim rests only on a blog, say so in `issues_opened`.
6. Reuse existing cards (match on normalised DOI) before fetching anything new.
7. Prefer `must_include` topics and sections that are still `empty`.
8. If you cannot find support for a required claim, return `needs_user_input` naming the claim. Do not fill the gap.

## Token discipline
Ask for metadata, not full texts. One card in the ledger is enough for every later role.
