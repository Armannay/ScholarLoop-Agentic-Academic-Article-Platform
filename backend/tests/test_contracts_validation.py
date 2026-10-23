"""Validation rules that encode the HARD RULES and the loop's structural assumptions.

If one of these tests starts failing, a contract got weaker - and a weaker contract is
how the orchestrator ends up trusting data it should have rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.account import Account, MonthlyBudget
from app.contracts.agent import AgentResult
from app.contracts.binding import AgentBinding
from app.contracts.brief import Brief
from app.contracts.ledger import ChangeEntry, TokenUsage
from app.contracts.llm import LLMRequest
from app.contracts.loop import LoopDecision
from app.contracts.patch import Patch, PatchOp
from app.contracts.plan import Plan
from app.contracts.quality import GATE_NAMES, QualityReport
from app.contracts.section import EMPTY_SECTION_HASH, SectionState
from app.contracts.source import SourceCard, normalise_doi
from app.contracts.verdict import Verdict
from tests.factories import SAMPLE_BRIEF, sample_payloads, sample_ulid

pytestmark = pytest.mark.contracts

PAYLOADS = sample_payloads()
WORDS_61 = " ".join(f"w{index}" for index in range(61))


# --------------------------------------------------------------------------- Brief
def test_brief_rejects_contradictory_constraints() -> None:
    """A brief that must include and must avoid the same thing is unsatisfiable."""
    with pytest.raises(ValidationError, match="unsatisfiable"):
        Brief.model_validate(
            {
                **SAMPLE_BRIEF,
                "must_include": ["a limitations section"],
                "must_avoid": ["A Limitations Section"],
            }
        )


@pytest.mark.parametrize("target_words", [0, 299, 30_001])
def test_brief_word_target_is_range_checked(target_words: int) -> None:
    """The total-words gate is meaningless without a sane target."""
    with pytest.raises(ValidationError):
        Brief.model_validate({**SAMPLE_BRIEF, "target_words": target_words})


def test_brief_tolerance_is_plus_minus_15_percent() -> None:
    """The gate bounds are derived from the brief, not hard-coded twice."""
    brief = Brief.model_validate(SAMPLE_BRIEF)
    assert (brief.word_tolerance_low, brief.word_tolerance_high) == (3400, 4600)


def test_brief_provided_data_may_be_empty_but_never_invented() -> None:
    """Empty provided_data is legal: the researcher must find real sources instead."""
    brief = Brief.model_validate({**SAMPLE_BRIEF, "provided_data": []})
    assert brief.provided_data == []


# ---------------------------------------------------------------------------- Task
def test_task_requires_exactly_one_role() -> None:
    """Mandatory distribution: no role and two roles are both invalid."""
    payload = PAYLOADS["Task"]
    with pytest.raises(ValidationError):
        del_role = {key: value for key, value in payload.items() if key != "role"}
        from app.contracts.task import Task

        Task.model_validate(del_role)

    from app.contracts.task import Task

    with pytest.raises(ValidationError):
        Task.model_validate({**payload, "role": ["writer", "reviewer"]})


def test_task_token_budget_must_be_positive() -> None:
    """A task with no budget could never be executed within the caps."""
    from app.contracts.task import Task

    with pytest.raises(ValidationError):
        Task.model_validate({**PAYLOADS["Task"], "token_budget": 0})


# ---------------------------------------------------------------------------- Plan
def _tasks(*ids: str) -> list[dict[str, object]]:
    """Build minimal task payloads with the given ids (no dependencies)."""
    return [
        {
            "schema_version": 1,
            "id": task_id,
            "title": f"Task {index}",
            "role": "writer",
            "section_ids": ["introduction"],
            "source_ids": [],
            "instructions": "Do the work.",
            "acceptance": ["It is done"],
            "token_budget": 1000,
            "depends_on": [],
        }
        for index, task_id in enumerate(ids)
    ]


def test_plan_rejects_duplicate_task_ids() -> None:
    """Two tasks with one id would make the ledger ambiguous."""
    task_id = sample_ulid(0)
    with pytest.raises(ValidationError, match="duplicate task ids"):
        Plan.model_validate({"version": 1, "goal": "g", "tasks": _tasks(task_id, task_id)})


def test_plan_rejects_dangling_dependency() -> None:
    """A dependency on a task that is not in the plan can never be satisfied."""
    tasks = _tasks(sample_ulid(0), sample_ulid(1))
    tasks[1]["depends_on"] = [sample_ulid(9)]
    with pytest.raises(ValidationError, match="unknown task ids"):
        Plan.model_validate({"version": 1, "goal": "g", "tasks": tasks})


def test_plan_rejects_dependency_cycle() -> None:
    """A cycle would deadlock the executor: it must be rejected at construction."""
    first, second = sample_ulid(0), sample_ulid(1)
    tasks = _tasks(first, second)
    tasks[0]["depends_on"] = [second]
    tasks[1]["depends_on"] = [first]
    with pytest.raises(ValidationError, match="dependency cycle"):
        Plan.model_validate({"version": 1, "goal": "g", "tasks": tasks})


def test_plan_reports_ready_tasks_and_role_distribution() -> None:
    """The executor's two questions: what can run now, and who is doing the work."""
    plan = Plan.model_validate(PAYLOADS["Plan"])
    distribution = plan.role_distribution()
    assert distribution["researcher"] == 1
    assert distribution["writer"] == 1
    researcher_task_id = next(task.id for task in plan.tasks if task.role.value == "researcher")
    ready_ids = {task.id for task in plan.ready_tasks(set())}
    assert ready_ids == {researcher_task_id}
    missing = plan.missing_required_roles()
    assert missing == sorted(missing, key=str), "missing_required_roles must be sorted"


