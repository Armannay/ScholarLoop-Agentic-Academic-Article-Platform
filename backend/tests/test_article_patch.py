"""Patch engine tests: the apply matrix, the reject matrix and the diff numbers.

The engine is pure, so every rule in ``app.article.patch`` is exercised here without
a store, a lock or a provider. The reject matrix matters most: each reason is what an
agent reads before spending a repair call, so the tests assert on the substance of
the message, not just on the exception type.
"""

from __future__ import annotations

import pytest

from app.article.patch import (
    MAX_PROBLEM_CHARS,
    PatchOutcome,
    PatchRejected,
    apply_ops,
    diff_stats,
    diff_texts,
    validate_ops,
)
from app.contracts.common import word_count
from app.contracts.enums import PatchOpType, SectionStatus
from app.contracts.patch import Patch, PatchOp
from app.core.hashing import content_hash

pytestmark = pytest.mark.article

BASE_TEXT = (
    "Agentic loops trade tokens for quality.\nEvery patch is a delta [1].\nTokens are counted."
)
#: "The loop " occurs three times, "the " twice: the ambiguity cases.
AMBIGUOUS_TEXT = "The loop reads the digest. The loop writes the ledger. The loop never sleeps."
SECTION = "introduction"


def make_patch(*ops: PatchOp, base_version: int = 1, section_id: str = SECTION) -> Patch:
    """A valid patch carrying the given ops."""
    return Patch(section_id=section_id, base_version=base_version, ops=list(ops))


def replace(old: str, new: str) -> PatchOp:
    """A ``replace_text`` op."""
    return PatchOp(op=PatchOpType.REPLACE_TEXT, old=old, new=new)


def insert_after(anchor: str, text: str) -> PatchOp:
    """An ``insert_after`` op."""
    return PatchOp(op=PatchOpType.INSERT_AFTER, anchor=anchor, text=text)


def delete(old: str) -> PatchOp:
    """A ``delete_text`` op."""
    return PatchOp(op=PatchOpType.DELETE_TEXT, old=old)


def replace_section(text: str) -> PatchOp:
    """A ``replace_section`` op."""
    return PatchOp(op=PatchOpType.REPLACE_SECTION, text=text)


def every_offset(text: str, needle: str) -> list[int]:
    """All start offsets, so a test can assert the reason quotes the real ones."""
    offsets: list[int] = []
    index = text.find(needle)
    while index != -1:
        offsets.append(index)
        index = text.find(needle, index + 1)
    return offsets


# ------------------------------------------------------------------------- apply matrix


@pytest.mark.parametrize(
    ("text", "ops", "expected"),
    [
        pytest.param(
            BASE_TEXT,
            [replace("quality", "evidence")],
            BASE_TEXT.replace("quality", "evidence"),
            id="replace_text",
        ),
        pytest.param(
            BASE_TEXT,
            [insert_after("for quality.", "We measure it.")],
            BASE_TEXT.replace("for quality.", "for quality.We measure it."),
            id="insert_after",
        ),
        pytest.param(
            BASE_TEXT,
            [delete("Every patch is a delta [1].")],
            "Agentic loops trade tokens for quality.\n\nTokens are counted.",
            id="delete_text",
        ),
        pytest.param(
            BASE_TEXT,
            [replace_section("A shorter body.")],
            "A shorter body.",
            id="replace_section",
        ),
        pytest.param(
            BASE_TEXT,
            [replace("quality", "evidence"), insert_after("for evidence.", "Twice.")],
            BASE_TEXT.replace("for quality.", "for evidence.Twice."),
            id="sequential-ops-see-earlier-results",
        ),
        pytest.param(
            BASE_TEXT,
            [delete("[1]"), replace("quality", "evidence")],
            BASE_TEXT.replace("[1]", "").replace("quality", "evidence"),
            id="delete-then-replace",
        ),
        pytest.param(
            AMBIGUOUS_TEXT,
            [replace("The loop reads", "It reads")],
            AMBIGUOUS_TEXT.replace("The loop reads", "It reads", 1),
            id="unique-prefix-of-a-repeated-phrase",
        ),
    ],
)
def test_apply_matrix(text: str, ops: list[PatchOp], expected: str) -> None:
    """Each op verb, and ops composed in order, produce exactly the expected body."""
    outcome = apply_ops(text, make_patch(*ops), status=SectionStatus.DRAFT)
    assert outcome.text == expected
    assert outcome.hash == content_hash(expected)
    assert outcome.changed is True
    assert outcome.ops_applied == len(ops)
    assert outcome.words == word_count(expected)


