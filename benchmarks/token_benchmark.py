#!/usr/bin/env python3
"""Token benchmark: the shipped pipeline versus a naive whole-article baseline.

Both arms execute **the same scripted article task** - one research brief, the
rubric's sections, three iterations, the same seven drafted sections, the same five
revisions, the same reviews, the same validations and the same five Planner calls,
25 calls per arm in total.
Every byte of article text, every source card and every change-log entry is
generated from a fixed seed (``SEED``), so two runs of this file spend exactly the
same tokens. Nothing here touches the network: the only provider is
:class:`~app.providers.mock.MockProvider` (HARD RULE 5).

The two arms differ in exactly four things, which is the point:

=====================  ======================================  =================================
                       naive baseline                          shipped pipeline
=====================  ======================================  =================================
context per call       whole article + whole change log        references only (task, section
                                                               under edit, source cards, the
                                                               bounded digest for the Planner)
edits                  no patches: every write re-emits the    patches: a targeted op carries
                       whole article                          only the delta
prompt caching         no cached blocks (``cache=False``)      the stable prefix is cache=True
output schema          a plain answer shape (~175 tokens a      the frozen ``AgentResult`` + role
                       call)                                   contract (~2.7k tokens a call)
=====================  ======================================  =================================

Both are measured with the repository's own convention (``estimate_tokens``, plus 4
tokens per message) and the report prints every token the provider is handed: prompt
tokens per role and iteration, the cached share, the output schema, output tokens and
the totals. The primary target is applied to prompt tokens, the measure this
repository's governor and telemetry use; the schema-inclusive number is reported next
to it, because a provider bills it, and because it is the single largest block of
input in the shipped run.

"Shipped pipeline" means the production code paths, not a re-implementation:

* the five role agents are built by
  :func:`app.agents.roles.factory.register_role_agents` with the real
  ``ContextBuilder`` (root rules, role rules, skill index, role prompt) and the
  real ``prepare()`` blocks each role computes for itself;
* the Planner is the real :class:`app.planner.service.PlannerService` over the real
  :class:`app.governor.context_builder.ContextBuilder`, reading the real bounded
  ``claude.md`` built by :class:`app.memory.digest.MemoryStore`;
* article text, versions, hashes, citations and section statuses move through
  :class:`app.article.store.ArticleStore`; entries, source cards and digest history
  move through :class:`app.memory.digest.MemoryStore`;
* every token number is the one ``MockProvider`` reports for the request the
  production code actually assembled, using the shared ``ceil(chars / 4)``
  convention.

The benchmark driver below replaces only the *loop controller*: it executes a fixed
schedule instead of asking the controller what to do next. That keeps the comparison
matched step for step, which is what a token benchmark needs; it is not a test of the
controller's stop logic (``backend/tests/test_orchestrator_*`` covers that).

Usage::

    python benchmarks/token_benchmark.py                 # print + write the report
    python benchmarks/token_benchmark.py --stdout        # print only (no file written)
    python benchmarks/token_benchmark.py --report /tmp/r.md

The committed report lives at ``docs/benchmarks/token-benchmark.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:  # run from a source checkout, no install
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.agents.binding import MappingBindingResolver  # noqa: E402
from app.agents.context import AgentRunState  # noqa: E402
from app.agents.governor import CallBudgetGovernor  # noqa: E402
from app.agents.registry import AgentRegistry  # noqa: E402
from app.agents.roles.base import (  # noqa: E402
    RoleAgent,
    RolePromptContextBuilder,
    load_role_prompt,
)
from app.agents.roles.factory import (  # noqa: E402
    default_role_context_builder,
    register_role_agents,
)
from app.agents.roles.inputs import MappingSectionTargets  # noqa: E402
from app.article.assemble import assemble, section_from  # noqa: E402
from app.article.store import ArticleStore  # noqa: E402
from app.contracts.agent import AgentResult  # noqa: E402
from app.contracts.binding import AgentBinding  # noqa: E402
from app.contracts.brief import Brief  # noqa: E402
from app.contracts.enums import (  # noqa: E402
    AgentRole,
    ArticleType,
    BindingRole,
    BindingStrategy,
    CitationStyle,
    MessageRole,
    PatchOpType,
    SectionStatus,
    VerificationStatus,
)
from app.contracts.llm import LLMMessage, LLMRequest, SystemBlock  # noqa: E402
from app.contracts.patch import PatchOp  # noqa: E402
from app.contracts.quality import QualityReport  # noqa: E402
from app.contracts.section import SectionState  # noqa: E402
from app.contracts.source import SourceCard  # noqa: E402
from app.contracts.task import Task  # noqa: E402
from app.core.hashing import content_hash  # noqa: E402
from app.core.ids import new_ulid  # noqa: E402
from app.core.policies import Policies, load_policies  # noqa: E402
from app.core.rubric import Rubric, SectionSpec, load_rubric, resolve_sections  # noqa: E402
from app.memory.digest import MemoryStore  # noqa: E402
from app.planner.budget import PlanBudget  # noqa: E402
from app.planner.context import (  # noqa: E402
    PlannerPromptBuilder,
    PlannerRunState,
    build_planner_contexts,
    planner_task,
)
from app.planner.drafts import (  # noqa: E402
    evaluation_schema,
    plan_delta_schema,
    plan_draft_schema,
)
from app.planner.probes import ArticleView, build_probe_report  # noqa: E402
from app.planner.prompts import (  # noqa: E402
    EVALUATOR_MAX_OUTPUT_TOKENS,
    PLANNER_MAX_OUTPUT_TOKENS,
)
from app.planner.service import PlannerService  # noqa: E402
from app.providers.base import (  # noqa: E402
    MESSAGE_OVERHEAD_TOKENS,
    estimate_tokens,
    prompt_tokens,
)
from app.providers.mock import MockCall, MockProvider  # noqa: E402
from app.skills.loader import SkillLoader  # noqa: E402
from app.tools.base import ToolContext, ToolRegistry, ToolResult  # noqa: E402
from app.tools.citation_check import CitationCheckTool  # noqa: E402
from app.tools.text_checks import TextChecksTool  # noqa: E402

__all__ = ["main", "run_benchmark"]

#: Fixed seed. Every corpus string, card and schedule is derived from it.
SEED: Final[int] = 20261028
#: The brief's word target. The rubric midpoints sum to 4925, inside the +-15% gate.
TARGET_WORDS: Final[int] = 4_800
#: Three iterations: draft everything, then revise twice.
ITERATIONS: Final[int] = 3

#: What the brief asks for: at least this much fewer input tokens. The report measures
#: against it and never moves it.
TARGET: Final[float] = 0.5
#: Where the generated report is committed.
DEFAULT_REPORT: Final[Path] = REPO_ROOT / "docs" / "benchmarks" / "token-benchmark.md"
#: The token convention both arms are measured with (mirrors app.providers.base).
ACCOUNT_ID: Final[str] = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

BRIEF: Final[Brief] = Brief(
    topic="Token-frugal orchestration for agentic academic writing",
    article_type=ArticleType.RESEARCH,
    audience="graduate students and applied NLP practitioners",
    language="en",
    target_words=TARGET_WORDS,
    citation_style=CitationStyle.APA7,
    must_include=[],
    must_avoid=["claims about model capability that no supplied evidence supports"],
    provided_data=["Synthetic benchmark fixture: no real measurement is quoted here."],
)

#: Sections the writers draft. ``abstract`` and ``references`` stay empty: the
#: benchmark's article task is the body, exactly as the rubric's section list says.
WRITTEN_SECTIONS: Final[tuple[str, ...]] = (
    "introduction",
    "related-work",
    "methodology",
    "results",
    "discussion",
    "limitations",
    "conclusion",
)
#: Which drafted sections each iteration revises (iteration index, 1-based body).
REVISIONS: Final[dict[int, tuple[str, ...]]] = {
    2: ("introduction", "related-work", "methodology"),
    3: ("results", "discussion"),
}

#: Synthetic source cards. They are fixtures, not literature: the venue says so and
#: every snippet says so. A benchmark may not invent sources (HARD RULE 1) - it may
#: only be explicit that its inputs are synthetic.
CARD_SEEDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("C01", "Synthetic fixture on budgeted multi-agent drafting", "Benchmark fixture A"),
    ("C02", "Synthetic fixture on token-aware pipeline scheduling", "Benchmark fixture B"),
    ("C03", "Synthetic fixture on bounded digest memory", "Benchmark fixture C"),
    ("C04", "Synthetic fixture on patch-based revision", "Benchmark fixture D"),
    ("C05", "Synthetic fixture on deterministic quality gates", "Benchmark fixture E"),
    ("C06", "Synthetic fixture on prompt-cache stability", "Benchmark fixture F"),
)


def _card_id(suffix: str) -> str:
    """A stable 26-character ULID-shaped id for a fixture card."""
    return f"01ARZ3NDEKTSV4RRFFQ69G5{suffix}"


def source_cards() -> tuple[SourceCard, ...]:
    """The six fixture cards, in a fixed order."""
    cards: list[SourceCard] = []
    for index, (suffix, title, venue) in enumerate(CARD_SEEDS):
        cards.append(
            SourceCard(
                id=_card_id(suffix),
                title=title,
                authors=[f"Fixture, {chr(ord('A') + index)}."],
                year=2024 - (index % 3),
                venue=venue,
                doi=f"10.0000/synthetic.{index + 1:03d}",
                snippet=(
                    "Synthetic benchmark fixture, not a real publication; it exists so a "
                    "token benchmark can run offline and deterministically."
                ),
                verification=VerificationStatus.UNKNOWN,
            )
        )
    return tuple(cards)


# --------------------------------------------------------------------------- corpus
_SUBJECTS: Final[tuple[str, ...]] = (
    "The deterministic controller",
    "The bounded digest",
    "Each planning cycle",
    "The patch applier",
    "The per-call budget",
    "The section store",
    "The review pass",
    "The validation pass",
)
_VERBS: Final[tuple[str, ...]] = (
    "records",
    "bounds",
    "replays",
    "shortens",
    "verifies",
    "summarises",
    "compares",
    "schedules",
)
_OBJECTS: Final[tuple[str, ...]] = (
    "every task before the next one starts",
    "the reference that the next call receives",
    "the delta rather than the whole document",
    "the ledger window into a fixed token ceiling",
    "each gate against the rubric's definition",
    "the open issues attached to a section",
    "the cached prefix across calls of one role",
    "the remaining budget before admitting work",
)


def _sentence(rng: random.Random, section_id: str, index: int) -> str:
    """One deterministic, placeholder-free sentence of synthetic academic prose."""
    subject = _SUBJECTS[rng.randrange(len(_SUBJECTS))]
    verb = _VERBS[rng.randrange(len(_VERBS))]
    obj = _OBJECTS[rng.randrange(len(_OBJECTS))]
    return f"In {section_id.replace('-', ' ')}, {subject.lower()} {verb} {obj} ({index})."


def _prose(seed: str, section_id: str, target_words: int, *, revision: int = 0) -> str:
    """Deterministic prose of roughly ``target_words`` words.

    Citations are sprinkled in as ``[[CITE:<id>]]`` markers so the writer's
    acceptance checks (and the citation gate) have something real to accept or
    reject. The marker set is derived from the section id, never from randomness, so
    a revision keeps citing the same cards.
    """
    rng = random.Random(f"{SEED}:{seed}:{section_id}:{revision}")  # noqa: S311
    cards = source_cards()
    # Index, not ``hash(section_id)``: the built-in string hash is salted per process,
    # which would make a "fixed seed" benchmark drift between runs.
    position = WRITTEN_SECTIONS.index(section_id) if section_id in WRITTEN_SECTIONS else 0
    cited = (cards[position % len(cards)], cards[(position + 2) % len(cards)])
    sentences: list[str] = []
    words = 0
    index = 0
    while words < target_words:
        sentence = _sentence(rng, section_id, index)
        if index % 5 == 4:
            sentence = sentence[:-1] + f" [[CITE:{cited[index % 2].id}]]."
        sentences.append(sentence)
        words += len(sentence.split())
        index += 1
    if revision:
        last = sentences[-1]
        sentences[-1] = (
            last[:-1] + f" Revision {revision} tightened this statement without adding a claim."
        )
    return " ".join(sentences)


_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    """Split a body into sentences. Rejoining with ``" "`` reproduces the text."""
    return [part for part in _SENTENCE_SPLIT.split(text.strip()) if part]


def _last_sentence(text: str) -> str:
    """The final sentence of a body: the locus a targeted revision patch edits."""
    return _sentences(text)[-1]


class Corpus:
    """The scripted article: one deterministic body text per section and version."""

    def __init__(self, specs: Sequence[SectionSpec]) -> None:
        self._targets: dict[str, int] = {spec.id: int(spec.target_words) for spec in specs}
        self._titles: dict[str, str] = {spec.id: spec.title for spec in specs}
        self._drafts: dict[str, str] = {
            section_id: _prose("draft", section_id, self._targets[section_id])
            for section_id in WRITTEN_SECTIONS
        }
        # A revision rewrites the final sentence of the draft and nothing else, so a
        # targeted ``replace_text`` patch reproduces it byte for byte. That is what
        # makes the two arms' articles identical.
        self._revisions: dict[tuple[str, int], str] = {}
        for iteration, section_ids in REVISIONS.items():
            for section_id in section_ids:
                parts = _sentences(self._drafts[section_id])
                parts[-1] = (
                    f"{parts[-1]} Revision {iteration} restates this without adding a claim."
                )
                self._revisions[(section_id, iteration)] = " ".join(parts)

    @property
    def targets(self) -> Mapping[str, int]:
        """Planned words per section (the rubric's midpoint)."""
        return dict(self._targets)

    @property
    def titles(self) -> Mapping[str, str]:
        """Section titles."""
        return dict(self._titles)

    def text(self, section_id: str, iteration: int) -> str:
        """The section body at the end of ``iteration``."""
        return self._revisions.get((section_id, iteration)) or self._drafts[section_id]

    def draft(self, section_id: str) -> str:
        """The first draft of a section."""
        return self._drafts[section_id]

    def revision(self, section_id: str, iteration: int) -> str:
        """The revised body written in ``iteration``."""
        return self._revisions[(section_id, iteration)]


# --------------------------------------------------------------------------- provider
class SplitProvider:
    """Wraps ``MockProvider`` and records where each prompt's tokens sit.

    Three buckets, and every prompt token lands in exactly one of them:

    * ``prefix`` - the cacheable stable prefix (``cache=True`` blocks);
    * ``blocks`` - the run-specific system blocks a role computed in ``prepare()``
      (section under edit, source cards, gate pack) plus any uncached semi-stable block;
    * ``message`` - the user message (brief, task, references, or - in the baseline -
      the whole article and the whole change log).

    The wrapper never changes a byte of the request and never answers for the mock;
    it only measures what the mock is about to be handed.
    """

    name = "mock"

    def __init__(self, inner: MockProvider) -> None:
        self._inner = inner
        self.splits: list[dict[str, int]] = []

    async def generate(
        self,
        request: LLMRequest,
        *,
        model: str | None = None,
        role: str | None = None,
        account_id: str | None = None,
    ) -> Any:
        """Measure the request, then delegate the answer to ``MockProvider``."""
        prefix = sum(estimate_tokens(block.text) for block in request.system_blocks if block.cache)
        blocks = sum(
            estimate_tokens(block.text) for block in request.system_blocks if not block.cache
        )
        message = sum(
            estimate_tokens(item.text) + MESSAGE_OVERHEAD_TOKENS for item in request.messages
        )
        schema = (
            0
            if request.json_schema is None
            else estimate_tokens(
                json.dumps(request.json_schema, sort_keys=True, separators=(",", ":"))
            )
        )
        if prefix + blocks + message != prompt_tokens(request):
            raise AssertionError(
                f"split {prefix}+{blocks}+{message} != prompt_tokens {prompt_tokens(request)}"
            )
        self.splits.append(
            {"prefix": prefix, "blocks": blocks, "message": message, "schema": schema}
        )
        return await self._inner.generate(request, model=model, role=role, account_id=account_id)

    async def count_tokens(self, request: LLMRequest) -> int | None:
        """Delegate (the mock is exact under the shared convention)."""
        return await self._inner.count_tokens(request)

    async def aclose(self) -> None:
        """Delegate."""
        await self._inner.aclose()


def _pruned_schema_size(schema: Mapping[str, Any]) -> int:
    """Tokens of a schema once unreachable ``$defs`` are dropped.

    A role's call schema is the whole ``AgentResult`` contract plus its own output
    model, and ``forbidden_result_fields`` removes the *properties* a role may not
    emit without removing the ``$defs`` they referenced. This walks the refs from
    the schema body and measures what is left, so the report can price the trim
    instead of guessing at it.
    """
    dumps = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    definitions: Mapping[str, Any] = schema.get("$defs", {}) or {}
    if not definitions:
        return estimate_tokens(dumps)

    def referenced(node: Any, found: set[str]) -> set[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                name = value.split("/")[-1] if key == "$ref" else ""
                if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                    found.add(name)
                else:
                    referenced(value, found)
        elif isinstance(node, list):
            for item in node:
                referenced(item, found)
        return found

    body = {key: value for key, value in schema.items() if key != "$defs"}
    reachable: set[str] = set()
    frontier = referenced(body, set())
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        frontier |= referenced(definitions.get(name, {}), set()) - reachable
    trimmed = dict(schema)
    trimmed["$defs"] = {key: value for key, value in definitions.items() if key in reachable}
    return estimate_tokens(json.dumps(trimmed, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------- trace
@dataclass(frozen=True)
class Call:
    """One measured provider call.

    ``prompt`` is the project's own token estimate (``app.providers.base.prompt_tokens``:
    system blocks + messages), which is what ``MockProvider`` reports and what the
    governor budgets against. ``schema`` is the constrained-output JSON schema handed to
    the provider alongside it - the planner's own prompt builder adds it to its input
    estimate, a provider bills it, so the benchmark counts it too.
    """

    role: str
    iteration: int
    label: str
    tokens_in: int
    tokens_cached: int
    tokens_out: int
    prefix: int = 0
    blocks: int = 0
    message: int = 0
    schema: int = 0

    @property
    def prompt(self) -> int:
        """All prompt tokens, cached or not."""
        return self.tokens_in + self.tokens_cached

    @property
    def wire(self) -> int:
        """Input tokens as a provider sees them: prompt + output schema."""
        return self.prompt + self.schema

    @property
    def split_total(self) -> int:
        """The four anatomy buckets; must equal ``prompt``."""
        return self.prefix + self.blocks + self.message


@dataclass(frozen=True)
class PlannerCall:
    """One Planner call, with the digest it read."""

    iteration: int
    kind: str
    tokens_in: int
    tokens_cached: int
    tokens_out: int
    digest_tokens: int
    prefix: int = 0
    blocks: int = 0
    message: int = 0
    schema: int = 0

    @property
    def prompt(self) -> int:
        """All prompt tokens, cached or not."""
        return self.tokens_in + self.tokens_cached

    @property
    def wire(self) -> int:
        """Input tokens as a provider sees them: prompt + output schema."""
        return self.prompt + self.schema

    @property
    def split_total(self) -> int:
        """The three prompt anatomy buckets; must equal ``prompt``."""
        return self.prefix + self.blocks + self.message


def _total_of(calls: Sequence[Call | PlannerCall], attr: str) -> int:
    """Sum one metric over calls: ``tokens_in``, ``tokens_cached``, ``tokens_out``,
    ``prompt`` (prompt tokens), ``schema`` or ``wire`` (prompt + schema)."""
    if attr == "prompt":
        return sum(call.prompt for call in calls)
    if attr == "wire":
        return sum(call.wire for call in calls)
    if attr == "schema":
        return sum(call.schema for call in calls)
    return sum(int(getattr(call, attr)) for call in calls)


@dataclass
class Trace:
    """Everything the report needs about one arm of the benchmark."""

    name: str
    calls: list[Call] = field(default_factory=list)
    planner_calls: list[PlannerCall] = field(default_factory=list)
    article_words: int = 0
    digest_tokens: int = 0
    ledger_entries: int = 0
    prefix_tokens: int = 0
    block_tokens: int = 0
    message_tokens: int = 0
    schema_tokens: int = 0
    pruned_schema_tokens: int = 0

    @property
    def all_calls(self) -> list[Call | PlannerCall]:
        """Agent and Planner calls, in execution order is not preserved here."""
        return [*self.calls, *self.planner_calls]

    def bucket(self, attr: str) -> int:
        """Sum one anatomy bucket (``prefix``, ``blocks``, ``message``) over all calls."""
        return sum(int(getattr(call, attr)) for call in self.all_calls)

    def total(self, attr: str) -> int:
        """Sum one metric across agent and Planner calls."""
        return _total_of(self.all_calls, attr)

    def agent_total(self, attr: str) -> int:
        """Sum one metric across agent calls only."""
        return _total_of(self.calls, attr)

    def planner_total(self, attr: str) -> int:
        """Sum one metric across Planner calls only."""
        return _total_of(self.planner_calls, attr)

    def _group(self, key: Any) -> dict[Any, dict[str, int]]:
        """Group totals by a call key (role name or iteration)."""
        rows: dict[Any, dict[str, int]] = {}

        def add(group: Any, call: Call | PlannerCall) -> None:
            row = rows.setdefault(group, {"in": 0, "cached": 0, "out": 0, "schema": 0, "calls": 0})
            row["in"] += call.tokens_in
            row["cached"] += call.tokens_cached
            row["out"] += call.tokens_out
            row["schema"] += call.schema
            row["calls"] += 1

        for agent_call in self.calls:
            add(agent_call.role if key == "role" else agent_call.iteration, agent_call)
        for planner_call in self.planner_calls:
            add("planner" if key == "role" else planner_call.iteration, planner_call)
        return rows

    def by_role(self) -> dict[str, dict[str, int]]:
        """Token totals per role (Planner included as ``planner``)."""
        return self._group("role")

    def by_iteration(self) -> dict[int, dict[str, int]]:
        """Token totals per iteration (iteration 0 holds the initial plan call)."""
        return self._group("iteration")


def check(trace: Trace) -> None:
    """Fail loudly if the anatomy buckets do not add up to the prompt total.

    Every prompt token must land in exactly one of cached prefix, role blocks or the
    user message; the schema rides beside the prompt. A benchmark whose parts do not
    sum to its whole is measuring the wrong thing, so this runs on every report.
    """
    for call in trace.all_calls:
        if call.split_total != call.prompt:
            raise AssertionError(
                f"{trace.name}: {call.role if isinstance(call, Call) else 'planner'} call "
                f"{call.label if isinstance(call, Call) else call.kind} splits to "
                f"{call.split_total}, prompt is {call.prompt}"
            )
    summed = trace.bucket("prefix") + trace.bucket("blocks") + trace.bucket("message")
    if summed != trace.total("prompt"):
        raise AssertionError(
            f"{trace.name}: buckets sum to {summed}, prompt {trace.total('prompt')}"
        )


def _wire(row: Mapping[str, int]) -> int:
    """Prompt + schema from a grouped row."""
    return row["in"] + row["cached"] + row["schema"]


# --------------------------------------------------------------------------- schedule
@dataclass(frozen=True)
class Step:
    """One scheduled agent call in the scripted article task."""

    iteration: int
    role: AgentRole
    label: str
    section_id: str | None = None


def schedule() -> list[Step]:
    """The fixed call schedule both arms execute, in order."""
    steps: list[Step] = [
        Step(1, AgentRole.ARCHITECT, "Outline the article structure"),
        Step(1, AgentRole.RESEARCHER, "Collect and card the primary sources"),
    ]
    for section_id in WRITTEN_SECTIONS:
        steps.append(
            Step(1, AgentRole.WRITER, f"Draft the {section_id.replace('-', ' ')}", section_id)
        )
    steps.append(Step(1, AgentRole.REVIEWER, "Review the drafted sections"))
    steps.append(Step(1, AgentRole.VALIDATOR, "Validate the draft against the rubric"))

    for iteration in range(2, ITERATIONS + 1):
        for section_id in REVISIONS[iteration]:
            steps.append(
                Step(
                    iteration,
                    AgentRole.WRITER,
                    f"Revise the {section_id.replace('-', ' ')}",
                    section_id,
                )
            )
        steps.append(
            Step(iteration, AgentRole.REVIEWER, f"Review the iteration-{iteration} revisions")
        )
        steps.append(
            Step(
                iteration,
                AgentRole.VALIDATOR,
                f"Validate the article after iteration {iteration}",
            )
        )
    return steps


def _task(step: Step, *, source_ids: Sequence[str] = ()) -> Task:
    """The ``Task`` a role agent receives for one schedule step."""
    return Task(
        id=new_ulid(),
        title=step.label,
        role=step.role,
        section_ids=[step.section_id] if step.section_id else [],
        source_ids=list(source_ids),
        instructions=(
            f"{step.label}. Work from the references supplied in this task; never invent "
            "sources, data or quotes."
        ),
        acceptance=["The answer is one JSON object that satisfies the role's contract"],
        token_budget=20_000,
        depends_on=[],
    )


# --------------------------------------------------------------------------- tools
class SyntheticScholarSearch:
    """A deterministic ``scholar_search`` that answers with the fixture cards."""

    name = "scholar_search"
    description = "Return the benchmark's fixture cards for any query."
    token_cost = 10

    def __init__(self, cards: Sequence[SourceCard]) -> None:
        self._cards = list(cards)
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 500},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        """Answer with the fixture cards (never a live index)."""
        _ = context
        limit = int(arguments.get("max_results", len(self._cards)) or len(self._cards))
        cards = self._cards[:limit]
        payload = {
            "cache_hit": False,
            "count": len(cards),
            "result_limited": len(cards) < len(self._cards),
            "sources": [json.loads(card.model_dump_json(by_alias=True)) for card in cards],
        }
        return ToolResult(
            tool=self.name,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )


class ArticleTextProvider:
    """Section text as the role agents read it: straight from the article store."""

    def __init__(self, article: ArticleStore) -> None:
        self._article = article

    async def section_texts(self, section_ids: Sequence[str]) -> Mapping[str, str]:
        """Current body of each requested section."""
        texts: dict[str, str] = {}
        for section_id in section_ids:
            row = self._article.storage.read_section(section_id)
            texts[section_id] = "" if row is None else row.text
        return texts


# --------------------------------------------------------------------------- answers
def _entry(task: Task, *, targets: Sequence[str] = (), summary: str = "") -> dict[str, Any]:
    """The envelope's ledger entry, with the runtime-owned fields left to the runtime."""
    return {
        "task_id": task.id,
        "role": task.role.value,
        "targets": list(targets),
        "summary": summary or f"{task.title} completed.",
        "status": "ok",
    }


def _answer(result: dict[str, Any], output: dict[str, Any]) -> str:
    """Serialise the two-document role answer the way a provider would return it."""
    return json.dumps({"result": result, "output": output}, ensure_ascii=False)


class AnswerFactory:
    """Builds the scripted role answers from the corpus. Deterministic, no model."""

    def __init__(self, corpus: Corpus, rubric: Rubric, cards: Sequence[SourceCard]) -> None:
        self._corpus = corpus
        self._rubric = rubric
        self._cards = list(cards)
        self._criteria = [criterion.name for criterion in rubric.criteria]

    # -- architect --------------------------------------------------------
    def architect(self, task: Task, iteration: int) -> str:
        """The outline: every rubric section, targets equal to the rubric midpoints."""
        sections = [
            {
                "id": section_id,
                "title": self._corpus.titles[section_id],
                "goal": f"Establish the {section_id.replace('-', ' ')} of the argument.",
                "target_words": self._corpus.targets[section_id],
                "key_claims": [f"The {section_id.replace('-', ' ')} reports what was measured."],
                "required_sources": ["At least one supplied fixture card."],
            }
            for section_id in (*WRITTEN_SECTIONS, "references")
        ]
        seeds = [
            {
                "id": row["id"],
                "title": row["title"],
                "order": order,
            }
            for order, row in enumerate(sections, start=1)  # abstract is order 0
        ]
        result = {
            "entry": _entry(
                task,
                targets=[str(row["id"]) for row in sections],
                summary="Outline the research article into eight rubric sections.",
            ),
            "sections": seeds,
        }
        _ = iteration
        return _answer(result, {"sections": sections})

    # -- researcher -------------------------------------------------------
    def researcher(self, task: Task) -> str:
        """Six cards plus one two-sentence finding each."""
        findings = [
            {
                "source_id": card.id,
                "finding": (
                    f"The fixture states a bounded claim about {card.title.lower()}. "
                    "It is synthetic input, not evidence for a real result."
                ),
            }
            for card in self._cards
        ]
        payload = [json.loads(card.model_dump_json(by_alias=True)) for card in self._cards]
        result = {
            "entry": _entry(
                task,
                targets=task.section_ids,
                summary="Card six synthetic sources, one finding each.",
            ),
            "source_cards": payload,
        }
        return _answer(result, {"findings": findings, "no_results": False})

    # -- writer -----------------------------------------------------------
    def writer(
        self,
        task: Task,
        section_id: str,
        *,
        current_text: str,
        base_version: int,
        new_text: str,
    ) -> str:
        """A patch: whole-section fill on an empty section, a targeted op afterwards."""
        if base_version == 0:
            inserted = new_text
            ops: list[dict[str, Any]] = [
                {"op": PatchOpType.REPLACE_SECTION.value, "text": inserted}
            ]
        else:
            inserted = _last_sentence(new_text)
            ops = [
                {
                    "op": PatchOpType.REPLACE_TEXT.value,
                    "old": _last_sentence(current_text),
                    "new": inserted,
                }
            ]
        markers = sorted(
            {
                part.split("[[CITE:")[1].split("]]")[0]
                for part in inserted.split()
                if "[[CITE:" in part
            }
        )
        result = {
            "entry": _entry(
                task,
                targets=[section_id],
                summary=f"Write the {section_id.replace('-', ' ')} within its word window.",
            ),
            "patches": [
                {
                    "section_id": section_id,
                    "base_version": base_version,
                    "ops": ops,
                }
            ],
        }
        output = {
            "section_id": section_id,
            "status": "ok",
            "cited_source_ids": markers,
            "missing_inputs": [],
        }
        return _answer(result, output)

    # -- reviewer ---------------------------------------------------------
    def reviewer(self, task: Task, section_ids: Sequence[str]) -> str:
        """Scores for every rubric criterion, one low-severity issue on the first section."""
        reviews = []
        for index, section_id in enumerate(section_ids):
            issues = []
            if index == 0:
                issues.append(
                    {
                        "severity": "low",
                        "anchor": "the opening sentence",
                        "problem": "The first claim arrives before the gap is stated.",
                        "suggested_fix": "State the gap, then the claim.",
                    }
                )
            reviews.append(
                {
                    "section_id": section_id,
                    "issues": issues,
                    "scores": dict.fromkeys(self._criteria, 82),
                }
            )
        result = {
            "entry": _entry(
                task,
                targets=list(section_ids),
                summary=f"Review {len(section_ids)} drafted section(s) against the rubric.",
            )
        }
        return _answer(result, {"reviews": reviews})

    # -- validator --------------------------------------------------------
    def validator(self, task: Task) -> str:
        """Subjective yes/no answers only: gates, scores and Q are computed elsewhere."""
        output = {
            "subjective_checks": {
                "claims_supported": True,
                "coherence": True,
                "methods_clarity": True,
                "results_match_claims": True,
                "limitations_and_ethics": True,
            },
            "rationale": ["Every cited claim names a supplied fixture card."],
        }
        result = {
            "entry": _entry(
                task, summary="Run the deterministic gates and answer the subjective checks."
            )
        }
        return _answer(result, output)


# --------------------------------------------------------------------------- planners
def plan_payload(corpus: Corpus) -> dict[str, Any]:
    """The initial plan draft: architect, researcher, one writer, reviewer, validator."""
    return {
        "goal": "A verified research article at Q >= 70 with every gate reported.",
        "tasks": [
            {
                "key": "T1",
                "title": "Outline the article structure",
                "role": "architect",
                "section_ids": list(WRITTEN_SECTIONS),
                "instructions": "Produce the section outline with a word target per section.",
                "acceptance": ["The outline covers every rubric section"],
                "token_budget": 6_000,
                "depends_on": [],
            },
            {
                "key": "T2",
                "title": "Collect and card the primary sources",
                "role": "researcher",
                "section_ids": ["introduction"],
                "instructions": "Find primary sources and record locatable cards.",
                "acceptance": ["Every card has a DOI or URL"],
                "token_budget": 6_000,
                "depends_on": ["T1"],
            },
            {
                "key": "T3",
                "title": "Draft the introduction",
                "role": "writer",
                "section_ids": ["introduction"],
                "instructions": "Open with the gap, then state the contribution.",
                "acceptance": ["Every citation names a supplied card"],
                "token_budget": 6_000,
                "depends_on": ["T2"],
            },
            {
                "key": "T4",
                "title": "Review the drafted sections",
                "role": "reviewer",
                "section_ids": ["introduction"],
                "instructions": "Score the rubric and name defects, never rewrite.",
                "acceptance": ["Every rubric criterion is scored"],
                "token_budget": 4_000,
                "depends_on": ["T3"],
            },
            {
                "key": "T5",
                "title": "Validate the draft against the rubric",
                "role": "validator",
                "section_ids": ["introduction"],
                "instructions": "Run the deterministic gates and score the soft criteria.",
                "acceptance": ["All eight hard gates are reported"],
                "token_budget": 4_000,
                "depends_on": ["T4"],
            },
        ],
    }


def replan_payload(corpus: Corpus) -> dict[str, Any]:
    """The delta that adds the remaining three writers after iteration 1."""
    del corpus
    return {
        "goal": None,
        "added": [
            {
                "key": "T6",
                "title": "Draft the related work",
                "role": "writer",
                "section_ids": ["related-work"],
                "instructions": "Position the article against the supplied cards.",
                "acceptance": ["Every citation names a supplied card"],
                "token_budget": 6_000,
                "depends_on": [],
            },
            {
                "key": "T7",
                "title": "Draft the methodology",
                "role": "writer",
                "section_ids": ["methodology"],
                "instructions": "Describe the procedure so a reader could repeat it.",
                "acceptance": ["The procedure is reproducible from the text"],
                "token_budget": 6_000,
                "depends_on": [],
            },
            {
                "key": "T8",
                "title": "Draft the results",
                "role": "writer",
                "section_ids": ["results"],
                "instructions": "Report only what the supplied evidence supports.",
                "acceptance": ["No claim outruns the evidence"],
                "token_budget": 6_000,
                "depends_on": [],
            },
        ],
        "removed": [],
        "reason": "Three sections still have no draft after the first validation.",
    }


def evaluation_payload(rubric: Rubric) -> dict[str, Any]:
    """The Planner's evaluation draft: every criterion scored, continue recommended."""
    return {
        "scores": dict.fromkeys([criterion.name for criterion in rubric.criteria], 78),
        "missing": ["The abstract is empty", "The discussion section has no draft"],
        "risks": ["The article is not yet at its target length"],
        "meets_minimum": False,
        "recommendation": "continue",
    }


# --------------------------------------------------------------------------- checks
def _assert_answer_is_a_patch(result: AgentResult, section_id: str, version: int) -> None:
    """A quick sanity check that the writer really returned a patch."""
    patch = next((item for item in result.patches if item.section_id == section_id), None)
    if patch is None:  # pragma: no cover - would mean the scripted answer was wrong
        raise RuntimeError(f"writer returned no patch for {section_id}")
    if patch.base_version != version:  # pragma: no cover
        raise RuntimeError(
            f"writer patched {section_id} at v{patch.base_version}, expected v{version}"
        )


def _patch_ops(patch: Any) -> list[PatchOp]:
    """Typed ops of a patch (helpers keep the driver readable)."""
    return list(patch.ops)


# --------------------------------------------------------------------------- optimized
class OptimizedRun:
    """The shipped pipeline: real agents, real Planner, real stores, fixed schedule."""

    def __init__(
        self, *, rubric: Rubric, policies: Policies, skills_dir: Path, rules_dir: Path
    ) -> None:
        self.rubric = rubric
        self.policies = policies
        self.brief = BRIEF
        self.cards = source_cards()
        self.specs = resolve_sections(rubric, self.brief.article_type)
        self.corpus = Corpus(self.specs)
        self.mock = MockProvider()
        self.provider = SplitProvider(self.mock)
        self.article = ArticleStore()
        self.memory = MemoryStore(policies, brief=self.brief)
        self.answers = AnswerFactory(self.corpus, rubric, self.cards)
        self.counters: dict[str, int] = {}
        self.trace = Trace(name="shipped pipeline")

        thresholds: dict[BindingRole, AgentBinding] = {
            BindingRole(role.value): AgentBinding(
                role=BindingRole(role.value),
                account_ids=[ACCOUNT_ID],
                strategy=BindingStrategy.DEDICATED,
                model=f"mock-{role.value}-1",
                max_input_tokens=policies.call_input_cap,
                max_output_tokens=8_000,
            )
            for role in AgentRole
        }
        thresholds[BindingRole.PLANNER] = AgentBinding(
            role=BindingRole.PLANNER,
            account_ids=[ACCOUNT_ID],
            strategy=BindingStrategy.DEDICATED,
            model="mock-planner-1",
            max_input_tokens=policies.call_input_cap,
            max_output_tokens=PLANNER_MAX_OUTPUT_TOKENS,
        )
        self.bindings = MappingBindingResolver(thresholds)
        governor = CallBudgetGovernor(policies)

        tools = ToolRegistry(
            [
                SyntheticScholarSearch(self.cards),
                TextChecksTool(rubric=rubric),
                CitationCheckTool(),
            ],
            role_tools={
                AgentRole.RESEARCHER: {"scholar_search"},
                AgentRole.VALIDATOR: {"text_checks", "citation_check"},
            },
        )
        self.registry = AgentRegistry(bindings=self.bindings)
        self.scoreboard = register_role_agents(
            self.registry,
            provider=self.provider,
            bindings=self.bindings,
            governor=governor,
            skills=SkillLoader(skills_dir),
            rules_dir=rules_dir,
            rubric=rubric,
            tools=tools,
            section_texts=ArticleTextProvider(self.article),
            targets=MappingSectionTargets(self.corpus.targets),
        )
        self.planner = PlannerService(
            provider=self.provider,
            bindings=self.bindings,
            governor=governor,
            contexts=build_planner_contexts(
                rules_dir=rules_dir,
                rubric=rubric,
                skills=SkillLoader(skills_dir),
                max_input_tokens=policies.call_input_cap,
                max_output_tokens=PLANNER_MAX_OUTPUT_TOKENS,
            ),
            rubric=rubric,
            policies=policies,
        )
        self._last_report: QualityReport | None = None

    # -- plumbing ---------------------------------------------------------
    async def _seed_sections(self) -> None:
        """Create ``abstract`` plus the rubric's sections, in reading order."""
        await self.article.create_section("abstract", "Abstract", order=0)
        for order, spec in enumerate(self.specs, start=1):
            await self.article.create_section(spec.id, spec.title, order=order)
        self.memory.update_sections(await self.article.list_state())

    async def _run_state(self, iteration: int) -> AgentRunState:
        """The run state a role agent reads: references, never bodies."""
        self.memory.update_sections(await self.article.list_state())
        return AgentRunState(
            iter=iteration,
            brief=self.brief,
            sections=tuple(await self.article.list_state()),
            sources=tuple(self.cards),
        )

    def _script(self, role: AgentRole, text: str) -> int:
        """Register the next answer for ``role`` and return its per-role call index."""
        index = self.counters.get(role.value, 0)
        self.counters[role.value] = index + 1
        self.mock.script_reply(role.value, index, text)
        return index

    def _record_since(self, start: int, *, role: str, iteration: int, label: str) -> None:
        """Move the calls made since ``start`` into the trace."""
        for call, split in zip(self.mock.calls[start:], self.provider.splits[start:], strict=False):
            self.trace.calls.append(
                Call(
                    role=role,
                    iteration=iteration,
                    label=label,
                    tokens_in=call.prompt_tokens,
                    tokens_cached=call.cached_tokens,
                    tokens_out=call.output_tokens,
                    prefix=split["prefix"],
                    blocks=split["blocks"],
                    message=split["message"],
                    schema=split["schema"],
                )
            )
            self.trace.prefix_tokens += split["prefix"]
            self.trace.block_tokens += split["blocks"]
            self.trace.message_tokens += split["message"]
            self.trace.schema_tokens += split["schema"]

    async def _apply(self, result: AgentResult) -> None:
        """Apply a role result exactly as the orchestrator does."""
        for patch in result.patches:
            await self.article.apply_patch(patch, entry_id=result.entry.id)
        if result.source_cards:
            self.memory.sources.ingest_many(result.source_cards, role=result.entry.agent_role)
        for section in result.sections:
            row = self.article.storage.read_section(section.id)
            if row is None:
                continue
            if list(section.open_issues) != list(row.open_issues):
                await self.article.set_open_issues(section.id, list(section.open_issues))
            if section.status is not row.status and row.version > 0:
                await self.article.set_status(section.id, section.status)
        self.memory.ledger.append(result.entry)
        if result.report is not None:
            self.memory.record_report(result.report)
            self._last_report = result.report
        self.memory.update_sections(await self.article.list_state())

    async def _agent_call(self, task: Task, iteration: int) -> AgentResult:
        """Run one real role agent and record every provider call it made."""
        start = len(self.mock.calls)
        result = await self.registry.run(task, await self._run_state(iteration))
        self._record_since(start, role=task.role.value, iteration=iteration, label=task.title)
        await self._apply(result)
        return result

    # -- planner ----------------------------------------------------------
    async def _digest(self) -> str:
        """Build the real bounded ``claude.md`` digest."""
        self.memory.update_sections(await self.article.list_state())
        snapshot = self.memory.build_digest()
        self.trace.digest_tokens = snapshot.tokens
        return snapshot.markdown

    async def _planner_call(self, kind: str, iteration: int, coro: Any) -> Any:
        """Record one Planner call (prompt size, cached tokens, digest size) around it."""
        start = len(self.mock.calls_for(BindingRole.PLANNER.value))
        digest = await self._digest()
        result = await coro(digest)
        calls: list[MockCall] = self.mock.calls_for(BindingRole.PLANNER.value)
        splits = self.provider.splits[len(self.mock.calls) - (len(calls) - start) :]
        for call, split in zip(calls[start:], splits, strict=False):
            self.trace.planner_calls.append(
                PlannerCall(
                    iteration=iteration,
                    kind=kind,
                    tokens_in=call.prompt_tokens,
                    tokens_cached=call.cached_tokens,
                    tokens_out=call.output_tokens,
                    digest_tokens=_tokens(digest),
                    prefix=split["prefix"],
                    blocks=split["blocks"],
                    message=split["message"],
                    schema=split["schema"],
                )
            )
            self.trace.prefix_tokens += split["prefix"]
            self.trace.block_tokens += split["blocks"]
            self.trace.message_tokens += split["message"]
            self.trace.schema_tokens += split["schema"]
        return result

    async def _plan(self, iteration: int) -> Any:
        """Call the real Planner for the first plan."""
        self._script_planner(len(self.mock.calls_for("planner")), plan_payload(self.corpus))
        budget = PlanBudget.from_policies(self.policies, iteration=iteration)
        plan = await self._planner_call(
            "plan",
            iteration,
            lambda digest: self.planner.plan(self.brief, digest, budget),
        )
        self.memory.set_plan(plan)
        return plan

    async def _replan(self, iteration: int, plan: Any) -> Any:
        """Call the real Planner for a delta and merge it."""
        self._script_planner(len(self.mock.calls_for("planner")), replan_payload(self.corpus))
        delta = await self._planner_call(
            "replan",
            iteration,
            lambda digest: self.planner.replan(
                digest,
                self._last_report,
                None,
                False,
                plan=plan,
                budget=PlanBudget.from_policies(self.policies, iteration=iteration),
            ),
        )
        merged = delta.apply_to(plan)
        self.memory.set_plan(merged)
        return merged

    async def _evaluate(self, iteration: int) -> None:
        """Call the real Planner evaluator over the real probe report."""
        self._script_planner(len(self.mock.calls_for("planner")), evaluation_payload(self.rubric))
        sections = await self.article.list_state()
        texts = {
            section.id: (self.article.storage.read_section(section.id) or None).text  # type: ignore[union-attr]
            for section in sections
        }
        view = ArticleView.from_sections(self.brief, sections, texts=texts, sources=self.cards)
        probes = build_probe_report(view, self.rubric, report=self._last_report)

        async def call(digest: str) -> Any:
            return await self.planner.evaluate(
                digest, self._last_report, probes, iteration=iteration
            )

        await self._planner_call("evaluate", iteration, call)

    def _script_planner(self, index: int, payload: Mapping[str, Any]) -> None:
        """Script the Planner's next answer (keyed by its own per-role call index)."""
        self.mock.script_reply(
            BindingRole.PLANNER.value,
            index,
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":")),
        )

    # -- the run ----------------------------------------------------------
    async def run(self) -> Trace:
        """Execute the scripted article task through the shipped pipeline."""
        await self._seed_sections()
        plan = await self._plan(0)

        for step in schedule():
            if step.role is AgentRole.ARCHITECT:
                task = _task(step)
                self._script(AgentRole.ARCHITECT, self.answers.architect(task, step.iteration))
                await self._agent_call(task, step.iteration)
            elif step.role is AgentRole.RESEARCHER:
                task = _task(step, source_ids=[card.id for card in self.cards])
                self._script(AgentRole.RESEARCHER, self.answers.researcher(task))
                await self._agent_call(task, step.iteration)
            elif step.role is AgentRole.WRITER:
                assert step.section_id is not None
                await self._write(step)
            elif step.role is AgentRole.REVIEWER:
                section_ids = [
                    section.id
                    for section in await self.article.list_state()
                    if section.id in WRITTEN_SECTIONS and section.version > 0
                ]
                task = _task(step, source_ids=[card.id for card in self.cards])
                task = task.model_copy(update={"section_ids": section_ids})
                self._script(AgentRole.REVIEWER, self.answers.reviewer(task, section_ids))
                await self._agent_call(task, step.iteration)
            elif step.role is AgentRole.VALIDATOR:
                task = _task(step, source_ids=[card.id for card in self.cards])
                sections = await self.article.list_state()
                task = task.model_copy(update={"section_ids": [section.id for section in sections]})
                self._script(AgentRole.VALIDATOR, self.answers.validator(task))
                await self._agent_call(task, step.iteration)
            else:  # pragma: no cover - the schedule has no other roles
                raise AssertionError(f"unhandled role {step.role}")

            if step.role is AgentRole.VALIDATOR:
                await self._evaluate(step.iteration)
                if step.iteration == 1:
                    plan = await self._replan(2, plan)

        words = 0
        for sid in self.article.section_ids():
            row = self.article.storage.read_section(sid)
            if row is not None and sid != "references":
                words += len(row.text.split())
        self.trace.article_words = words
        self.trace.ledger_entries = len(self.memory.ledger.entries())
        self.trace.pruned_schema_tokens = self._pruned_schema_tokens()
        return self.trace

    def _pruned_schema_tokens(self) -> int:
        """What this run's role schemas would cost with unreachable ``$defs`` dropped."""
        total = 0
        for role in AgentRole:
            calls = sum(1 for call in self.trace.calls if call.role == role.value)
            agent = self.registry.resolve(role)
            if calls and isinstance(agent, RoleAgent):
                total += calls * _pruned_schema_size(agent.call_schema())
        return total

    async def _write(self, step: Step) -> None:
        """One writer call: draft on iteration 1, targeted revision afterwards."""
        assert step.section_id is not None
        section_id = step.section_id
        row = self.article.storage.read_section(section_id)
        assert row is not None
        current_text = row.text
        new_text = (
            self.corpus.draft(section_id)
            if step.iteration == 1
            else self.corpus.revision(section_id, step.iteration)
        )
        task = _task(step, source_ids=[card.id for card in self.cards])
        self._script(
            AgentRole.WRITER,
            self.answers.writer(
                task,
                section_id,
                current_text=current_text,
                base_version=row.version,
                new_text=new_text,
            ),
        )
        result = await self._agent_call(task, step.iteration)
        _assert_answer_is_a_patch(result, section_id, row.version)


def _tokens(text: str) -> int:
    """The shared ``ceil(chars / 4)`` token convention."""
    from app.providers.base import estimate_tokens

    return estimate_tokens(text)


def _naive_answer_schema(*, article: bool = False) -> dict[str, Any]:
    """A compact schema for the baseline's answer, the size a plain loop would send."""
    properties: dict[str, Any] = {
        "entry": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "role": {"type": "string"},
                "targets": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["task_id", "role", "targets", "summary", "status"],
            "additionalProperties": False,
        }
    }
    required = ["entry"]
    if article:
        properties["article"] = {"type": "string", "description": "The whole article."}
        required.append("article")
    return {
        "title": "BaselineAnswer",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- naive
class NaiveRun:
    """The baseline: whole article plus whole log in every call, no patches, no cache.

    The *static* instruction prefix - root rules, role rules, skill index, role prompt,
    tool schemas - is built by the same production builders as the shipped arm, with
    ``cache=False``. That is deliberate: the baseline must not be a worse-instructed
    pipeline, or the benchmark would measure "fewer instructions" instead of "fewer
    tokens". Everything that differs is run-specific material:

    * the user message carries the whole article and the whole change log instead of
      references (task, section state, digest slice);
    * a write re-emits the whole article instead of a patch;
    * nothing is cached.

    It walks the *same* schedule and produces the *same* article, so the arms are
    matched call for call. Answers go through ``MockProvider`` too, so the token
    arithmetic is byte-for-byte the same convention as the shipped arm.
    """

    def __init__(
        self,
        *,
        rubric: Rubric,
        policies: Policies,
        skills_dir: Path,
        rules_dir: Path,
    ) -> None:
        self.rubric = rubric
        self.policies = policies
        self.specs = resolve_sections(rubric, BRIEF.article_type)
        self.corpus = Corpus(self.specs)
        self.cards = source_cards()
        self.mock = MockProvider()
        self.provider = SplitProvider(self.mock)
        self.answers = AnswerFactory(self.corpus, rubric, self.cards)
        self.trace = Trace(name="naive baseline")
        self.counters: dict[str, int] = {}
        self.log: list[dict[str, Any]] = []
        self.texts: dict[str, str] = {spec.id: "" for spec in self.specs}
        self.orders = {spec.id: order for order, spec in enumerate(self.specs, start=1)}

        self.skills = SkillLoader(skills_dir)
        self.tools = ToolRegistry(
            [
                SyntheticScholarSearch(self.cards),
                TextChecksTool(rubric=rubric),
                CitationCheckTool(),
            ],
            role_tools={
                AgentRole.RESEARCHER: {"scholar_search"},
                AgentRole.VALIDATOR: {"text_checks", "citation_check"},
            },
        )
        self.role_builders = {
            role: RolePromptContextBuilder(
                inner=default_role_context_builder(
                    role, skills=self.skills, rules_dir=rules_dir, tools=self.tools
                ),
                prompt=load_role_prompt(role),
            )
            for role in AgentRole
        }
        contexts = build_planner_contexts(
            rules_dir=rules_dir,
            rubric=rubric,
            skills=self.skills,
            max_input_tokens=policies.call_input_cap,
            max_output_tokens=PLANNER_MAX_OUTPUT_TOKENS,
        )
        self.planner_builders = {
            "plan": PlannerPromptBuilder(contexts.planning),
            "replan": PlannerPromptBuilder(contexts.planning),
            "evaluate": PlannerPromptBuilder(contexts.evaluation),
        }

    # -- shared state -----------------------------------------------------
    def _section_states(self) -> tuple[SectionState, ...]:
        """The article's section states, as the baseline's own store would hold them."""
        states: list[SectionState] = []
        for spec in self.specs:
            text = self.texts[spec.id]
            states.append(
                SectionState(
                    id=spec.id,
                    title=spec.title,
                    order=self.orders[spec.id],
                    version=1 if text else 0,
                    hash=content_hash(text),
                    words=len(text.split()),
                    status=SectionStatus.DRAFT if text else SectionStatus.EMPTY,
                )
            )
        return tuple(states)

    def _run_state(self, iteration: int) -> AgentRunState:
        """The run state a builder reads (references only; the message carries bodies)."""
        return AgentRunState(
            iter=iteration,
            brief=BRIEF,
            sections=self._section_states(),
            sources=self.cards,
        )

    # -- prompt assembly --------------------------------------------------
    def _prefix(self, role: AgentRole, task: Task, iteration: int) -> list[SystemBlock]:
        """The production static prefix for ``role``, with caching turned off."""
        context = self.role_builders[role].build(task=task, run_state=self._run_state(iteration))
        return [SystemBlock(text=block.text, cache=False) for block in context.system_blocks]

    def _planner_prefix(self, kind: str, iteration: int) -> list[SystemBlock]:
        """The production Planner prefix (rules + planner prompt + rubric + skills)."""
        builder = self.planner_builders[kind]
        task = planner_task(
            purpose=kind,
            instructions="Baseline planner call.",
            budget=PlanBudget.from_policies(self.policies, iteration=iteration),
            role=AgentRole.ARCHITECT if kind != "evaluate" else AgentRole.VALIDATOR,
            id_factory=new_ulid,
        )
        request = builder.build(
            task=task,
            run_state=PlannerRunState(brief=BRIEF, iter=iteration, digest=""),
            instruction="Baseline planner call.",
            json_schema={"type": "object"},
            max_input_tokens=self.policies.call_input_cap,
            max_output_tokens=PLANNER_MAX_OUTPUT_TOKENS,
        )
        blocks = request.request.system_blocks
        return [SystemBlock(text=block.text, cache=False) for block in blocks]

    def _article_markdown(self) -> str:
        """The whole current article, as the baseline sends it."""
        sections = [section_from(state, self.texts[state.id]) for state in self._section_states()]
        return assemble(sections, title=BRIEF.topic).markdown

    def _log_markdown(self) -> str:
        """The whole change log: every entry, every iteration."""
        if not self.log:
            return "(no changes yet)"
        return "\n".join(
            json.dumps(entry, sort_keys=True, separators=(",", ":")) for entry in self.log
        )

    def _message(self, instruction: str, task: Task | None) -> str:
        """Brief, whole article, whole log, task - the baseline's context."""
        parts = [
            "## Brief",
            json.dumps(json.loads(BRIEF.model_dump_json(by_alias=True)), sort_keys=True),
            "## Full article",
            self._article_markdown(),
            "## Full change log",
            self._log_markdown(),
            "## Instruction",
            instruction,
        ]
        if task is not None:
            parts.extend(
                [
                    "## Task",
                    json.dumps(json.loads(task.model_dump_json(by_alias=True)), sort_keys=True),
                ]
            )
        return "\n\n".join(parts)

    @staticmethod
    def _request(
        blocks: Sequence[SystemBlock],
        message: str,
        *,
        schema: Mapping[str, Any] | None = None,
        max_output: int = 8_000,
    ) -> LLMRequest:
        """A baseline request: the same prefix, one whole-article user message.

        The baseline also asks for a JSON answer through a schema, so the wire cost of
        structured output is charged to both arms (its schema is far smaller - it
        describes a plain ``{entry, article}`` answer, not the frozen contracts).
        """
        return LLMRequest(
            system_blocks=list(blocks),
            messages=[LLMMessage(role=MessageRole.USER, text=message)],
            max_output_tokens=max_output,
            json_schema=dict(schema) if schema is not None else None,
        )

    def _record(self, start: int, *, role: str, iteration: int, label: str) -> None:
        """Move the calls made since ``start`` into the trace."""
        for call, split in zip(self.mock.calls[start:], self.provider.splits[start:], strict=False):
            self.trace.calls.append(
                Call(
                    role=role,
                    iteration=iteration,
                    label=label,
                    tokens_in=call.prompt_tokens,
                    tokens_cached=call.cached_tokens,
                    tokens_out=call.output_tokens,
                    prefix=split["prefix"],
                    blocks=split["blocks"],
                    message=split["message"],
                    schema=split["schema"],
                )
            )
            self.trace.prefix_tokens += split["prefix"]
            self.trace.block_tokens += split["blocks"]
            self.trace.message_tokens += split["message"]
            self.trace.schema_tokens += split["schema"]

    async def _call(
        self,
        request: LLMRequest,
        *,
        role: str,
        iteration: int,
        label: str,
        answer: str,
    ) -> None:
        """Script one answer, make one measured baseline call, log it."""
        index = self.counters.get(role, 0)
        self.counters[role] = index + 1
        self.mock.script_reply(role, index, answer)
        start = len(self.mock.calls)
        await self.provider.generate(request, role=role)
        self._record(start, role=role, iteration=iteration, label=label)
        self.log.append(
            {
                "iter": iteration,
                "role": role,
                "label": label,
                "summary": f"{label} completed in the baseline loop.",
                "status": "ok",
            }
        )

    # -- planner ----------------------------------------------------------
    async def _planner(
        self, *, kind: str, iteration: int, payload: Mapping[str, Any], instruction: str
    ) -> None:
        """One baseline Planner call, measured exactly like the shipped one."""
        blocks = self._planner_prefix(kind, iteration)
        planner_schema = {
            "plan": plan_draft_schema(),
            "replan": plan_delta_schema(),
            "evaluate": evaluation_schema(),
        }[kind]
        request = self._request(
            blocks,
            self._message(f"[{kind}] {instruction}", None),
            schema=planner_schema,
            max_output=max(PLANNER_MAX_OUTPUT_TOKENS, EVALUATOR_MAX_OUTPUT_TOKENS),
        )
        index = self.counters.get("planner", 0)
        self.counters["planner"] = index + 1
        self.mock.script_reply(
            "planner",
            index,
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":")),
        )
        start = len(self.mock.calls_for("planner"))
        await self.provider.generate(request, role="planner")
        calls = self.mock.calls_for("planner")
        splits = self.provider.splits[len(self.mock.calls) - (len(calls) - start) :]
        for call, split in zip(calls[start:], splits, strict=False):
            self.trace.planner_calls.append(
                PlannerCall(
                    iteration=iteration,
                    kind=kind,
                    tokens_in=call.prompt_tokens,
                    tokens_cached=call.cached_tokens,
                    tokens_out=call.output_tokens,
                    digest_tokens=_tokens(self._article_markdown()) + _tokens(self._log_markdown()),
                    prefix=split["prefix"],
                    blocks=split["blocks"],
                    message=split["message"],
                    schema=split["schema"],
                )
            )
            self.trace.prefix_tokens += split["prefix"]
            self.trace.block_tokens += split["blocks"]
            self.trace.message_tokens += split["message"]
            self.trace.schema_tokens += split["schema"]

    # -- the run ----------------------------------------------------------
    async def run(self) -> Trace:
        """Execute the same scripted article task, naively."""
        await self._planner(
            kind="plan",
            iteration=0,
            payload=plan_payload(self.corpus),
            instruction="Produce the plan for this brief.",
        )

        for step in schedule():
            task = _task(step, source_ids=[card.id for card in self.cards])
            if step.role is AgentRole.ARCHITECT:
                await self._call(
                    self._request(
                        self._prefix(AgentRole.ARCHITECT, task, step.iteration),
                        self._message("Outline the article.", task),
                        schema=_naive_answer_schema(),
                    ),
                    role="architect",
                    iteration=step.iteration,
                    label=step.label,
                    answer=self.answers.architect(task, step.iteration),
                )
            elif step.role is AgentRole.RESEARCHER:
                await self._call(
                    self._request(
                        self._prefix(AgentRole.RESEARCHER, task, step.iteration),
                        self._message("Collect and card the primary sources.", task),
                        schema=_naive_answer_schema(),
                    ),
                    role="researcher",
                    iteration=step.iteration,
                    label=step.label,
                    answer=self.answers.researcher(task),
                )
            elif step.role is AgentRole.WRITER:
                await self._write(step)
            elif step.role is AgentRole.REVIEWER:
                await self._review(step)
            elif step.role is AgentRole.VALIDATOR:
                await self._validate(step)
                await self._planner(
                    kind="evaluate",
                    iteration=step.iteration,
                    payload=evaluation_payload(self.rubric),
                    instruction="Evaluate the article state against the brief and the rubric.",
                )
                if step.iteration == 1:
                    await self._planner(
                        kind="replan",
                        iteration=2,
                        payload=replan_payload(self.corpus),
                        instruction="Revise the plan. Return only the delta JSON.",
                    )

        self.trace.article_words = sum(len(text.split()) for text in self.texts.values())
        self.trace.ledger_entries = len(self.log)
        self.trace.digest_tokens = _tokens(self._log_markdown())
        return self.trace

    async def _write(self, step: Step) -> None:
        """A writer call that re-emits the whole article (no patches)."""
        assert step.section_id is not None
        section_id = step.section_id
        new_text = (
            self.corpus.draft(section_id)
            if step.iteration == 1
            else self.corpus.revision(section_id, step.iteration)
        )
        self.texts[section_id] = new_text
        task = _task(step, source_ids=[card.id for card in self.cards])
        answer = json.dumps(
            {
                "entry": _entry(task, targets=[section_id], summary=f"Rewrite {section_id}."),
                "article": self._article_markdown(),
            },
            ensure_ascii=False,
        )
        await self._call(
            self._request(
                self._prefix(AgentRole.WRITER, task, step.iteration),
                self._message(step.label, task),
                schema=_naive_answer_schema(article=True),
            ),
            role="writer",
            iteration=step.iteration,
            label=step.label,
            answer=answer,
        )

    async def _review(self, step: Step) -> None:
        """A reviewer call over the whole article."""
        section_ids = [sid for sid in self.texts if self.texts[sid]]
        task = _task(step, source_ids=[card.id for card in self.cards])
        task = task.model_copy(update={"section_ids": section_ids})
        await self._call(
            self._request(
                self._prefix(AgentRole.REVIEWER, task, step.iteration),
                self._message(step.label, task),
                schema=_naive_answer_schema(),
            ),
            role="reviewer",
            iteration=step.iteration,
            label=step.label,
            answer=self.answers.reviewer(task, section_ids),
        )

    async def _validate(self, step: Step) -> None:
        """A validator call over the whole article."""
        task = _task(step, source_ids=[card.id for card in self.cards])
        task = task.model_copy(update={"section_ids": list(self.texts)})
        await self._call(
            self._request(
                self._prefix(AgentRole.VALIDATOR, task, step.iteration),
                self._message(step.label, task),
                schema=_naive_answer_schema(),
            ),
            role="validator",
            iteration=step.iteration,
            label=step.label,
            answer=self.answers.validator(task),
        )


