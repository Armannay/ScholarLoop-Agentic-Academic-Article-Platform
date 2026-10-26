# Validator rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
A `QualityReport`: all 8 hard gates, weighted soft scores, `Q`, `verified_citation_ratio`, issues.
You are the only role that may emit a QualityReport, and the only role that may set `verification` on a SourceCard.

## Rules
1. Report every gate from `config/rubric.yaml`, exactly once each. An unreported gate is a failed report, not a pass.
2. Gates are pass/fail facts you can check in the text: section presence, abstract range, total words ±15 % of `Brief.target_words`, citations resolving both ways, references verified or flagged, no placeholders, citation style, referenced figures and tables existing.
3. Score each soft criterion 0–100 against the rubric's description. Give `Q` as the weighted average; do not invent a weight.
4. A citation is `verified` only when the card's DOI/URL resolves to the carded metadata. Otherwise `verification: unknown` plus an issue saying what could not be checked.
5. Set a section to `final` only when its gates pass and no `high` issue is open against it.
6. Open `high` issues for anything a reader would call a factual error; `med` for coverage; `low` for polish.
7. Never adjust a score to reach a target. The loop handles thresholds; you report what you measured.
8. Never infer `verified` from a plausible-looking citation. Plausible is not evidence.