# ---------------------------------------------------------------------- SourceCard
def test_source_card_requires_a_locator() -> None:
    """HARD RULE 1: an unlocatable reference cannot be verified, so it is not allowed."""
    payload = {**PAYLOADS["SourceCard"], "doi": None, "url": None}
    with pytest.raises(ValidationError, match="DOI or a URL"):
        SourceCard.model_validate(payload)


def test_source_card_snippet_is_capped_at_60_words() -> None:
    """The snippet cap is what keeps a card digestible inside the token budget."""
    with pytest.raises(ValidationError, match="at most 60 words"):
        SourceCard.model_validate({**PAYLOADS["SourceCard"], "snippet": WORDS_61})


def test_source_card_normalises_doi_and_defaults_to_unknown() -> None:
    """DOIs resolve to one spelling, and nothing is 'verified' by default."""
    card = SourceCard.model_validate(
        {**PAYLOADS["SourceCard"], "doi": "DOI: 10.1234/AbC.2024", "verification": "unknown"}
    )
    assert card.doi == "10.1234/abc.2024"
    assert card.verification.value == "unknown"
    assert card.is_locatable


def test_normalise_doi_strips_resolver_prefixes() -> None:
    """Both spellings of a DOI compare equal."""
    assert normalise_doi("https://doi.org/10.1/X") == normalise_doi("doi:10.1/x") == "10.1/x"


@pytest.mark.parametrize("doi", ["not-a-doi", "10.1", "10.1234"])
def test_source_card_rejects_malformed_doi(doi: str) -> None:
    """A DOI that cannot resolve is worse than no DOI."""
    with pytest.raises(ValidationError, match="malformed DOI"):
        SourceCard.model_validate({**PAYLOADS["SourceCard"], "doi": doi})


def test_source_card_rejects_non_http_url() -> None:
    """Only absolute http(s) URLs are locatable."""
    with pytest.raises(ValidationError, match="absolute http"):
        SourceCard.model_validate({**PAYLOADS["SourceCard"], "url": "ftp://example.org/x.pdf"})


