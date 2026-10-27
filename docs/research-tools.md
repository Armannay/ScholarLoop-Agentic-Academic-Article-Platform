# Research and validation tools

Step 6 tools are deterministic and make no LLM calls. Construct them with
`app.tools.registry.build_tool_registry()` and invoke through `ToolRegistry`.
`config/tool_policy.yaml` grants search and sanitization to the researcher;
the validator receives DOI, citation, text and sanitization tools. Other roles
are denied by default. Every tool exposes a parameter schema and `token_cost`.
Results are always untrusted data; a caller must not follow instructions in them.

## Search

`scholar_search` accepts `query`, optional `max_results` (1–20), and optional
`year_from`/`year_to`. OpenAlex is tried first; Crossref and then Semantic Scholar
are fallbacks if the preceding index fails or yields no usable cards. Index
endpoint, field, and filter assumptions are marked `# VERIFY` in the adapters.
Results are `SourceCard` JSON objects with `verification: "unknown"`.

Queries are normalized with Unicode NFKC, case folding, and whitespace folding.
The SQLite cache key hashes that query plus the year bounds, not the requested
result count; a bounded batch is cached for 30 days and sliced to N on return.
The default database is `~/.cache/scholarloop/research.sqlite3`; callers can
provide a different path. Search terms and source metadata are stored there, so
use an appropriately protected path. One instance serializes cache misses and
spaces HTTP requests by one second by default. Retries are bounded, honor a
reasonable `Retry-After`, and fall back rather than retry early if the requested
wait is too long. Independent instances/processes do not share a rate limiter.

Duplicate DOIs and highly similar normalized titles are discarded in index
order. The title similarity heuristic can merge distinct works with very similar
titles; review the results. Cards require a real title, year, venue, and DOI or
URL; missing metadata is not invented. If there is no abstract, the snippet is
explicitly labeled `Title:` and quotes the available title, not a claimed
abstract. IDs are stable content-derived ULIDs with an epoch timestamp prefix;
they are not creation-time timestamps. The response has `cache_hit`, `count`,
`result_limited` and `sources`. Large result sets may be shortened to preserve
a valid JSON envelope under the registry's default output cap.

## DOI verification

`doi_verify` accepts a complete `source` object. Crossref is queried by its
normalized DOI. The response contains `verified`, `mismatch_fields`,
`missing_fields`, `comparisons`, and `updated_source` (a complete SourceCard).
Both title and publication year must be present and match before verification;
a missing field stays `unknown`, and a definite discrepancy is `mismatch`. A
returned DOI, when present, must also match. Online and print publication years
are accepted, while the metadata record's creation date is not a publication
year. Unavailable Crossref metadata returns `ok: false`, not a verification.
For unusually large cards, `updated_source` is omitted (`null`), the result is
marked truncated, and `source_update` carries the ID and verification state for
the caller to apply to its original card. Persistence remains the validator's
responsibility through the SourceStore's role-checked verification operation.

## Citation checks

`citation_check` accepts `iter`, `citation_style`, `sections`, `references`, and
`source_cards`. Each section has `id` and `text`. A structured reference has a
rendered `key`, its reference `text`, and an optional `source_id`. Alternatively,
`references` may be a one-entry-per-line string; include `[[CITE:<source_id>]]`
in a line to associate it with a card. If the array is empty, the tool also
looks for a `references` section. Raw text is parsed heuristically; ambiguous
or missing source IDs are reported instead of inferred from a title.

The body may contain `[[CITE:<source_id>]]` markers, APA author-date citations,
or IEEE numeric citations. Both directions of the citation/reference graph,
unresolved IDs, duplicate entries, and basic APA7/IEEE in-text **and reference**
forms are checked. Unrendered machine markers deliberately fail the style gate:
they are valid graph identifiers but are not final APA/IEEE rendering. Harvard
and Vancouver require manual style review. These regex checks are incomplete and
may produce false positives or negatives; they are not a substitute for an
editorial review. The response exposes diagnostic lists and counts plus a
partial `QualityReport`; only the names in `assessed_gates` were checked. The
verified ratio uses unique cited IDs, with unresolved citations in the
denominator (zero when there are no citations). Diagnostic and issue truncation
is explicit; verification-or-flagging fails closed if issues are omitted.

## Text checks

`text_checks` accepts `iter`, `article_type`, `target_words`, and sections. The
rubric determines required sections, abstract range and configurable placeholder
patterns. Standalone `lorem` and empty Markdown headings are also flagged.
`metrics.section_words` and `metrics.total_words` use the contracts' shared
whitespace-based word count; the article total excludes the references section.
Duplicate/invalid section IDs are rejected rather than silently overwritten.
Only `assessed_gates` in the partial QualityReport were evaluated. When output
limits require it, omitted issue and section-count totals are recorded.

## Sanitization

`sanitize_text` accepts `text` and `provenance` and returns plain `text`, the
unchanged provenance, a removed-instruction-line count, and truncation state.
Input is capped before parsing; HTML, active elements and common instruction-like
lines are stripped. The tool result remains explicitly untrusted. This is a
best-effort heuristic, not a prompt-injection security boundary: obfuscated or
novel instructions can survive, and innocent lines can be removed. Never execute
or obey retrieved content. JSON output is shortened when necessary to fit the
default registry result cap without being cut mid-document.

## Tests

`backend/tests/fixtures/research/` records the source and reductions for offline
HTTP response snapshots. The tests use `httpx.MockTransport` only and do not
contact scholarly services. Run `make check` for the full quality gate.
