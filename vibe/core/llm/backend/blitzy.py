"""Blitzy LLM backend.

Implements the :class:`~vibe.core.llm.types.BackendLike` protocol against
the Blitzy API. Uses ``httpx`` for ALL HTTP work (AAP rule 9 -- NO
``import anthropic``, NO ``import mistralai``).

Context-check semantics (AAP rule 8):
    - HTTP 200 on ``GET /context?...`` -> ``connected = True``
    - HTTP 404 on ``GET /context?...`` -> ``connected = False`` (NOT an error)
    - Any other non-2xx or timeout -> :class:`BlitzyConnectionError`

Streaming consumes Server-Sent Events from ``POST /v1/api/chat`` with field
priority ``content -> text -> message -> delta.content``. Events lacking ALL
four fields are silently skipped.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from http import HTTPStatus
import json
import types
from typing import TYPE_CHECKING

import httpx

from vibe.core.llm.exceptions import BlitzyConnectionError
from vibe.core.observability import mark_ready, span
from vibe.core.types import AvailableTool, LLMChunk, LLMMessage, Role, StrToolChoice

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig, VibeConfig


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
# Timeout values mandated by AAP section 0.1:
#   - Context endpoint: 5-second one-shot timeout.
#   - Streaming endpoint: 10-second connect timeout, 3600-second read timeout
#     (i.e., up to one hour of idle-tolerant streaming for long completions).
# These are surfaced as module constants so tests can monkey-patch them and
# operators can audit them without spelunking through the class body.

_CONTEXT_TIMEOUT_SECONDS = 5.0
_STREAM_CONNECT_TIMEOUT_SECONDS = 10.0
_STREAM_READ_TIMEOUT_SECONDS = 3600.0
_API_BASE_DEFAULT = "https://api.blitzy.com"


# ---------------------------------------------------------------------------
# BlitzyLLMBackend
# ---------------------------------------------------------------------------


class BlitzyLLMBackend:
    """Blitzy provider backend (httpx-only).

    HTTP semantics:
        - context check (``GET /context?repo=&branch=``): 200 -> connected=True;
          404 -> connected=False (NOT an error per AAP rule 8); any other
          non-2xx or timeout -> :class:`BlitzyConnectionError`.
        - completion (``POST /v1/api/chat``): SSE stream. Parser strips
          ``data: `` prefix, JSON-decodes, extracts content with field priority
          ``content -> text -> message -> delta.content``. Events lacking all
          four are skipped silently.

    Lifecycle:
        Instances MUST be entered as async context managers. The
        ``__aenter__`` step constructs the underlying ``httpx.AsyncClient``
        and performs the one-shot context check; ``__aexit__`` releases the
        client. Calling ``complete``/``complete_streaming`` without
        ``async with`` raises :class:`RuntimeError`.

    Attributes:
        connected: Public boolean indicating whether the Blitzy backend
            successfully matched the requested ``(repo, branch)`` pair on
            the context-check endpoint. True after a 200, False after a 404
            (per AAP rule 8). Tests inspect this attribute directly.
    """

    def __init__(
        self,
        provider: ProviderConfig,
        config: VibeConfig,
        *,
        repo: str = "",
        branch: str = "",
    ) -> None:
        """Construct a Blitzy backend.

        Resolves the API key via the shared three-tier chain
        (env var -> ``VibeConfig.blitzy_api_key`` field -> interactive
        ``getpass`` prompt). The resolver is imported LAZILY here (not at
        module load) so that any future circular-import scenario through
        ``vibe.core.config`` does not surface as a hard module-import error.

        Args:
            provider: The active provider config (carries ``api_base`` and
                ``api_key_env_var``). Type-only forward reference; runtime
                duck-typing is sufficient.
            config: The active :class:`VibeConfig` instance, passed through
                to ``resolve_or_prompt`` for the second resolution tier.
            repo: Repository name detected by ``vibe.core.git_context.detect``
                or supplied by the caller. Empty string is permitted (AAP
                rule 3 -- git context detection MUST NOT fail loudly).
            branch: Branch name detected by ``vibe.core.git_context.detect``
                or supplied by the caller. Empty string is permitted.

        Raises:
            MissingAPIKeyError: When the user declines the interactive
                prompt (AAP rule 10). Propagated from ``resolve_or_prompt``.
        """
        # Lazy import to avoid a potential circular-import cycle:
        # ``vibe.core.llm.api_key_prompt`` imports from ``vibe.core.config``,
        # which in some dev scenarios may transitively touch this backend.
        # Importing here defers the resolution to construction time and
        # keeps the module-level import graph minimal.
        from vibe.core.llm.api_key_prompt import resolve_or_prompt

        self._provider = provider
        self._config = config
        self._repo = repo
        self._branch = branch
        # Three-tier resolution: env var -> config field -> interactive prompt.
        # The provider string ``"blitzy"`` is the canonical lowercase token
        # (matches ``Backend.BLITZY.value`` and the ``--provider blitzy``
        # argparse choice -- AAP rule 13, no orphaned strings).
        #
        # The ``or "BLITZY_API_KEY"`` fallback is defensive: the default
        # ``ProviderConfig`` for Blitzy in ``vibe/core/config.py`` already
        # sets ``api_key_env_var="BLITZY_API_KEY"``, but exotic provider
        # configs may pass empty. Falling back to the canonical env-var name
        # keeps the resolver's tier-1 lookup working in all cases.
        self._api_key = resolve_or_prompt(
            "blitzy",
            self._provider.api_key_env_var or "BLITZY_API_KEY",
            "blitzy_api_key",
            config,
        )
        # ``self._api_key`` is guaranteed non-empty here. ``resolve_or_prompt``
        # raises ``MissingAPIKeyError("blitzy")`` on declined prompt -- AAP
        # rule 10 mandates the CLI then exit. The value has already been
        # registered with ``observability.register_sensitive`` so every
        # subsequent log record under the ``vibe`` namespace will scrub it
        # (AAP rule 2).
        self._api_base = (provider.api_base or _API_BASE_DEFAULT).rstrip("/")
        # ``_client`` is constructed in ``__aenter__`` so that the client
        # lifecycle aligns with the async context manager scope.
        self._client: httpx.AsyncClient | None = None
        # ``connected`` is a PUBLIC attribute (no underscore prefix) per the
        # schema's ``members_exposed``. Tests inspect it directly to verify
        # the rule 8 ladder (200 -> True, 404 -> False).
        self.connected: bool = False
        # Idempotency flag for the connection-status message that prints
        # exactly once before the first ``complete``/``complete_streaming``
        # call (AAP section 0.6.1 Group 1).
        self._connection_message_printed = False
        # Timeout configuration for the streaming endpoint. The context
        # endpoint passes its own ``timeout=_CONTEXT_TIMEOUT_SECONDS`` at
        # the call site so that the slow-stream timeout is not inherited.
        self._timeout = httpx.Timeout(
            connect=_STREAM_CONNECT_TIMEOUT_SECONDS,
            read=_STREAM_READ_TIMEOUT_SECONDS,
            write=10.0,
            pool=10.0,
        )

    async def __aenter__(self) -> BlitzyLLMBackend:
        """Open the HTTP client and perform the one-shot context check.

        The ``httpx.AsyncClient`` is constructed with the
        ``_STREAM_CONNECT_TIMEOUT_SECONDS`` / ``_STREAM_READ_TIMEOUT_SECONDS``
        pair and the ``X-API-Key`` auth header. The context check is then
        performed with a 5-second override timeout because the context
        endpoint is expected to respond quickly -- using the longer stream
        read timeout would let a network blackhole stall startup.

        Status code handling (AAP rule 8):
            - 200: ``connected = True``.
            - 404: ``connected = False`` and continue -- NOT an error.
            - other non-2xx OR timeout: raise :class:`BlitzyConnectionError`.

        Returns:
            ``self`` for the canonical ``async with backend as b: ...`` idiom.

        Raises:
            BlitzyConnectionError: on any non-2xx-non-404 status, on
                ``httpx.TimeoutException`` (any flavor), or on
                ``httpx.RequestError`` (any transport-layer failure).
        """
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
        )
        url = f"{self._api_base}/context?repo={self._repo}&branch={self._branch}"
        # ``httpx.TimeoutException`` is the parent class for ConnectTimeout,
        # ReadTimeout, WriteTimeout, and PoolTimeout -- catching it covers
        # every flavor of slow-network failure. ``httpx.RequestError`` is
        # the parent of all non-timeout transport errors (ConnectError,
        # NetworkError, ProtocolError, ...). Combining the two yields
        # complete coverage of pre-response failure modes.
        #
        # The ``from exc`` clause preserves the cause chain for debuggers
        # without exposing it via ``BlitzyConnectionError.__str__`` -- the
        # error message rendered by users only references repo, branch,
        # status_code, and url (none of which contain the API key, per
        # AAP rule 2).
        #
        # AAP observability rule + docs/observability/dashboard.json:
        # the ``provider.connect`` span name is one of the four span names
        # the dashboard "Span breakdown per session" trace view expects.
        # We record it ALWAYS — even on raise — because the contextmanager
        # in :func:`vibe.core.observability.span` records the duration on
        # ``finally``, giving operators visibility into failed-handshake
        # latency. The span attrs ``repo`` and ``branch`` are the dimensions
        # operators slice on; ``status_code`` is filled in below as the
        # ladder branches are walked.
        with span(
            "provider.connect", provider="blitzy", repo=self._repo, branch=self._branch
        ) as span_attrs:
            try:
                response = await self._client.get(url, timeout=_CONTEXT_TIMEOUT_SECONDS)
            except httpx.TimeoutException as exc:
                span_attrs["status_code"] = None
                span_attrs["outcome"] = "timeout"
                raise BlitzyConnectionError(
                    self._repo, self._branch, None, url
                ) from exc
            except httpx.RequestError as exc:
                span_attrs["status_code"] = None
                span_attrs["outcome"] = "request_error"
                raise BlitzyConnectionError(
                    self._repo, self._branch, None, url
                ) from exc

            span_attrs["status_code"] = response.status_code
            if response.status_code == HTTPStatus.OK:
                self.connected = True
                span_attrs["outcome"] = "connected"
            elif response.status_code == HTTPStatus.NOT_FOUND:
                # AAP rule 8: 404 on /context is NOT an error; the operator
                # is simply running the agent against a repo+branch the
                # Blitzy backend has no knowledge of. The agent continues
                # to serve completions; only the "Connected to ..." UX
                # message changes.
                self.connected = False
                span_attrs["outcome"] = "not_connected"
            else:
                # Any other status (4xx other than 404, any 5xx) is a
                # connection failure per AAP rule 8.
                span_attrs["outcome"] = "error"
                raise BlitzyConnectionError(
                    self._repo, self._branch, response.status_code, url
                )

        # Signal readiness to the observability subsystem AFTER the context
        # check completes successfully (whether connected=True or
        # connected=False). Only timeouts / non-2xx-non-404 statuses skip
        # this call -- they raise above (the span is recorded before the
        # exception propagates because the contextmanager finalizes on
        # ``finally``).
        mark_ready()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Release the underlying HTTP client.

        Uses ``httpx.AsyncClient.aclose()`` (the canonical async close
        method). Setting ``_client = None`` afterwards is defensive against
        accidental double-close on edge cases like nested ``async with``
        misuse.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _print_connection_message_once(self) -> None:
        """Print the per-session connection status message exactly once.

        Emitted lazily on the first ``complete``/``complete_streaming`` call
        (NOT at ``__aenter__`` time) per AAP section 0.6.1 Group 1. The
        idempotency flag ``_connection_message_printed`` ensures the message
        does not re-print on subsequent turns.

        Uses ``print`` (stdout) -- this is user-facing UX, NOT diagnostic
        logging. Routing through ``logging`` would risk being swallowed by
        the structured-logging formatter or buffered behind handlers.
        """
        if self._connection_message_printed:
            return
        if self.connected:
            print(f"Connected to {self._repo}({self._branch})")
        else:
            print("no repository connected")
        self._connection_message_printed = True

    async def complete_streaming(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
    ) -> AsyncGenerator[LLMChunk, None]:
        """Stream completions from ``POST /v1/api/chat`` as ``LLMChunk``s.

        The Blitzy completion endpoint speaks Server-Sent Events. The wire
        format is ``data: <json>\\n\\n`` per event; the parser strips the
        ``data: `` prefix, JSON-decodes, and extracts the assistant content
        with field priority ``content -> text -> message -> delta.content``
        (see :func:`_parse_sse_event`). Events that lack all four fields,
        ``[DONE]`` sentinels, malformed JSON, and empty events are silently
        skipped -- the loop does NOT yield a chunk for those.

        Buffer-based parsing handles partial events that span ``aiter_bytes``
        boundaries: bytes are accumulated until a ``\\n\\n`` separator is
        found, at which point the leading event is drained and parsed while
        any trailing partial bytes wait for the next chunk.

        Args:
            model: Active model config; ``model.name`` is forwarded to the
                Blitzy backend as the canonical model identifier.
            messages: Conversation history. Each :class:`LLMMessage` is
                serialized via ``model_dump(mode="json")`` for the request
                body.
            temperature: Sampling temperature; passed through verbatim.
            tools: Optional list of available tools; when truthy, serialized
                via ``model_dump(mode="json")`` per element.
            max_tokens: Optional max output tokens; included only when not
                ``None``.
            tool_choice: Optional tool-choice directive. String values
                (``"auto"``, ``"none"``, ``"any"``, ``"required"``) are
                passed verbatim; ``AvailableTool`` instances are serialized
                via ``model_dump(mode="json")``.
            extra_headers: Optional extra request headers. The
                ``X-API-Key`` header is FILTERED OUT (case-insensitive) to
                prevent the caller from overriding the auth header.

        Yields:
            :class:`LLMChunk` objects, one per SSE event with extractable
            content. The yielded ``message.role`` is always
            ``Role.assistant``; ``usage`` is ``None`` (the standard Blitzy
            SSE shape does not carry per-event token usage).

        Raises:
            RuntimeError: If invoked outside an ``async with`` block (the
                underlying ``_client`` has not been initialized).
            BlitzyConnectionError: On any HTTP error (status >= 400) from
                ``POST /v1/api/chat``. The 404 exemption applies only to
                the ``/context`` endpoint per AAP rule 8; 404 on the
                completion endpoint IS surfaced as a connection error.
        """
        # Lazy connection-status print -- AAP section 0.6.1 Group 1.
        self._print_connection_message_once()

        # Defensive check: caller forgot to use ``async with``. Raising
        # ``RuntimeError`` (NOT ``BlitzyConnectionError``) is the correct
        # choice here because this is a programmer error against the
        # backend's lifecycle contract, not a Blitzy-side fault.
        if self._client is None:
            raise RuntimeError(
                "BlitzyLLMBackend used outside async-context-manager scope; "
                "call `async with` to initialize the http client first."
            )

        url = f"{self._api_base}/v1/api/chat"
        # Build the request body. ``mode="json"`` on ``model_dump`` ensures
        # ``Role`` (a ``StrEnum``) renders as its string value and any
        # nested non-JSON-native types are converted appropriately.
        body: dict[str, object] = {
            "messages": [m.model_dump(mode="json") for m in messages],
            "repo": self._repo,
            "branch": self._branch,
            "temperature": temperature,
            "model": model.name,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = [t.model_dump(mode="json") for t in tools]
        if tool_choice is not None:
            # ``StrToolChoice`` is a string literal type alias; if the caller
            # passed a string (``"auto" | "none" | "any" | "required"``),
            # forward it verbatim. Otherwise it's an ``AvailableTool`` -- we
            # serialize via ``model_dump(mode="json")`` to match the format
            # of the ``tools`` array.
            body["tool_choice"] = (
                tool_choice
                if isinstance(tool_choice, str)
                else tool_choice.model_dump(mode="json")
            )

        # Build per-request headers. Setting ``X-API-Key`` explicitly here
        # (in addition to the client-level header) provides defense in depth
        # against any future code path that constructs a new client without
        # the auth header. The ``extra_headers`` merge filters out any
        # incoming ``X-API-Key`` (case-insensitive) so callers cannot
        # override the auth header by accident or design.
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        if extra_headers:
            headers.update({
                k: v for k, v in extra_headers.items() if k.lower() != "x-api-key"
            })

        # AAP observability rule + docs/observability/dashboard.json:
        # the ``llm.complete`` span name is dashboard-required ("P99 LLM
        # latency by provider" metric panel slices on this span's
        # duration histogram, partitioned by ``attrs.provider``). We
        # record the span around the FULL streaming exchange (request,
        # response framing, and chunk aggregation) so that the duration
        # captures end-to-end completion time as the user experiences it.
        # ``chunk_count`` and ``content_length`` are updated incrementally
        # as the SSE parser yields content to give the dashboard a
        # payload-size dimension for slow-response triage.
        #
        # The buffer-based SSE drain lives in :meth:`_parse_sse_stream` to
        # keep this function within the project's ``max-nested-blocks=4``
        # lint budget. The helper owns the inner ``async for / while / if``
        # nesting; the caller owns the ``span / async with stream``
        # framing.
        with span("llm.complete", provider="blitzy") as span_attrs:
            span_attrs["chunk_count"] = 0
            span_attrs["content_length"] = 0
            # ``httpx.AsyncClient.stream`` is the async-context-manager
            # streaming primitive. The ``json=`` kwarg serializes the body
            # via the same JSON encoder used by ``httpx`` elsewhere and
            # sets the request ``Content-Type`` automatically (we set it
            # explicitly above for symmetry).
            async with self._client.stream(
                "POST", url, json=body, headers=headers
            ) as resp:
                span_attrs["status_code"] = resp.status_code
                if resp.status_code >= HTTPStatus.BAD_REQUEST:
                    # On any error during completion, raise
                    # BlitzyConnectionError. 404 on ``/v1/api/chat`` is
                    # unusual; we surface it as a connection error because
                    # the rule-8 exemption is specific to the context-check
                    # endpoint.
                    span_attrs["outcome"] = "error"
                    raise BlitzyConnectionError(
                        self._repo, self._branch, resp.status_code, url
                    )

                async for content in self._parse_sse_stream(resp):
                    span_attrs["chunk_count"] += 1
                    span_attrs["content_length"] += len(content)
                    yield LLMChunk(
                        message=LLMMessage(role=Role.assistant, content=content)
                    )
                span_attrs["outcome"] = "ok"

    async def _parse_sse_stream(
        self, resp: httpx.Response
    ) -> AsyncGenerator[str, None]:
        """Drain an SSE byte stream and yield assistant content strings.

        This helper exists to keep :meth:`complete_streaming` within the
        project-wide ``max-nested-blocks=4`` lint budget (Ruff ``PLR1702``).
        By moving the ``async for chunk in resp.aiter_bytes() / while
        b"\\n\\n" in buffer / if content`` nesting into a private async
        generator, the caller's body shrinks to ``with span / async with
        stream / async for content`` (depth 3), well within the budget.

        Buffer-based parsing handles partial events that span
        ``aiter_bytes`` boundaries: bytes are accumulated until a
        ``\\n\\n`` separator is found, at which point the leading event
        is drained and parsed while any trailing partial bytes wait for
        the next chunk. Empty content (events that lack all four
        priority fields, ``[DONE]`` sentinels, malformed JSON, and empty
        events) is silently skipped per :func:`_parse_sse_event`.

        Args:
            resp: The open ``httpx.Response`` to drain. The caller owns
                the ``async with self._client.stream(...) as resp:``
                context manager; this helper only iterates its body.

        Yields:
            str: Non-empty assistant content extracted from each SSE
                event in field-priority order.
        """
        buffer = b""
        async for chunk in resp.aiter_bytes():
            buffer += chunk
            while b"\n\n" in buffer:
                # Split at the FIRST ``\n\n``, leaving the rest in the
                # buffer for the next iteration.
                raw_event, buffer = buffer.split(b"\n\n", 1)
                content = _parse_sse_event(raw_event)
                if content:
                    yield content

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
    ) -> LLMChunk:
        """Aggregate ``complete_streaming`` into a single ``LLMChunk``.

        The Blitzy API only provides an SSE-streaming completion endpoint
        per AAP section 0.5.1 -- there is no separate non-streaming
        endpoint to call. ``complete`` therefore delegates to
        ``complete_streaming`` and concatenates the ``content`` of every
        yielded chunk into a single assistant message.

        ``LLMChunk.usage`` is ``None`` on the returned aggregate because
        the standard Blitzy SSE event shape does not include per-chunk
        token usage. If the API later adds a final ``usage`` event, this
        method should be updated to aggregate it; the public signature
        does NOT need to change.

        Args:
            model: See :meth:`complete_streaming`.
            messages: See :meth:`complete_streaming`.
            temperature: See :meth:`complete_streaming`.
            tools: See :meth:`complete_streaming`.
            max_tokens: See :meth:`complete_streaming`.
            tool_choice: See :meth:`complete_streaming`.
            extra_headers: See :meth:`complete_streaming`.

        Returns:
            A single :class:`LLMChunk` whose ``message.content`` is the
            concatenation of every streamed chunk's content. ``usage`` is
            ``None``.

        Raises:
            RuntimeError: If invoked outside an ``async with`` block
                (propagated from ``complete_streaming``).
            BlitzyConnectionError: On any HTTP error from
                ``POST /v1/api/chat`` (propagated from
                ``complete_streaming``).
        """
        accumulated = ""
        async for chunk in self.complete_streaming(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        ):
            # ``chunk.message`` is always non-None (it's a required field
            # on :class:`LLMChunk`), but ``message.content`` is optional --
            # guard against ``None`` and empty-string content for safety.
            if chunk.message and chunk.message.content:
                accumulated += chunk.message.content
        return LLMChunk(message=LLMMessage(role=Role.assistant, content=accumulated))

    async def count_tokens(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        tools: list[AvailableTool] | None = None,
        tool_choice: StrToolChoice | AvailableTool | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> int:
        """Estimate prompt token count using the AAP rule-7 estimator.

        Per AAP section 0.2.3 and rule 7, the canonical estimator for the
        compaction trigger is::

            len(json.dumps(messages)) // 4

        Roughly: 4 characters of JSON-serialized payload per token. This is
        intentionally a static estimate (NO HTTP call) because:

        1. The Blitzy API does not document a token-counting endpoint.
        2. Compaction must be cheap enough to run before every turn
           without adding latency to the user-facing interaction.

        The ``ensure_ascii=False`` keyword to :func:`json.dumps` matches the
        existing project convention for length-based estimation: Unicode
        characters serialized as their natural multi-byte UTF-8 sequence
        more closely approximate the actual tokenizer behavior of modern
        LLMs (which generally treat multi-byte sequences as single tokens
        more often than the ``\\uXXXX`` escape would imply).

        Args:
            model: Active model config. Unused by this estimator but
                required by the protocol signature.
            messages: Conversation history to estimate. Serialized via
                ``model_dump(mode="json")`` to match what the
                ``complete_streaming`` request body would contain.
            temperature: Unused; accepted for protocol parity.
            tools: Unused; accepted for protocol parity.
            tool_choice: Unused; accepted for protocol parity.
            extra_headers: Unused; accepted for protocol parity.

        Returns:
            The estimated prompt token count as a non-negative integer.
        """
        # AAP rule 7 mandates ``len(json.dumps(messages)) // 4`` exactly.
        # ``mode="json"`` on ``model_dump`` ensures the same serialization
        # path as the request body, so the estimate corresponds to what
        # the wire request would actually carry.
        serialized = json.dumps(
            [m.model_dump(mode="json") for m in messages], ensure_ascii=False
        )
        return len(serialized) // 4


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_data_payload(raw: bytes) -> str | None:
    """Decode SSE event bytes and join all ``data:`` lines.

    Handles the SSE wire format: multi-line ``data:`` lines are joined with
    ``\\n`` per the SSE spec. Non-``data:`` lines (``event:``, ``id:``,
    ``retry:``, comment lines starting with ``:``) are skipped.

    Args:
        raw: The raw bytes of a single SSE event.

    Returns:
        The combined payload string (without ``data:`` prefixes), or
        ``None`` for empty input or input with no ``data:`` lines.
    """
    # ``errors="replace"`` is defensive against malformed UTF-8: invalid
    # byte sequences become the Unicode replacement character (U+FFFD)
    # rather than raising. The SSE spec mandates UTF-8, but a buggy server
    # could send anything; replacement is preferable to propagating an
    # exception that would terminate the entire stream.
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    data_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("data:"):
            data_lines.append(stripped[len("data:") :].strip())
    if not data_lines:
        return None
    return "\n".join(data_lines)


def _extract_content_from_payload(payload: dict[str, object]) -> str | None:
    """Extract assistant content from a decoded SSE payload object.

    Field priority per AAP section 0.5.1 / 0.6.1 Group 1 (FIRST-MATCH WINS,
    in EXACT order)::

        content -> text -> message -> delta.content

    Args:
        payload: A JSON object decoded from a single SSE event's ``data:``
            payload.

    Returns:
        The first non-empty string field's value (in priority order), or
        ``None`` when none of the four known fields contain a non-empty
        string.
    """
    # First-match wins. The ``isinstance(value, str)`` check filters out
    # any non-string field value (numbers, nested dicts, lists, ``None``),
    # and ``and value`` filters out empty strings -- a present-but-empty
    # field is treated as "no content".
    for key in ("content", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    # ``delta.content`` is the FOURTH-priority field. The Anthropic and
    # OpenAI streaming protocols both use a ``delta`` envelope wrapping a
    # ``content`` field for incremental chunks; supporting this nested
    # shape gives the Blitzy backend compatibility with both styles.
    delta = payload.get("delta")
    if isinstance(delta, dict):
        value = delta.get("content")
        if isinstance(value, str) and value:
            return value
    return None


def _parse_sse_event(raw: bytes) -> str | None:
    """Parse a single SSE event payload and extract assistant content.

    Field priority per AAP section 0.5.1 / 0.6.1 Group 1 (FIRST-MATCH WINS,
    in EXACT order)::

        content -> text -> message -> delta.content

    The first-match invariant is verified by
    ``tests/backend/test_blitzy_backend.py``: an event containing all four
    fields MUST yield the ``content`` value, NOT ``text``/``message``/
    ``delta.content``.

    Returns ``None`` (causing the caller to skip the event WITHOUT yielding
    a chunk) in any of the following cases:

    - Empty input bytes.
    - Input contains no ``data:`` lines.
    - The combined ``data:`` payload is the SSE ``[DONE]`` sentinel.
    - JSON decode failure (malformed payload).
    - The decoded payload is not a JSON object (e.g., array, string,
      number, ``null``).
    - The payload lacks all four known content fields, or every present
      field has a non-string value or an empty string.

    Multi-line ``data:`` is supported per the SSE spec: an event payload
    split across multiple ``data:`` lines is joined with ``\\n`` before
    JSON decoding. This is a defensive-correctness feature -- production
    Blitzy events should fit on a single ``data:`` line, but the parser
    handles the multi-line case to avoid a hard failure on a server-side
    encoding change.

    Args:
        raw: The raw bytes of a single SSE event (the chunk between two
            ``\\n\\n`` separators). May be empty bytes.

    Returns:
        The extracted assistant content as a non-empty string, or ``None``
        if the event should be silently skipped.
    """
    payload_str = _extract_data_payload(raw)
    # Skip when the event had no ``data:`` lines (empty, comments-only,
    # or a non-data event-only frame).
    if payload_str is None:
        return None
    # ``[DONE]`` is the canonical sentinel that marks the end of the stream
    # (popularized by OpenAI's SSE protocol and adopted by many SSE-based
    # LLM APIs). The caller skips it by treating ``None`` as "no content".
    if payload_str == "[DONE]":
        return None
    # JSON-decode. ``json.JSONDecodeError`` is a subclass of ``ValueError``;
    # the broader catch is kept for completeness against future Python
    # changes.
    try:
        payload = json.loads(payload_str)
    except (ValueError, json.JSONDecodeError):
        return None
    # Blitzy SSE events MUST be JSON objects. Arrays, strings, numbers,
    # booleans, and ``null`` are all rejected -- the caller treats ``None``
    # as "skip this event".
    if not isinstance(payload, dict):
        return None
    return _extract_content_from_payload(payload)
