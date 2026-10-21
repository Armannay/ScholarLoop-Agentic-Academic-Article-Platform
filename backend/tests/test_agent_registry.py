"""Agent registry: deterministic registration, enable/disable, call-time bindings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agents.binding import CallableBindingResolver
from app.agents.context import AgentRunState
from app.agents.errors import (
    AgentDisabled,
    AgentNotRegistered,
    AgentRegistrationError,
)
from app.agents.governor import CallBudgetGovernor
from app.agents.registry import AgentRegistry
from app.contracts.binding import AgentBinding
from app.contracts.enums import AgentRole, BindingRole, BindingStrategy, SectionStatus
from app.contracts.section import SectionState
from app.providers.errors import NoAccountAvailable
from app.providers.mock import MockProvider, Script
from tests.test_agent_runtime import (
    RecordingProvider,
    StubWriter,
    make_binding,
    make_builder,
    make_task,
    result_payload,
)

pytestmark = pytest.mark.agents


def make_state() -> AgentRunState:
    return AgentRunState(
        iter=1,
        sections=(
            SectionState(
                id="introduction",
                title="Introduction",
                order=0,
                version=2,
                hash="sha256:" + "cd" * 32,
                words=90,
                status=SectionStatus.DRAFT,
            ),
        ),
    )


class _StubReviewer(StubWriter):
    """A second role, registered only where a test needs it."""

    role = AgentRole.REVIEWER


def _agent(provider: RecordingProvider, repo_root: Path, resolver: Any = None) -> StubWriter:
    return StubWriter(
        provider=provider,
        bindings=resolver or CallableBindingResolver(lambda role: make_binding()),
        governor=CallBudgetGovernor(),
        context_builder=make_builder(repo_root),
    )


def _registry(resolver: Any = None) -> AgentRegistry:
    return AgentRegistry(bindings=resolver or CallableBindingResolver(lambda role: make_binding()))


async def test_registered_agent_executes_the_task(repo_root: Path) -> None:
    """The registry delegates to the agent and returns its result unchanged."""
    task = make_task()
    provider = RecordingProvider(
        MockProvider(Script().json_reply("writer", 0, result_payload(task)))
    )
    registry = _registry()
    registry.register(AgentRole.WRITER, lambda: _agent(provider, repo_root))

    result = await registry.run(task, make_state())

    assert result.entry.task_id == task.id
    assert provider.call_count == 1


def test_factory_is_called_once_and_resolution_is_cached(repo_root: Path) -> None:
    """Registration and resolution are deterministic and cheap."""
    provider = RecordingProvider(MockProvider(Script()))
    calls: list[int] = []

    def factory() -> StubWriter:
        calls.append(1)
        return _agent(provider, repo_root)

    registry = _registry()
    registry.register(AgentRole.WRITER, factory)
    first = registry.resolve(AgentRole.WRITER)
    second = registry.resolve(AgentRole.WRITER)

    assert first is second
    assert calls == [1]


def test_unknown_role_is_an_error() -> None:
    """An unregistered role never silently resolves to a default agent."""
    registry = _registry()
    with pytest.raises(AgentNotRegistered):
        registry.resolve(AgentRole.WRITER)
    with pytest.raises(AgentNotRegistered):
        registry.binding_for(AgentRole.WRITER)


async def test_disabled_role_cannot_execute(repo_root: Path) -> None:
    """Disabling prevents both resolution and dispatch; re-enabling restores it."""
    task = make_task()
    provider = RecordingProvider(
        MockProvider(Script().json_reply("writer", 0, result_payload(task)))
    )
    registry = _registry()
    registry.register(AgentRole.WRITER, lambda: _agent(provider, repo_root))

    registry.disable(AgentRole.WRITER)
    assert registry.is_enabled(AgentRole.WRITER) is False
    with pytest.raises(AgentDisabled):
        registry.resolve(AgentRole.WRITER)
    with pytest.raises(AgentDisabled):
        await registry.run(task, make_state())
    assert provider.call_count == 0

    registry.enable(AgentRole.WRITER)
    assert registry.is_enabled(AgentRole.WRITER) is True
    result = await registry.run(task, make_state())
    assert result.entry.task_id == task.id


def test_unknown_and_disabled_roles_fail_safely_without_building(
    repo_root: Path,
) -> None:
    """A disabled role's factory is never called, so no provider is even constructed."""
    built: list[int] = []
    registry = _registry()
    registry.register(
        AgentRole.WRITER,
        lambda: built.append(1) or _agent(RecordingProvider(MockProvider(Script())), repo_root),
        enabled=False,
    )

    with pytest.raises(AgentDisabled):
        registry.resolve(AgentRole.WRITER)
    registry.build_all()
    assert built == []


