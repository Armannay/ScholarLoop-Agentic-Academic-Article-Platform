"""Logging setup with a non-negotiable redaction filter.

Every handler installed by :func:`configure_logging` carries
:class:`RedactionFilter`, so a key that reaches a log call - by accident, from a
third-party library, or from a provider error body - is rewritten to ``[REDACTED]``
before it is written (HARD RULE 2). The filter is attached to *handlers*, not to the
logger, so it cannot be bypassed by a module that uses ``logging.getLogger`` directly.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

from app.core.redaction import redact

__all__ = ["RedactionFilter", "configure_logging", "get_logger"]

_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s :: %(message)s"
_CONFIGURED: Final[str] = "scholarloop_configured"


class RedactionFilter(logging.Filter):
    """Rewrites secret-shaped strings in the message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record (never drop a log line)."""
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive: a broken record must not crash
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        # ``exc_text`` is filled in by the formatter *after* filters run, so a key
        # inside an exception message would otherwise escape. Render and redact now.
        if record.exc_info is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure_logging(level: str = "INFO", *, stream: object | None = None) -> None:
    """Configure root logging once, redaction always on.

    Idempotent: repeated calls only adjust the level, so importing the app twice (tests,
    reloads) does not duplicate handlers.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in root.handlers:
        if getattr(handler, _CONFIGURED, False):
            handler.setLevel(level.upper())
            return

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(RedactionFilter())
    setattr(handler, _CONFIGURED, True)
    root.handlers = [handler]

    # httpx is chatty at INFO and would drown the loop's own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Named logger; redaction is applied by the handler, not here."""
    return logging.getLogger(name)
