"""Tests for secret redaction in the logs.

Hard constraint #6: the RSA key is mounted read-only and **never logged**.
That guarantee was enforced by five regexes and nothing else — no test
asserted that any of them fired.

Two properties matter and they are enforced in different places:

- ``RedactingFilter`` reaches ``record.msg`` and ``record.args``. It runs
  before the formatter, so it is what protects a handler that was configured
  without a scrubbing formatter.
- ``ScrubbingFormatter`` scrubs the **final rendered line**. That is the one
  that survives ``%``-style lazy formatting, which is how this codebase logs:
  in ``log.warning("KALSHI-ACCESS-KEY: %s", key)`` neither the template nor
  the argument matches a pattern on its own — the secret only becomes
  recognisable after substitution, which happens inside the formatter, after
  every filter has run.

It also covers tracebacks. ``formatException`` output is rendered by the
formatter, so a filter can never reach it, and a traceback is exactly where an
unexpected string ends up.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import (
    JsonFormatter,
    RedactingFilter,
    ScrubbingFormatter,
    scrub,
)

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAxSuperSecretKeyMaterialGoesHere0123456789\n"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/==\n"
    "-----END RSA PRIVATE KEY-----"
)
SIGNATURE = "cU9hZ2xJdGVyYXRvclNpZ25hdHVyZUJhc2U2NA=="
ACCESS_KEY = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
ANTHROPIC_KEY = "sk-ant-api03-DEADBEEFdeadbeefDEADBEEFdeadbeef"


def record(msg: str, *args: object, **kwargs: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="app.kalshi.auth",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=kwargs.get("exc_info"),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The patterns themselves
# ---------------------------------------------------------------------------


class TestScrub:
    def test_a_pem_block_is_removed_whole(self) -> None:
        """Not just the header line. The key material is the part that
        matters, and it spans newlines — hence DOTALL."""
        out = scrub(f"loaded key: {PEM}")
        assert "MIIEowIBAAKCAQEA" not in out
        assert "SuperSecretKeyMaterial" not in out
        assert "<REDACTED:PRIVATE_KEY>" in out

    def test_a_pem_block_embedded_in_other_text_is_removed(self) -> None:
        out = scrub(f"before {PEM} after")
        assert out.startswith("before ")
        assert out.endswith(" after")
        assert "MIIEowIBAAKCAQEA" not in out

    @pytest.mark.parametrize("sep", [":", "=", ": ", " = "])
    def test_the_signature_header_is_redacted(self, sep: str) -> None:
        out = scrub(f"KALSHI-ACCESS-SIGNATURE{sep}{SIGNATURE}")
        assert SIGNATURE not in out
        assert "<REDACTED>" in out

    @pytest.mark.parametrize("sep", [":", "=", ": ", " = "])
    def test_the_access_key_header_is_redacted(self, sep: str) -> None:
        out = scrub(f"KALSHI-ACCESS-KEY{sep}{ACCESS_KEY}")
        assert ACCESS_KEY not in out
        assert "<REDACTED>" in out

    def test_the_headers_are_matched_case_insensitively(self) -> None:
        """Header case is not something we control on the way into a log."""
        assert SIGNATURE not in scrub(f"kalshi-access-signature: {SIGNATURE}")
        assert ACCESS_KEY not in scrub(f"Kalshi-Access-Key: {ACCESS_KEY}")

    def test_an_anthropic_key_keeps_only_its_prefix(self) -> None:
        """Enough to identify which key leaked, not enough to use it."""
        out = scrub(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}")
        assert ANTHROPIC_KEY not in out
        assert "DEADBEEF" not in out
        assert "sk-ant-" in out

    def test_a_header_inside_a_json_body_is_still_caught(self) -> None:
        body = json.dumps({"KALSHI-ACCESS-SIGNATURE": SIGNATURE})
        assert SIGNATURE not in scrub(body)

    def test_ordinary_text_is_left_alone(self) -> None:
        """Over-redaction makes logs useless, which is its own failure."""
        text = "placing demo order: buy 10 yes KXMLB-26-HOU @ 0.5600 (bid)"
        assert scrub(text) == text

    def test_scrubbing_is_idempotent(self) -> None:
        once = scrub(f"KALSHI-ACCESS-KEY: {ACCESS_KEY}")
        assert scrub(once) == once


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


class TestRedactingFilter:
    def test_it_scrubs_the_message(self) -> None:
        rec = record(f"headers: KALSHI-ACCESS-SIGNATURE: {SIGNATURE}")
        assert RedactingFilter().filter(rec) is True
        assert SIGNATURE not in rec.getMessage()

    def test_it_scrubs_string_arguments(self) -> None:
        """A secret passed as ``%s`` is a string argument, not part of the
        template."""
        rec = record("auth failed for %s", f"KALSHI-ACCESS-KEY: {ACCESS_KEY}")
        RedactingFilter().filter(rec)
        assert ACCESS_KEY not in rec.getMessage()

    def test_it_scrubs_dict_arguments(self) -> None:
        rec = record("auth failed for %(who)s", {"who": f"key={PEM}"})
        RedactingFilter().filter(rec)
        assert "MIIEowIBAAKCAQEA" not in rec.getMessage()

    def test_it_leaves_non_string_arguments_alone(self) -> None:
        """Scrubbing an int would raise; the filter must not touch them."""
        rec = record("placed %d contracts at %s", 10, "0.5600")
        assert RedactingFilter().filter(rec) is True
        assert rec.getMessage() == "placed 10 contracts at 0.5600"

    def test_it_never_drops_a_record(self) -> None:
        """Redaction is not suppression: a scrubbed log line still has to be
        emitted, or the guard becomes a silent hole in the audit trail."""
        assert RedactingFilter().filter(record("anything")) is True


# ---------------------------------------------------------------------------
# The formatters — the half that survives lazy formatting
# ---------------------------------------------------------------------------


class TestLazyFormatting:
    """``log.warning("...: %s", exc)`` is how this codebase logs.

    ``RedactingFilter`` only scrubs arguments that are already ``str`` — it
    cannot scrub what it cannot see, and it must not call ``scrub`` on an int
    or a dict. The substitution that turns an exception object into text
    happens inside the formatter, after every filter has run. So for a
    non-string argument the formatter's scrub of the **final rendered line** is
    the only guard there is.
    """

    @staticmethod
    def _record_carrying_a_secret_exception() -> logging.LogRecord:
        exc = RuntimeError(f"rejected request with KALSHI-ACCESS-KEY: {ACCESS_KEY}")
        return record("could not sign request: %s", exc)

    def test_the_filter_cannot_reach_a_non_string_argument(self) -> None:
        """Stated as a fact about the design, not a defect to fix here.

        This is exactly why the formatter has to scrub as well.
        """
        rec = self._record_carrying_a_secret_exception()
        RedactingFilter().filter(rec)
        assert ACCESS_KEY in rec.getMessage()

    def test_the_console_formatter_catches_it(self) -> None:
        rec = self._record_carrying_a_secret_exception()
        RedactingFilter().filter(rec)
        out = ScrubbingFormatter("%(message)s").format(rec)
        assert ACCESS_KEY not in out
        assert "<REDACTED>" in out

    def test_the_json_formatter_catches_it(self) -> None:
        rec = self._record_carrying_a_secret_exception()
        RedactingFilter().filter(rec)
        out = JsonFormatter().format(rec)
        assert ACCESS_KEY not in out
        assert "<REDACTED>" in json.loads(out)["msg"]

    def test_a_pem_passed_as_a_string_argument_is_caught_by_both(self) -> None:
        for formatter in (ScrubbingFormatter("%(message)s"), JsonFormatter()):
            rec = record("could not read key %s", PEM)
            RedactingFilter().filter(rec)
            out = formatter.format(rec)
            assert "MIIEowIBAAKCAQEA" not in out, type(formatter).__name__

    def test_the_formatter_alone_is_enough(self) -> None:
        """A handler configured without the filter is still safe.

        Belt and braces on purpose: the two guards cover different halves and
        neither is the only one.
        """
        rec = record("could not read key %s", PEM)
        out = ScrubbingFormatter("%(message)s").format(rec)
        assert "MIIEowIBAAKCAQEA" not in out


class TestTracebacks:
    """A filter can never reach ``formatException`` output.

    Exception text is rendered by the formatter, which runs after every
    filter, so ``log.exception(...)`` on an error carrying key material wrote
    it out verbatim, past the guard. Hard constraint #6 has no exception for
    tracebacks, and a traceback is exactly where an unexpected string ends up.
    """

    @staticmethod
    def _exc_record() -> logging.LogRecord:
        try:
            raise ValueError(f"could not parse {PEM}")
        except ValueError:
            import sys

            return record("signing failed", exc_info=sys.exc_info())

    def test_the_console_formatter_scrubs_the_traceback(self) -> None:
        out = ScrubbingFormatter("%(message)s").format(self._exc_record())
        assert "Traceback" in out  # the traceback is still there
        assert "MIIEowIBAAKCAQEA" not in out
        assert "<REDACTED:PRIVATE_KEY>" in out

    def test_the_json_formatter_scrubs_the_traceback(self) -> None:
        out = JsonFormatter().format(self._exc_record())
        assert "MIIEowIBAAKCAQEA" not in out
        payload = json.loads(out)
        assert "exc" in payload
        assert "MIIEowIBAAKCAQEA" not in payload["exc"]

    def test_a_pem_survives_json_escaping_and_is_still_matched(self) -> None:
        """``json.dumps`` turns the PEM's newlines into literal ``\\n``
        sequences. The DOTALL pattern still spans them — but it is the sort of
        thing that stops being true if the escaping changes.

        Deliberately without the filter, so it is the formatter's scrub of the
        serialised payload doing the work.
        """
        rec = record("key was %s", PEM)
        out = JsonFormatter().format(rec)
        assert "BEGIN RSA PRIVATE KEY" not in out
        assert "MIIEowIBAAKCAQEA" not in out


class TestConfiguration:
    def test_the_console_handler_gets_both_guards(self) -> None:
        """Filter *and* scrubbing formatter. Either alone leaves a hole."""
        from app.core.logging import configure_logging

        root = logging.getLogger()
        original = list(root.handlers)
        original_level = root.level
        try:
            configure_logging(level="INFO", fmt="console")
            handler = logging.getLogger().handlers[0]
            assert any(isinstance(f, RedactingFilter) for f in handler.filters)
            assert isinstance(handler.formatter, ScrubbingFormatter)
        finally:
            root.handlers[:] = original
            root.setLevel(original_level)

    def test_the_json_handler_gets_both_guards(self) -> None:
        from app.core.logging import configure_logging

        root = logging.getLogger()
        original = list(root.handlers)
        original_level = root.level
        try:
            configure_logging(level="INFO", fmt="json")
            handler = logging.getLogger().handlers[0]
            assert any(isinstance(f, RedactingFilter) for f in handler.filters)
            assert isinstance(handler.formatter, JsonFormatter)
        finally:
            root.handlers[:] = original
            root.setLevel(original_level)

    def test_a_secret_does_not_reach_a_stream_end_to_end(self) -> None:
        """The whole path: logger -> filter -> formatter -> stream."""
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter())
        handler.setFormatter(ScrubbingFormatter("%(message)s"))

        log = logging.getLogger("test.redaction.endtoend")
        log.propagate = False
        log.setLevel(logging.INFO)
        log.handlers[:] = [handler]

        log.info("KALSHI-ACCESS-KEY: %s", ACCESS_KEY)
        log.info("loaded %s", PEM)

        written = stream.getvalue()
        assert ACCESS_KEY not in written
        assert "MIIEowIBAAKCAQEA" not in written