# --------------------------------------------------------------------------- Patch
@pytest.mark.parametrize(
    ("op", "payload"),
    [
        ("replace_text", {"old": "a"}),  # missing new
        ("insert_after", {"text": "b"}),  # missing anchor
        ("delete_text", {"anchor": "x"}),  # missing old
        ("replace_section", {"old": "x"}),  # missing text
    ],
)
def test_patch_op_requires_its_payload(op: str, payload: dict[str, str]) -> None:
    """Each op has exactly one payload shape."""
    with pytest.raises(ValidationError, match="requires non-empty"):
        PatchOp.model_validate({"op": op, **payload})


def test_patch_op_rejects_foreign_payload_fields() -> None:
    """A replace_text that also carries text would apply two different edits."""
    with pytest.raises(ValidationError, match="must not carry"):
        PatchOp.model_validate({"op": "replace_text", "old": "a", "new": "b", "text": "c"})


def test_patch_op_rejects_noop_replacement() -> None:
    """old == new is a no-op and always a bug in the calling agent."""
    with pytest.raises(ValidationError, match="no-op"):
        PatchOp.model_validate({"op": "replace_text", "old": "same", "new": "same"})


def test_patch_requires_at_least_one_op() -> None:
    """An empty patch would create a ledger entry that changed nothing."""
    with pytest.raises(ValidationError):
        Patch.model_validate({"section_id": "introduction", "base_version": 0, "ops": []})


def test_patch_section_id_must_be_a_slug() -> None:
    """Section ids are stable slugs, never free text."""
    with pytest.raises(ValidationError):
        Patch.model_validate(
            {
                "section_id": "Introduction Section",
                "base_version": 0,
                "ops": [{"op": "replace_section", "text": "x"}],
            }
        )


# ----------------------------------------------------------------------- TokenUsage
def test_token_usage_wire_format_uses_in() -> None:
    """``in`` is a Python keyword, so the alias carries the wire format."""
    usage = TokenUsage(**{"in": 10, "out": 5, "cached": 3})
    assert usage.model_dump() == {"in": 10, "out": 5, "cached": 3}
    assert usage.model_dump_json() == '{"in":10,"out":5,"cached":3}'


def test_token_usage_never_double_counts() -> None:
    """total = full-rate prompt + cached prompt + completion; cached is separate."""
    usage = TokenUsage(**{"in": 100, "out": 50, "cached": 200})
    assert usage.total == 350
    assert usage.full_rate == 150
    assert usage.cache_hit_ratio == pytest.approx(200 / 300)


def test_token_usage_rejects_negative_counts() -> None:
    """A negative count means a broken adapter, and must not reach the budget."""
    with pytest.raises(ValidationError):
        TokenUsage(**{"in": -1, "out": 0})


# --------------------------------------------------------------------- ChangeEntry
def test_change_entry_summary_is_capped_at_60_words() -> None:
    """The digest's size guarantee starts here."""
    with pytest.raises(ValidationError, match="at most 60 words"):
        ChangeEntry.model_validate({**PAYLOADS["ChangeEntry"], "summary": WORDS_61})


def test_change_entry_stores_a_pointer_not_a_patch_body() -> None:
    """patch_ref is a reference: an inline patch body belongs nowhere in the ledger."""
    entry = ChangeEntry.model_validate(PAYLOADS["ChangeEntry"])
    assert entry.patch_ref is not None
    assert entry.patch_ref.startswith("patch:")
    with pytest.raises(ValidationError):
        ChangeEntry.model_validate({**PAYLOADS["ChangeEntry"], "patch_ref": "has spaces in it"})


def test_change_entry_rejects_planner_as_an_author_role() -> None:
    """The Planner plans and evaluates; it never authors a ledger entry."""
    with pytest.raises(ValidationError):
        ChangeEntry.model_validate({**PAYLOADS["ChangeEntry"], "role": "planner"})


