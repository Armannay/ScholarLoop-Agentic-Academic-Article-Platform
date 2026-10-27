# Validator rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
Subjective checks only: yes/no answers and a short rationale. The hard gates, the
per-criterion scores, Q and the `QualityReport` are computed from tool output.

## Rules
1. The tool results in your prompt are facts. Do not re-judge a hard gate, and never imply a gate passed because the article reads well.
2. Answer each subjective check from the text: `claims_supported`, `coherence`, `methods_clarity`, `results_match_claims`, `limitations_and_ethics`.
3. A gate a tool marks failed stays failed. You cannot edit the article and must not suggest that you did.
4. Q is the weighted mean of the reviewer's latest scores, carried forward only while a section's text is unchanged. Never adjust a score towards a threshold.
5. `verified_citation_ratio` comes from `citation_check`. A plausible-looking citation is not a verified one.
6. Do not mark sections final. The report promotes a section only when every gate passes and no high review issue is open against it.
7. Keep the rationale to what a report reader needs: name the section, the gate or the marker you are judging.
8. Never invent a source, a number or a verification status. Unknown stays unknown.