# --------------------------------------------------------------------------- report
ROLE_ORDER: Final[tuple[str, ...]] = (
    "architect",
    "researcher",
    "writer",
    "reviewer",
    "validator",
    "planner",
)

BLANK: Final[dict[str, int]] = {"in": 0, "cached": 0, "out": 0, "schema": 0, "calls": 0}


def _n(value: int) -> str:
    """A number with thousands separators."""
    return f"{value:,}"


def _pct(part: int, whole: int) -> str:
    """``part`` as a percentage of ``whole``."""
    return f"{part / whole * 100:.1f}%" if whole else "n/a"


def _saved(naive_value: int, shipped_value: int) -> float:
    """Fraction of the baseline that the shipped arm avoided."""
    return 0.0 if naive_value <= 0 else (naive_value - shipped_value) / naive_value


def _delta(naive_value: int, shipped_value: int) -> str:
    """Signed change from the baseline, in tokens and percent."""
    if naive_value == 0:
        return "-"
    change = shipped_value - naive_value
    return f"{change:+,} ({change / naive_value * 100:+.1f}%)"


def _share(part: int, whole: int) -> str:
    """A count with its share of a whole: ``38,343 (35.3%)``."""
    return f"{_n(part)} ({_pct(part, whole)})"


def _row(cells: Sequence[Any]) -> str:
    """One markdown table row."""
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """A markdown table, header and delimiter included."""
    lines = [_row(header), "|" + "|".join("---" for _ in header) + "|"]
    lines.extend(_row(row) for row in rows)
    return lines