def test_change_entry_status_needs_user_input_is_reachable() -> None:
    """HARD RULE 1's escape hatch exists on the entry."""
    entry = ChangeEntry.model_validate(
        {**PAYLOADS["ChangeEntry"], "status": "needs_user_input", "patch_ref": None}
    )
    assert not entry.is_success


# ---------------------------------------------------------------------- AgentResult
def test_agent_result_requires_targets_for_patches() -> None:
    """The ledger must be able to describe what changed."""
    payload = {**PAYLOADS["AgentResult"], "entry": {**PAYLOADS["ChangeEntry"], "targets": []}}
    with pytest.raises(ValidationError, match=r"missing from entry\.targets"):
        AgentResult.model_validate(payload)


def test_agent_result_requires_patch_ref_when_patches_present() -> None:
    """A pointer-less patch entry would force the digest to carry patch bodies."""
    payload = {
        **PAYLOADS["AgentResult"],
        "entry": {**PAYLOADS["ChangeEntry"], "patch_ref": None},
    }
    with pytest.raises(ValidationError, match="must set patch_ref"):
        AgentResult.model_validate(payload)


def test_only_the_validator_may_attach_a_quality_report() -> None:
    """A writer scoring its own work would break the loop's incentives."""
    with pytest.raises(ValidationError, match="may not return a QualityReport"):
        AgentResult.model_validate({**PAYLOADS["AgentResult"], "report": PAYLOADS["QualityReport"]})


def test_validator_result_with_report_is_accepted() -> None:
    """The validator is the one role that reports a Q."""
    payload = {
        **PAYLOADS["AgentResult"],
        "entry": {**PAYLOADS["ChangeEntry"], "role": "validator", "patch_ref": None},
        "patches": [],
        "report": PAYLOADS["QualityReport"],
    }
    result = AgentResult.model_validate(payload)
    assert result.report is not None
    assert result.report.Q == 67


def test_agent_result_rejects_duplicate_source_cards() -> None:
    """Two cards with one id would make citation resolution ambiguous."""
    card = PAYLOADS["SourceCard"]
    with pytest.raises(ValidationError, match="duplicate source card ids"):
        AgentResult.model_validate({**PAYLOADS["AgentResult"], "source_cards": [card, dict(card)]})


# --------------------------------------------------------------------- SectionState
def test_empty_section_state_is_self_consistent() -> None:
    """An empty section has version 0, no words, no citations, the empty hash."""
    state = SectionState(id="introduction", title="Introduction", order=0)
    assert state.hash == EMPTY_SECTION_HASH
    assert state.status.value == "empty"


def test_empty_section_cannot_claim_content() -> None:
    """An 'empty' section with words is a state-machine bug."""
    with pytest.raises(ValidationError, match="0 words"):
        SectionState(id="introduction", title="Introduction", order=0, words=10)


def test_non_empty_section_needs_a_version() -> None:
    """Content without a version would break the base_version check on patches."""
    with pytest.raises(ValidationError, match="needs version"):
        SectionState(id="introduction", title="Introduction", order=0, words=10, status="draft")


def test_section_state_reports_unresolved_citations() -> None:
    """Unresolved markers are what the both-ways citation gate looks for."""
    state = SectionState.model_validate(PAYLOADS["SectionState"])
    assert [citation.key for citation in state.unresolved_citations] == ["(Petrova et al., 2023)"]


def test_section_advanced_resets_status_to_draft() -> None:
    """New content invalidates a previous review - automatically, not by convention."""
    state = SectionState.model_validate(
        {**PAYLOADS["SectionState"], "status": "final", "open_issues": []}
    )
    advanced = state.advanced(version=3, hash_="sha256:" + "cd" * 32, words=430)
    assert advanced.status.value == "draft"
    assert advanced.version == 3 and advanced.words == 430


def test_section_hash_must_be_spelled_sha256() -> None:
    """One hash spelling keeps comparison cheap and unambiguous."""
    with pytest.raises(ValidationError):
        SectionState.model_validate({**PAYLOADS["SectionState"], "hash": "deadbeef"})


