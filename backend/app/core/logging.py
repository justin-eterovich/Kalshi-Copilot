"""Logging setup with a hard guarantee: private keys never reach the logs."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<REDACTED:PRIVATE_KEY>",
    ),
    # Kalshi signature headers and any bearer-ish token.
    #
    # The optional quotes matter: headers are most often logged as a dict, and
    # `{'KALSHI-ACCESS-SIGNATURE': 'abc...'}` puts a quote between the name and
    # the colon. A pattern requiring the separator immediately after the name
    # matched the header line but not the repr of the same header — which is
    # the form it actually appears in. The value stops at a quote, comma or
    # brace so only the secret is consumed, not the rest of the dict.
    (
        re.compile(
            r"(KALSHI-ACCESS-SIGNATURE['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}\]]+", re.I
        ),
        r"\1<REDACTED>",
    ),
    (
        re.compile(
            r"(KALSHI-ACCESS-KEY['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}\]]+", re.I
        ),
        r"\1<REDACTED>",
    ),
    (re.compile(r"(sk-ant-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"), r"\1<REDACTED>"),
)


def scrub(text: str) -> str:
    """Strip anything that looks like a secret out of ``text``."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Applies :func:`scrub` to every record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Only scrub the message when it is the finished text. With ``args``
        # present it is a *format template*, and scrubbing can eat the `%s`
        # itself: `log.warning("KALSHI-ACCESS-KEY: %s", key)` became
        # "KALSHI-ACCESS-KEY: <REDACTED>" with one argument still queued, so
        # `getMessage()` raised "not all arguments converted" and the handler
        # dropped the record entirely. The secret did not leak — the line just
        # vanished, which is its own kind of failure in an audit trail.
        #
        # Nothing is lost by skipping it: the arguments are scrubbed below, and
        # `ScrubbingFormatter` scrubs the fully rendered line afterwards, so a
        # secret written into a literal template is still caught downstream.
        if isinstance(record.msg, str) and not record.args:
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: scrub(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    scrub(a) if isinstance(a, str) else a for a in record.args
                )
        return True


class ScrubbingFormatter(logging.Formatter):
    """A formatter whose *entire* output is scrubbed, tracebacks included.

    :class:`RedactingFilter` can only reach ``record.msg`` and ``record.args``.
    Exception text and stack info are rendered by the formatter, which runs
    **after** every filter — so ``log.exception(...)`` on an error carrying key
    material wrote it out verbatim, past the guard. Hard constraint #6 has no
    exception for tracebacks, and a traceback is exactly where an unexpected
    string ends up.

    Scrubbing the final line rather than each part is deliberate: it is the one
    place every path converges, so a future formatter field cannot bypass it.
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class JsonFormatter(ScrubbingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # Scrubbed after serialisation, so it covers every field including the
        # traceback. A PEM block survives JSON escaping as literal "\n"
        # separators, which the DOTALL pattern still spans.
        return scrub(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())

    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            ScrubbingFormatter(
                "%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty and rarely useful at INFO.
    for noisy in ("httpx", "httpcore", "websockets.client", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