def _role_row(trace: Trace, role: str) -> dict[str, int]:
    """One role's totals, zero-filled so both arms tabulate identically."""
    return trace.by_role().get(role, dict(BLANK))


def _iteration_row(trace: Trace, iteration: int) -> dict[str, int]:
    """One iteration's totals, zero-filled."""
    return trace.by_iteration().get(iteration, dict(BLANK))


def _prompt(row: Mapping[str, int]) -> int:
    """Prompt tokens from a grouped row."""
    return row["in"] + row["cached"]


def _call_role(call: Call | PlannerCall) -> str:
    """The role label used in the report."""
    return call.role if isinstance(call, Call) else "planner"


def _call_label(call: Call | PlannerCall) -> str:
    """The label used in the report."""
    return call.label if isinstance(call, Call) else call.kind


def _call_key(call: Call | PlannerCall) -> tuple[int, str, str]:
    """The identity both arms share for one scheduled call."""
    return (call.iteration, _call_role(call), _call_label(call))


def _totals_table(naive: Trace, optimized: Trace) -> list[str]:
    """Every token metric the brief asks for, in one table."""
    rows: list[list[Any]] = []
    metrics = (
        ("**Prompt tokens (input, repo convention)**", "prompt"),
        ("  of which uncached (full rate)", "tokens_in"),
        ("  of which served from cache", "tokens_cached"),
        ("  plus the JSON output schema", "schema"),
        ("  = input tokens incl. schema", "wire"),
        ("Output tokens", "tokens_out"),
    )
    for label, attr in metrics:
        n_value, o_value = naive.total(attr), optimized.total(attr)
        rows.append([label, _n(n_value), _n(o_value), _delta(n_value, o_value)])
    all_naive = naive.total("wire") + naive.total("tokens_out")
    all_opt = optimized.total("wire") + optimized.total("tokens_out")
    rows.append(
        [
            "**All tokens (prompt + schema + output)**",
            _n(all_naive),
            _n(all_opt),
            _delta(all_naive, all_opt),
        ]
    )
    return _table(["Metric", "Naive baseline", "Shipped pipeline", "Shipped minus naive"], rows)