def test_insert_after_splices_immediately_after_the_anchor() -> None:
    """Insertion is a raw splice: no space, no newline the op did not ask for."""
    outcome = apply_ops(
        "Alpha beta",
        make_patch(insert_after("Alpha", "gamma"), base_version=0),
        status=SectionStatus.EMPTY,
    )
    assert outcome.text == "Alphagamma beta"
    # PatchOp keeps whitespace (``str_strip_whitespace=False``), so the op carries
    # exactly the bytes the agent sent; the splice itself adds nothing.
    assert PatchOp(op=PatchOpType.INSERT_AFTER, anchor="Alpha", text="  b  ").text == "  b  "


def test_insert_after_can_carry_a_leading_space() -> None:
    """The gap after the anchor is payload, not formatting.

    ``insert_after`` matches literal text, so an agent that wants "for quality. We
    measure it." has to put the leading space in ``text``. Stripping it used to shift
    the edit one character left, which is why agents had to anchor on the *previous*
    token to fake the space.
    """
    outcome = apply_ops(
        BASE_TEXT,
        make_patch(insert_after("for quality.", " We measure it.")),
        status=SectionStatus.DRAFT,
    )
    assert outcome.text == BASE_TEXT.replace("for quality.", "for quality. We measure it.")
    assert "quality.  We" not in outcome.text
    assert outcome.changed is True


def test_delete_text_can_carry_a_trailing_newline() -> None:
    """A needle may end in a newline, so a whole line can be deleted in one op.

    Without the trailing newline in ``old`` the line break survives and leaves an
    empty paragraph behind - the difference is exactly the whitespace the contract
    used to strip.
    """
    body = "Keep this.\nDelete this line.\nKeep that."
    outcome = apply_ops(
        body,
        make_patch(delete("Delete this line.\n")),
        status=SectionStatus.DRAFT,
    )
    assert outcome.text == "Keep this.\nKeep that."
    assert PatchOp(op=PatchOpType.DELETE_TEXT, old="Delete this line.\n").old == (
        "Delete this line.\n"
    )

    without_newline = apply_ops(
        body,
        make_patch(delete("Delete this line.")),
        status=SectionStatus.DRAFT,
    )
    assert without_newline.text == "Keep this.\n\nKeep that."


def test_apply_reports_the_numbers_the_ledger_quotes() -> None:
    """Diff stats count characters that actually moved."""
    outcome = apply_ops(
        BASE_TEXT, make_patch(replace("quality", "quality!")), status=SectionStatus.DRAFT
    )
    assert isinstance(outcome, PatchOutcome)
    assert outcome.stats.chars_added == 1
    assert outcome.stats.chars_removed == 0
    assert outcome.stats.net_chars == 1
    assert outcome.words == word_count(BASE_TEXT) + 0


def test_whitespace_only_patch_is_not_progress() -> None:
    """A patch that only reflows whitespace changes no content: ``changed`` is False.

    The store relies on this to avoid inventing versions, and the erosion detectors
    rely on it to avoid seeing churn where there was none.
    """
    outcome = apply_ops(
        "One line.\nTwo lines.",
        make_patch(replace_section("One line.   \nTwo lines.\n\n\n")),
        status=SectionStatus.DRAFT,
    )
    assert outcome.changed is False
    assert outcome.hash == content_hash("One line.\nTwo lines.")
    assert outcome.stats.touched_chars == 0