# -------------------------------------------------------------------- QualityReport
def test_quality_report_requires_all_eight_gates() -> None:
    """An absent gate must never read as a pass."""
    gates = dict(PAYLOADS["QualityReport"]["gates"])
    gates.pop(GATE_NAMES[0])
    with pytest.raises(ValidationError, match="every hard gate must be reported"):
        QualityReport.model_validate({**PAYLOADS["QualityReport"], "gates": gates})


def test_quality_report_rejects_unknown_gates() -> None:
    """Gate names are fixed in contracts v1."""
    gates = {**PAYLOADS["QualityReport"]["gates"], "vibes_check": True}
    with pytest.raises(ValidationError, match="unknown gate names"):
        QualityReport.model_validate({**PAYLOADS["QualityReport"], "gates": gates})


@pytest.mark.parametrize("field", ["Q", "verified_citation_ratio"])
def test_quality_report_ranges(field: str) -> None:
    """Q is 0-100 and the citation ratio is 0-1."""
    bad = 101 if field == "Q" else 1.5
    with pytest.raises(ValidationError):
        QualityReport.model_validate({**PAYLOADS["QualityReport"], field: bad})


def test_quality_report_scores_are_bounded() -> None:
    """A criterion score outside 0-100 would silently distort the weighted average."""
    scores = {**PAYLOADS["QualityReport"]["scores"], "coherence": 130}
    with pytest.raises(ValidationError):
        QualityReport.model_validate({**PAYLOADS["QualityReport"], "scores": scores})


def test_quality_report_gate_helpers() -> None:
    """Failed gates are listed in canonical order, for stable logging."""
    gates = dict.fromkeys(GATE_NAMES, True)
    gates["no_placeholders"] = False
    gates["citations_resolve_both_ways"] = False
    report = QualityReport.model_validate(
        {**PAYLOADS["QualityReport"], "gates": gates, "issues": []}
    )
    assert report.failed_gates == ["citations_resolve_both_ways", "no_placeholders"]
    assert not report.all_gates_pass
    assert report.high_severity_issues() == []


# -------------------------------------------------------------------------- Verdict
def test_verdict_cannot_finish_below_its_own_minimum() -> None:
    """Contradictory advice would make the loop's decision unauditable."""
    with pytest.raises(ValidationError, match="contradicts meets_minimum"):
        Verdict.model_validate(
            {
                "meets_minimum": False,
                "missing": ["references"],
                "risks": [],
                "recommendation": "finish",
            }
        )


def test_verdict_continue_needs_a_target() -> None:
    """'Continue' without anything missing is an infinite loop with extra steps."""
    with pytest.raises(ValidationError, match="at least one entry in"):
        Verdict.model_validate(
            {
                "meets_minimum": True,
                "missing": [],
                "risks": [],
                "recommendation": "continue",
            }
        )


# --------------------------------------------------------------------- LoopDecision
def test_loop_decision_reasons_are_codes_not_prose() -> None:
    """Reasons are counted and charted, so they must be stable codes."""
    with pytest.raises(ValidationError, match="lower_snake_case"):
        LoopDecision.model_validate(
            {"iter": 1, "action": "CONTINUE", "reasons": ["Q is below the minimum"], "evidence": {}}
        )


def test_loop_decision_needs_at_least_one_reason() -> None:
    """A decision nobody can explain is a bug in the controller."""
    with pytest.raises(ValidationError):
        LoopDecision.model_validate(
            {"iter": 1, "action": "STOP_USER", "reasons": [], "evidence": {}}
        )


def test_loop_decision_classifies_terminal_actions() -> None:
    """``is_terminal`` is the loop's exit condition and must cover every STOP_*."""
    stop = LoopDecision(
        iter=1, action="STOP_BUDGET", reasons=["iteration_token_cap_hit"], evidence={}
    )
    cont = LoopDecision(iter=1, action="CONTINUE", reasons=["q_below_minimum"], evidence={"Q": 60})
    assert stop.is_terminal and not stop.needs_planner
    assert cont.needs_planner and not cont.is_terminal


