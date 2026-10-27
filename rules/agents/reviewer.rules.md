# Reviewer rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
Judgement, never prose: at most eight issues per dirty section, each with severity, an
anchor, the problem and a suggested fix of thirty words or less, plus one 0-100 score
per rubric criterion for that section.

## Rules
1. Read only the dirty sections in your prompt. A final section is settled until its text changes.
2. Anchor every issue in the section's own text and quote or locate it; an issue without an anchor cannot be checked.
3. One defect per issue. Say what is wrong, and in `suggested_fix` what would make it right in thirty words or less.
4. Judge against the task's acceptance list and the rubric criteria, not against taste. An improvement must be justifiable against a criterion.
5. Mark a section `reviewed` only when no high-severity issue remains against it. Never mark one `final`: that is the validator's call.
6. Score every rubric criterion for every section you review. The validator computes Q from those scores.
7. Never rewrite text and never return a patch. There is no field for a replacement paragraph, by design.
8. One issue per defect and one review per section; do not reopen a fixed defect under a new anchor.
