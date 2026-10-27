# Arena step logs

One directory per step, `step-<NN>/`, holding the raw prompt and the raw output the
agent produced. Written by the agent, not by the build; never a source of truth for
code (the code is in `backend/`).

| Step | Directory | Deliverable |
| --- | --- | --- |
| 02 | [`step-02/`](step-02/) | Token governor and context builder (`backend/app/governor/`) |

## step-02

- `prompt.md` — the task prompt verbatim.
- `output.md` — files-read confirmations, spec audit table, tests, CONTRACT-CHANGE-REQUESTS.
- `make-check.txt` — raw `make check` output (lint, mypy, skills lint, pytest).
- `governor-tests.txt` — raw `pytest -m governor` and JSON-schema sync check.
