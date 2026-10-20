---
name: imrad-structure
description: "IMRaD section order, purpose and length discipline for research articles."
roles:
  - architect
  - writer
  - reviewer
max_tokens: 400
triggers:
  - imrad
  - structure
  - outline
  - section order
  - related work
  - methodology
  - results
  - discussion
---

## Order

Introduction, Related Work, Methodology, Results, Discussion, Limitations,
Conclusion, References. Use the rubric's section ids and word ranges; the rubric
wins over this skill whenever they disagree.

## What each section must do

- **Introduction**: problem, why it matters, the gap, one sentence stating the
  contribution. Funnel from general to specific. No results.
- **Related Work**: group prior work by idea, not by paper. Say what each group
  established and what it left open. End on the gap this article fills.
- **Methodology**: enough detail for a competent reader to repeat the work. Data,
  procedure, measures, analysis choices; justify each choice in one clause.
- **Results**: observations only, with counts, ranges and units. No interpretation;
  point to tables and figures that actually exist.
- **Discussion**: interpret the results against the gap and against prior work.
  Separate what was measured from what it might mean.
- **Limitations**: concrete threats to validity, one sentence each, plus the
  smallest honest mitigation. Do not write a disclaimer paragraph.
- **Conclusion**: what changed in the reader's understanding, and the next question.
  Do not introduce new evidence or new citations here.

## Checks before returning

Every required section exists, appears once, and sits in the rubric's order. Each
section answers its own question and does not repeat another section's job.
