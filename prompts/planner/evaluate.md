<!-- Planner call: evaluate. Compact view only; full text is opt-in (deep_check=true). -->
Evaluate the article state against the brief and the rubric. The hard-gate results are facts; you cannot overturn them. Return only the evaluation JSON.

## Digest (claude.md)
{digest}

## Task
{task_json}

## Hard gates (deterministic; you cannot change these)
{gates_json}

## Compact article view
{compact_view}

## Validator report
{report_json}

If any gate failed, `meets_minimum` is false and `missing` names the gate. Reply with one
JSON object shaped like `evaluate.golden.json`.
