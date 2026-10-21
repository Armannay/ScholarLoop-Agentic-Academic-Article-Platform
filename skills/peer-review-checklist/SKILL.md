---
name: peer-review-checklist
description: "Structured self-review before validating: gates, evidence and actionable issues."
roles:
  - reviewer
  - validator
max_tokens: 400
triggers:
  - review
  - reviewer
  - checklist
  - feedback
  - quality
  - validation
---

## Walk the eight hard gates

1. Every required section exists, exactly once, in rubric order.
2. Abstract sits inside the rubric range.
3. Total words are within 15% of `Brief.target_words`.
4. Citations resolve both ways: every marker has a card, every card is cited.
5. References are verified or explicitly flagged `unknown`.
6. No placeholders: TODO, TBD, XXX, `[citation needed]`, empty headings.
7. In-text style matches `Brief.citation_style`.
8. Every referenced figure or table exists, and every figure or table is referenced.

Report each gate as pass or fail with the measurement behind it. An unmeasured gate
is a fail, never a pass.

## Then the soft criteria

Score against the rubric's descriptions. Quote or locate the evidence for each
score; "reads well" is not evidence. Do not adjust a score to reach a threshold.

## Issues

- `high`: factual error, fabricated or unresolvable source, gate failure.
- `med`: missing coverage, unsupported claim, structural weakness.
- `low`: clarity, register, repetition.
Each issue names the section and says what would close it. Open the issue; do not
rewrite the section yourself.
