# Offline HTTP response fixtures

These are reduced JSON response snapshots, used only with `httpx.MockTransport`.
No test sends a request to a scholarly index. Fields unrelated to these adapters
(and Semantic Scholar abstracts) were removed to keep the fixtures small; the
remaining values and response envelopes come from the following public fixtures:

- OpenAlex: [recorded `openalex_search.yml` cassette](https://github.com/nexus-scholar/core/blob/master/tests/Fixture/vcr_cassettes/openalex_search.yml).
- Crossref search: [recorded `title-Introduction_to_Algorithms.json`](https://github.com/nannyu/booknest/blob/main/fixtures/crossref/title-Introduction_to_Algorithms.json); only its first two items are retained.
- Crossref DOI: [recorded `crossref_sb_doi.json`](https://github.com/JacobLinCool/StreamDecisionBench/blob/main/paper/bib/sources/crossref_sb_doi.json).
- Semantic Scholar: [recorded `15-search-bulk.json`](https://github.com/inflexa-ai/inflexa/blob/main/harness/src/tools/lib/__fixtures__/semantic-scholar/15-search-bulk.json); only its first two items are retained. The bulk-search fixture and standard-search endpoint share the tested `data` item envelope; it is not a recording of the exact non-bulk request.

These snapshots are test data, not an endorsement or a guarantee of current API
behavior. Endpoint and parameter assumptions are marked `# VERIFY` in the tools.
