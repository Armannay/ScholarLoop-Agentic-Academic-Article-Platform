"""Patch - how an agent changes an article without rewriting it.

Every op carries only the delta (HARD RULE 3). ``base_version`` is the section
version the agent built against; the applier rejects the patch if the section has
moved on, which is what keeps three parallel writers from clobbering each other.

Patch payloads are **byte-exact**. The applier matches ``old``/``anchor`` literally
and writes ``new``/``text`` verbatim, so the contract must not normalise them: a
paragraph break is ``"\\n\\n"``, a sentence separator can be ``" "``, and stripping
either would silently change the article (or make a legitimate op unmatchable).
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from app.contracts.common import Contract, NonEmptyStr, SectionId, Ulid, VersionedContract
from app.contracts.enums import PatchOpType

__all__ = ["FORBIDDEN_FIELDS", "REQUIRED_FIELDS", "Patch", "PatchOp"]

#: Per-op payload contract: which fields must be present, which must be absent.
REQUIRED_FIELDS: dict[PatchOpType, tuple[str, ...]] = {
    PatchOpType.REPLACE_TEXT: ("old", "new"),
    PatchOpType.INSERT_AFTER: ("anchor", "text"),
    PatchOpType.DELETE_TEXT: ("old",),
    PatchOpType.REPLACE_SECTION: ("text",),
}
FORBIDDEN_FIELDS: dict[PatchOpType, tuple[str, ...]] = {
    PatchOpType.REPLACE_TEXT: ("anchor", "text"),
    PatchOpType.INSERT_AFTER: ("old", "new"),
    PatchOpType.DELETE_TEXT: ("anchor", "new", "text"),
    PatchOpType.REPLACE_SECTION: ("anchor", "old", "new"),
}


class PatchOp(Contract):
    """A single edit. ``old`` is matched literally, so no regex escaping is needed.

    Unlike every other contract, payload text keeps its whitespace: ``" "`` and
    ``"\\n\\n"`` are meaningful deltas here, not formatting noise. The op's own
    required/forbidden-field rules still reject ``None`` and ``""``.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=False,
    )

    op: PatchOpType
    anchor: NonEmptyStr | None = Field(
        default=None, max_length=200, description="Literal text after which to insert."
    )
    old: str | None = Field(default=None, description="Literal text to replace or delete.")
    new: str | None = Field(default=None, description="Replacement text.")
    text: str | None = Field(default=None, description="Text to insert or the new section body.")

    @model_validator(mode="after")
    def _check_payload(self) -> PatchOp:
        required = REQUIRED_FIELDS[self.op]
        forbidden = FORBIDDEN_FIELDS[self.op]

        missing = [name for name in required if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(f"op '{self.op.value}' requires non-empty {missing}")

        illegal = [name for name in forbidden if getattr(self, name) is not None]
        if illegal:
            raise ValueError(f"op '{self.op.value}' must not carry {illegal}")

        if self.op is PatchOpType.REPLACE_TEXT and self.old == self.new:
            raise ValueError("replace_text with old == new is a no-op")
        return self

    @property
    def is_global(self) -> bool:
        """True when the op does not depend on where it lands (whole-section replace)."""
        return self.op is PatchOpType.REPLACE_SECTION


class Patch(VersionedContract):
    """An ordered list of ops against one section. Empty ``ops`` is invalid."""

    section_id: SectionId
    base_version: int = Field(
        ge=0, description="Section version this patch was authored against (0 = empty section)."
    )
    ops: list[PatchOp] = Field(min_length=1, max_length=200)
    patch_id: Ulid | None = Field(
        default=None,
        description="Optional id so a ChangeEntry can point at this patch (patch_ref).",
    )

    def op_counts(self) -> dict[str, int]:
        """Histogram of ops, for ledger summaries and tests."""
        counts: dict[str, int] = {}
        for op in self.ops:
            counts[op.op.value] = counts.get(op.op.value, 0) + 1
        return counts
