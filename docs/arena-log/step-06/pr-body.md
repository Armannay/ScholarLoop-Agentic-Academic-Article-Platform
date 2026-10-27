## Step

Step 06 — deterministic research and validation tools. This updates the existing
Step 6 implementation already present on `main`, rather than replacing it.

## Depends on

- Step 5 `Tool` protocol, scoped `ToolRegistry`, and existing role grants.
- Frozen `SourceCard` / `QualityReport` contracts and the rubric loader/config.

## Contract changes

None. The contracts and the requested read-only files are unchanged.

**CONTRACT-CHANGE-REQUESTS:** None. The remote did not advertise a
`contracts-v1.1` tag when checked; schema synchronization was verified against
this branch's `main` base.

## Summary

- Cache normalized search queries for 30 days, use stable source IDs, deduplicate
  similar titles and DOIs, filter all fallbacks by year, and honor rate limits.
- Require title and year evidence for DOI verification and return the requested
  verification/mismatch result with an updated SourceCard.
- Resolve machine citation IDs and rendered markers in both directions; report
  unresolved IDs, duplicates and regex-level APA7/IEEE style violations.
- Add per-section word metrics, empty-heading/placeholder detection, and drop
  instruction-like lines from sanitized retrieved text while retaining provenance.
- Add offline recorded HTTP fixtures, focused regressions, and usage/limitations
  documentation. No LLM calls were added to tools.

## Verification

- `make check`: **1003 passed, 8 skipped**, one existing Starlette deprecation warning.
- `make schemas-check`: 23 contract schemas are up to date.
- Tests use `httpx.MockTransport`; no scholarly-index requests are made in CI.

## Limitations

Style and prompt-injection detection are best-effort heuristics. Index endpoint,
parameter and quota assumptions are marked `# VERIFY`; live service behavior was
not tested in this sandbox. See `docs/research-tools.md`.