def _anatomy_table(naive: Trace, optimized: Trace) -> list[str]:
    """Where every prompt token sits, and how big the schema is, per arm."""
    rows: list[list[Any]] = []
    for name, trace in (("Naive baseline", naive), ("Shipped pipeline", optimized)):
        prompt = trace.total("prompt")
        rows.append(
            [
                name,
                _share(trace.prefix_tokens, prompt),
                _share(trace.block_tokens, prompt),
                _share(trace.message_tokens, prompt),
                _n(prompt),
                _share(trace.schema_tokens, prompt),
            ]
        )
    return _table(
        [
            "Arm",
            "Cached prefix",
            "Role blocks",
            "User message",
            "Prompt total",
            "Schema (beside the prompt)",
        ],
        rows,
    )


def _role_table(naive: Trace, optimized: Trace) -> list[str]:
    """Prompt, schema, output tokens and saving per role."""
    rows: list[list[Any]] = []
    for role in ROLE_ORDER:
        n_row, o_row = _role_row(naive, role), _role_row(optimized, role)
        if not n_row["calls"] and not o_row["calls"]:
            continue
        rows.append(
            [
                role,
                n_row["calls"],
                _n(_prompt(n_row)),
                _n(n_row["out"]),
                o_row["calls"],
                _n(_prompt(o_row)),
                _n(o_row["schema"]),
                _n(o_row["out"]),
                _pct(_prompt(n_row) - _prompt(o_row), _prompt(n_row)),
            ]
        )
    rows.append(
        [
            "**all**",
            len(naive.all_calls),
            _n(naive.total("prompt")),
            _n(naive.total("tokens_out")),
            len(optimized.all_calls),
            _n(optimized.total("prompt")),
            _n(optimized.total("schema")),
            _n(optimized.total("tokens_out")),
            _pct(naive.total("prompt") - optimized.total("prompt"), naive.total("prompt")),
        ]
    )
    return _table(
        [
            "Role",
            "Naive calls",
            "Naive prompt",
            "Naive out",
            "Shipped calls",
            "Shipped prompt",
            "Shipped schema",
            "Shipped out",
            "Prompt saved %",
        ],
        rows,
    )


