"""Sanitise retrieved text while preserving its provenance as untrusted data."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, ClassVar

from app.tools.base import ToolContext, ToolResult

__all__ = ["MAX_INPUT_CHARS", "SanitizeTool", "SanitizedText", "sanitize_untrusted_text"]

MAX_INPUT_CHARS = 200_000
_INSTRUCTION_PATTERNS = (
    re.compile(
        r"\b(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+instructions\b",
        re.I,
    ),
    re.compile(
        r"\b(reveal|print|exfiltrate)\s+(the\s+)?(secret|api\s+key|credential)s?\b",
        re.I,
    ),
)


@dataclass(frozen=True)
class SanitizedText:
    """Sanitized text plus explicit truncation state; provenance stays external."""

    text: str
    truncated: bool


class _TextExtractor(HTMLParser):
    """Extract visible text and discard script/style/template contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._suppressed += 1
        elif (
            tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"} and not self._suppressed
        ):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "noscript"} and self._suppressed:
            self._suppressed -= 1
        elif tag.lower() in {"p", "div", "li", "tr"} and not self._suppressed:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def sanitize_untrusted_text(value: str, *, max_chars: int = MAX_INPUT_CHARS) -> SanitizedText:
    """Remove markup/control characters and flag instruction-like phrases as quoted data."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    truncated = len(value) > max_chars
    bounded = value[:max_chars]
    text = bounded
    try:
        for _ in range(2):
            extractor = _TextExtractor()
            extractor.feed(text)
            extractor.close()
            extracted = "".join(extractor.parts)
            if extracted == text or "<" not in extracted:
                text = extracted
                break
            text = extracted
    except Exception:
        # HTMLParser is intentionally best-effort: malformed external HTML remains data.
        text = re.sub(r"<[^>]{0,2_000}>", " ", bounded)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n", text).strip()
    for pattern in _INSTRUCTION_PATTERNS:
        text = pattern.sub(lambda match: f"[quoted untrusted text: {match.group(0)}]", text)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return SanitizedText(text=text, truncated=truncated)


class SanitizeTool:
    """Tool form of the HTML/text sanitizer. Provenance is returned unchanged."""

    _NAME: ClassVar[str] = "sanitize_text"
    _DESCRIPTION: ClassVar[str] = (
        "Strip markup from retrieved text, preserve provenance, and mark instruction-like text."
    )
    _TOKEN_COST: ClassVar[int] = 8
    _PARAMETERS: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_INPUT_CHARS},
            "provenance": {"type": "string", "maxLength": 2_000},
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
        cleaned = sanitize_untrusted_text(str(arguments["text"]))
        payload = {
            "text": cleaned.text,
            "provenance": str(arguments["provenance"]),
            "truncated": cleaned.truncated,
            "trust": "untrusted",
        }
        return ToolResult(
            tool=self.name,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            truncated=cleaned.truncated,
        )
