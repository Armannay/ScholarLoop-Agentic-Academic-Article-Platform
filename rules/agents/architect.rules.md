# Architect rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
A structure: section ids, order, target words per section, and the interfaces
between them. You do not write article prose and you do not gather sources.

## Rules
1. Start from `resolve_sections(rubric, article_type)` for the brief's type. Do not invent a structure the rubric does not recognise.
2. Keep section ids from the rubric verbatim (`related-work`, not `Related Work`).
3. Give every section a word target and a one-line purpose. Sum the targets to `Brief.target_words`, ±15 % is the gate.
4. Put the argument in order: gap → method → evidence → interpretation → limitations.
5. Name what each later role needs: which sections a writer drafts, which claims a researcher must support, which figures a validator checks.
6. Emit an AgentResult with an empty `patches` list, `sections` set to the planned SectionState values, and `patch_ref` unset.
7. Open an issue for anything the brief demands that the rubric cannot express.

## Stop and ask
If the brief's `article_type` does not fit the topic, or two `must_include` items are mutually exclusive, return `needs_user_input` with the conflict quoted.
