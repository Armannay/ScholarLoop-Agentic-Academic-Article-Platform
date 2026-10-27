CONTEXT: Repo https://github.com/Armannay/ScholarLoop-Agentic-Academic-Article-Platform. Create your branch from the latest main: <arena/29d74702-scholarloop-agentic-academic-a branch>.
FIRST read these files (do not modify them) and confirm in one line each that you read them: everything under backend/app/contracts/, backend/app/core/errors.py, backend/app/providers/{base,errors,mock}.py, config/policies.yaml, plus the EXTRA FILES below. Contracts are frozen (tag <contracts-v1.1>). If you need a contract change, do NOT edit it: list it under CONTRACT-CHANGE-REQUESTS.
Quality gate: make check must pass. Open a PR to main when done, filling Step / Depends on / Contract changes. Save this prompt and your raw output in docs/arena-log/step-<NN>/.
TASK:
<ROLE: Python engineer (HTTP APIs, caching, input hygiene).
GOAL: Deterministic, cache-first tools for the researcher and validator. No LLM calls inside tools.
USE: Tool protocol and ToolRegistry from Step 5; contracts (SourceCard, QualityReport); config/rubric.yaml.
DELIVERABLES (backend/app/tools/)
1. scholar_search.py: query OpenAlex (primary), Crossref and Semantic Scholar (fallback) via httpx; normalise to SourceCard; cache by normalised-query hash (SQLite, TTL 30 days); polite rate limiting and backoff; dedupe by DOI and title similarity; return at most N cards. Mark uncertain endpoints and params # VERIFY.
2. doi_verify.py: resolve a DOI via Crossref and compare title and year with the claimed reference; return {verified, mismatch_fields}; sets SourceCard.verification.
3. citation_check.py: parse in-text markers like [[CITE:source_id]] and the reference list; report orphans in both directions, unresolved ids, duplicate references, and APA7/IEEE style violations (regex level); compute verified_citation_ratio.
4. text_checks.py: word_count (section and article), placeholder_scan (TODO, lorem, XXX, empty headings), abstract_length, required_sections(article_type) driven by config/rubric.yaml.
5. sanitize.py: strip HTML and scripts from fetched text, cap size, drop instruction-like lines, return plain text with provenance.
6. Register every tool in ToolRegistry with a JSON-schema parameter spec and a token-cost hint.
TESTS: recorded HTTP fixtures (no live calls in CI); cache hits; dedupe; DOI mismatch detection; orphan detection; sanitizer against prompt-injection samples.>
EXTRA FILES: <Step 6 (research and validation tools). Use the “Step 6” prompt.
EXTRA FILES: backend/app/tools/base.py and the tool registry from Step 5, config/rubric.yaml, backend/app/core/rubric.py.>
