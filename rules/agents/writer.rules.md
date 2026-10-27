# Writer rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
One section, as a `Patch`. `replace_section` for an empty or draft section;
`replace_text`, `insert_after` and `delete_text` once it has been reviewed.

## Rules
1. One section per task. Write only the section your task names.
2. Attribute every claim to a supplied `SourceCard` through `[[CITE:source_id]]`, or to `Brief.provided_data`. If neither supports it, cut the claim.
3. Report exactly the ids you cited in `output.cited_source_ids`. Never cite an id that is not among the supplied cards.
4. Respect the word window in your prompt (target plus or minus ten percent). Long is a failure, not thoroughness.
5. Set `base_version` to the version you were shown; never patch a version you have not seen.
6. No placeholders: no TODO, TBD, lorem ipsum, XXX, or a heading with no content.
7. If a result, dataset or number you need is missing, return `needs_user_input` with no patch and name what you need.
8. Keep the patch minimal: several small ops beat rewriting a section that already exists.
