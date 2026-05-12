"""Unit tests for AAP behavioral Rule 2 — API key masking.

This module verifies the exact behavioral guarantee mandated by the Agent
Action Plan (verbatim):

    Rule 2: BLITZY_API_KEY, ANTHROPIC_API_KEY, and MISTRAL_API_KEY values
    MUST NOT appear in any log line, exception message, or traceback;
    verified by asserting each value absent from captured log output in
    tests.

The mechanism under test is :data:`vibe.core.observability.KEY_MASK_FILTER`
together with :func:`vibe.core.observability.register_sensitive`. The
filter is auto-installed on the ``"vibe"`` logger at module-import time and
also exposed for explicit attachment to any other handler (e.g., the
JSON-logging handler installed by ``configure_json_logging``).

The tests cover every Rule 2 surface:

1. ``record.getMessage()`` — masked when the key is embedded in the
   format string or supplied via positional ``args``.
2. ``record.exc_text`` and the formatted traceback — masked when an
   exception carries the key in its message.
3. ``str(exception)`` — masked when an exception is logged via
   ``logger.exception(...)`` and the formatted traceback is rendered into
   ``caplog.text``.

A precision check verifies that **unregistered** values are NOT touched
(the filter must operate as a deny-list, not a whitelist), and an
idempotency check verifies that registering the same value twice does not
double-mask.
"""

from __future__ import annotations

from collections.abc import Generator
import logging
from types import TracebackType
from typing import Any

import pytest

from vibe.core.observability import (
    KEY_MASK_FILTER,
    register_sensitive,
    set_correlation_id,
)

# ---------------------------------------------------------------------------
# Test data — the three explicitly-named provider keys from Rule 2
# ---------------------------------------------------------------------------