def _iteration_table(naive: Trace, optimized: Trace) -> list[str]:
    """Prompt, schema and output tokens per iteration."""
    rows: list[list[Any]] = []
    for iteration in sorted(set(naive.by_iteration()) | set(optimized.by_iteration())):
        n_row, o_row = _iteration_row(naive, iteration), _iteration_row(optimized, iteration)
        rows.append(
            [
                iteration,
                n_row["calls"],
                _n(_prompt(n_row)),
                _n(n_row["out"]),
                o_row["calls"],
                _n(_prompt(o_row)),
                _n(o_row["schema"]),
                _n(o_row["out"]),
                _pct(_prompt(n_row) - _prompt(o_row), _prompt(n_row)),
            ]
        )
    return _table(
        [
            "Iteration",
            "Naive calls",
            "Naive prompt",
            "Naive out",
            "Shipped calls",
            "Shipped prompt",
            "Shipped schema",
            "Shipped out",
            "Prompt saved %",
        ],
        rows,
    )


def _planner_table(naive: Trace, optimized: Trace) -> list[str]:
    """Planner input per call: prompt, the history it read, output."""
    rows: list[list[Any]] = []
    naive_calls, optimized_calls = naive.planner_calls, optimized.planner_calls
    for index in range(max(len(naive_calls), len(optimized_calls))):
        n_call = naive_calls[index] if index < len(naive_calls) else None
        o_call = optimized_calls[index] if index < len(optimized_calls) else None
        head = o_call or n_call
        assert head is not None
        n_prompt = n_call.prompt if n_call else 0
        o_prompt = o_call.prompt if o_call else 0
        rows.append(
            [
                head.iteration,
                head.kind,
                _n(n_prompt) if n_call else "-",
                _n(n_call.digest_tokens) if n_call else "-",
                _n(o_call.prompt) if o_call else "-",
                _n(o_call.digest_tokens) if o_call else "-",
                _n(o_call.schema) if o_call else "-",
                _n(o_call.tokens_out) if o_call else "-",
                _pct(n_prompt - o_prompt, n_prompt) if n_call else "-",
            ]
        )
    rows.append(
        [
            "**all**",
            f"{len(naive_calls)} / {len(optimized_calls)}",
            _n(naive.planner_total("prompt")),
            "-",
            _n(optimized.planner_total("prompt")),
            "-",
            _n(optimized.planner_total("schema")),
            _n(optimized.planner_total("tokens_out")),
            _pct(
                naive.planner_total("prompt") - optimized.planner_total("prompt"),
                naive.planner_total("prompt"),
            ),
        ]
    )
    return _table(
        [
            "Iter",
            "Kind",
            "Naive prompt",
            "Naive history read",
            "Shipped prompt",
            "Shipped digest",
            "Schema",
            "Output",
            "Prompt saved %",
        ],
        rows,
    )