def test_duplicate_registration_is_rejected(repo_root: Path) -> None:
    """One role, one agent: a second registration is a wiring bug."""
    registry = _registry()

    def factory() -> StubWriter:
        return _agent(RecordingProvider(MockProvider(Script())), repo_root)

    registry.register(AgentRole.WRITER, factory)
    with pytest.raises(AgentRegistrationError):
        registry.register(AgentRole.WRITER, factory)


def test_factory_must_return_an_agent_for_the_registered_role(repo_root: Path) -> None:
    """A factory that builds the wrong role is caught at resolve time."""
    registry = _registry()
    registry.register(
        AgentRole.WRITER,
        lambda: _StubReviewer(
            provider=RecordingProvider(MockProvider(Script())),
            bindings=CallableBindingResolver(lambda role: make_binding()),
            governor=CallBudgetGovernor(),
            context_builder=make_builder(repo_root),
        ),
    )
    with pytest.raises(AgentRegistrationError):
        registry.resolve(AgentRole.WRITER)


def test_register_agent_instance(repo_root: Path) -> None:
    """Pre-built agents register by their own role."""
    provider = RecordingProvider(MockProvider(Script()))
    registry = _registry()
    registry.register_agent(_agent(provider, repo_root))
    assert registry.registered_roles() == (AgentRole.WRITER,)


async def test_binding_is_checked_at_dispatch_and_reflects_hot_swaps(
    repo_root: Path,
) -> None:
    """Swap the binding between two registry runs: the next call uses it."""
    task = make_task()
    holder: dict[str, AgentBinding] = {"binding": make_binding(model="mock-a")}
    resolver = CallableBindingResolver(lambda role: holder["binding"])
    provider = RecordingProvider(
        MockProvider(
            Script()
            .json_reply("writer", 0, result_payload(task))
            .json_reply("writer", 1, result_payload(task))
        )
    )
    registry = _registry(resolver)
    registry.register(AgentRole.WRITER, lambda: _agent(provider, repo_root, resolver))

    await registry.run(task, make_state())
    holder["binding"] = make_binding(model="mock-b")
    await registry.run(task, make_state())

    assert provider.models == ["mock-a", "mock-b"]


async def test_registry_refuses_a_task_without_an_active_binding(
    repo_root: Path,
) -> None:
    """The registry fails before dispatch when the role has no usable account."""
    task = make_task()
    provider = RecordingProvider(MockProvider(Script()))
    registry = _registry(CallableBindingResolver(lambda role: None))
    registry.register(AgentRole.WRITER, lambda: _agent(provider, repo_root))

    with pytest.raises(NoAccountAvailable):
        await registry.run(task, make_state())
    assert provider.call_count == 0


def test_registered_roles_are_in_enum_order(repo_root: Path) -> None:
    """Registration order never changes the reported order."""
    provider = RecordingProvider(MockProvider(Script()))
    registry = _registry()
    registry.register(AgentRole.VALIDATOR, lambda: _agent(provider, repo_root))
    registry.register(AgentRole.WRITER, lambda: _agent(provider, repo_root))

    assert registry.registered_roles() == (AgentRole.WRITER, AgentRole.VALIDATOR)


def test_binding_role_matches_the_agent_role(repo_root: Path) -> None:
    """The binding role is derived, never configured separately."""
    provider = RecordingProvider(MockProvider(Script()))
    agent = StubWriter(
        provider=provider,
        bindings=CallableBindingResolver(lambda role: make_binding()),
        governor=CallBudgetGovernor(),
        context_builder=make_builder(repo_root),
    )
    assert agent.role is AgentRole.WRITER
    assert agent.binding_role is BindingRole.WRITER


def test_binding_strategy_is_dedicated_for_single_account_bindings() -> None:
    """Guard the fixture itself: failover/round_robin would demand two accounts."""
    binding = make_binding()
    assert binding.strategy is BindingStrategy.DEDICATED
    assert len(binding.account_ids) == 1
