"""Lock tests: mutual exclusion, bounded waiting, and no deadlock at 50 writers.

The scenario these tests stand in for is the orchestrator running ``max_parallel``
agents that each patch a section. Two of them may target the same section; the store
must serialise them, and neither may lose an edit or hang the iteration.
"""

from __future__ import annotations

import asyncio

import pytest

from app.article import (
    ArticleStore,
    InMemoryStorage,
    LockReentrancy,
    LockTimeout,
    SectionLocks,
    StalePatch,
    order_section_ids,
)
from app.contracts.common import word_count
from app.contracts.enums import PatchOpType
from app.contracts.patch import Patch, PatchOp

pytestmark = pytest.mark.article

CONCURRENCY = 50


@pytest.fixture
def locks() -> SectionLocks:
    """A lock manager with a short default deadline."""
    return SectionLocks(timeout=2.0)


def test_order_section_ids_is_the_one_global_order() -> None:
    """Sorted and de-duplicated, whatever order the caller asked for."""
    assert order_section_ids(["methods", "introduction", "methods"]) == [
        "introduction",
        "methods",
    ]
    assert order_section_ids([]) == []


async def test_one_section_at_a_time(locks: SectionLocks) -> None:
    """Inside the critical section only one task is ever present."""
    inside = 0
    peak = 0

    async def worker(index: int) -> None:
        nonlocal inside, peak
        async with locks.hold("introduction"):
            inside += 1
            peak = max(peak, inside)
            assert locks.is_locked("introduction")
            await asyncio.sleep(0)
            inside -= 1

    await asyncio.gather(*(worker(i) for i in range(CONCURRENCY)))
    assert peak == 1
    assert inside == 0
    assert locks.is_locked("introduction") is False
    assert locks.stats().acquisitions == CONCURRENCY


async def test_different_sections_never_wait_for_each_other() -> None:
    """A writer on ``methods`` is not blocked by a writer holding ``introduction``."""
    locks = SectionLocks(timeout=0.5)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_introduction() -> None:
        async with locks.hold("introduction"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_introduction())
    await entered.wait()
    async with locks.hold("methods"):
        pass  # would time out if the manager locked the article instead of the section
    release.set()
    await holder
    assert locks.stats().timeouts == 0


async def test_deadlock_free_across_crossed_batches() -> None:
    """{a,b} and {b,a} taken concurrently both finish: the global order wins."""
    locks = SectionLocks(timeout=1.0)
    barrier = asyncio.Barrier(2)

    async def worker(first: str, second: str) -> list[str]:
        await barrier.wait()
        async with locks.hold_many([first, second]):
            await asyncio.sleep(0)
            return [first, second]

    results = await asyncio.wait_for(
        asyncio.gather(worker("a", "b"), worker("b", "a")), timeout=5.0
    )
    assert sorted(results, key=len) == [["a", "b"], ["b", "a"]]
    assert locks.stats().timeouts == 0


async def test_batch_deadline_is_shared_not_multiplied() -> None:
    """Five blocked sections cost one timeout, not five."""
    locks = SectionLocks(timeout=0.2)
    release = asyncio.Event()

    async def hold_all() -> None:
        async with locks.hold_many([f"section-{i}" for i in range(5)]):
            await release.wait()

    holder = asyncio.create_task(hold_all())
    await asyncio.sleep(0.01)
    started = asyncio.get_running_loop().time()
    with pytest.raises(LockTimeout) as excinfo:
        async with locks.hold_many([f"section-{i}" for i in range(5)]):
            pass  # pragma: no cover - never entered
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0
    assert excinfo.value.timeout == 0.2
    release.set()
    await holder
    assert locks.stats().timeouts == 1


async def test_timeout_names_the_section_and_releases_everything() -> None:
    """A timeout is diagnosable, and locks already held are given back."""
    locks = SectionLocks(timeout=0.1)
    release = asyncio.Event()

    async def hold() -> None:
        async with locks.hold("introduction"):
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    with pytest.raises(LockTimeout) as excinfo:
        async with locks.hold("introduction"):
            pass  # pragma: no cover - never entered
    assert excinfo.value.section_id == "introduction"
    assert excinfo.value.waited >= 0.1
    assert "introduction" in excinfo.value.message

    # The failed attempt must not have swallowed the lock.
    release.set()
    await holder
    async with locks.hold("introduction", timeout=0.5):
        pass