def _call_table(naive: Trace, optimized: Trace) -> list[str]:
    """Every call in both arms, paired by (iteration, role, label)."""
    unmatched = {_call_key(call): call for call in naive.all_calls}
    rows: list[list[Any]] = []
    for call in optimized.all_calls:
        key = _call_key(call)
        before = unmatched.pop(key, None)
        n_prompt = before.prompt if before else 0
        rows.append(
            [
                call.iteration,
                _call_role(call),
                key[2],
                _n(n_prompt) if before else "-",
                _n(call.prompt),
                _pct(n_prompt - call.prompt, n_prompt) if before else "-",
                _n(call.schema),
            ]
        )
    if unmatched:
        raise AssertionError(f"baseline calls with no shipped counterpart: {sorted(unmatched)}")
    return _table(
        ["Iter", "Role", "Call", "Naive prompt", "Shipped prompt", "Saved %", "Schema"],
        rows,
    )


def _where_tokens_go(optimized: Trace) -> list[str]:
    """The shipped arm's prompt and schema by role, largest prompt first."""
    prompt_total = optimized.total("prompt")
    rows: list[list[Any]] = []
    for role, row in optimized.by_role().items():
        rows.append(
            [
                role,
                row["calls"],
                _n(_prompt(row)),
                _pct(_prompt(row), prompt_total),
                _n(row["schema"]),
                _n(_prompt(row) + row["schema"]),
            ]
        )
    rows.sort(key=lambda row: -int(str(row[2]).replace(",", "")))
    rows.append(
        [
            "**all**",
            len(optimized.all_calls),
            _n(prompt_total),
            "100.0%",
            _n(optimized.total("schema")),
            _n(optimized.total("wire")),
        ]
    )
    return _table(["Role", "Calls", "Prompt", "Share of prompt", "Schema", "Prompt + schema"], rows)


