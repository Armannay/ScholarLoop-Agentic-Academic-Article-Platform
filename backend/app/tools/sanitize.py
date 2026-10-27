"""Best-effort sanitisation of retrieved text; the result is still untrusted data."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, ClassVar

from app.tools.base import ToolContext, ToolResult

__all__ = ["MAX_INPUT_CHARS", "SanitizeTool", "SanitizedText", "sanitize_untrusted_text"]

MAX_INPUT_CHARS = 200_000
# Leave room for the default ToolRegistry result cap and JSON envelope.
_MAX_RESULT_CHARS = 7_500
_INSTRUCTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,120}"
        r"\b(?:instructions?|prompts?|rules?|directives?|polic(?:y|ies)|safeguards?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:reveal|print|exfiltrate|send|leak|dump|steal)\b.{0,120}"
        r"\b(?:secrets?|api[ _-]?keys?|credentials?|passwords?|system\s+prompt)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:you are now|act as|pretend (?:that )?you are|assume the role of)\b"
        r".{0,80}\b(?:system|assistant|developer|administrator)\b",
        re.I,
    ),
    re.compile(r"^\s*(?:system|developer|assistant|user|tool)\s*:", re.I),
    re.compile(r"(?:\[/?(?:system|developer|assistant|user|inst)\]|<\|[^|]{1,40}\|>)", re.I),
    re.compile(
        r"\b(?:follow|obey|execute)\b.{0,80}\b(?:instructions?|commands?|directives?)\b", re.I
    ),
)


@dataclass(frozen=True)
class SanitizedText:
    """Plain text, truncation state, and the number of discarded instruction lines."""

    text: str
    truncated: bool
    removed_instruction_lines: int = 0


class _TextExtractor(HTMLParser):
    """Extract text and discard active or non-visible element contents."""

    _SUPPRESSED = frozenset({"script", "style", "template", "noscript", "iframe", "object"})
    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag.lower() in self._SUPPRESSED:
            self._suppressed += 1
        elif tag.lower() in self._BLOCKS and not self._suppressed:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SUPPRESSED and self._suppressed:
            self._suppressed -= 1
        elif tag.lower() in self._BLOCKS and not self._suppressed:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def _instruction_like(line: str) -> bool:
    # Normalize compatibility characters and remove invisible format controls before
    # matching (e.g. an instruction with a zero-width character inside "previous").
    comparable = unicodedata.normalize("NFKC", line)
    comparable = "".join(char for char in comparable if unicodedata.category(char) != "Cf")
    return any(pattern.search(comparable) for pattern in _INSTRUCTION_PATTERNS)


def sanitize_untrusted_text(value: str, *, max_chars: int = MAX_INPUT_CHARS) -> SanitizedText:
    """Strip markup and drop suspicious lines, without claiming an injection-proof boundary.

    The input is capped *before* parsing. Encoded markup is decoded and re-parsed a
    bounded number of times. All returned content must still be treated as untrusted.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    truncated = len(value) > max_chars
    text = value[:max_chars]
    try:
        for _ in range(3):
            extractor = _TextExtractor()
            extractor.feed(text)
            extractor.close()
            extracted = "".join(extractor.parts)
            if extracted == text:
                text = extracted
                break
            text = extracted
    except (ValueError, AssertionError):
        # Fail closed instead of returning raw script contents on malformed input.
        return SanitizedText(text="", truncated=True)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines: list[str] = []
    removed = 0
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if _instruction_like(line):
            removed += 1
        else:
            lines.append(line)
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars]
        truncated = True
    return SanitizedText(text=result, truncated=truncated, removed_instruction_lines=removed)


class SanitizeTool:
    """Return capped plain text and its unchanged provenance as untrusted data."""

    _NAME: ClassVar[str] = "sanitize_text"
    _DESCRIPTION: ClassVar[str] = (
        "Strip markup, drop instruction-like lines, and preserve the source provenance."
    )
    _TOKEN_COST: ClassVar[int] = 8
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_INPUT_CHARS},
            "provenance": {"type": "string", "minLength": 1, "maxLength": 2_000},
        },
        "required": ["text", "provenance"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.name = self._NAME
        self.description = self._DESCRIPTION
        self.token_cost = self._TOKEN_COST
        self.parameters = dict(self._PARAMETERS)

    async def run(self, arguments: Mapping[str, Any], *, context: ToolContext) -> ToolResult:
        _ = context
        provenance = str(arguments["provenance"])
        if not provenance.strip():
            return ToolResult(tool=self.name, ok=False, content="provenance must not be blank")
        cleaned = sanitize_untrusted_text(str(arguments["text"]))

        def encode(text: str, *, truncated: bool) -> str:
            return json.dumps(
                {
                    "text": text,
                    "provenance": provenance,
                    "truncated": truncated,
                    "removed_instruction_lines": cleaned.removed_instruction_lines,
                    "trust": "untrusted",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        content = encode(cleaned.text, truncated=cleaned.truncated)
        if len(content) <= _MAX_RESULT_CHARS:
            return ToolResult(tool=self.name, content=content, truncated=cleaned.truncated)
        if len(encode("", truncated=True)) > _MAX_RESULT_CHARS:
            return ToolResult(tool=self.name, ok=False, content="provenance exceeds result limit")
        # Account for JSON escaping as well as text length, so the registry does not
        # cut an otherwise valid JSON document in half at its default 8k limit.
        low, high = 0, len(cleaned.text)
        while low < high:
            middle = (low + high + 1) // 2
            if len(encode(cleaned.text[:middle], truncated=True)) <= _MAX_RESULT_CHARS:
                low = middle
            else:
                high = middle - 1
        return ToolResult(
            tool=self.name,
            content=encode(cleaned.text[:low], truncated=True),
            truncated=True,
        )
