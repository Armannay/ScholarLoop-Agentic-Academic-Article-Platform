"""Plan - the Planner's only output shape. Todo list plus the rules for the run.

``Plan`` is a *document* contract: it validates the whole graph at once (acyclic
dependencies, resolved ``depends_on``, unique ids, budget sanity) so the
orchestrator can trust it without re-checking. The Planner never writes article
text, so nothing here can contain prose destined for a section body.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.contracts.common import NonEmptyList, NonEmptyStr, VersionedContract
from app.contracts.enums import AgentRole
from app.contracts.task import Task

__all__ = ["Plan", "PlanConflict"]

#: Roles that must appear at least once in a runnable plan.
REQUIRED_ROLES: frozenset[AgentRole] = frozenset(
    {
        AgentRole.ARCHITECT,
        AgentRole.RESEARCHER,
        AgentRole.WRITER,
        AgentRole.VALIDATOR,
    }
)


class PlanConflict(ValueError):
    """Raised for a structurally invalid plan (cycle, dangling dep, duplicate id)."""


class Plan(VersionedContract):
    """A versioned todo list. ``version`` increments on every REPLANNING pass."""

    version: int = Field(ge=1, description="1 for the initial plan, +1 per replan.")
    goal: NonEmptyStr = Field(max_length=800, description="What 'done' means for this run.")
    tasks: NonEmptyList[Task] = Field(max_length=200)

    @model_validator(mode="after")
    def _validate_graph(self) -> Plan:
        ids = [task.id for task in self.tasks]
        if len(set(ids)) != len(ids):
            duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
            raise PlanConflict(f"duplicate task ids: {duplicates}")

        known = set(ids)
        for task in self.tasks:
            dangling = sorted(set(task.depends_on) - known)
            if dangling:
                raise PlanConflict(
                    f"task {task.id} ('{task.title}') depends on unknown task ids: {dangling}"
                )
            if task.id in task.depends_on:
                raise PlanConflict(f"task {task.id} depends on itself")

        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        """Kahn's algorithm: detects dependency cycles without recursing."""
        indegree = {task.id: 0 for task in self.tasks}
        edges: dict[str, list[str]] = {task.id: [] for task in self.tasks}
        for task in self.tasks:
            for dependency in task.depends_on:
                edges[dependency].append(task.id)
                indegree[task.id] += 1

        ready = [task_id for task_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for downstream in edges[current]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)

        if visited != len(self.tasks):
            stuck = sorted(task_id for task_id, degree in indegree.items() if degree > 0)
            raise PlanConflict(f"dependency cycle among tasks: {stuck}")

    def of_role(self, role: AgentRole) -> list[Task]:
        """Tasks owned by ``role``, in declaration order."""
        return [task for task in self.tasks if task.role is role]

    def role_distribution(self) -> dict[AgentRole, int]:
        """Count of tasks per role - the digest the Planner sees instead of the plan."""
        distribution = dict.fromkeys(AgentRole, 0)
        for task in self.tasks:
            distribution[task.role] += 1
        return distribution

    def missing_required_roles(self) -> list[AgentRole]:
        """Required roles with no task, sorted for stable error messages."""
        return sorted(REQUIRED_ROLES - set(self.role_distribution()), key=str)

    def task_by_id(self, task_id: str) -> Task | None:
        """Look up a task by id, or None."""
        return next((task for task in self.tasks if task.id == task_id), None)

    def ready_tasks(self, completed_ids: set[str]) -> list[Task]:
        """Tasks whose dependencies are all satisfied and which are not yet complete."""
        return [
            task
            for task in self.tasks
            if task.id not in completed_ids and set(task.depends_on) <= completed_ids
        ]