# ------------------------------------------------------------------------- Account
def test_account_never_stores_a_plaintext_key() -> None:
    """HARD RULE 2: the contract itself refuses a raw key."""
    key_like = "sk-ant-" + "A" * 24
    with pytest.raises(ValidationError, match="raw API key"):
        Account.model_validate({**PAYLOADS["Account"], "key_ciphertext": key_like})


def test_account_ciphertext_is_not_in_the_repr() -> None:
    """A ciphertext in a traceback or log line is still a leak."""
    account = Account.model_validate(PAYLOADS["Account"])
    assert "AAAA" not in repr(account)


def test_account_public_hides_the_key() -> None:
    """The API view carries a fingerprint, never key material."""
    public = Account.model_validate(PAYLOADS["Account"]).public()
    assert public.key_fingerprint and len(public.key_fingerprint) == 12
    assert "key_ciphertext" not in public.model_dump()


def test_account_provider_constraints() -> None:
    """anthropic has a vendor endpoint; mock has none; only openai_compatible sets one."""
    with pytest.raises(ValidationError, match="leave base_url empty"):
        Account.model_validate({**PAYLOADS["Account"], "base_url": "https://api.example.org"})
    compatible = Account.model_validate(
        {
            **PAYLOADS["Account"],
            "provider": "openai_compatible",
            "base_url": "https://api.example.org/v1",
        }
    )
    assert compatible.base_url == "https://api.example.org/v1"
    with pytest.raises(ValidationError, match="does not use base_url"):
        Account.model_validate(
            {**PAYLOADS["Account"], "provider": "mock", "base_url": "https://x.example"}
        )


def test_monthly_budget_is_optional() -> None:
    """No ceiling means 'inherit the run budget', which is the honest default."""
    assert MonthlyBudget().is_unlimited
    assert not MonthlyBudget(tokens=1_000_000).is_unlimited


# --------------------------------------------------------------------- AgentBinding
def test_binding_strategy_needs_enough_accounts() -> None:
    """Failover with one account is not failover."""
    with pytest.raises(ValidationError, match="needs at least two accounts"):
        AgentBinding.model_validate({**PAYLOADS["AgentBinding"], "account_ids": [sample_ulid(4)]})


def test_binding_rejects_duplicate_accounts() -> None:
    """A repeated account is not a fallback."""
    with pytest.raises(ValidationError, match="must be unique"):
        AgentBinding.model_validate(
            {**PAYLOADS["AgentBinding"], "account_ids": [sample_ulid(4), sample_ulid(4)]}
        )


def test_binding_output_cap_cannot_exceed_input_cap() -> None:
    """A binding that could ask for more output than context is a config error."""
    with pytest.raises(ValidationError, match="must not exceed"):
        AgentBinding.model_validate({**PAYLOADS["AgentBinding"], "max_output_tokens": 30_000})


def test_binding_accepts_a_dedicated_single_account() -> None:
    """The one legitimate single-account case."""
    binding = AgentBinding.model_validate(
        {
            **PAYLOADS["AgentBinding"],
            "account_ids": [sample_ulid(4)],
            "strategy": "dedicated",
        }
    )
    assert binding.is_active


# ------------------------------------------------------------------------ LLMRequest
def test_llm_request_last_message_must_be_from_the_user() -> None:
    """A request ending with an assistant turn invites the model to continue its own text."""
    with pytest.raises(ValidationError, match="last message must come from the user"):
        LLMRequest.model_validate(
            {
                "system_blocks": [{"text": "sys", "cache": False}],
                "messages": [{"role": "assistant", "text": "sure"}],
                "max_output_tokens": 128,
            }
        )


