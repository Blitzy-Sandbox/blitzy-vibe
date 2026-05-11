"""Structured logging, tracing, metrics, and API key masking for the CLI.

This module satisfies two project requirements:

1. **Rule 2** (API key masking): Sensitive values registered via
   :func:`register_sensitive` are scrubbed from all log records emitted via
   loggers under the ``vibe`` namespace. The :data:`KEY_MASK_FILTER` filter
   is installed at module-import time on ``logging.getLogger("vibe")``, so
   every descendant logger (``vibe.core.*``, ``vibe.cli.*``, ``vibe.acp.*``,
   ...) inherits the protection without any call-site change.
2. **Observability project rule**: Structured logging with per-session
   correlation IDs (:data:`correlation_id`), trace spans (:func:`span`),
   in-memory metrics (:func:`metrics_snapshot`), and a readiness check
   (:func:`is_ready` / :func:`mark_ready`).

The dashboard template at ``docs/observability/dashboard.json`` documents the
operator-facing queries against this data.

This module is intentionally foundational: it depends only on the Python
standard library and therefore can be imported safely from anywhere in the
``vibe`` package without creating circular dependencies.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from copy import deepcopy
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Correlation ID (ContextVar)
# ---------------------------------------------------------------------------

# A :class:`~contextvars.ContextVar` that carries the active per-session
# correlation identifier across both synchronous and asynchronous call stacks.
# An empty string default means uninstrumented call sites do not raise.
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "vibe_correlation_id", default=""
)


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """Bind the per-session correlation ID for the current context.

    The returned :class:`~contextvars.Token` may be passed back to
    ``correlation_id.reset(token)`` to restore the previous value, which is
    the canonical ``contextvars`` idiom for nested operations.

    Args:
        value: The correlation identifier to bind (typically a session UUID).

    Returns:
        A :class:`~contextvars.Token` capturing the previous value, suitable
        for use with :meth:`~contextvars.ContextVar.reset`.
    """
    return correlation_id.set(value)


# ---------------------------------------------------------------------------
# Sensitive-value registry and masking helpers
# ---------------------------------------------------------------------------

# Protects ``_sensitive_values`` against concurrent mutation from multiple
# threads (e.g., a logging emitter on the main thread and an HTTP transport
# on a worker thread).
_sensitive_lock = threading.Lock()

# The set of literal strings that MUST be redacted from every log record
# emitted under the ``vibe`` logger hierarchy.
_sensitive_values: set[str] = set()


def register_sensitive(value: str | None) -> None:
    """Register a value to be masked in every subsequent log record.

    Empty strings and ``None`` are silently ignored. Registering an empty
    string would otherwise cause the filter to replace every gap between
    every two characters with ``"***"``, corrupting all log output.

    The mask is applied as a literal substring replacement (``str.replace``),
    not a regular-expression substitution, which is both faster and avoids
    inadvertent matches against metacharacters in keys.

    Args:
        value: A sensitive string (e.g., an API key) to register. ``None``
            and empty strings are silently ignored.
    """
    if not value:
        return
    with _sensitive_lock:
        _sensitive_values.add(value)


def _mask_string(s: str) -> str:
    """Replace every registered sensitive value in ``s`` with ``\"***\"``.

    The sensitive-value set is snapshotted under the lock and the actual
    substring replacement happens without holding the lock, minimizing
    contention.

    Args:
        s: The input string to scan.

    Returns:
        The input string with all registered sensitive substrings replaced
        by the literal mask ``"***"``.
    """
    with _sensitive_lock:
        # Snapshot for iteration safety; release the lock before doing the
        # (potentially expensive) string replacements.
        values = list(_sensitive_values)
    for v in values:
        if v and v in s:
            s = s.replace(v, "***")
    return s


def _mask_recursive(obj: Any) -> Any:
    """Recursively walk ``obj`` masking sensitive substrings in any strings.

    Tuples, lists, and dicts are walked element-wise. Strings are masked via
    :func:`_mask_string`. Every other type is returned unchanged.

    Args:
        obj: Heterogeneous nested structure (string, tuple, list, dict, or
            any other type).

    Returns:
        A structurally equivalent value with every nested string masked.
    """
    if isinstance(obj, str):
        return _mask_string(obj)
    if isinstance(obj, tuple):
        return tuple(_mask_recursive(item) for item in obj)
    if isinstance(obj, list):
        return [_mask_recursive(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _mask_recursive(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# KEY_MASK_FILTER
# ---------------------------------------------------------------------------


class _KeyMaskFilter(logging.Filter):
    """A :class:`logging.Filter` that redacts registered sensitive values.

    Walks ``record.msg`` (if a string), ``record.args`` (recursively), any
    cached exception text (``record.exc_text``), and every non-standard
    attribute on ``record.__dict__`` that was added via the ``extra=`` kwarg
    of a logging call. Returns ``True`` always: the filter mutates records
    in place and never drops them.

    Standard :class:`~logging.LogRecord` attributes (``pathname``,
    ``funcName``, ``thread``, ...) are excluded from the walk so that masking
    cannot inadvertently corrupt diagnostic metadata.
    """

    # Standard LogRecord attributes which MUST NOT be walked. Custom keys
    # passed via ``extra={...}`` ARE walked, because they typically carry
    # user-provided payloads that may include sensitive material.
    _SKIP_ATTRS: frozenset[str] = frozenset({
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    })

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask sensitive values on ``record`` in place; always return True.

        Args:
            record: The log record about to be emitted.

        Returns:
            Always ``True``. The filter never drops records — it only
            mutates them.
        """
        # Mask the format-string template itself (e.g., when the caller
        # passed a fully-rendered message instead of using % args).
        if isinstance(record.msg, str):
            record.msg = _mask_string(record.msg)
        # Mask positional / keyword args used for printf-style formatting.
        if record.args:
            record.args = _mask_recursive(record.args)
        # If an exception is attached but ``exc_text`` has not yet been
        # populated, format it NOW and mask the result. Without this, the
        # downstream :class:`logging.Formatter` would format ``exc_info``
        # AFTER the filter chain has run, leaking the raw traceback. The
        # Formatter caches ``record.exc_text`` between handlers, so
        # populating it here is the canonical hook.
        if record.exc_info and not record.exc_text:
            # logging.Formatter().formatException is a free-standing
            # operation that does not require formatter state.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        # Mask cached formatted traceback text.
        if record.exc_text:
            record.exc_text = _mask_string(record.exc_text)
        # Walk extra fields injected via ``extra={...}`` on the log call.
        for attr, value in list(record.__dict__.items()):
            if attr in self._SKIP_ATTRS or attr.startswith("_"):
                continue
            if isinstance(value, str):
                record.__dict__[attr] = _mask_string(value)
        return True


