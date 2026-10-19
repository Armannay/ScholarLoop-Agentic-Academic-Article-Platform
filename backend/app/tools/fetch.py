"""``fetch_url``: read a public web page as bounded, untrusted text.

The result is data, never instruction (HARD RULE 4): the tool happens to return a
string, but the prompt builder frames it with ``UntrustedToolResult.as_prompt_text``
before any model sees it.

Safety and frugality are enforced here, not delegated to the model:

* every hop of a redirect chain passes :func:`ensure_public_url` again;
* non-text content types (PDF, images, archives) are refused rather than decoded badly;
* HTML is reduced to readable text with a stdlib parser, then capped at ``max_chars``;
* transport failures become a small ``status`` envelope instead of an exception, so
  one dead link cannot stop an iteration.
"""

from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import urljoin

from app.core.logging import get_logger
from app.tools.arguments import optional_int, reject_unknown_arguments, require_string
from app.tools.errors import HostResolutionError, HTTPFetchError, ToolURLBlocked
from app.tools.http import (
    DEFAULT_TIMEOUT_S,
    HostResolver,
    HTTPClient,
    ensure_public_url,
)

__all__ = [
    "CONTENT_TYPES",
    "DEFAULT_MAX_CHARS",
    "MAX_CHARS",
    "MAX_REDIRECTS",
    "FetchUrlTool",
    "html_to_text",
]

logger = get_logger(__name__)

DEFAULT_MAX_CHARS: Final[int] = 12_000
MAX_CHARS: Final[int] = 40_000
MIN_CHARS: Final[int] = 500
MAX_REDIRECTS: Final[int] = 3
ERROR_EXCERPT_CHARS: Final[int] = 500

#: Media types the tool knows how to turn into prompt-safe text.
CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "application/json",
        "application/ld+json",
        "application/xml",
        "text/xml",
    }
)

_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
_SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas"}
)
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class _HTMLTextExtractor(HTMLParser):
    """Collect visible text and drop script/style payloads."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _SKIP_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str, *, max_chars: int) -> tuple[str, bool]:
    """Reduce HTML to whitespace-normalised text and report whether it was cut."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in parser.text.splitlines()]
    text = "\n".join(line for line in lines if line)
    truncated = len(text) > max_chars
    return text[:max_chars], truncated


class FetchUrlTool:
    """``fetch_url``: one public URL in, quoted text or an explicit refusal out."""

    name = "fetch_url"
    token_cost_hint = 3_000
    parameters: Final[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 8,
                "maxLength": 2000,
                "description": "Absolute public http(s) URL to read.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": MIN_CHARS,
                "maximum": MAX_CHARS,
                "default": DEFAULT_MAX_CHARS,
                "description": "Maximum characters of text to return.",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        http: HTTPClient,
        resolver: HostResolver,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._http = http
        self._resolver = resolver
        self._timeout_s = timeout_s

    async def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reject_unknown_arguments(arguments, self.parameters["properties"])
        url = require_string(arguments, "url", max_length=2000)
        max_chars = optional_int(
            arguments,
            "max_chars",
            default=DEFAULT_MAX_CHARS,
            minimum=MIN_CHARS,
            maximum=MAX_CHARS,
        )

        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                current = await ensure_public_url(current, resolver=self._resolver)
            except ToolURLBlocked as exc:
                # Log the reason, never the full URL: it is model-supplied.
                logger.warning("fetch_url blocked a URL at hop %s: %s", hop, exc)
                return {"status": "blocked", "reason": str(exc)}
            except HostResolutionError as exc:
                return {"status": "unavailable", "reason": "host_unresolved", "detail": str(exc)}

            try:
                response = await self._http.get(current, timeout_s=self._timeout_s)
            except HTTPFetchError as exc:
                return {"status": "unavailable", **exc.as_dict()}

            if response.status_code in _REDIRECT_STATUSES:
                if not response.location:
                    return {
                        "status": "http_error",
                        "reason": "redirect_without_location",
                        "http_status": response.status_code,
                    }
                if hop >= MAX_REDIRECTS:
                    return {"status": "unavailable", "reason": "too_many_redirects"}
                current = urljoin(current, response.location)
                continue

            return self._render(
                response.content_type,
                response.text,
                response.final_url,
                max_chars=max_chars,
                response_truncated=response.truncated,
                status_code=response.status_code,
            )

        return {"status": "unavailable", "reason": "too_many_redirects"}  # pragma: no cover

    @staticmethod
    def _render(
        content_type: str,
        text: str,
        final_url: str,
        *,
        max_chars: int,
        response_truncated: bool,
        status_code: int,
    ) -> dict[str, Any]:
        """Turn a response body into the tool envelope agents actually consume."""
        media_type = content_type or "text/plain"
        if status_code >= 400:
            return {
                "status": "http_error",
                "http_status": status_code,
                "url": final_url,
                "content_type": media_type,
                "text": text[:ERROR_EXCERPT_CHARS],
            }
        if media_type not in CONTENT_TYPES:
            return {
                "status": "unsupported_content_type",
                "url": final_url,
                "content_type": media_type,
            }

        if media_type in {"text/html", "application/xhtml+xml"}:
            body, cut = html_to_text(text, max_chars=max_chars)
        else:
            body, cut = text[:max_chars], len(text) > max_chars

        return {
            "status": "ok",
            "url": final_url,
            "content_type": media_type,
            "text": body,
            "truncated": cut or response_truncated or len(text) > max_chars,
            "source_chars": len(text),
        }
