<!-- Planner call: replan. Returns a delta, never a rewrite of the whole plan. -->
Revise the plan at this event. Return only the delta JSON: the tasks to add, the task ids to remove, and the reason.

## Digest (claude.md)
{digest}

## Task
{task_json}

## Current plan
{plan_json}

## Last loop decision
{decision_json}

## Strategy change requested
Only when this is the run's first plateau: change the approach, do not repeat the same task shape.

Reply with one JSON object shaped like `replan.golden.json`.