# Synthetic, deterministic API-key values. None of these are real keys;
# their only purpose is to provide unique substrings that the filter must
# scrub from every captured surface. Each value is intentionally long and
# distinctive so that a partial mismatch (or a missed substitution) would
# be easy to spot in test failure output.
PROVIDERS_AND_KEYS = [
    pytest.param(
        "blitzy", "BLITZY_API_KEY", "sk-blitzy-supersecret-1234567890", id="blitzy"
    ),
    pytest.param(
        "anthropic",
        "ANTHROPIC_API_KEY",
        "sk-ant-api01-supersecret-ABC123",
        id="anthropic",
    ),
    pytest.param(
        "mistral", "MISTRAL_API_KEY", "mst-api-supersecret-XYZ789", id="mistral"
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_sensitive_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the sensitive-values registry with a fresh, empty set per test.

    The masking registry is module-global (:data:`vibe.core.observability.\
_sensitive_values`) and is mutated by :func:`register_sensitive` calls in
    other tests, module-import code paths, and production code. Without
    explicit isolation, values from one test leak into the next and a
    "REGISTERED-VALUE not masked" assertion can pass spuriously because a
    value left over from a previous test is still in the registry.

    Using ``monkeypatch.setattr`` here swaps the module attribute for the
    duration of the test (yielding control to the test body) and then
    automatically restores the original ``set`` object on teardown. The
    helper functions inside ``observability`` (``register_sensitive``,
    ``_mask_string``) read ``_sensitive_values`` from the module globals on
    every call, so the swap propagates correctly to all code paths.
    """
    import vibe.core.observability as obs

    monkeypatch.setattr(obs, "_sensitive_values", set())


@pytest.fixture(autouse=True)
def _attach_mask_filter_to_caplog(
    caplog: pytest.LogCaptureFixture,
) -> Generator[None, None, None]:
    """Attach :data:`KEY_MASK_FILTER` to the ``caplog`` handler.

    Rationale: Python's :mod:`logging` module applies logger-level filters
    only at the originating logger; ancestor logger filters do NOT cascade
    to records emitted by descendant loggers when those records propagate
    upward to handlers attached at the root (which is where pytest's
    ``caplog`` handler lives). The ``KEY_MASK_FILTER`` is auto-installed
    on ``logging.getLogger("vibe")`` at import time, which is sufficient
    for records emitted directly on the ``"vibe"`` logger, but the AAP
    requires end-to-end masking for records emitted on descendant loggers
    such as ``"vibe.core.foo"`` or the ``"vibe.test"`` logger used by
    these tests.

    Attaching the filter directly to ``caplog.handler`` reproduces the
    production deployment pattern in
    :func:`vibe.core.observability.configure_json_logging` (which also
    attaches the filter to the configured handler), and ensures that
    ``caplog.records`` and ``caplog.text`` carry the masked output that
    Rule 2 asserts.
    """
    caplog.handler.addFilter(KEY_MASK_FILTER)
    yield
    caplog.handler.removeFilter(KEY_MASK_FILTER)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _vibe_test_logger() -> logging.Logger:
    """Return a child of the ``"vibe"`` logger used by every Rule 2 test.

    Using a child of the ``"vibe"`` namespace is the convention requested
    by the AAP (see the agent prompt for this file). Records emitted on
    this logger propagate up to the root, pass through pytest's
    ``caplog`` handler, and are masked by the filter that the
    ``_attach_mask_filter_to_caplog`` fixture has attached to that
    handler.
    """
    return logging.getLogger("vibe.test")


# ---------------------------------------------------------------------------
# Filter installation sanity check
# ---------------------------------------------------------------------------


class TestFilterInstallation:
    """Verify the AAP-mandated auto-installation of :data:`KEY_MASK_FILTER`."""

    def test_filter_is_attached_to_vibe_logger_at_import_time(self) -> None:
        """The filter must be installed on ``logging.getLogger("vibe")`` at import.

        Per the AAP, ``vibe/core/observability.py`` is responsible for
        ensuring that no module-import-time race exists between
        ``register_sensitive`` calls and log emission. The contract is
        that simply importing ``vibe.core.observability`` is sufficient
        to opt the entire ``"vibe"`` namespace into masking; downstream
        modules must not need to call any explicit setup function.
        """
        vibe_filters = logging.getLogger("vibe").filters
        assert KEY_MASK_FILTER in vibe_filters, (
            "KEY_MASK_FILTER must be auto-installed on the 'vibe' logger "
            "at module-import time per AAP Rule 2."
        )

    def test_register_sensitive_is_callable_with_string(self) -> None:
        """The public ``register_sensitive`` function accepts a string value.

        This guards against signature drift — any change that would
        require call sites in ``vibe.core.llm.api_key_prompt`` to adapt
        is a backwards-incompatible change that would break Rule 2's
        primary registration mechanism.
        """
        # Must not raise.
        register_sensitive("dummy-key-for-signature-check")


# ---------------------------------------------------------------------------
# Per-provider masking tests (parameterized over the three named keys)
# ---------------------------------------------------------------------------


class TestAPIKeyMasking:
    """Verify Rule 2 across all three named providers and all surfaces."""

    @pytest.mark.parametrize(("provider", "env_var", "key"), PROVIDERS_AND_KEYS)
    def test_api_key_value_absent_from_log_records(
        self, provider: str, env_var: str, key: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rule 2: the raw key value must not appear in any captured record.

        Exercises both message-template emission (``%s`` substitution via
        positional args) and inline-template emission (key interpolated
        into the format string by the caller).
        """
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive(key)
        logger = _vibe_test_logger()

        logger.info("got %s key: %s", provider, key)
        logger.info(f"inline {env_var} value is {key}")

        # No captured record's rendered message may contain the raw key.
        for record in caplog.records:
            rendered = record.getMessage()
            assert key not in rendered, (
                f"Rule 2 violated for {provider}: raw key value leaked into "
                f"record.getMessage() -> {rendered!r}"
            )

        # No captured surface (caplog.text aggregates the entire formatted
        # output, including any traceback text) may contain the raw key.
        assert key not in caplog.text, (
            f"Rule 2 violated for {provider}: raw key value leaked into "
            f"caplog.text -> {caplog.text!r}"
        )

        # And the mask sentinel must have been substituted in.
        assert "***" in caplog.text, (
            f"Filter did not substitute the mask sentinel for {provider}; "
            f"caplog.text={caplog.text!r}"
        )

    @pytest.mark.parametrize(("provider", "env_var", "key"), PROVIDERS_AND_KEYS)
    def test_api_key_absent_from_exception_message(
        self, provider: str, env_var: str, key: str
    ) -> None:
        """Rule 2: ``str(exception)`` MUST be scrubbed by the filter.

        The agent prompt mandates testing this surface by constructing a
        :class:`logging.LogRecord` carrying ``exc_info``, applying the
        filter, and asserting the rendered output via
        :meth:`logging.Formatter.format`. This unit-test path proves the
        filter mechanism itself works, independent of any logger
        configuration.
        """
        del env_var  # Documented as part of the test matrix; unused here.
        register_sensitive(key)

        try:
            raise RuntimeError(f"upstream {provider} failed with key {key}")
        except RuntimeError as exc:
            record = logging.LogRecord(
                name="vibe.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=0,
                msg="operation failed",
                args=(),
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        # Apply the filter as the logging machinery would.
        assert KEY_MASK_FILTER.filter(record) is True
        # Format the record — this exercises both the message and the
        # exception-text rendering paths.
        formatted = logging.Formatter("%(message)s").format(record)
        assert key not in formatted, (
            f"Rule 2 violated for {provider}: raw key value leaked into "
            f"the formatted record -> {formatted!r}"
        )
        # The original exception message MUST also be masked once the
        # filter has run, even if a caller inspects record.exc_text
        # directly.
        assert record.exc_text is not None
        assert key not in record.exc_text, (
            f"Rule 2 violated for {provider}: raw key value leaked into "
            f"record.exc_text -> {record.exc_text!r}"
        )
        assert "***" in record.exc_text

    @pytest.mark.parametrize(("provider", "env_var", "key"), PROVIDERS_AND_KEYS)
    def test_api_key_absent_from_traceback(
        self, provider: str, env_var: str, key: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rule 2: traceback text emitted via ``logger.exception`` is masked.

        This is the end-to-end variant of the previous test. The exception
        is logged through the standard ``logger.exception`` flow, the
        traceback is formatted by the logging machinery, and the result is
        captured by ``caplog``. Both the rendered ``caplog.text`` and the
        per-record ``record.exc_text`` are verified.
        """
        del env_var
        caplog.set_level(logging.ERROR, logger="vibe")
        register_sensitive(key)
        logger = _vibe_test_logger()

        try:
            raise RuntimeError(f"{provider} backend failed: {key}")
        except RuntimeError:
            logger.exception("operation failed during %s call", provider)

        # The aggregated log output (which includes traceback rendering)
        # must not contain the raw key.
        assert key not in caplog.text, (
            f"Rule 2 violated for {provider}: raw key value leaked into "
            f"caplog.text traceback -> {caplog.text!r}"
        )
        # The masked sentinel must appear somewhere in the traceback.
        assert "***" in caplog.text

        # Per-record verification: every record's exc_text (if any) is
        # scrubbed.
        for record in caplog.records:
            if record.exc_text is not None:
                assert key not in record.exc_text, (
                    f"Rule 2 violated for {provider}: raw key value leaked "
                    f"into record.exc_text -> {record.exc_text!r}"
                )

    @pytest.mark.parametrize(("provider", "env_var", "key"), PROVIDERS_AND_KEYS)
    def test_inline_key_in_format_string_masked(
        self, provider: str, env_var: str, key: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rule 2: a key embedded directly in the message text must be masked.

        Callers occasionally bypass the ``%s`` formatting protocol and
        build the message via an f-string. Rule 2 must hold for this path
        too — the filter targets ``record.msg`` (the format-string
        template), so a fully-rendered string still gets scrubbed.
        """
        del env_var
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive(key)
        logger = _vibe_test_logger()

        logger.info(f"{provider}: {key}")

        assert key not in caplog.text, (
            f"Rule 2 violated for {provider}: raw key value leaked into "
            f"inline format string -> {caplog.text!r}"
        )
        assert "***" in caplog.text

    @pytest.mark.parametrize(("provider", "env_var", "key"), PROVIDERS_AND_KEYS)
    def test_key_in_args_tuple_masked(
        self, provider: str, env_var: str, key: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rule 2: a key supplied as a printf-style argument must be masked.

        This is the canonical logging pattern — a static format string
        plus positional arguments — and is the primary path the filter
        must protect. The filter walks ``record.args`` recursively, so
        the key is scrubbed before any formatter renders the final
        string.
        """
        del env_var
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive(key)
        logger = _vibe_test_logger()

        logger.info("data: %s %s for %s", "alpha", key, provider)

        for record in caplog.records:
            rendered = record.getMessage()
            assert key not in rendered, (
                f"Rule 2 violated for {provider}: key leaked through "
                f"args tuple -> {rendered!r}"
            )
        assert key not in caplog.text
        assert "***" in caplog.text


# ---------------------------------------------------------------------------
# Multi-key and precision tests
# ---------------------------------------------------------------------------


class TestPrecisionAndEdgeCases:
    """Filter must be precise: mask only registered values, nothing else."""

    def test_multiple_registered_keys_all_masked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """All three provider keys must be masked when registered together.

        This is the realistic deployment scenario: an operator running
        a single session may have configured multiple providers (e.g.,
        Blitzy as the primary backend with Anthropic as a compaction
        fallback). Rule 2 mandates masking of every registered value
        independently of how many are present.
        """
        caplog.set_level(logging.INFO, logger="vibe")
        keys = [
            "sk-blitzy-supersecret-1234567890",
            "sk-ant-api01-supersecret-ABC123",
            "mst-api-supersecret-XYZ789",
        ]
        for key in keys:
            register_sensitive(key)

        logger = _vibe_test_logger()
        logger.info(
            "deployed providers: blitzy=%s, anthropic=%s, mistral=%s",
            keys[0],
            keys[1],
            keys[2],
        )

        # All three keys must be absent from caplog.text.
        for key in keys:
            assert key not in caplog.text, (
                f"Rule 2 violated: {key!r} leaked into caplog.text"
            )
        # The non-sensitive scaffolding text remains visible.
        assert "deployed providers" in caplog.text
        assert "blitzy=" in caplog.text
        assert "anthropic=" in caplog.text
        assert "mistral=" in caplog.text
        # Each occurrence is replaced — the mask should appear at least
        # three times (one per key).
        assert caplog.text.count("***") >= 3

    def test_unregistered_value_not_masked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The filter must be precise: unregistered values pass through unchanged.

        This is the critical invariant that prevents the filter from
        becoming an over-zealous redactor that hides legitimate operational
        data. Only values explicitly registered via
        :func:`register_sensitive` are scrubbed.
        """
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive("REGISTERED-VALUE")
        logger = _vibe_test_logger()

        logger.info("present: %s, absent: %s", "REGISTERED-VALUE", "OTHER-VALUE")

        assert "REGISTERED-VALUE" not in caplog.text, "Registered value was not masked"
        assert "OTHER-VALUE" in caplog.text, (
            "Unregistered value was incorrectly masked — filter is not precise"
        )
        assert "***" in caplog.text

    def test_empty_string_registration_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``register_sensitive("")`` must be a silent no-op.

        Registering an empty string would otherwise cause the filter to
        replace every zero-length gap between two characters with
        ``"***"``, completely corrupting log output. This test guards
        against that pathology — the module's ``register_sensitive``
        already short-circuits on falsy input, and this test pins the
        contract.
        """
        caplog.set_level(logging.INFO, logger="vibe")
        # Both kinds of falsy input that the ``register_sensitive``
        # short-circuit must handle.
        register_sensitive("")

        logger = _vibe_test_logger()
        logger.info("test message with no sensitive values")

        # The message must round-trip unchanged.
        assert "test message with no sensitive values" in caplog.text
        assert "***" not in caplog.text, (
            "Empty-string registration spuriously caused masking; the filter "
            "is not robust against this corner case"
        )

    def test_short_registered_value_still_masked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The filter masks values regardless of length, including short keys.

        Rule 2 mentions API keys (which are typically long), but the
        filter must operate on the registered substring without regard to
        length. This guards against any premature optimization that might
        skip short strings for performance reasons.
        """
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive("abc")
        logger = _vibe_test_logger()

        logger.info("hello abc world")

        # The short value is masked.
        assert "abc" not in caplog.text.split("hello ")[1].split(" world")[0]
        # And the surrounding non-sensitive text is preserved verbatim.
        assert "hello *** world" in caplog.text

    def test_register_sensitive_idempotent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Registering the same value twice must not cause double-masking.

        The internal registry is a :class:`set`, which inherently
        deduplicates. This test pins that contract so a future refactor
        (e.g., to a list) cannot regress it.
        """
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive("repeated-key")
        register_sensitive("repeated-key")

        logger = _vibe_test_logger()
        logger.info("payload contains repeated-key once")

        # Exactly one mask sentinel — not two.
        assert caplog.text.count("***") == 1, (
            f"Idempotency violated: expected 1 '***' substitution, got "
            f"{caplog.text.count('***')} in {caplog.text!r}"
        )
        assert "repeated-key" not in caplog.text

    def test_register_sensitive_none_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``register_sensitive(None)`` must be a silent no-op.

        The function accepts ``str | None`` per its signature. Passing
        ``None`` (e.g., when an env var read returns ``None``) must not
        raise and must not corrupt the registry.
        """
        caplog.set_level(logging.INFO, logger="vibe")
        register_sensitive(None)

        logger = _vibe_test_logger()
        logger.info("any message")

        assert "any message" in caplog.text
        assert "***" not in caplog.text


# ---------------------------------------------------------------------------
# Filter mechanism tests (direct invocation, no logger involved)
# ---------------------------------------------------------------------------


class TestFilterMechanism:
    """Direct invocation tests for the :data:`KEY_MASK_FILTER` object.

    These tests exercise the filter as a pure transformer on a manually
    constructed :class:`logging.LogRecord`, decoupling the masking logic
    from logger configuration. This proves that the filter itself behaves
    correctly regardless of where it is attached.
    """

    def _build_record(
        self,
        msg: str,
        args: tuple[Any, ...] = (),
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
        | None = None,
    ) -> logging.LogRecord:
        """Construct a minimal :class:`logging.LogRecord` for filter tests."""
        return logging.LogRecord(
            name="vibe.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=msg,
            args=args,
            exc_info=exc_info,
        )

    def test_filter_returns_true_and_does_not_drop_records(self) -> None:
        """The filter must always return True — masking does not gate emission.

        Rule 2 requires masking, not suppression. A record with a
        registered key must still be emitted; it must just have its
        sensitive content replaced.
        """
        register_sensitive("the-key")
        record = self._build_record("msg with the-key")

        assert KEY_MASK_FILTER.filter(record) is True

    def test_filter_mutates_msg_in_place(self) -> None:
        """The filter must mutate ``record.msg`` directly when it is a string."""
        register_sensitive("the-key")
        record = self._build_record("msg with the-key")

        KEY_MASK_FILTER.filter(record)

        assert record.msg == "msg with ***"
        assert "the-key" not in record.msg

    def test_filter_mutates_args_recursively(self) -> None:
        """The filter walks ``record.args`` recursively, masking nested values.

        Note on the args structure: Python's :meth:`logging.LogRecord.__init__`
        applies a special-case unwrap when ``args`` is a single-element tuple
        containing a non-empty :class:`~collections.abc.Mapping`; in that case
        ``record.args`` becomes the mapping itself rather than a tuple wrapping
        it. To exercise the recursive walk unambiguously we use a multi-element
        tuple containing a nested list — this preserves the tuple shape so the
        recursive descent through tuples → lists → strings is verified.
        """
        register_sensitive("nested-secret")
        # Two positional args so LogRecord does NOT unwrap the tuple; the
        # second arg is a nested list containing the sensitive value plus a
        # non-sensitive sibling.
        record = self._build_record(
            "context: %s %s", args=("plain", ["nested-secret", "other"])
        )

        KEY_MASK_FILTER.filter(record)

        # The args tuple is preserved as a tuple (no unwrap occurred), the
        # nested list still has two elements, and the sensitive element has
        # been replaced with the mask sentinel.
        assert isinstance(record.args, tuple)
        assert record.args[0] == "plain"
        assert record.args[1] == ["***", "other"]
        assert "nested-secret" not in record.getMessage()
        assert "***" in record.getMessage()

    def test_filter_handles_record_with_no_exc_info(self) -> None:
        """The filter is well-defined for records carrying no exception info.

        This is the common case (every non-error log call), so the filter
        must short-circuit gracefully without touching ``record.exc_text``
        when ``record.exc_info`` is ``None``.
        """
        register_sensitive("any-key")
        record = self._build_record("plain message")

        KEY_MASK_FILTER.filter(record)

        assert record.exc_text is None

    def test_filter_does_not_mutate_unrelated_attributes(self) -> None:
        """Standard LogRecord attributes (``levelname``, ``name``, ...) are preserved.

        The filter must touch only ``msg``, ``args``, ``exc_text``, and
        extras passed via ``extra={...}``. Standard diagnostic metadata
        (logger name, level, source location) must remain untouched so
        operators can still correlate masked records back to their call
        sites.
        """
        register_sensitive("name")  # Deliberately a substring of attribute values
        record = self._build_record("payload: %s", args=("name",))

        KEY_MASK_FILTER.filter(record)

        # These attributes must NOT be touched by the filter.
        assert record.name == "vibe.test"
        assert record.levelname == "INFO"
        # The args value WAS masked.
        assert record.args == ("***",)

    def test_filter_masks_extra_kwargs_on_record(self) -> None:
        """Extras supplied via ``logger.info("...", extra={"k": v})`` are masked.

        The implementation walks ``record.__dict__`` for non-standard
        attributes and masks any string value found. This is a critical
        guard for callers who use the structured-logging idiom of passing
        sensitive context via ``extra``.
        """
        register_sensitive("extra-secret")
        record = self._build_record("base message")
        # Simulate what ``logger.info(..., extra={"user_token": "extra-secret"})``
        # would produce on the LogRecord. The Python logging machinery
        # injects extra-dict entries directly into ``record.__dict__``, so
        # using ``setattr`` here is the most faithful reproduction of the
        # production code path.
        setattr(record, "user_token", "value=extra-secret")  # noqa: B010

        KEY_MASK_FILTER.filter(record)

        masked_value: str = record.__dict__["user_token"]
        assert "extra-secret" not in masked_value
        assert masked_value == "value=***"

    def test_filter_handles_exception_via_record_construction(self) -> None:
        """Construct a record with ``exc_info`` and verify the filter formats it.

        This mirrors the path described in the agent prompt: the filter
        formats ``exc_info`` lazily into ``exc_text`` (so the formatter's
        own rendering does not leak the raw key), and the resulting
        ``exc_text`` is scrubbed. Verifying via :class:`logging.Formatter`
        proves the contract holds end-to-end for handlers that have not
        been configured with the filter directly.
        """
        register_sensitive("traceback-secret")

        try:
            raise ValueError("boom: traceback-secret leaked")
        except ValueError as exc:
            record = self._build_record(
                "operation failed", exc_info=(type(exc), exc, exc.__traceback__)
            )

        KEY_MASK_FILTER.filter(record)
        formatted = logging.Formatter("%(message)s").format(record)

        assert "traceback-secret" not in formatted
        assert "***" in formatted


# ---------------------------------------------------------------------------
# Correlation ID interaction
# ---------------------------------------------------------------------------


class TestCorrelationIdInteraction:
    """The mask filter must not interfere with correlation-id propagation.

    AAP §0.6.1 Group 1 establishes that the observability module exposes
    both a sensitive-value masking surface AND a per-session correlation
    ID context. The two concerns are orthogonal but share the same
    module; this test class pins the boundary.
    """

    def test_set_correlation_id_returns_token(self) -> None:
        """``set_correlation_id`` returns a ContextVar.Token suitable for reset."""
        from contextvars import Token

        token = set_correlation_id("session-12345")
        try:
            assert isinstance(token, Token)
        finally:
            # Be a good citizen: reset the contextvar so leakage to
            # subsequent tests is impossible.
            from vibe.core.observability import correlation_id

            correlation_id.reset(token)

    def test_filter_does_not_remove_correlation_id_from_context(self) -> None:
        """The mask filter must NOT touch the correlation_id contextvar."""
        from vibe.core.observability import correlation_id

        token = set_correlation_id("session-correlation-id")
        try:
            register_sensitive("any-secret")
            record = logging.LogRecord(
                name="vibe.test",
                level=logging.INFO,
                pathname=__file__,
                lineno=0,
                msg="payload: any-secret",
                args=(),
                exc_info=None,
            )
            KEY_MASK_FILTER.filter(record)

            # The contextvar is untouched.
            assert correlation_id.get() == "session-correlation-id"
            # But the record was masked.
            assert "any-secret" not in record.msg
            assert "***" in record.msg
        finally:
            correlation_id.reset(token)