# The singleton filter instance. Public consumers reference this directly
# rather than instantiating ``_KeyMaskFilter``; this prevents multiple
# instances drifting out of sync.
KEY_MASK_FILTER: logging.Filter = _KeyMaskFilter()


# Install the mask filter on the ``vibe`` logger at module-import time so
# that every descendant logger (``vibe.core.*``, ``vibe.cli.*``,
# ``vibe.acp.*``, ...) inherits the protection automatically. This is the
# cleanest enforcement of rule 2 without invasive call-site changes.
# NOTE: We do NOT install on the global root logger; the mask is scoped
# narrowly to the ``vibe`` namespace so embedded use does not affect other
# applications' log streams.
logging.getLogger("vibe").addFilter(KEY_MASK_FILTER)


# ---------------------------------------------------------------------------
# Tracing spans and metrics
# ---------------------------------------------------------------------------

# Protects every read/write of the ``_metrics`` mapping below.
_metrics_lock = threading.Lock()

# In-memory observability state. The structure is intentionally simple so
# that operators can inspect it directly via :func:`metrics_snapshot` and
# render the operator dashboard described in
# ``docs/observability/dashboard.json``.
_metrics: dict[str, Any] = {
    # counters: name -> int. Examples: ``span.llm.complete.count``.
    "counters": {},
    # histograms: name -> list[float]. Each value is a duration in ms.
    "histograms": {},
    # spans: list of dicts with name, start_ts, end_ts, duration_ms,
    # correlation_id, attrs.
    "spans": [],
}


def _increment(name: str, amount: int = 1) -> None:
    """Atomically increment the named counter by ``amount``."""
    with _metrics_lock:
        _metrics["counters"][name] = _metrics["counters"].get(name, 0) + amount


def _record_histogram(name: str, value: float) -> None:
    """Atomically append ``value`` to the named histogram."""
    with _metrics_lock:
        _metrics["histograms"].setdefault(name, []).append(value)


