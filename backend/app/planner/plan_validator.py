"""Deterministic plan validation - the part of planning the model does not own.

A plan is accepted only when every check below passes. Each check produces a *sentence
the retry can act on*, names the offending task, and is emitted in a stable order, so a
retry prompt is reproducible for a given bad draft.

Checks, in the order they are reported:

1. the plan is non-empty and no larger than the per-iteration cap (8 by default);
2. task keys are unique;
3. every task has exactly one valid ``AgentRole`` (and never the planner's own role);
4. every task states acceptance criteria and a positive token budget;
5. ``depends_on`` names known keys, never itself, and forms an acyclic graph;
6. every section id is a rubric section, or one an earlier architect task introduces;
7. the summed task budgets fit ``PlanBudget.remaining_run_tokens``;
8. a whole plan covers the roles the contract requires (architect, researcher, writer,
   validator).

Dependencies come first, sections second: rule 6 is evaluated in topological order, which
is what makes "created by an *earlier* architect task" checkable at all.

Nothing here mutates a contract or calls a provider: the validator is a pure function of
a draft, a brief and a budget.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import Field, ValidationError, model_validator

from app.contracts.brief import Brief
from app.contracts.common import SECTION_ID_PATTERN, NonEmptyStr, Ulid, VersionedContract
from app.contracts.enums import AgentRole, BindingRole
from app.contracts.ledger import TokenUsage
from app.contracts.plan import REQUIRED_ROLES, Plan, PlanConflict
from app.contracts.task import Task
from app.core.policies import Policies
from app.core.rubric import Rubric, SectionSpec, resolve_sections
from app.planner.budget import MAX_TASKS_PER_ITERATION, PlanBudget
from app.planner.drafts import DraftTask, PlanDeltaDraft, PlanDraft
from app.planner.errors import PlanRejected

__all__ = [
    "MAX_TASKS_PER_PLAN",
    "VALID_PLAN_ROLES",
    "PlanDelta",
    "PlanValidator",
    "plan_role_order",
]

#: Per-iteration task ceiling (Step 8 brief), kept as one constant for the validator.
MAX_TASKS_PER_PLAN: Final[int] = MAX_TASKS_PER_ITERATION
#: The five roles a plan may assign work to. The planner is not one of them.
VALID_PLAN_ROLES: Final[tuple[str, ...]] = tuple(role.value for role in AgentRole)
#: Minimum length of an acceptance statement: shorter is a label, not a check.
MIN_ACCEPTANCE_CHARS: Final[int] = 3

_ZERO_USAGE: Final[dict[str, int]] = {"in": 0, "out": 0, "cached": 0}


def plan_role_order() -> tuple[str, ...]:
    """The valid plan roles in a stable, printable order."""
    return VALID_PLAN_ROLES


@dataclass(frozen=True)
class _TaskView:
    """The fields the checks read, for either a draft task or a contract task."""

    key: str
    role: str
    section_ids: tuple[str, ...]
    acceptance: tuple[str, ...]
    token_budget: int
    depends_on: tuple[str, ...]

    @classmethod
    def of_draft(cls, task: DraftTask) -> _TaskView:
        """Normalise a draft task."""
        return cls(
            key=task.key,
            role=task.role.strip(),
            section_ids=tuple(task.section_ids),
            acceptance=tuple(task.acceptance),
            token_budget=task.token_budget,
            depends_on=tuple(task.depends_on),
        )

    @classmethod
    def of_task(cls, task: Task) -> _TaskView:
        """Normalise a contract task (its id is the key)."""
        return cls(
            key=task.id,
            role=task.role.value,
            section_ids=tuple(task.section_ids),
            acceptance=tuple(task.acceptance),
            token_budget=task.token_budget,
            depends_on=tuple(task.depends_on),
        )


class PlanDelta(VersionedContract):
    """A change to an existing plan: tasks added, task ids removed, and why.

    ``apply_to`` is the only way to combine a delta with a plan, so the merged result is
    always validated by the ``Plan`` contract (ids unique, dependencies resolved and
    acyclic) instead of being patched together by the caller.
    """

    base_version: int = Field(ge=1, description="Plan version this delta applies to.")
    version: int = Field(ge=2, description="Version of the resulting plan (base + 1).")
    goal: NonEmptyStr = Field(max_length=800)
    added: list[Task] = Field(default_factory=list, max_length=MAX_TASKS_PER_PLAN)
    removed: list[Ulid] = Field(
        default_factory=list, max_length=32, description="Task ids dropped from the plan."
    )
    strategy_change: bool = Field(
        default=False, description="True when the first plateau forced a new approach."
    )
    reason: NonEmptyStr = Field(max_length=400, description="Why the plan changes.")

    @model_validator(mode="after")
    def _version_follows_base(self) -> PlanDelta:
        if self.version <= self.base_version:
            raise ValueError(
                f"delta version {self.version} must be greater than base_version "
                f"{self.base_version}"
            )
        return self

    @property
    def is_empty(self) -> bool:
        """True when the delta changes nothing (a legitimate answer to "no change")."""
        return not self.added and not self.removed

    def apply_to(self, plan: Plan) -> Plan:
        """Return the merged plan, or raise :class:`PlanConflict`.

        Raises:
            PlanConflict: the delta targets another plan version, removes an id the plan
                does not have, or adds a task id that already exists.
        """
        if plan.version != self.base_version:
            raise PlanConflict(
                f"delta applies to plan v{self.base_version}, but the current plan is "
                f"v{plan.version}"
            )
        known = {task.id for task in plan.tasks}
        unknown = sorted(set(self.removed) - known)
        if unknown:
            raise PlanConflict(f"delta removes task ids that are not in the plan: {unknown}")
        added_ids = [task.id for task in self.added]
        if len(set(added_ids)) != len(added_ids):
            raise PlanConflict("delta adds the same task id twice")
        collisions = sorted(set(added_ids) & (known - set(self.removed)))
        if collisions:
            raise PlanConflict(f"delta re-adds task ids that already exist: {collisions}")
        dropped = set(self.removed)
        tasks = [task for task in plan.tasks if task.id not in dropped]
        return Plan(version=self.version, goal=self.goal, tasks=[*tasks, *self.added])


class PlanValidator:
    """Validates plan drafts and plan deltas against contracts, rubric and budget."""

    def __init__(
        self,
        rubric: Rubric,
        policies: Policies,
        *,
        max_tasks: int = MAX_TASKS_PER_PLAN,
    ) -> None:
        if not 1 <= max_tasks <= MAX_TASKS_PER_PLAN:
            raise ValueError(f"max_tasks must be 1..{MAX_TASKS_PER_PLAN}, got {max_tasks}")
        self._rubric = rubric
        self._policies = policies
        self._max_tasks = max_tasks

    @property
    def rubric(self) -> Rubric:
        """The rubric whose sections bound a plan."""
        return self._rubric

    @property
    def policies(self) -> Policies:
        """The policies the loop runs under."""
        return self._policies

    @property
    def max_tasks(self) -> int:
        """Per-plan task ceiling."""
        return self._max_tasks

    def sections_for(self, brief: Brief) -> dict[str, SectionSpec]:
        """Sections available to a plan for ``brief`` (rubric plus ``must_include``)."""
        specs = resolve_sections(
            self._rubric, brief.article_type, extra_titles=list(brief.must_include)
        )
        return {spec.id: spec for spec in specs}

    # -- public ------------------------------------------------------------
    def problems(self, draft: PlanDraft, *, brief: Brief, budget: PlanBudget) -> list[str]:
        """Every reason ``draft`` cannot become a valid plan, in stable order."""
        return self._problems(
            [_TaskView.of_draft(task) for task in draft.tasks],
            brief=brief,
            budget=budget,
            require_required_roles=True,
        )

    def build(
        self,
        draft: PlanDraft,
        *,
        brief: Brief,
        budget: PlanBudget,
        id_factory: Callable[[], str],
        version: int = 1,
    ) -> Plan:
        """Map draft keys to ULIDs and return the validated contract ``Plan``.

        Raises:
            PlanRejected: the draft is invalid (the same list :meth:`problems` returns),
                or the mapped tasks do not satisfy the ``Plan``/``Task`` contracts.
        """
        found = self.problems(draft, brief=brief, budget=budget)
        if found:
            raise PlanRejected(
                "plan draft failed validation",
                problems=found,
                attempts=0,
                tokens=TokenUsage(**_ZERO_USAGE),
            )
        key_to_id = {task.key: id_factory() for task in draft.tasks}
        try:
            tasks = [
                Task(
                    id=key_to_id[task.key],
                    title=task.title,
                    role=AgentRole(task.role.strip()),
                    section_ids=list(task.section_ids),
                    source_ids=[],
                    instructions=task.instructions,
                    acceptance=list(task.acceptance),
                    token_budget=task.token_budget,
                    depends_on=[key_to_id[dependency] for dependency in task.depends_on],
                )
                for task in draft.tasks
            ]
            return Plan(version=version, goal=draft.goal, tasks=tasks)
        except (PlanConflict, ValidationError) as exc:
            raise PlanRejected(
                "the plan contract rejected the draft",
                problems=[_contract_problem(exc)],
                attempts=0,
                tokens=TokenUsage(**_ZERO_USAGE),
            ) from exc

    def plan_problems(self, plan: Plan, *, brief: Brief, budget: PlanBudget) -> list[str]:
        """Every reason ``plan`` is not a valid plan for this brief and budget."""
        return self._problems(
            [_TaskView.of_task(task) for task in plan.tasks],
            brief=brief,
            budget=budget,
            require_required_roles=True,
        )

    def validate_plan(self, plan: Plan, *, brief: Brief, budget: PlanBudget) -> None:
        """Raise :class:`PlanRejected` when ``plan`` is invalid."""
        found = self.plan_problems(plan, brief=brief, budget=budget)
        if found:
            raise PlanRejected(
                f"plan v{plan.version} failed validation",
                problems=found,
                attempts=0,
                tokens=TokenUsage(**_ZERO_USAGE),
            )

    def resolve_removals(self, plan: Plan, removed: Sequence[str]) -> tuple[list[str], list[str]]:
        """Resolve removal entries to task ids, plus the problems that prevented it.

        An entry may be a task id or a task title: a model is more reliable with titles
        than with 26-character ULIDs, and accepting both costs one dictionary.
        """
        by_id = {task.id for task in plan.tasks}
        by_title = {task.title.casefold(): task.id for task in plan.tasks}
        resolved: list[str] = []
        problems: list[str] = []
        for entry in removed:
            target = entry if entry in by_id else by_title.get(entry.casefold())
            if target is None:
                problems.append(
                    f"removed: '{entry}' is neither a task id nor a task title in the "
                    f"current plan (v{plan.version})"
                )
                continue
            if target not in resolved:
                resolved.append(target)
        return resolved, problems

    def delta_problems(
        self, plan: Plan, delta: PlanDeltaDraft, *, brief: Brief, budget: PlanBudget
    ) -> list[str]:
        """Every reason this delta cannot be applied, in stable order.

        The check runs on the *merged* task set, so removing the last task of a required
        role, or adding a task whose section is unknown, is caught before anything is
        built. It is pure: no ids are minted and no plan is constructed.
        """
        removed, problems = self.resolve_removals(plan, delta.removed)
        added_views = [_TaskView.of_draft(task) for task in delta.added]
        if len(added_views) > self._max_tasks:
            problems.append(
                f"added: {len(added_views)} tasks exceed the per-iteration maximum of "
                f"{self._max_tasks}"
            )
        dropped = set(removed)
        merged = [_TaskView.of_task(task) for task in plan.tasks if task.id not in dropped]
        merged.extend(added_views)
        problems.extend(
            self._problems(
                merged,
                brief=brief,
                budget=budget,
                require_required_roles=True,
            )
        )
        return problems

    def apply_delta(
        self,
        plan: Plan,
        delta: PlanDeltaDraft,
        *,
        brief: Brief,
        budget: PlanBudget,
        id_factory: Callable[[], str],
        strategy_change: bool,
    ) -> PlanDelta:
        """Turn a delta draft into a validated :class:`PlanDelta`.

        Raises:
            PlanRejected: the delta (or the plan it produces) is invalid.
        """
        problems = self.delta_problems(plan, delta, brief=brief, budget=budget)
        if problems:
            raise PlanRejected(
                "plan delta failed validation",
                problems=problems,
                attempts=0,
                tokens=TokenUsage(**_ZERO_USAGE),
            )

        removed, _ = self.resolve_removals(plan, delta.removed)
        key_to_id = {task.key: id_factory() for task in delta.added}
        try:
            added = [
                Task(
                    id=key_to_id[task.key],
                    title=task.title,
                    role=AgentRole(task.role.strip()),
                    section_ids=list(task.section_ids),
                    source_ids=[],
                    instructions=task.instructions,
                    acceptance=list(task.acceptance),
                    token_budget=task.token_budget,
                    depends_on=[
                        key_to_id.get(dependency, dependency) for dependency in task.depends_on
                    ],
                )
                for task in delta.added
            ]
            result = PlanDelta(
                base_version=plan.version,
                version=plan.version + 1,
                goal=delta.goal or plan.goal,
                added=added,
                removed=removed,
                strategy_change=strategy_change,
                reason=delta.reason,
            )
        except ValidationError as exc:
            raise PlanRejected(
                "the plan contract rejected the delta",
                problems=[_contract_problem(exc)],
                attempts=0,
                tokens=TokenUsage(**_ZERO_USAGE),
            ) from exc
        return result

    # -- internals ---------------------------------------------------------
    def _problems(
        self,
        views: Sequence[_TaskView],
        *,
        brief: Brief | None,
        budget: PlanBudget,
        require_required_roles: bool,
    ) -> list[str]:
        """The full check list for a set of normalised tasks."""
        if not views:
            return ["tasks: a plan needs at least one task"]

        problems: list[str] = []
        if len(views) > self._max_tasks:
            problems.append(
                f"tasks: {len(views)} tasks exceed the per-iteration maximum of {self._max_tasks}"
            )
        keys = [view.key for view in views]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            problems.append(f"tasks: duplicate task keys {duplicates}")

        problems.extend(self._task_problems(views))
        problems.extend(self._graph_problems(views, known=set(keys)))
        order = self._topological_order(views)
        if order is None:
            problems.append("tasks: the depends_on graph has a cycle; a plan must be a DAG")
        elif brief is not None:
            problems.extend(self._section_problems(views, order, brief=brief))

        total = sum(view.token_budget for view in views)
        if total > budget.remaining_run_tokens:
            problems.append(
                f"budget: tasks request {total} tokens; the remaining run budget is "
                f"{budget.remaining_run_tokens}"
            )
        if require_required_roles:
            assigned = {view.role for view in views}
            missing = sorted(role.value for role in REQUIRED_ROLES if role.value not in assigned)
            if missing:
                problems.append(
                    f"roles: no task is assigned to {', '.join(missing)}; a runnable plan "
                    "needs at least one task per required role "
                    f"({', '.join(sorted(role.value for role in REQUIRED_ROLES))})"
                )
        return problems

    def _task_problems(self, views: Sequence[_TaskView]) -> list[str]:
        """Per-task checks: role, acceptance, budget and self-dependency."""
        problems: list[str] = []
        planner_role = BindingRole.PLANNER.value
        for index, view in enumerate(views):
            label = f"tasks[{index}] ({view.key})"
            role = view.role.strip()
            if role not in VALID_PLAN_ROLES:
                if role.casefold() == planner_role:
                    problems.append(
                        f"{label}: role '{role}' is the planner; the planner may not assign "
                        f"a task to itself. Valid roles: {', '.join(VALID_PLAN_ROLES)}"
                    )
                else:
                    problems.append(
                        f"{label}: role '{role}' is not one of {', '.join(VALID_PLAN_ROLES)}"
                    )
            if not view.acceptance:
                problems.append(
                    f"{label}: acceptance is empty; every task needs at least one "
                    "checkable statement"
                )
            else:
                thin = [
                    item for item in view.acceptance if len(item.strip()) < MIN_ACCEPTANCE_CHARS
                ]
                if thin:
                    problems.append(
                        f"{label}: acceptance entries {thin[:2]} are too short to be checkable"
                    )
            if view.token_budget < 1:
                problems.append(
                    f"{label}: token_budget must be a positive integer, got {view.token_budget}"
                )
            if view.key in view.depends_on:
                problems.append(f"{label}: a task cannot depend on itself")
        return problems

    def _graph_problems(self, views: Sequence[_TaskView], *, known: set[str]) -> list[str]:
        """Dependencies must name a task in this plan or an existing task id."""
        problems: list[str] = []
        for index, view in enumerate(views):
            for dependency in view.depends_on:
                if dependency != view.key and dependency not in known:
                    problems.append(
                        f"tasks[{index}] ({view.key}): depends_on '{dependency}' is not a "
                        "task key in this plan"
                    )
        return problems

    def _topological_order(self, views: Sequence[_TaskView]) -> list[str] | None:
        """Kahn's algorithm in declaration order; ``None`` when a cycle exists."""
        indegree = {view.key: 0 for view in views}
        edges: dict[str, list[str]] = {view.key: [] for view in views}
        counted: set[str] = set()
        for view in views:
            if view.key in counted:
                continue
            counted.add(view.key)
            for dependency in dict.fromkeys(view.depends_on):
                if dependency == view.key or dependency not in edges:
                    continue
                edges[dependency].append(view.key)
                indegree[view.key] += 1

        ready = [view.key for view in views if indegree.get(view.key, 0) == 0]
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for downstream in edges[current]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)
        if len(order) != len(indegree):
            return None
        return order

    def _section_problems(
        self, views: Sequence[_TaskView], order: Sequence[str], *, brief: Brief
    ) -> list[str]:
        """Section ids must exist, or be introduced by an earlier architect task."""
        known = self.sections_for(brief)
        by_key = {view.key: view for view in views}
        created: set[str] = set()
        problems: list[str] = []
        for key in order:
            view = by_key[key]
            label = f"tasks ({view.key})"
            for section_id in view.section_ids:
                if not _valid_section_id(section_id):
                    problems.append(
                        f"{label}: section id '{section_id}' is not a valid slug "
                        "(lowercase letters, digits, '.', '_' or '-')"
                    )
                    continue
                if section_id in known:
                    continue
                if view.role.strip() == AgentRole.ARCHITECT.value:
                    created.add(section_id)
                    continue
                if section_id in created:
                    continue
                problems.append(
                    f"{label}: section '{section_id}' is neither in the rubric for this "
                    "brief nor created by an earlier architect task"
                )
        return problems


def _valid_section_id(value: str) -> bool:
    """True when ``value`` matches the contract's section-id pattern."""
    return 1 <= len(value) <= 64 and re.fullmatch(SECTION_ID_PATTERN, value) is not None


def _contract_problem(exc: PlanConflict | ValidationError) -> str:
    """One-line, retry-readable summary of a contract rejection."""
    if isinstance(exc, PlanConflict):
        return f"plan: {exc}"
    first = exc.errors(include_url=False)[:1]
    if not first:
        return "plan: the contract rejected the draft"
    error = first[0]
    location = ".".join(str(part) for part in error.get("loc", ())) or "plan"
    return f"{location}: {error.get('msg', 'invalid')}"
