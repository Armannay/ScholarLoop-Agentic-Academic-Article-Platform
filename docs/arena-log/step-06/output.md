# Step 06 — raw output

Date: 2026-10-27. Session branch: `arena/2663b4cf-scholarloop-agentic-academic-a`.
The branch was provided by Arena and started at `010b0e9f36cc5618c7e231284564b378a0892d16`,
which matched the remote `main` at checkout and when checked after implementation.
The prompt named another branch; the Arena session branch is fixed, so no other
branch was created or used. Step 6 files were already present on `main`; they
were audited and corrected rather than recreated.

## Required-file confirmations

Read `backend/app/contracts/__init__.py`.
Read `backend/app/contracts/account.py`.
Read `backend/app/contracts/agent.py`.
Read `backend/app/contracts/binding.py`.
Read `backend/app/contracts/brief.py`.
Read `backend/app/contracts/common.py`.
Read `backend/app/contracts/enums.py`.
Read `backend/app/contracts/ledger.py`.
Read `backend/app/contracts/llm.py`.
Read `backend/app/contracts/loop.py`.
Read `backend/app/contracts/patch.py`.
Read `backend/app/contracts/plan.py`.
Read `backend/app/contracts/quality.py`.
Read `backend/app/contracts/registry.py`.
Read `backend/app/contracts/section.py`.
Read `backend/app/contracts/source.py`.
Read `backend/app/contracts/task.py`.
Read `backend/app/contracts/verdict.py`.
Read `backend/app/core/errors.py`.
Read `backend/app/providers/base.py`.
Read `backend/app/providers/errors.py`.
Read `backend/app/providers/mock.py`.
Read `config/policies.yaml`.
Read `backend/app/tools/base.py`.
Read `backend/app/tools/registry.py`.
Read `config/rubric.yaml`.
Read `backend/app/core/rubric.py`.

## Result

- Search now uses normalized, limit-independent 30-day SQLite caching, stable
  source IDs, title-similarity/DOI deduplication, consistent year filtering,
  bounded polite retry/backoff, and recorded OpenAlex/Crossref/Semantic Scholar
  fixtures. Incomplete abstracts use a clearly labeled title excerpt.
- DOI checks require both title and year evidence, return `verified` and
  `mismatch_fields`, and produce a complete updated SourceCard or a bounded
  verification delta.
- Citation checks parse machine and rendered markers, report graph and reference
  problems and basic APA7/IEEE violations, and compute a conservative unique-ID
  verification ratio. Text checks expose per-section/total counts and detect
  placeholders and empty headings. Sanitization drops instruction-like lines,
  strips active markup, and keeps provenance with bounded JSON output.
- The existing Step 5 registry and role policy already register all five tools;
  their read-only files were not modified. Tool-specific schemas/cost hints are
  exposed by the concrete tools.
- Live index requests were not used in tests. The sanitization and citation-style
  checks are heuristics; see `docs/research-tools.md` for limitations.

## Raw checks

The complete `make check` stdout/stderr is in `make-check.txt`:

```
ruff check backend: All checks passed!
ruff format --check backend: 180 files already formatted
mypy: Success: no issues found in 119 source files
skill lint: 7 skill(s) OK in skills
pytest: 1003 passed, 8 skipped, 1 warning in 20.06s
EXIT=0
```

`make schemas-check` output is in `schemas-check.txt`: 23 contract schemas are
up to date. The warning is Starlette's existing httpx deprecation warning.

## CONTRACT-CHANGE-REQUESTS

None. No files under `backend/app/contracts/` or other requested read-only files
were modified. `git ls-remote` did not list a remote `contracts-v1.1` tag, so the
frozen files were checked against the session's `main` commit, not a tag SHA.