def test_removing_a_blank_line_is_a_real_change() -> None:
    """Interior whitespace is content: reflowing a paragraph invalidates a patch."""
    outcome = apply_ops(
        "One line.\n\nTwo lines.",
        make_patch(replace_section("One line.\nTwo lines.")),
        status=SectionStatus.DRAFT,
    )
    assert outcome.changed is True
    assert outcome.stats.chars_removed == 1


# ------------------------------------------------------------------------ reject matrix


@pytest.mark.parametrize(
    ("text", "ops", "status", "expected_reason"),
    [
        pytest.param(
            BASE_TEXT,
            [replace("absent phrase", "x")],
            SectionStatus.DRAFT,
            "'old' occurs 0 time(s)",
            id="replace_text-not-found",
        ),
        pytest.param(
            AMBIGUOUS_TEXT,
            [replace("The loop", "x")],
            SectionStatus.DRAFT,
            "'old' occurs 3 time(s)",
            id="replace_text-ambiguous",
        ),
        pytest.param(
            BASE_TEXT,
            [insert_after("absent anchor", "x")],
            SectionStatus.DRAFT,
            "'anchor' occurs 0 time(s)",
            id="insert_after-anchor-not-found",
        ),
        pytest.param(
            AMBIGUOUS_TEXT,
            [insert_after("the ", "x")],
            SectionStatus.DRAFT,
            "'anchor' occurs 2 time(s)",
            id="insert_after-anchor-ambiguous",
        ),
        pytest.param(
            BASE_TEXT,
            [delete("absent phrase")],
            SectionStatus.DRAFT,
            "'old' occurs 0 time(s)",
            id="delete_text-not-found",
        ),
        pytest.param(
            AMBIGUOUS_TEXT,
            [delete("the ")],
            SectionStatus.DRAFT,
            "'old' occurs 2 time(s)",
            id="delete_text-ambiguous",
        ),
        pytest.param(
            BASE_TEXT,
            [replace_section("New body.")],
            SectionStatus.REVIEWED,
            "allowed only when the section is draft or empty",
            id="replace_section-on-reviewed",
        ),
        pytest.param(
            BASE_TEXT,
            [replace_section("New body.")],
            SectionStatus.FINAL,
            "section 'introduction' is 'final'",
            id="replace_section-on-final",
        ),
    ],
)
def test_reject_matrix(
    text: str, ops: list[PatchOp], status: SectionStatus, expected_reason: str
) -> None:
    """Every rejection names the op index, the field and the precise cause."""
    with pytest.raises(PatchRejected) as excinfo:
        apply_ops(text, make_patch(*ops), status=status)
    problem = excinfo.value.problems[0]
    assert expected_reason in problem.reason
    assert problem.op_index == 0
    assert problem.op is ops[0].op
    assert problem.hint
    assert excinfo.value.section_id == SECTION
    assert len(excinfo.value.repair_instruction()) <= MAX_PROBLEM_CHARS * 2 + 120


def test_ambiguous_match_lists_the_real_offsets() -> None:
    """An ambiguous match says where it matched, so the agent can widen it."""
    with pytest.raises(PatchRejected) as excinfo:
        apply_ops(AMBIGUOUS_TEXT, make_patch(replace("The loop", "x")), status=SectionStatus.DRAFT)
    reason = excinfo.value.problems[0].reason
    assert "at chars 0, 27, 55" in reason
    assert every_offset(AMBIGUOUS_TEXT, "The loop") == [0, 27, 55]
    assert "Widen 'old'" in excinfo.value.problems[0].hint


def test_whitespace_mismatch_is_named_as_whitespace() -> None:
    """A near miss caused by line breaks says so instead of claiming the text is absent."""
    with pytest.raises(PatchRejected) as excinfo:
        apply_ops(
            BASE_TEXT,
            make_patch(delete("Every patch is a delta [1]. Tokens are counted.")),
            status=SectionStatus.DRAFT,
        )
    assert "whitespace" in excinfo.value.problems[0].hint


