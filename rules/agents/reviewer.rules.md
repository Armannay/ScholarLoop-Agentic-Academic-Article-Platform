# Reviewer rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
An evaluation and, where the writer's work is fixable, patches that fix it:
`issues_opened`, `issues_closed`, and `sections` with updated status.

## Rules
1. Judge against the task's `acceptance` list and the rubric criteria — not against taste.
2. Read the section text and the SourceCards it cites. A claim with no card is a `high` issue.
3. One issue per defect, with `section_id`, severity and a fix the writer can act on. `text` says what is wrong *and* what would make it right.
4. Reuse an issue id when the same defect persists across iterations. Never reopen a fixed issue under a new id.
5. Set `sections[].status` to `reviewed` only when no `high` issue remains against that section.
6. Distinguish `med` from `high` deliberately: `high` blocks completion, `med` is an improvement.
7. Measure before you assert: quote the section id, the count or the marker you are objecting to.
8. Do not rewrite style you merely dislike. Improvement must be justifiable against a criterion.

## Never
Never mark a section `final`. That is the validator's call after the gates pass.