def _sensitivity(naive: Trace, optimized: Trace) -> list[str]:
    """The same run, measured five defensible ways."""
    variants = (
        (
            "**Prompt tokens (repo convention)**",
            naive.total("prompt"),
            optimized.total("prompt"),
        ),
        (
            "Prompt + output schema (provider wire)",
            naive.total("wire"),
            optimized.total("wire"),
        ),
        (
            "Uncached prompt only (cache hits excluded)",
            naive.total("tokens_in"),
            optimized.total("tokens_in"),
        ),
        ("Output", naive.total("tokens_out"), optimized.total("tokens_out")),
        (
            "All tokens (prompt + schema + output)",
            naive.total("wire") + naive.total("tokens_out"),
            optimized.total("wire") + optimized.total("tokens_out"),
        ),
    )
    rows = []
    for label, n_value, o_value in variants:
        savings = _saved(n_value, o_value)
        rows.append(
            [
                label,
                _n(n_value),
                _n(o_value),
                f"{savings * 100:.1f}%",
                "yes" if savings >= TARGET else "no",
            ]
        )
    return _table(["Measure", "Naive", "Shipped", "Saved", "Target met?"], rows)


def render_report(
    naive: Trace,
    optimized: Trace,
    *,
    deterministic: bool | None,
    target: float = TARGET,
) -> str:
    """Render the Markdown report from the two measured traces."""
    check(naive)
    check(optimized)

    n_prompt, o_prompt = naive.total("prompt"), optimized.total("prompt")
    n_wire, o_wire = naive.total("wire"), optimized.total("wire")
    n_out, o_out = naive.total("tokens_out"), optimized.total("tokens_out")
    n_all, o_all = n_wire + n_out, o_wire + o_out
    saved = _saved(n_prompt, o_prompt)
    wire_saved = _saved(n_wire, o_wire)
    uncached_saved = _saved(naive.total("tokens_in"), optimized.total("tokens_in"))
    output_saved = _saved(n_out, o_out)
    all_saved = _saved(n_all, o_all)
    met, wire_met = saved >= target, wire_saved >= target
    calls = len(optimized.all_calls)
    schema = optimized.total("schema")
    schema_per_call = schema / max(1, calls)
    measured = (saved, wire_saved, uncached_saved, all_saved, output_saved)

    writer_n, writer_o = _role_row(naive, "writer"), _role_row(optimized, "writer")
    reviewer_n, reviewer_o = _role_row(naive, "reviewer"), _role_row(optimized, "reviewer")
    validator_n, validator_o = _role_row(naive, "validator"), _role_row(optimized, "validator")
    architect_o, researcher_o = (
        _role_row(optimized, "architect"),
        _role_row(optimized, "researcher"),
    )
    one_shot = _prompt(architect_o) + _prompt(researcher_o)
    biggest_schema = max(optimized.all_calls, key=lambda call: call.schema, default=None)
    largest_planner = max(optimized.planner_calls, key=lambda call: call.prompt, default=None)
    digest_max = max((call.digest_tokens for call in optimized.planner_calls), default=0)
    reviewer_blocks = sum(call.blocks for call in optimized.calls if call.role == "reviewer")
    writer_blocks = sum(call.blocks for call in optimized.calls if call.role == "writer")
    planner_message = sum(call.message for call in optimized.planner_calls)
    prefix_share = optimized.prefix_tokens / max(1, o_prompt)
    schema_cut = schema - optimized.pruned_schema_tokens
    reviewer_cut = reviewer_blocks * 2 // 3
    writer_cut = writer_blocks // 2
    projected = o_wire - schema_cut - reviewer_cut - writer_cut
    projected_saved = _saved(n_wire, projected)
    role_schema = (
        _n(biggest_schema.schema) + " tokens, on the " + _call_role(biggest_schema) + " call"
        if biggest_schema is not None
        else "n/a"
    )

    lines: list[str] = [
        "# Token benchmark: shipped pipeline vs naive whole-article baseline",
        "",
        "Generated by `benchmarks/token_benchmark.py`: one scripted article task, run end",
        "to end twice, `MockProvider` only. No live API call, no key, no network (HARD RULE",
        "5), and no configured limit or threshold was changed to make it pass.",
        "",
        "## Headline",
        "",
        f"* **Input tokens (prompt): {saved * 100:.1f}% fewer**, {_n(n_prompt)} -> "
        f"{_n(o_prompt)}. Target at least {target * 100:.0f}%: "
        f"**{'MET' if met else 'NOT MET'}**.",
        f"* **Input tokens incl. the JSON output schema: {wire_saved * 100:.1f}% fewer**, "
        f"{_n(n_wire)} -> {_n(o_wire)}: the target is "
        f"{'met' if wire_met else 'NOT met'} on that reading.",
        f"* **Output tokens: {output_saved * 100:.1f}% fewer**, {_n(n_out)} -> "
        f"{_n(o_out)}. **All tokens: {all_saved * 100:.1f}% fewer**, {_n(n_all)} -> "
        f"{_n(o_all)}.",
        f"* **Cached prompt tokens:** {_n(naive.total('tokens_cached'))} in the baseline, "
        f"{_n(optimized.total('tokens_cached'))} shipped. Caching changes the bill, not "
        "the token count: the same bytes are sent either way.",
        "",
        "### Which measure is the answer",
        "",
        "The repository's own accounting is prompt tokens: `prompt_tokens` over system",
        "blocks and messages, which is what `MockProvider` reports, what `TokenUsage`",
        f"records and what the governor caps. On it the pipeline clears the target at "
        f"{saved * 100:.1f}%.",
        "",
        "A provider bills one thing more: every call also carries a JSON schema",
        "(`LLMRequest.json_schema`) that constrains the answer. It is input on the wire -",
        "the Anthropic adapter ships it as a forced tool, the OpenAI adapter as",
        f"`response_format` - and it is {_n(schema)} tokens here, {_pct(schema, o_wire)} of",
        "the shipped arm's wire input. The Planner's own prompt builder already counts",
        "schema tokens in its input estimate; the role runtime does not. Counting it in",
        f"both arms, the saving is {wire_saved * 100:.1f}% and the target is",
        f"{'met' if wire_met else 'not met'}.",
        "",
        "The report is built on the prompt-token measure because that is the one the",
        "target, the governor and the telemetry are phrased in, and because the two arms",
        "ask for structured output with schemas of very different size - a difference in",
        "output format, not in context. That difference is reported rather than buried: it",
        "is the first fix below, and it is worth "
        f"{_n(schema - naive.total('schema'))} tokens over this run.",
        "",
        "## What was measured",
        "",
        f"One scripted article task, run end to end twice. Each arm makes the same {calls}",
        f"calls over {ITERATIONS} iterations - {len(optimized.calls)} role calls and",
        f"{len(optimized.planner_calls)} Planner calls - against one {TARGET_WORDS:,}-word",
        f"brief, and each ends with the same {optimized.article_words:,}-word article and",
        f"the same {optimized.ledger_entries}-entry ledger. Same work, same text, same",
        "answers; only the context construction differs.",
        "",
        "| | Naive baseline | Shipped pipeline |",
        "|---|---|---|",
        "| Static instructions | identical bytes, same production builders | identical bytes |",
        "| Prompt caching | `cache=False` everywhere | the stable prefix is `cache=True` |",
        "| Per-call context | the whole article and the whole change log in the user message "
        "| the task, the section under edit, source cards, gate pack |",
        "| Edits | no patches: a write re-emits the whole article | patches: a targeted op "
        "carries only the delta |",
        "| Planner context | the whole article and the whole log | a bounded `claude.md` "
        "digest, a compact view, deterministic gate results |",
        f"| Output schema | a plain `{{entry, article}}` shape, "
        f"{_n(naive.total('schema'))} tokens over the run | the frozen contract, "
        f"{_n(schema)} tokens over the run |",
        "",
        f"The static prefix is byte-identical: {_n(optimized.prefix_tokens)} tokens in both",
        "arms, built by `RolePromptContextBuilder`, `load_role_prompt`, `SkillLoader` and",
        "`ToolRegistry`. The shipped arm runs the real `AgentRegistry`, `PlannerService`,",
        "`ArticleStore` and `MemoryStore` code paths; the sequence of calls is a fixed",
        "schedule, so both arms do identical work and every call has a counterpart.",
        "",
        "Counts use the repository's convention (`estimate_tokens`): `ceil(chars / 4)`,",
        "plus 4 tokens per message. The schema is serialised through the same convention.",
        "",
        "## Totals",
        "",
        *_totals_table(naive, optimized),
        "",
        f"All {calls} calls of both arms are in these sums.",
        "",
        "## Prompt anatomy",
        "",
        "Every prompt token falls into exactly one bucket, and the three buckets sum to the",
        "prompt total in both arms; the report generator asserts that before rendering a",
        "line. `Role blocks` is what a role assembled in `prepare()` - the section under",
        "edit, the source cards, the validator's gate pack - or, in the baseline, the",
        "static prefix it cannot cache. The schema sits beside the prompt, so it is the",
        "last column and not part of the total.",
        "",
        *_anatomy_table(naive, optimized),
        "",
        f"The baseline puts {_pct(naive.message_tokens, n_prompt)} of its prompt in the",
        "user message, because that message is the whole article plus the whole log every",
        f"time. The shipped pipeline puts {_pct(optimized.message_tokens, o_prompt)} there",
        f"and {_pct(optimized.block_tokens, o_prompt)} into bounded role blocks; the",
        f"remaining {_pct(optimized.prefix_tokens, o_prompt)} is the stable prefix, re-sent",
        f"on all {calls} calls.",
        "",
        "## Tokens by role",
        "",
        *_role_table(naive, optimized),
        "",
        "Three roles carry the win and two one-shot roles get worse; both halves are the",
        "story.",
        "",
        f"* The architect ({_n(_prompt(architect_o))}) and the researcher "
        f"({_n(_prompt(researcher_o))}) run before any article exists, so the baseline has",
        "nothing big to send, while the shipped arm sends the rubric's section list with",
        "word targets, the tool envelope and the source cards. Together they are "
        f"{_pct(one_shot, o_prompt)} of shipped prompt: a bound worth knowing, not worth",
        "optimising first.",
        f"* The reviewer drops from {_n(_prompt(reviewer_n))} to {_n(_prompt(reviewer_o))} "
        f"({_pct(_prompt(reviewer_n) - _prompt(reviewer_o), _prompt(reviewer_n))} less),",
        f"  but it pays {_n(reviewer_o['schema'])} of schema and still reads every dirty",
        f"  section in full: {_n(reviewer_blocks)} tokens of role blocks over three reviews.",
        "  Counting the schema it is the one regression, and it is the first fix below.",
        f"* The writer does the work that matters: {_n(_prompt(writer_n))} -> "
        f"{_n(_prompt(writer_o))} prompt "
        f"({_pct(_prompt(writer_n) - _prompt(writer_o), _prompt(writer_n))} less) and",
        f"  {_n(writer_n['out'])} -> {_n(writer_o['out'])} output "
        f"({_pct(writer_n['out'] - writer_o['out'], writer_n['out'])} less), because a",
        "  revision carries the section it edits and a patch carries a delta.",
        f"* The validator drops from {_n(_prompt(validator_n))} to {_n(_prompt(validator_o))} "
        f"({_pct(_prompt(validator_n) - _prompt(validator_o), _prompt(validator_n))} less):",
        "  the gate pack and the compact view replace a whole-article re-read.",
        "",
        "## Tokens by iteration",
        "",
        "Iteration 0 is the initial Planner call. Iteration 1 drafts every section; later",
        "iterations revise a subset, which is where the baseline's whole-article re-reads",
        "get expensive.",
        "",
        *_iteration_table(naive, optimized),
        "",
    ]

    iterations = sorted(set(naive.by_iteration()) | set(optimized.by_iteration()))
    if iterations:
        first = iterations[0]
        first_delta = _n(
            _prompt(_iteration_row(optimized, first)) - _prompt(_iteration_row(naive, first))
        )
        lines.extend(
            [
                f"Iteration {first} is the only one the shipped pipeline loses "
                f"({first_delta} tokens more): a single Planner call with no article to",
                "save on yet.",
                "From iteration 1 onwards the saved fraction grows with the article, which",
                "is the shape a context-optimised pipeline should have.",
                "",
            ]
        )

    lines.extend(
        [
            "## Planner input tokens per call",
            "",
            "The Planner never reads article prose: it reads the bounded `claude.md` digest",
            f"(2,000-token ceiling, observed maximum {digest_max:,}), a compact article view",
            "and the deterministic gate results. The baseline Planner reads the whole",
            "article and the whole change log on every call.",
            "",
            *_planner_table(naive, optimized),
            "",
        ]
    )
    if largest_planner is not None and naive.planner_calls:
        lines.extend(
            [
                f"The shipped Planner's largest prompt is {_n(largest_planner.prompt)} "
                f"tokens ({largest_planner.kind}, iteration "
                f"{largest_planner.iteration}); the baseline's is "
                f"{_n(max(call.prompt for call in naive.planner_calls))}. What remains in",
                f"the shipped one is a cached prefix of ~{_n(largest_planner.prefix)} and a",
                f"bounded digest of {_n(largest_planner.digest_tokens)}: flat across",
                "iterations, because the digest is capped, while the baseline's grows with",
                "the article. The digest is the reason the Planner stays cheap.",
                "",
            ]
        )

    lines.extend(
        [
            "## Where the shipped tokens sit",
            "",
            *_where_tokens_go(optimized),
            "",
            f"Per call: {n_prompt / max(1, calls):,.0f} prompt tokens in the baseline against "
            f"{o_prompt / max(1, calls):,.0f} shipped, plus {schema_per_call:,.0f} of schema.",
            "",
            "Two things that are easy to misread in a token benchmark:",
            "",
            "* **Caching does not reduce tokens, only the bill.** The shipped arm sends",
            f"  {_n(optimized.total('tokens_cached'))} cached and",
            f"  {_n(optimized.total('tokens_in'))} uncached tokens; together they are the",
            f"  {_n(o_prompt)} prompt total. Dropping `cache=True` would not change that sum",
            "  by one token, so the saving above is context saved, not discount claimed.",
            "* **Patches are the output-side win.** A baseline write re-emits the whole",
            f"  article: {_n(n_out)} output tokens against {_n(o_out)} shipped "
            f"({output_saved * 100:.1f}% fewer). Output is billed too, but the target is",
            "  about input, so it is reported rather than spent.",
            "",
            "## Where the remaining tokens go, and what to fix",
            "",
            "Five buckets, measured, largest first. None of the fixes changes a contract or",
            "a configured threshold; each is a prompt-construction change with its size",
            "known from this run.",
            "",
            f"### 1. The output schema: {_n(schema)} tokens, {_pct(schema, o_wire)} of wire input",
            "",
            "Every call carries `RoleAgent.call_schema()`: the full `AgentResult` contract",
            f"plus the role's own output model, re-sent {calls} times, {schema_per_call:,.0f}",
            f"tokens a call on average (largest: {role_schema}). A writer's schema still",
            "defines structures the writer cannot emit - `QualityReport`, `SourceCard` -",
            "because `forbidden_result_fields` strips properties from the envelope and",
            "leaves their `$defs` in place. It is the biggest block of input in the run and",
            "it is identical on every call of a role.",
            "",
            "* **Send it once, in the role's cached prefix.** A role's schema is",
            "  byte-identical every time; as a `cache=True` block it is billed at the cached",
            "  rate instead of the full rate. No token is removed, most of the cost is, and",
            "  the governor's `call_input_cap` check would then see what the provider sees.",
            "* **Prune `$defs` to what the stripped schema can reach**, and tighten the field",
            "  descriptions written for humans. A writer cannot emit a `QualityReport`, so",
            "  its definition is dead weight on every writer call. Measured on this run's",
            f"  schemas, dropping unreachable `$defs` takes them from {_n(schema)} to",
            f"  {_n(optimized.pruned_schema_tokens)} tokens: {_n(schema_cut)} fewer on the",
            f"  wire, i.e. {_saved(n_wire, o_wire - schema_cut) * 100:.1f}% instead of",
            f"  {wire_saved * 100:.1f}% on the strict measure. It is schema presentation -",
            "  the contracts do not change.",
            "* **Make the schema per role, not per contract.** The validator needs `report`,",
            "  the writer needs `patches`, the researcher needs `source_cards`; none needs",
            "  all three.",
            "",
            f"### 2. The reviewer re-reads every dirty section: {_n(reviewer_blocks)} tokens",
            "",
            "`ReviewerAgent.prepare()` sends the full text of every section with a version on",
            "every review. By iteration 3 the article is complete, so the reviewer reads all",
            "of it, three times. It only needs the sections it has not seen since its last",
            "review, which the store can answer from the per-section content hash it already",
            f"keeps: roughly two thirds of {_n(reviewer_blocks)} tokens.",
            "",
            f"### 3. The writer's section block: {_n(writer_blocks)} tokens",
            "",
            "A revision sends the whole section under edit so the model can anchor a",
            "`replace_text` op, although only a sentence of it is being edited. A",
            "paragraph-scoped block, or just the anchors the patch already names, would cut",
            "this the way the patch cuts the output side - the same references-not-content",
            "rule the rest of the pipeline follows.",
            "",
            f"### 4. The static prefix: {_n(optimized.prefix_tokens)} tokens, "
            f"{prefix_share * 100:.0f}% of the prompt",
            "",
            "Cached, so cheap in money, but still context: rules, role prompt, tool schemas",
            "and the whole skill index travel with every call, including calls that use none",
            "of them. Sending skill names and loading a body on demand (the loader already",
            "supports it) would shrink this for every role.",
            "",
            f"### 5. The Planner's evaluation re-sends its digest: {_n(planner_message)} tokens",
            "",
            "The digest is capped, but it is sent on every evaluation and changes little",
            "between them. A delta since the previous evaluation keeps the Planner prompt",
            "proportional to the iteration's work rather than to the run's history.",
            "",
            "### Putting the fixes together",
            "",
            "Arithmetic on the same measured run, not a new measurement: pruning schemas "
            f"({_n(schema_cut)}), reviewer deltas ({_n(reviewer_cut)}), a paragraph-scoped "
            f"writer block ({_n(writer_cut)})",
            f"would move wire input from {_n(o_wire)} to about {_n(projected)}:",
            f"{projected_saved * 100:.1f}% against the baseline. That is short of the target,",
            f"and the honest conclusion is that the {_n(schema)} of contract schema is the",
            "difference: the pipeline's context optimisations save what the schemas add.",
            "Removing that cost entirely means changing what each role must return - a",
            "contract change (see the handoff note in the commit), not a prompt tweak. The",
            f"target as written is met on the prompt-token measure ({saved * 100:.1f}%) and",
            f"not on the wire measure ({wire_saved * 100:.1f}%).",
            "",
            "## Sensitivity",
            "",
            "The same two runs, measured five ways. No row is a different run.",
            "",
            *_sensitivity(naive, optimized),
            "",
            f"Five numbers from one experiment, between {min(measured) * 100:.1f}% and "
            f"{max(measured) * 100:.1f}%. They are all printed rather than the best one.",
            "The first row is the repository's measure and the target is judged on it; the",
            "second is the strictest reading of the same run, and the one to watch if the",
            "contract schemas are billed for what they are.",
            "",
            "## Every call",
            "",
            "Both arms, paired by (iteration, role, call). Early drafts favour the baseline -",
            "the article is still shorter than the shipped pipeline's fixed per-call",
            "context - and the crossover is visible as it grows.",
            "",
            *_call_table(naive, optimized),
            "",
            "## Determinism",
            "",
        ]
    )
    lines.append(
        "* Both arms were run twice and produced identical per-call totals; the script"
        " exits non-zero on any drift."
        if deterministic
        else "* Not checked in this run (`--no-determinism-check`)."
    )
    lines.extend(
        [
            "* All synthetic material - article prose, source cards, change log - comes from",
            "  `SEED` through `random.Random`, and every repeated choice is indexed rather",
            "  than hashed: `hash()` of a string is salted per process and would make totals",
            "  drift between runs.",
            "",
            "## How to reproduce",
            "",
            "```bash",
            "python benchmarks/token_benchmark.py --stdout   # print, write nothing",
            "python benchmarks/token_benchmark.py            # rewrite this file",
            "```",
            "",
            "## Scope and limits",
            "",
            "* **Synthetic inputs.** The article, the source cards and the change log are",
            "  fixtures generated from `SEED`; the cards say so in their venue and snippet",
            "  fields. No real source, datum or quote is claimed (HARD RULE 1). The",
            "  comparison is about token accounting, not text quality.",
            "* **Approximate tokens.** `ceil(chars / 4)` is the convention the governor",
            "  budgets against. A vendor tokeniser moves the absolute numbers, not the ratio.",
            "* **A fixed schedule stands in for the loop controller.** Both arms execute the",
            "  same calls so every call can be paired; stop conditions, replanning and",
            "  parallel scheduling are covered by the test suite, not here.",
            "* **Answers are scripted.** The same corpus produces both arms' answers, so the",
            "  benchmark isolates context construction, patch granularity, caching and schema",
            "  cost - not model quality.",
            "* **The baseline sends a schema too.** It asks for JSON in the plain shape a",
            "  naive loop returns, so structured output is charged to both arms; the size",
            "  difference is a design difference and is reported, not hidden.",
            "* **No live call, no secret.** `MockProvider`, one seed, no key, no network. The",
            "  only file the script writes is this report.",
            "",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- entry point
async def _run_once() -> tuple[Trace, Trace]:
    """Run both arms once and return their traces."""
    rubric = load_rubric(REPO_ROOT / "config" / "rubric.yaml")
    policies = load_policies(REPO_ROOT / "config" / "policies.yaml")
    optimized = OptimizedRun(
        rubric=rubric,
        policies=policies,
        skills_dir=REPO_ROOT / "skills",
        rules_dir=REPO_ROOT / "rules",
    )
    naive = NaiveRun(
        rubric=rubric,
        policies=policies,
        skills_dir=REPO_ROOT / "skills",
        rules_dir=REPO_ROOT / "rules",
    )
    optimized_trace = await optimized.run()
    naive_trace = await naive.run()
    return naive_trace, optimized_trace


def _signature(trace: Trace) -> tuple[tuple[int, int, int, int], ...]:
    """A per-call token fingerprint, used for the determinism check."""
    return tuple(
        (call.prompt, call.schema, call.tokens_out, call.tokens_cached) for call in trace.all_calls
    )


async def run_benchmark(*, check_determinism: bool = True) -> str:
    """Run the benchmark and return the rendered report."""
    naive, optimized = await _run_once()
    deterministic: bool | None = None
    if check_determinism:
        naive_again, optimized_again = await _run_once()
        deterministic = _signature(naive) == _signature(naive_again) and _signature(
            optimized
        ) == _signature(optimized_again)
        if not deterministic:
            raise RuntimeError(
                "benchmark is not deterministic: a second run produced different token "
                "totals (rerun with --no-determinism-check to see the drift)"
            )
    return render_report(naive, optimized, deterministic=deterministic)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: render the report, optionally write it to ``docs/benchmarks/``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"where to write the report (default: {DEFAULT_REPORT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument("--stdout", action="store_true", help="print only; write no file")
    parser.add_argument(
        "--no-determinism-check",
        action="store_true",
        help="skip the second run that proves the totals are reproducible",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(run_benchmark(check_determinism=not args.no_determinism_check))
    print(report)
    if not args.stdout:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"\n[written] {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