def test_near_miss_suggests_the_closest_line() -> None:
    """A typo'd anchor gets the stored line back, which is enough for one repair."""
    with pytest.raises(PatchRejected) as excinfo:
        apply_ops(
            BASE_TEXT,
            make_patch(insert_after("Every patch is a delt [1].", "x")),
            status=SectionStatus.DRAFT,
        )
    assert "Every patch is a delta [1]." in excinfo.value.problems[0].hint


def test_absent_text_says_to_re_read_the_section() -> None:
    """Nothing close in the body => the honest instruction is to re-read it."""
    with pytest.raises(PatchRejected) as excinfo:
        apply_ops(BASE_TEXT, make_patch(delete("7f3a9c")), status=SectionStatus.DRAFT)
    assert "Re-read the section" in excinfo.value.problems[0].hint


def test_every_problem_is_reported_in_one_pass() -> None:
    """Validation runs to completion: one repair round trip can fix the whole patch."""
    patch = make_patch(
        replace("absent", "x"),
        insert_after("also absent", "y"),
        delete("absent too"),
    )
    with pytest.raises(PatchRejected) as excinfo:
        apply_ops(BASE_TEXT, patch, status=SectionStatus.DRAFT)
    assert [problem.op_index for problem in excinfo.value.problems] == [0, 1, 2]
    assert "Fix 3 op(s)" in excinfo.value.repair_instruction()


def test_validate_ops_returns_problems_without_raising() -> None:
    """The dry run and the real run agree, and the dry run never throws."""
    patch = make_patch(replace("absent", "x"))
    problems = validate_ops(BASE_TEXT, patch, status=SectionStatus.DRAFT)
    assert len(problems) == 1
    assert problems[0].op_index == 0
    good = make_patch(replace("quality", "evidence"))
    assert validate_ops(BASE_TEXT, good, status=SectionStatus.DRAFT) == ()
    assert apply_ops(BASE_TEXT, good, status=SectionStatus.DRAFT).changed is True


def test_rejected_patch_leaves_the_body_untouched() -> None:
    """All-or-nothing: a later bad op cannot half-apply an earlier good one."""
    patch = make_patch(replace("quality", "evidence"), delete("absent"))
    with pytest.raises(PatchRejected):
        apply_ops(BASE_TEXT, patch, status=SectionStatus.DRAFT)
    assert validate_ops(BASE_TEXT, patch, status=SectionStatus.DRAFT)[0].op_index == 1


# ----------------------------------------------------------------------------- diffing


def test_diff_stats_counts_only_what_moved() -> None:
    """Pure insertion adds; pure deletion removes; a rewrite counts both sides."""
    assert diff_stats("abc", "abcX").chars_added == 1
    assert diff_stats("abc", "abcX").chars_removed == 0
    assert diff_stats("abcX", "abc").chars_removed == 1
    assert diff_stats("abc", "abc").touched_chars == 0
    replaced = diff_stats("abcde", "abXde")
    assert (replaced.chars_added, replaced.chars_removed) == (1, 1)
    assert replaced.touched_chars == 2


def test_diff_texts_renders_a_unified_diff() -> None:
    """The rendered diff is what the API and a reviewer's issue text quote."""
    rendered = diff_texts("one\ntwo\n", "one\ntwo\nthree\n", from_label="v1", to_label="v2")
    assert rendered.lines_added == 1
    assert rendered.lines_removed == 0
    assert rendered.unified.startswith("--- v1")
    assert "+++ v2" in rendered.unified
    assert "+three" in rendered.unified


def test_hash_is_stable_for_equal_content() -> None:
    """Same content, same hash - including across trivial whitespace differences."""
    first = apply_ops(
        BASE_TEXT, make_patch(replace("quality", "evidence")), status=SectionStatus.DRAFT
    )
    second = apply_ops(
        f"{BASE_TEXT}\n\n", make_patch(replace("quality", "evidence")), status=SectionStatus.DRAFT
    )
    assert first.hash == second.hash == content_hash(first.text)