def test_llm_request_schema_must_be_shaped_like_a_schema() -> None:
    """Structured output is only useful if the schema is a real one."""
    with pytest.raises(ValidationError, match="'type' or '\\$ref'"):
        LLMRequest.model_validate(
            {
                "messages": [{"role": "user", "text": "hi"}],
                "max_output_tokens": 128,
                "json_schema": {"properties": {"a": {"type": "string"}}},
            }
        )


def test_llm_request_carries_no_credentials() -> None:
    """The request is prompt + cap + schema. Provider selection happens elsewhere."""
    request = LLMRequest.model_validate(PAYLOADS["LLMRequest"])
    assert "key" not in request.model_dump()
    assert request.is_structured


def test_llm_request_requires_at_least_one_message() -> None:
    """There is nothing to answer without a user turn."""
    with pytest.raises(ValidationError):
        LLMRequest.model_validate({"messages": [], "max_output_tokens": 128})


# ------------------------------------------------------------------------ Hardening
# Every rule below closes a hole found in the v1 audit (docs/CONTRACT-AUDIT.md).
# None of them changes a field, type or required-ness: the exported wire format is
# identical, the accepted value space is simply the one the docstrings describe.


def test_plan_missing_required_roles_counts_tasks_not_keys() -> None:
    """A role with zero tasks is missing; the old set difference could never see it."""
    plan = Plan.model_validate(PAYLOADS["Plan"])  # researcher + writer only
    assert [role.value for role in plan.missing_required_roles()] == ["architect", "validator"]
    assert plan.missing_required_roles() == sorted(plan.missing_required_roles(), key=str)


def test_plan_missing_required_roles_is_empty_once_every_role_has_a_task() -> None:
    """A plan that covers all five roles reports nothing missing."""
    from app.contracts.enums import AgentRole

    tasks = [
        {
            **{key: value for key, value in PAYLOADS["Task"].items() if key != "depends_on"},
            "id": sample_ulid(index),
            "role": role.value,
            "depends_on": [],
        }
        for index, role in enumerate(AgentRole)
    ]
    plan = Plan.model_validate({"version": 1, "goal": "Cover every role once.", "tasks": tasks})
    assert plan.missing_required_roles() == []


def test_section_advanced_revalidates_the_new_metrics() -> None:
    """``model_copy(update=...)`` skips validation; the contract must not."""
    from app.contracts.enums import SectionStatus

    state = SectionState.model_validate(PAYLOADS["SectionState"])
    advanced = state.advanced(version=state.version + 1, hash_="sha256:" + "cd" * 32, words=430)
    assert (advanced.version, advanced.words, advanced.status) == (
        state.version + 1,
        430,
        SectionStatus.DRAFT,
    )
    assert advanced.citations == state.citations, "content metrics change, citations do not"

    for version, hash_, words in (
        (0, "sha256:" + "cd" * 32, 430),  # version 0 means the empty section
        (state.version + 1, "not-a-hash", 430),
        (state.version + 1, "sha256:" + "cd" * 32, -1),
    ):
        with pytest.raises(ValidationError):
            state.advanced(version=version, hash_=hash_, words=words)


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "insert_after", "anchor": "sentence.", "text": " "},
        {"op": "replace_text", "old": "a", "new": "b\n\n"},
        {"op": "delete_text", "old": "  "},
    ],
)
def test_patch_op_preserves_whitespace_exactly(payload: dict[str, str]) -> None:
    """Literal matching and byte-exact writing both need the payload unmodified."""
    op = PatchOp.model_validate(payload)
    for field, value in payload.items():
        if field != "op":
            assert getattr(op, field) == value


def test_patch_op_still_rejects_an_empty_payload_field() -> None:
    """Whitespace is content; an empty string is still a missing field."""
    with pytest.raises(ValidationError) as excinfo:
        PatchOp.model_validate({"op": "insert_after", "anchor": "sentence.", "text": ""})
    assert excinfo.value.errors()[0]["loc"] == ("text",)


