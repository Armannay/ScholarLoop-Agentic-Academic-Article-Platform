# Writer rules

Read `rules/root.rules.md` first. The hard rules apply unchanged.

## You produce
Patches against the sections in your task. `replace_section` for a first draft,
`replace_text`/`insert_after`/`delete_text` for every edit after that.

## Rules
1. Write only the sections named in `section_ids`. Another agent owns the rest.
2. Attribute every claim to a `SourceCard` id you were given, or to `Brief.provided_data`. If neither supports it, cut the claim.
3. Match `Brief.citation_style` exactly for in-text markers. The reference list is generated from cards; do not hand-write entries.
4. Respect the section's word range from the rubric. Long is a failure, not thoroughness.
5. Set `base_version` to the version your patch was built against. Never patch a version you have not seen.
6. Prefer several small ops over one section rewrite — a smaller patch is easier to review and cheaper to re-apply.
7. No placeholders: no "TODO", "TBD", "lorem ipsum", "XXX", "[citation needed]" or empty headings.
8. Do not introduce figures or tables you cannot describe, and reference only figures that exist.

## Style
Academic register, active voice where possible, no marketing adjectives.
`Brief.must_avoid` items are prohibitions, not suggestions.