async def test_reentrant_use_fails_fast() -> None:
    """Locking a section this task already holds is a bug, not contention."""
    locks = SectionLocks(timeout=0.1)
    with pytest.raises(LockReentrancy):
        async with locks.hold("introduction"), locks.hold("introduction"):
            pass  # pragma: no cover - never entered
    assert locks.stats().reentrancy_errors == 1
    assert locks.is_locked("introduction") is False


async def test_locks_are_released_when_the_body_raises() -> None:
    """An exception inside the critical section must not strand the lock."""
    locks = SectionLocks(timeout=0.2)
    with pytest.raises(RuntimeError, match="boom"):
        async with locks.hold("introduction"):
            raise RuntimeError("boom")
    async with locks.hold("introduction"):
        pass
    assert locks.is_locked("introduction") is False


async def test_owner_id_tracks_the_holding_task() -> None:
    """Diagnostics can name the task that holds a section."""
    locks = SectionLocks(timeout=0.2)
    assert locks.owner_id("introduction") is None
    async with locks.hold("introduction"):
        assert locks.owner_id("introduction") == id(asyncio.current_task())
    assert locks.owner_id("introduction") is None


async def test_run_locked_wraps_an_action() -> None:
    """The orchestrator's one-liner form holds the sections for the action."""
    locks = SectionLocks(timeout=0.2)
    seen: list[bool] = []

    async def action() -> str:
        seen.append(locks.is_locked("introduction") and locks.is_locked("methods"))
        return "done"

    assert await locks.run_locked(["introduction", "methods"], action) == "done"
    assert seen == [True]


# ------------------------------------------------------- 50 concurrent patches, one store


async def test_fifty_concurrent_patches_lose_nothing() -> None:
    """Fifty tasks append a sentence to one section; every sentence survives.

    Each task reads the head, builds a patch against it and retries on
    ``StalePatch`` - exactly what the orchestrator does when two agents collide. The
    ``<!--END-->`` marker keeps the anchor short and unique no matter how long the
    body grows (``PatchOp.anchor`` is capped at 200 characters).
    """
    store = ArticleStore(InMemoryStorage(), locks=SectionLocks(timeout=5.0))
    marker = "<!--END-->"
    await store.create_section("discussion", "Discussion", text=f"Start.{marker}")

    stale_retries = 0

    async def writer(index: int) -> None:
        nonlocal stale_retries
        sentence = f"Sentence {index:02d} adds evidence."
        for _ in range(200):
            head = await store.get_section("discussion")
            candidate = Patch(
                section_id="discussion",
                base_version=head.version,
                ops=[PatchOp(op=PatchOpType.INSERT_AFTER, anchor=marker, text=f"{sentence}")],
            )
            try:
                await store.apply_patch(candidate)
            except StalePatch:
                stale_retries += 1
                await asyncio.sleep(0)
                continue
            return
        raise AssertionError(f"writer {index} gave up after 200 attempts")

    await asyncio.gather(*(writer(i) for i in range(CONCURRENCY)))

    text = await store.get_text("discussion")
    missing = [i for i in range(CONCURRENCY) if f"Sentence {i:02d} adds evidence." not in text]
    assert missing == []
    head = await store.get_section("discussion")
    assert head.version == CONCURRENCY + 1
    assert len(await store.history("discussion")) == CONCURRENCY + 1
    assert head.words == word_count(text)
    assert stale_retries > 0  # the writers really did collide
    assert store.locks.stats().timeouts == 0


async def test_parallel_writers_on_different_sections_do_not_collide() -> None:
    """Distinct sections mean distinct locks, so nothing is stale at all."""
    store = ArticleStore(InMemoryStorage(), locks=SectionLocks(timeout=5.0))
    sections = [f"section-{i}" for i in range(CONCURRENCY)]
    for index, section_id in enumerate(sections):
        await store.create_section(section_id, f"Section {index}", order=index, text="Base.")

    async def writer(section_id: str, index: int) -> None:
        await store.patch_section(
            section_id, [PatchOp(op=PatchOpType.REPLACE_TEXT, old="Base.", new=f"Body {index}.")]
        )

    await asyncio.gather(*(writer(section_id, i) for i, section_id in enumerate(sections)))
    states = await store.list_state()
    assert len(states) == CONCURRENCY
    # v1 from the seeded body, v2 from each writer's own patch.
    assert all(state.version == 2 for state in states)
    assert store.locks.stats().timeouts == 0
