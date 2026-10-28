# QA and security status

This is an evidence inventory, not a security certification. Run the offline suite with
`python -m pytest -q`. No live provider is contacted by that suite.

## Existing checks

- Secret redaction across the account lifecycle: `backend/tests/test_no_key_leak.py`;
  tree scanning: `backend/tests/test_secret_scan.py`. These do not establish that every
  possible prompt or exception path is secret-free.
- Authentication and owner isolation: `test_accounts_api.py`, `test_projects_api.py`,
  `test_runs_api.py` and `test_runs_article_api.py`. They are not an exhaustive audit of
  every registered route and method.
- Injection samples, sanitization bounds and provenance: `test_step6_tools.py` and
  `test_research_tools.py`. Sanitization is best effort, not an instruction boundary;
  fetched material remains untrusted even after filtering.
- Provider account resolution and failover: `test_provider_pool.py`; orchestrator stop,
  rollback, checkpoint and resume behaviour: `test_orchestrator_runner.py` and
  `test_orchestrator_persistence.py`. The orchestrator tests use scripted port doubles,
  not an end-to-end MockProvider-backed stack.
- Search and DOI clients construct fixed upstream URLs. Before adding arbitrary URL
  fetching, test private and loopback addresses, DNS rebinding, redirects and response
  size limits at the transport boundary. Existing tests do not prove complete SSRF
  resistance across all outbound adapters.

## Dependency audit

On 2026-10-28, `pip-audit --local --progress-spinner off` reported vulnerabilities in
local environment tooling: pip 23.0.1 and setuptools 66.1.1. The local editable
`scholarloop` distribution could not be matched to PyPI. Update and re-audit the build
and runtime environments independently; an audit of an environment is not a complete
transitive lockfile audit. Do not treat these findings as a clean dependency scan.

## Coverage

CI measures statement coverage separately for `orchestrator`, `governor` and `memory`,
and fails if any is below 80%. This does not imply branch coverage or end-to-end
correctness. The local suite measured 92%, 93%, and 96%, respectively, on 2026-10-28.

## Remaining release work

- Add full MockProvider-backed end-to-end scenarios, including account deletion during
  a run and crash recovery.
- Add a reproducible real-pipeline token benchmark and simulations before tuning stop
  thresholds. A synthetic estimate is not evidence of production savings.
- Audit every route and outbound adapter, including authorization, prompt handling,
  redirects, DNS and byte limits; independently review the results.
- Add an opt-in real-provider smoke test only after a verified hard monetary cap can be
  enforced. No real-provider test has been run as part of this review.
