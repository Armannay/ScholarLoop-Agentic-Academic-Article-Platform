---
name: abstract-writing
description: "Write a self-contained abstract in five moves within the rubric word range."
roles:
  - architect
  - writer
  - reviewer
max_tokens: 400
triggers:
  - abstract
  - summary
  - keywords
---

## Five moves, roughly one to two sentences each

1. **Context**: the problem area, stated for a reader outside the subfield.
2. **Gap**: what is missing or unreliable in current knowledge.
3. **Approach**: what this article does about it (design, data, method).
4. **Findings**: the main result, with a number only if the article reports one.
5. **Implication**: what the finding changes for practice or for research.

## Rules

- Stay inside the rubric's abstract range (default 150-300 words).
- Self-contained: no citations, no undefined abbreviations, no "see Section 4".
- No claims that the body does not support; no adjectives standing in for evidence.
- Methods in past tense, established knowledge in present tense, one tense per move.
- Do not open with "This paper presents" for every sentence; vary the subject.
- Do not add keywords unless the brief or rubric asks for them.
- Prefer concrete nouns and numbers over "novel", "significant" and "comprehensive".

## Check

Read only the abstract. Could a reader state the question, the approach and the
main finding? If not, one of the five moves is missing or buried.