@pytest.mark.parametrize(
    "reason",
    ["plateau-detected", "123", "_leading", "trailing_", "double__underscore", "Q_below"],
)
def test_loop_decision_reason_must_be_a_snake_case_code(reason: str) -> None:
    """Hyphens, digits-first, stray underscores and capitals are not reason codes."""
    with pytest.raises(ValidationError, match="lower_snake_case"):
        LoopDecision(iter=1, action="CONTINUE", reasons=[reason], evidence={})


def test_loop_decision_accepts_snake_case_codes() -> None:
    """The rule bans prose, not the codes the controller actually emits."""
    decision = LoopDecision(
        iter=1,
        action="CONTINUE",
        reasons=["q_below_minimum", "budget_80pct", "gate_citations_resolve_both_ways"],
        evidence={"Q": 61},
    )
    assert len(decision.reasons) == 3


def test_verdict_continue_below_minimum_still_needs_a_target() -> None:
    """``meets_minimum=false`` does not make an aimless continue acceptable."""
    with pytest.raises(ValidationError, match="at least one entry in"):
        Verdict.model_validate(
            {
                "meets_minimum": False,
                "missing": [],
                "risks": [],
                "recommendation": "continue",
            }
        )


def test_quality_report_rejects_duplicate_issue_ids() -> None:
    """Issue ids are the ledger's identity for a defect; two rows cannot share one."""
    issue = PAYLOADS["QualityReport"]["issues"][0]
    with pytest.raises(ValidationError, match="duplicate issue ids"):
        QualityReport.model_validate({**PAYLOADS["QualityReport"], "issues": [issue, dict(issue)]})


def test_change_entry_cannot_open_and_close_the_same_issue() -> None:
    """An id in both lists makes open-issue state depend on list order."""
    with pytest.raises(ValidationError, match="both open and close"):
        ChangeEntry.model_validate(
            {
                **PAYLOADS["ChangeEntry"],
                "issues_opened": ["ISSUE-cite-001"],
                "issues_closed": ["ISSUE-cite-001"],
            }
        )


def test_agent_result_rejects_duplicate_section_states() -> None:
    """Two states for one section would make the store's choice unexplained."""
    section = PAYLOADS["AgentResult"]["sections"][0]
    with pytest.raises(ValidationError, match="duplicate section states"):
        AgentResult.model_validate(
            {**PAYLOADS["AgentResult"], "sections": [section, dict(section)]}
        )


@pytest.mark.parametrize("language", ["en", "en-GB", "zh-Hant-TW", "pt-BR", "x-private"])
def test_brief_accepts_bcp47_language_tags(language: str) -> None:
    """The shape that is written into every prompt must be a language tag."""
    assert Brief.model_validate({**SAMPLE_BRIEF, "language": language}).language == language


@pytest.mark.parametrize("language", ["hello world", "!!", "en_US", "english language", "e"])
def test_brief_rejects_non_bcp47_language_tags(language: str) -> None:
    """A 2..35-character string is not automatically a language."""
    with pytest.raises(ValidationError):
        Brief.model_validate({**SAMPLE_BRIEF, "language": language})


def test_account_rejects_an_openrouter_style_plaintext_key() -> None:
    """The plaintext guard must cover every vendor family, not only Anthropic/OpenAI."""
    raw_key = "sk-" + "or-v1-" + "0" * 32  # assembled so the secret scanner stays quiet
    with pytest.raises(ValidationError, match="raw API key"):
        Account.model_validate({**PAYLOADS["Account"], "key_ciphertext": raw_key})


def test_llm_response_may_report_an_empty_completion() -> None:
    """An empty answer is a model outcome the runtime can repair, not a crash."""
    from app.contracts.llm import LLMResponse

    usage = TokenUsage(**{"in": 3, "out": 0})
    response = LLMResponse(text="", usage=usage, model="mock", latency_ms=5)
    assert response.text == ""

    with pytest.raises(ValidationError):
        LLMResponse(text="text", usage=usage, model="", latency_ms=5)