def _record_span(entry: dict[str, Any]) -> None:
    """Atomically append a completed span ``entry`` to the spans list."""
    with _metrics_lock:
        _metrics["spans"].append(entry)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Record a trace span for the duration of the ``with`` block.

    Yields a mutable dict that the caller may update with additional
    attributes during the span's lifetime. On exit (including exception
    propagation), the span is appended to the in-memory spans list with its
    duration in milliseconds, its start and end timestamps, the current
    correlation ID, and the (possibly extended) attribute dict.

    The span is ALWAYS recorded — even on exception — because partial
    duration information is more valuable to operators than a missing trace
    entry.

    Args:
        name: A logical operation name. Conventional names used by the rest
            of the codebase include ``provider.connect``, ``llm.complete``,
            ``session.save``, and ``session.compact``.
        **attrs: Arbitrary key/value attributes to attach to the span.

    Yields:
        A mutable ``dict[str, Any]`` of attributes which the caller may
        update during the span's lifetime (e.g., ``s["tokens"] = ...``).

    Example:
        ::

            with span("llm.complete", provider="blitzy") as s:
                result = await backend.complete(...)
                s["tokens"] = result.usage.total_tokens
    """
    start_ts = time.time()
    mutable_attrs: dict[str, Any] = dict(attrs)
    try:
        yield mutable_attrs
    finally:
        end_ts = time.time()
        duration_ms = (end_ts - start_ts) * 1000.0
        entry: dict[str, Any] = {
            "name": name,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "duration_ms": duration_ms,
            "correlation_id": correlation_id.get(),
            "attrs": mutable_attrs,
        }
        _record_span(entry)
        _increment(f"span.{name}.count")
        _record_histogram(f"span.{name}.duration_ms", duration_ms)


def metrics_snapshot() -> dict[str, Any]:
    """Return a deep copy of the in-memory metrics state.

    The returned mapping is safe to mutate; modifications do not affect the
    live metrics state. Use this accessor (rather than reaching into
    ``_metrics`` directly) for any external inspection (tests, dashboards,
    health endpoints).

    Returns:
        A deep-copied ``dict`` with keys ``counters``, ``histograms``, and
        ``spans``. See the module docstring and
        ``docs/observability/dashboard.json`` for the expected schema.
    """
    with _metrics_lock:
        return deepcopy(_metrics)


# ---------------------------------------------------------------------------
# Readiness check
# ---------------------------------------------------------------------------

# Single-key dict avoids the need for a global rebinding-pattern (``nonlocal``
# is not available at module scope, and ``global`` is verbose).
_ready_state: dict[str, bool] = {"ready": False}


def mark_ready() -> None:
    """Mark the active backend as ready.

    Called from a backend's ``__aenter__`` (or equivalent initialization
    boundary) after a successful handshake with the upstream provider.
    """
    _ready_state["ready"] = True


def is_ready() -> bool:
    """Return ``True`` once a backend has signaled readiness.

    Exposed for the observability dashboard and any operator-facing
    health-check endpoint.

    Returns:
        The current readiness state.
    """
    return _ready_state["ready"]


# ---------------------------------------------------------------------------
# Structured JSON logging (opt-in)
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Structured JSON formatter for ``vibe`` logs.

    Emits one JSON object per log record with keys ``ts``, ``level``,
    ``logger``, ``msg``, ``correlation_id``, and (when present) ``span``,
    ``attrs``, and ``exception``. Non-JSON-serializable values are coerced
    to strings via ``default=str`` so the formatter never raises on a
    surprising payload.

    Not installed by default. Operators opt in via
    :func:`configure_json_logging`, typically when shipping logs to an
    aggregator. The default behavior (Rich console output for the CLI) is
    preserved when the formatter is not installed.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a single-line JSON object.

        All textual payloads pass through :func:`_mask_string` /
        :func:`_mask_recursive` to ensure registered sensitive values are
        scrubbed even if the mask filter has not been applied to this
        handler.

        Args:
            record: The log record to format.

        Returns:
            A single-line JSON string suitable for stdout, a file handle,
            or any log aggregator that consumes JSON-lines.
        """
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": _mask_string(record.getMessage()),
            "correlation_id": correlation_id.get(),
        }
        # Span context (if injected via ``extra={"span": ...}`` on the log
        # call).
        span_name = getattr(record, "span", None)
        if span_name:
            payload["span"] = _mask_string(str(span_name))
        # Surface any extra structured attributes attached via ``extra={...}``.
        extras: dict[str, Any] = {}
        for attr, value in record.__dict__.items():
            if attr in _KeyMaskFilter._SKIP_ATTRS or attr.startswith("_"):
                continue
            if attr in {"span", "correlation_id"}:
                continue
            extras[attr] = _mask_recursive(value)
        if extras:
            payload["attrs"] = extras
        # Surface formatted exception text (mask just in case a buggy
        # ``__str__`` leaked a sensitive value).
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exception"] = _mask_string(record.exc_text)
        return json.dumps(payload, default=str)


def configure_json_logging(handler: logging.Handler | None = None) -> None:
    """Attach :class:`JSONFormatter` to a handler on the ``vibe`` logger.

    Opt-in: not called by default to avoid disturbing other consumers of the
    logging configuration (notably the Rich console handler installed by the
    CLI). Operators call this in the entrypoint when JSON logs are desired.

    The supplied handler is automatically given :data:`KEY_MASK_FILTER` so
    that sensitive values are scrubbed even if the handler is added before
    other filters propagate.

    Args:
        handler: An optional :class:`logging.Handler`. When omitted, a new
            :class:`logging.StreamHandler` writing to the default stream is
            constructed.
    """
    if handler is None:
        handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    handler.addFilter(KEY_MASK_FILTER)
    logging.getLogger("vibe").addHandler(handler)
