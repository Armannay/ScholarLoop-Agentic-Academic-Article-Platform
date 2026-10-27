<!-- Planner call: plan. User message, assembled by app.planner.context.PlannerPromptBuilder. -->
Produce the plan for this brief. You are the Planner, not an executing agent: return only the plan JSON.

## Digest (claude.md)
{digest}

## Task
{task_json}

At most 8 tasks, one role each, sum of token_budget <= {remaining_run_tokens}.
Reply with one JSON object shaped like `plan.golden.json`.
