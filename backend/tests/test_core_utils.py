"""ULIDs, hashing, redaction and logging: the primitives everything else assumes."""

from __future__ import annotations

import logging
import re

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.common import Ulid, word_count
from app.core.clock import to_utc, utc_from_epoch, utc_now
from app.core.hashing import content_hash, hash_matches, normalise_text
from app.core.ids import UlidFactory, is_ulid, new_ulid, ulid_timestamp_ms
from app.core.logging import RedactionFilter, get_logger
from app.core.redaction import PLACEHOLDER_MARKERS, find_secrets, redact
from tests.factories import sample_ulid


# ---------------------------------------------------------------------------- ULIDs
def test_new_ulid_is_26_crockford_characters() -> None:
    """The id format the ledger and every contract depend on."""
    value = new_ulid()
    assert len(value) == 26
    assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", value)
    assert is_ulid(value)


def test_ulid_encodes_its_timestamp() -> None:
    """Ids sort by creation time, which is what makes the ledger cheap to scan."""
    value = new_ulid(timestamp_ms=1_700_000_000_000, entropy=bytes(10))
    assert ulid_timestamp_ms(value) == 1_700_000_000_000


def test_ulid_factory_is_monotonic_within_a_millisecond() -> None:
    """A burst of ids in one millisecond stays unique *and* ordered."""
    factory = UlidFactory(timestamp_ms=1_700_000_000_000, entropy=bytes(10))
    ids = [factory() for _ in range(5)]
    assert len(set(ids)) == 5
    assert ids == sorted(ids)
    assert all(is_ulid(value) for value in ids)


def test_ulid_factory_is_reproducible() -> None:
    """Deterministic ids keep test failures diffable."""
    first = UlidFactory(timestamp_ms=1_700_000_000_000, entropy=bytes(10))
    second = UlidFactory(timestamp_ms=1_700_000_000_000, entropy=bytes(10))
    for _ in range(3):
        assert first() == second()


def test_ulid_entropy_must_be_80_bits() -> None:
    """The random component is exactly 10 bytes."""
    with pytest.raises(ValueError, match="10 bytes"):
        new_ulid(entropy=b"short")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-ulid",
        "01J0M4Z0PR8Y3Z1YV4S6M7ABC",  # 25 chars
        "81J0M4Z0PR8Y3Z1YV4S6M7ABCD",  # first char above 7: 48-bit time overflow
        "01J0M4Z0PR8Y3Z1YV4S6M7ABC!",  # illegal alphabet character
        None,
        7,
    ],
)
def test_is_ulid_rejects_bad_values(value: object) -> None:
    """Validation is strict - an id that is not a ULID is not an id."""
    assert is_ulid(value) is False


def test_ulid_contract_type_rejects_a_non_ulid() -> None:
    """The annotated type used inside contracts enforces the same thing."""

    class Holder(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: Ulid

    assert Holder(id=sample_ulid()).id == sample_ulid()
    with pytest.raises(ValidationError, match="ULID"):
        Holder(id="introduction")


# ---------------------------------------------------------------------------- hashing
def test_content_hash_is_prefixed_and_stable() -> None:
    """One spelling (``sha256:<hex>``) makes hash comparison trivial."""
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("hello world")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash("hello"))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("line\r\n", "line\n"),  # CRLF vs LF
        ("e\u0301", "\u00e9"),  # NFD vs NFC
        ("text   \n", "text\n"),  # trailing whitespace
        ("\ntext\n", "text"),  # leading/trailing blank lines
    ],
)
def test_normalisation_ignores_trivial_differences(left: str, right: str) -> None:
    """Re-encoding must not invalidate a patch; reflowing must."""
    assert content_hash(left) == content_hash(right)


def test_normalisation_keeps_interior_whitespace() -> None:
    """Changing a paragraph's internal spacing is a real edit."""
    assert normalise_text("a  b") == "a  b"
    assert content_hash("a  b") != content_hash("a b")


def test_hash_matches_rejects_a_misspelled_hash() -> None:
    """A hash that is not sha256-shaped never matches, even against itself."""
    assert hash_matches(content_hash("x"), content_hash("x"))
    assert not hash_matches("deadbeef", "deadbeef")


# -------------------------------------------------------------------------- text utils
@pytest.mark.parametrize(
    ("text", "expected"),
    [("", 0), ("one", 1), ("one two", 2), ("one\ntwo\tthree", 3), ("   ", 0)],
)
def test_word_count(text: str, expected: int) -> None:
    """The single word-count definition used by the snippet and summary caps."""
    assert word_count(text) == expected


# ------------------------------------------------------------------------- timestamps
def test_clock_is_utc_and_normalises_naive_input() -> None:
    """Naive datetimes are read as UTC rather than rejected, and never as local time."""
    now = utc_now()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 0
    assert to_utc(utc_from_epoch(0)).year == 1970
    naive = utc_now().replace(tzinfo=None)
    assert to_utc(naive).tzinfo is not None


# -------------------------------------------------------------------------- redaction
def test_secret_shapes_are_detected() -> None:
    """HARD RULE 2's detector: the four real key shapes we care about."""
    anthropic = "sk-ant-" + "a1B2" * 10
    openai = "sk-" + "B" * 40
    aws = "AKIA" + "C" * 16
    github = "ghp_" + "d" * 40

    for sample, label in (
        (anthropic, "anthropic key"),
        (openai, "openai key"),
        (aws, "aws access key"),
        (github, "github token"),
    ):
        names = [name for name, _ in find_secrets(sample)]
        assert label in names, f"{label} not detected in {label}"


def test_placeholders_are_not_secrets() -> None:
    """The examples in .env.example and the docs must stay legitimate."""
    for marker in PLACEHOLDER_MARKERS:
        value = f"{marker}1234567890"
        assert find_secrets(f'api_key = "{value}"') == [], f"{marker} was treated as a secret"


def test_redact_rewrites_only_the_secret() -> None:
    """Redaction keeps the surrounding sentence readable."""
    text = f"using key sk-ant-{'x1Y2' * 6} for the call"
    redacted = redact(text)
    assert "sk-ant-" not in redacted
    assert redacted.startswith("using key ") and redacted.endswith(" for the call")


def test_logging_filter_redacts_message_and_exception_text() -> None:
    """A key reaching a log call - from anywhere - never reaches the handler."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"provider call with key {'sk-ant-' + 'z9Y8' * 6}",
        args=(),
        exc_info=None,
    )
    record.exc_text = f"Traceback: sk-ant-{'z9Y8' * 6}"
    assert RedactionFilter().filter(record) is True
    assert "sk-ant-" not in record.getMessage()
    assert record.exc_text is not None and "sk-ant-" not in record.exc_text


def test_logger_is_usable_without_setup() -> None:
    """Importing a module and logging must never raise."""
    get_logger("scholarloop.test").info("hello %s", "world")
