"""Bounded ``claude.md`` digest builder and unified run memory facade.

ADR-0002 & HARD RULE 3:

* the Planner reads ``claude.md`` (bounded to ``policies.digest_max_tokens``,
  default 2,000 tokens) — never the full ledger and never article prose;
* the last ``policies.recent_window`` (default 3) ledger entries are kept
  verbatim with their 60-word summaries, ids, targets, patch pointers and
  issue transitions;
* when the digest reaches ``policies.compact_at_tokens`` (``compact_trigger`` =
  80% of ``digest_max_tokens``, default 1,600 tokens) — or when ``force_compact``
  is requested at a checkpoint — entries older than ``recent_window`` are
  compacted into a :class:`~app.memory.compactor.CompactedBatch`;
* the builder guarantees ``estimate_tokens(snapshot.markdown) <=
  policies.digest_max_tokens`` by progressively tightening older history and
  auxiliary reference tables before touching the verbatim recent window.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from app.contracts.agent import AgentResult
from app.contracts.brief import Brief
from app.contracts.common import Contract
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.plan import Plan
from app.contracts.quality import QualityReport
from app.contracts.section import SectionState
from app.contracts.source import SourceCard
from app.core.policies import DEFAULT_POLICIES, Policies
from app.memory.compactor import CompactedBatch, DigestCompactor
from app.memory.errors import DigestBudgetExceeded, DigestError
from app.memory.ledger import InMemoryLedger, IssueSummary, LedgerStore
from app.memory.sources import InMemorySourceStore, SourceStore
from app.providers.base import LLMProvider, estimate_tokens

__all__ = [
    "DEFAULT_DIGEST_FILENAME",
    "DigestBuilder",
    "DigestSnapshot",
    "MemoryStore",
    "RunMemory",
]

DEFAULT_DIGEST_FILENAME: Final[str] = "claude.md"


class DigestSnapshot(Contract):
    """Immutable snapshot of a rendered ``claude.md`` digest and its budget metrics."""

    markdown: str = Field(min_length=1, description="Rendered claude.md markdown text.")
    tokens: int = Field(ge=1, description="Estimated token count via ceil(chars / 4).")
    max_tokens: int = Field(ge=1, description="Policy ceiling (digest_max_tokens).")
    compact_at_tokens: int = Field(ge=1, description="Policy compaction trigger threshold.")
    compacted: bool = Field(
        default=False,
        description="True when older entries were compacted for this snapshot.",
    )
    recent_entry_ids: list[str] = Field(default_factory=list)
    compacted_entry_ids: list[str] = Field(default_factory=list)
    compacted_batch: CompactedBatch | None = None
    open_issues: list[str] = Field(default_factory=list)
    latest_q: int | None = Field(default=None, ge=0, le=100)

    @property
    def within_budget(self) -> bool:
        """True when ``tokens <= max_tokens``."""
        return self.tokens <= self.max_tokens

    @property
    def utilization(self) -> float:
        """Fraction of ``max_tokens`` consumed by ``markdown``."""
        return self.tokens / self.max_tokens


class DigestBuilder:
    """Renders and bounds the ``claude.md`` digest from run references.

    Section states, source cards, plan distribution, quality reports and ledger
    entries are formatted strictly from contract metadata — section body text never
    enters the digest (HARD RULE 3).
    """

    def __init__(
        self,
        policies: Policies = DEFAULT_POLICIES,
        *,
        compactor: DigestCompactor | None = None,
    ) -> None:
        self._policies = policies
        self._compactor = compactor or DigestCompactor()

    @property
    def policies(self) -> Policies:
        """Policies governing ``digest_max_tokens``, ``recent_window`` and ``compact_trigger``."""
        return self._policies

    @property
    def compactor(self) -> DigestCompactor:
        """The :class:`DigestCompactor` used when compaction triggers."""
        return self._compactor

    def should_compact(self, markdown_or_tokens: str | int) -> bool:
        """True when the digest token count has reached ``policies.compact_at_tokens``."""
        tokens = (
            markdown_or_tokens
            if isinstance(markdown_or_tokens, int)
            else estimate_tokens(markdown_or_tokens)
        )
        return tokens >= self._policies.compact_at_tokens

    def build(
        self,
        ledger: LedgerStore,
        *,
        brief: Brief | None = None,
        plan: Plan | None = None,
        sections: Sequence[SectionState] | None = None,
        sources: SourceStore | Sequence[SourceCard] | None = None,
        reports: Sequence[QualityReport] | None = None,
        prior_compacted: CompactedBatch | None = None,
        force_compact: bool = False,
    ) -> DigestSnapshot:
        """Synchronously build a bounded :class:`DigestSnapshot`."""
        older, recent = ledger.window(self._policies.recent_window)
        issue_summary = ledger.issue_summary()
        total_tokens = ledger.total_tokens()

        # First render without compacting older entries (unless already compacted or forced)
        compacted_batch: CompactedBatch | None = None
        compacted = False

        if older and (force_compact or prior_compacted is not None):
            prior_text = prior_compacted.summary if prior_compacted is not None else None
            compacted_batch = self._compactor.compact_sync(older, prior_summary=prior_text)
            compacted = True
        else:
            uncompacted_md = self._render_document(
                ledger_count=len(ledger.entries()),
                total_tokens=total_tokens,
                issue_summary=issue_summary,
                brief=brief,
                plan=plan,
                completed_tasks=ledger.completed_task_ids(),
                sections=sections or (),
                sources=sources,
                reports=reports or (),
                older_entries=older,
                compacted_batch=None,
                recent_entries=recent,
                detail_level=3,
            )
            if older and self.should_compact(uncompacted_md):
                compacted_batch = self._compactor.compact_sync(older)
                compacted = True

        return self._finalize_snapshot(
            ledger_count=len(ledger.entries()),
            total_tokens=total_tokens,
            issue_summary=issue_summary,
            brief=brief,
            plan=plan,
            completed_tasks=ledger.completed_task_ids(),
            sections=sections or (),
            sources=sources,
            reports=reports or (),
            older_entries=older,
            compacted_batch=compacted_batch,
            compacted=compacted,
            recent_entries=recent,
        )

    async def abuild(
        self,
        ledger: LedgerStore,
        *,
        provider: LLMProvider | None = None,
        model: str | None = None,
        account_id: str | None = None,
        brief: Brief | None = None,
        plan: Plan | None = None,
        sections: Sequence[SectionState] | None = None,
        sources: SourceStore | Sequence[SourceCard] | None = None,
        reports: Sequence[QualityReport] | None = None,
        prior_compacted: CompactedBatch | None = None,
        force_compact: bool = False,
    ) -> DigestSnapshot:
        """Asynchronously build a bounded :class:`DigestSnapshot` (uses LLM compactor if needed)."""
        older, recent = ledger.window(self._policies.recent_window)
        issue_summary = ledger.issue_summary()
        total_tokens = ledger.total_tokens()

        need_compaction = bool(older) and (force_compact or prior_compacted is not None)
        if older and not need_compaction:
            uncompacted_md = self._render_document(
                ledger_count=len(ledger.entries()),
                total_tokens=total_tokens,
                issue_summary=issue_summary,
                brief=brief,
                plan=plan,
                completed_tasks=ledger.completed_task_ids(),
                sections=sections or (),
                sources=sources,
                reports=reports or (),
                older_entries=older,
                compacted_batch=None,
                recent_entries=recent,
                detail_level=3,
            )
            need_compaction = self.should_compact(uncompacted_md)

        compacted_batch: CompactedBatch | None = None
        compacted = False
        if need_compaction and older:
            prior_text = prior_compacted.summary if prior_compacted is not None else None
            compacted_batch = await self._compactor.compact(
                older,
                provider=provider,
                model=model,
                account_id=account_id,
                prior_summary=prior_text,
            )
            compacted = True

        return self._finalize_snapshot(
            ledger_count=len(ledger.entries()),
            total_tokens=total_tokens,
            issue_summary=issue_summary,
            brief=brief,
            plan=plan,
            completed_tasks=ledger.completed_task_ids(),
            sections=sections or (),
            sources=sources,
            reports=reports or (),
            older_entries=older,
            compacted_batch=compacted_batch,
            compacted=compacted,
            recent_entries=recent,
        )

    def write(self, path: Path, snapshot: DigestSnapshot) -> Path:
        """Write ``snapshot.markdown`` to ``path`` (typically ``claude.md``)."""
        if not snapshot.within_budget:
            raise DigestBudgetExceeded(
                f"refusing to write oversized digest ({snapshot.tokens} > {snapshot.max_tokens})",
                tokens=snapshot.tokens,
                max_tokens=snapshot.max_tokens,
            )
        target = path / DEFAULT_DIGEST_FILENAME if path.is_dir() else path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(snapshot.markdown, encoding="utf-8")
        except OSError as exc:
            raise DigestError(
                f"failed to write digest to {target}: {exc}",
                path=str(target),
            ) from exc
        return target

    # -- rendering & budget fitting ----------------------------------------
    def _finalize_snapshot(
        self,
        *,
        ledger_count: int,
        total_tokens: TokenUsage,
        issue_summary: IssueSummary,
        brief: Brief | None,
        plan: Plan | None,
        completed_tasks: set[str],
        sections: Sequence[SectionState],
        sources: SourceStore | Sequence[SourceCard] | None,
        reports: Sequence[QualityReport],
        older_entries: list[ChangeEntry],
        compacted_batch: CompactedBatch | None,
        compacted: bool,
        recent_entries: list[ChangeEntry],
    ) -> DigestSnapshot:
        max_tokens = self._policies.digest_max_tokens
        compact_at = self._policies.compact_at_tokens

        # Try progressively more compact detail levels (3 -> 2 -> 1 -> 0) until the
        # rendered markdown fits inside digest_max_tokens.
        rendered = ""
        for level in (3, 2, 1, 0):
            if older_entries and compacted_batch is None and level < 3:
                compacted_batch = self._compactor.compact_sync(older_entries)
                compacted = True

            rendered = self._render_document(
                ledger_count=ledger_count,
                total_tokens=total_tokens,
                issue_summary=issue_summary,
                brief=brief,
                plan=plan,
                completed_tasks=completed_tasks,
                sections=sections,
                sources=sources,
                reports=reports,
                older_entries=older_entries,
                compacted_batch=compacted_batch,
                recent_entries=recent_entries,
                detail_level=level,
            )
            if estimate_tokens(rendered) <= max_tokens:
                break

        if estimate_tokens(rendered) > max_tokens:
            rendered = self._emergency_trim(
                recent_entries=recent_entries,
                compacted_batch=compacted_batch,
                max_tokens=max_tokens,
            )

        tokens = estimate_tokens(rendered)
        if tokens > max_tokens:
            raise DigestBudgetExceeded(
                f"digest exceeds digest_max_tokens ({tokens} > {max_tokens})",
                tokens=tokens,
                max_tokens=max_tokens,
            )

        for entry in recent_entries:
            if entry.id not in rendered or entry.summary not in rendered:
                raise DigestBudgetExceeded(
                    f"digest_max_tokens ({max_tokens}) cannot preserve verbatim "
                    f"recent entry {entry.id!r} and its summary",
                    tokens=tokens,
                    max_tokens=max_tokens,
                    entry_id=entry.id,
                )

        latest_q = reports[-1].Q if reports else None
        open_issues = self._combined_open_issues(issue_summary, reports)

        return DigestSnapshot(
            markdown=rendered,
            tokens=tokens,
            max_tokens=max_tokens,
            compact_at_tokens=compact_at,
            compacted=compacted,
            recent_entry_ids=[entry.id for entry in recent_entries],
            compacted_entry_ids=(
                list(compacted_batch.entry_ids) if compacted and compacted_batch is not None else []
            ),
            compacted_batch=compacted_batch,
            open_issues=open_issues,
            latest_q=latest_q,
        )

    def _render_document(
        self,
        *,
        ledger_count: int,
        total_tokens: TokenUsage,
        issue_summary: IssueSummary,
        brief: Brief | None,
        plan: Plan | None,
        completed_tasks: set[str],
        sections: Sequence[SectionState],
        sources: SourceStore | Sequence[SourceCard] | None,
        reports: Sequence[QualityReport],
        older_entries: Sequence[ChangeEntry],
        compacted_batch: CompactedBatch | None,
        recent_entries: Sequence[ChangeEntry],
        detail_level: int,
    ) -> str:
        blocks: list[str] = []
        latest_iter = max(
            [0]
            + [entry.iter for entry in recent_entries]
            + [entry.iter for entry in older_entries]
            + [report.iter for report in reports]
        )

        # 1. Header
        blocks.append(
            "# ScholarLoop Digest (claude.md)\n"
            f"iter: {latest_iter} | entries: {ledger_count} | "
            f"run_tokens: {total_tokens.total}/{self._policies.run_token_cap} "
            f"(in={total_tokens.in_}, out={total_tokens.out}, cached={total_tokens.cached}) | "
            f"digest_cap: {self._policies.digest_max_tokens}"
        )

        # 2. Brief
        if brief is not None and detail_level >= 1:
            brief_lines = [
                "## Brief",
                f"- topic: {brief.topic}",
                (
                    f"- type: {brief.article_type.value} | style: {brief.citation_style.value} | "
                    f"lang: {brief.language} | target_words: {brief.target_words} "
                    f"({brief.word_tolerance_low}..{brief.word_tolerance_high})"
                ),
            ]
            if detail_level >= 2 and brief.must_include:
                brief_lines.append("- must_include: " + "; ".join(brief.must_include[:6]))
            if detail_level >= 2 and brief.must_avoid:
                brief_lines.append("- must_avoid: " + "; ".join(brief.must_avoid[:6]))
            blocks.append("\n".join(brief_lines))

        # 3. Plan summary (role distribution, never full task instructions)
        if plan is not None and detail_level >= 1:
            dist = plan.role_distribution()
            dist_str = ", ".join(f"{role.value}:{count}" for role, count in dist.items() if count)
            ready = plan.ready_tasks(completed_tasks)
            plan_lines = [
                "## Plan",
                (
                    f"- v{plan.version} | tasks: {len(completed_tasks)}/{len(plan.tasks)} done, "
                    f"{len(ready)} ready | roles: {dist_str}"
                ),
            ]
            if detail_level >= 2:
                plan_lines.append(f"- goal: {plan.goal}")
            if detail_level >= 3 and ready:
                ready_refs = ", ".join(f"{t.id}({t.role.value})" for t in ready[:6])
                plan_lines.append(f"- ready_ids: {ready_refs}")
            blocks.append("\n".join(plan_lines))

        # 4. Section states (references only, never prose)
        if sections and detail_level >= 1:
            ordered = sorted(sections, key=lambda item: item.order)
            sec_lines = ["## Sections (state references only)"]
            limit = len(ordered) if detail_level >= 2 else min(8, len(ordered))
            for sec in ordered[:limit]:
                short_hash = sec.hash[:19]
                unresolved = len(sec.unresolved_citations)
                line = (
                    f"- {sec.id} | v{sec.version} | {sec.words}w | {sec.status.value} | "
                    f"{short_hash} | cites:{len(sec.citations)}(unres:{unresolved})"
                )
                if sec.open_issues:
                    line += f" | issues:{','.join(sec.open_issues[:4])}"
                sec_lines.append(line)
            blocks.append("\n".join(sec_lines))

        # 5. Source cards summary
        source_cards = self._extract_sources(sources)
        if source_cards and detail_level >= 1:
            verified = sum(1 for c in source_cards if c.verification.value == "verified")
            unknown = sum(1 for c in source_cards if c.verification.value == "unknown")
            mismatch = sum(1 for c in source_cards if c.verification.value == "mismatch")
            ratio = verified / len(source_cards)
            src_lines = [
                "## Sources",
                (
                    f"- total: {len(source_cards)} (verified={verified}, unknown={unknown}, "
                    f"mismatch={mismatch}, ratio={ratio:.2f})"
                ),
            ]
            if detail_level >= 2:
                cap = 10 if detail_level >= 3 else 5
                for card in source_cards[:cap]:
                    locator = card.doi or card.url or "no-locator"
                    src_lines.append(
                        f"- {card.id} | {card.year} | {locator} | {card.verification.value}"
                    )
            blocks.append("\n".join(src_lines))

        # 6. Quality & issues
        open_issues = self._combined_open_issues(issue_summary, reports)
        if reports or open_issues or issue_summary.total_opened > 0:
            qual_lines = ["## Quality & Issues"]
            if reports:
                latest = reports[-1]
                q_hist = " -> ".join(f"i{r.iter}:{r.Q}" for r in reports[-6:])
                failed = ", ".join(latest.failed_gates) or "none"
                qual_lines.append(
                    f"- Q: {latest.Q} (q_min={self._policies.q_min}) | history: {q_hist} | "
                    f"failed_gates: {failed}"
                )
            qual_lines.append(
                f"- issues: open={len(open_issues)} "
                f"(opened={issue_summary.total_opened}, closed={issue_summary.total_closed}, "
                f"churn={issue_summary.churn_ratio:.2f})"
            )
            if open_issues and detail_level >= 1:
                cap = 10 if detail_level >= 2 else 5
                qual_lines.append("- open_issue_ids: " + ", ".join(open_issues[:cap]))
            blocks.append("\n".join(qual_lines))

        # 7. Compacted / Earlier history
        if compacted_batch is not None:
            comp_budget: int | None = None
            if detail_level == 2:
                comp_budget = max(80, self._policies.digest_max_tokens // 4)
            elif detail_level <= 1:
                comp_budget = max(48, self._policies.digest_max_tokens // 6)
            blocks.append("## Compacted History\n" + compacted_batch.render(max_tokens=comp_budget))
        elif older_entries:
            older_lines = ["## Earlier Entries"]
            for entry in older_entries:
                older_lines.append(self._format_entry_line(entry, verbatim=False))
            blocks.append("\n".join(older_lines))

        # 8. Recent window (kept verbatim per ADR-0002)
        recent_lines = [f"## Recent Entries (last {self._policies.recent_window} verbatim)"]
        if not recent_entries:
            recent_lines.append("- (no ledger entries yet)")
        else:
            for entry in recent_entries:
                recent_lines.append(self._format_entry_line(entry, verbatim=True))
        blocks.append("\n".join(recent_lines))

        return "\n\n".join(blocks)

    @staticmethod
    def _format_entry_line(entry: ChangeEntry, *, verbatim: bool) -> str:
        targets = ",".join(entry.targets) or "none"
        meta = (
            f"- {entry.id} | i{entry.iter} | task={entry.task_id} | "
            f"role={entry.role_name} | status={entry.status.value} | "
            f"targets={targets} | tokens={entry.tokens.total}"
        )
        if entry.patch_ref:
            meta += f" | {entry.patch_ref}"
        if entry.issues_opened:
            meta += f" | +issues:{','.join(entry.issues_opened)}"
        if entry.issues_closed:
            meta += f" | -issues:{','.join(entry.issues_closed)}"
        if verbatim:
            return f"{meta}\n  summary: {entry.summary}"
        return f"{meta} | {entry.summary}"

    @staticmethod
    def _extract_sources(
        sources: SourceStore | Sequence[SourceCard] | None,
    ) -> list[SourceCard]:
        if sources is None:
            return []
        if isinstance(sources, SourceStore):
            return sources.list_sources()
        return list(sources)

    @staticmethod
    def _combined_open_issues(
        issue_summary: IssueSummary,
        reports: Sequence[QualityReport],
    ) -> list[str]:
        combined = list(issue_summary.open_issues)
        closed = set(issue_summary.closed_issues)
        if reports:
            for issue in reports[-1].issues:
                if issue.id not in combined and issue.id not in closed:
                    combined.append(issue.id)
        return combined

    def _emergency_trim(
        self,
        *,
        recent_entries: Sequence[ChangeEntry],
        compacted_batch: CompactedBatch | None,
        max_tokens: int,
    ) -> str:
        """Last-resort budget fitting that never truncates recent entries or summaries.

        Raises:
            DigestBudgetExceeded: the configured ``max_tokens`` cannot hold the
                required verbatim recent entries (and compacted history when present).
        """
        header = "# ScholarLoop Digest (claude.md)"
        recent_lines = [f"## Recent Entries (last {self._policies.recent_window} verbatim)"]
        if not recent_entries:
            recent_lines.append("- (no ledger entries yet)")
        else:
            for entry in recent_entries:
                recent_lines.append(self._format_entry_line(entry, verbatim=True))
        recent_block = "\n".join(recent_lines)

        if compacted_batch is not None:
            for comp_budget in (
                max(24, max_tokens // 3),
                max(20, max_tokens // 4),
                32,
                20,
            ):
                compacted_section = "## Compacted History\n" + compacted_batch.render(
                    max_tokens=comp_budget
                )
                candidate = "\n\n".join([header, compacted_section, recent_block])
                if estimate_tokens(candidate) <= max_tokens:
                    return candidate
        else:
            candidate = "\n\n".join([header, recent_block])
            if estimate_tokens(candidate) <= max_tokens:
                return candidate

        required_tokens = estimate_tokens(candidate)
        raise DigestBudgetExceeded(
            f"digest_max_tokens ({max_tokens}) cannot preserve the "
            f"{len(recent_entries)} required verbatim recent ledger entries "
            f"without truncation (requires at least {required_tokens} tokens)",
            tokens=required_tokens,
            max_tokens=max_tokens,
            recent_entries=len(recent_entries),
        )


class MemoryStore:
    """Unified run-level memory facade combining ledger, sources, reports and digest.

    Provides a single coordinator that the orchestrator can feed :class:`AgentResult`
    envelopes into after each task execution.
    """

    def __init__(
        self,
        policies: Policies = DEFAULT_POLICIES,
        *,
        brief: Brief | None = None,
        plan: Plan | None = None,
        ledger: InMemoryLedger | None = None,
        sources: InMemorySourceStore | None = None,
        compactor: DigestCompactor | None = None,
        digest_path: Path | None = None,
    ) -> None:
        self._policies = policies
        self._brief = brief
        self._plan = plan
        self._ledger = ledger if ledger is not None else InMemoryLedger()
        self._sources = sources if sources is not None else InMemorySourceStore()
        self._compactor = compactor if compactor is not None else DigestCompactor()
        self._builder = DigestBuilder(policies, compactor=self._compactor)
        self._sections: dict[str, SectionState] = {}
        self._reports: list[QualityReport] = []
        self._last_snapshot: DigestSnapshot | None = None
        self._digest_path = digest_path

    @property
    def policies(self) -> Policies:
        """Active run policies."""
        return self._policies

    @property
    def ledger(self) -> InMemoryLedger:
        """The append-only :class:`InMemoryLedger`."""
        return self._ledger

    @property
    def sources(self) -> InMemorySourceStore:
        """The :class:`InMemorySourceStore`."""
        return self._sources

    @property
    def compactor(self) -> DigestCompactor:
        """The :class:`DigestCompactor`."""
        return self._compactor

    @property
    def builder(self) -> DigestBuilder:
        """The :class:`DigestBuilder`."""
        return self._builder

    @property
    def brief(self) -> Brief | None:
        """The run's :class:`Brief`, if set."""
        return self._brief

    def set_brief(self, brief: Brief) -> None:
        """Bind or update the run's :class:`Brief`."""
        self._brief = brief.model_copy(deep=True)

    @property
    def plan(self) -> Plan | None:
        """The latest :class:`Plan`, if set."""
        return self._plan

    def set_plan(self, plan: Plan) -> None:
        """Bind or update the run's :class:`Plan`."""
        self._plan = plan.model_copy(deep=True)

    @property
    def reports(self) -> list[QualityReport]:
        """Recorded validator :class:`QualityReport` objects in chronological order."""
        return [report.model_copy(deep=True) for report in self._reports]

    @property
    def sections(self) -> list[SectionState]:
        """Known :class:`SectionState` references ordered by ``order``."""
        return [
            sec.model_copy(deep=True)
            for sec in sorted(self._sections.values(), key=lambda item: item.order)
        ]

    @property
    def last_snapshot(self) -> DigestSnapshot | None:
        """Most recently built :class:`DigestSnapshot`, or ``None``."""
        return self._last_snapshot

    def update_sections(self, sections: Iterable[SectionState]) -> None:
        """Register or update section state references."""
        for section in sections:
            self._sections[section.id] = section.model_copy(deep=True)

    def record_report(self, report: QualityReport) -> None:
        """Record a validator :class:`QualityReport`."""
        self._reports.append(report.model_copy(deep=True))

    def record_agent_result(self, result: AgentResult) -> ChangeEntry:
        """Ingest one :class:`AgentResult` into ledger, source store, sections and reports."""
        entry = self._ledger.append(result.entry)
        if result.source_cards:
            self._sources.ingest_many(result.source_cards, role=result.entry.agent_role)
        if result.sections:
            self.update_sections(result.sections)
        if result.report is not None:
            self.record_report(result.report)
        return entry

    apply_result = record_agent_result

    def build_digest(self, *, force_compact: bool = False) -> DigestSnapshot:
        """Synchronously build (and optionally persist) the ``claude.md`` digest."""
        prior = self._last_snapshot.compacted_batch if self._last_snapshot is not None else None
        snapshot = self._builder.build(
            self._ledger,
            brief=self._brief,
            plan=self._plan,
            sections=self.sections,
            sources=self._sources,
            reports=self._reports,
            prior_compacted=prior,
            force_compact=force_compact,
        )
        self._last_snapshot = snapshot
        if self._digest_path is not None:
            self._builder.write(self._digest_path, snapshot)
        return snapshot

    async def abuild_digest(
        self,
        *,
        provider: LLMProvider | None = None,
        model: str | None = None,
        account_id: str | None = None,
        force_compact: bool = False,
    ) -> DigestSnapshot:
        """Asynchronously build (and optionally persist) the ``claude.md`` digest."""
        prior = self._last_snapshot.compacted_batch if self._last_snapshot is not None else None
        snapshot = await self._builder.abuild(
            self._ledger,
            provider=provider,
            model=model,
            account_id=account_id,
            brief=self._brief,
            plan=self._plan,
            sections=self.sections,
            sources=self._sources,
            reports=self._reports,
            prior_compacted=prior,
            force_compact=force_compact,
        )
        self._last_snapshot = snapshot
        if self._digest_path is not None:
            self._builder.write(self._digest_path, snapshot)
        return snapshot


#: Alias for callers that refer to the unified memory coordinator as RunMemory.
RunMemory = MemoryStore
