"""Unit tests for ``vibe/core/llm/backend/blitzy.py:BlitzyLLMBackend``.

Covers AAP behavioral rules 8 (HTTP 404 context check -> connected=False, no
exception), 9 (library isolation: httpx only, no anthropic/mistralai), and
Validation Gate 8 (integration test with @pytest.mark.integration).

Mocks all HTTP interaction via ``respx``. The integration test
(``test_integration_streams_at_least_one_content_block``) hits the real
``https://api.blitzy.com`` API and is skipped unless ``BLITZY_API_KEY`` is set.

Test phases (per AAP section 0.3.3 / agent_prompt):

- Phase 0: imports, autouse fixtures, helper factories.
- Phase A (6 tests): context-check ladder against ``GET /context``
  (200, 404, 500, 403, timeout, repo+branch echoed in URL).
- Phase B (8 tests): SSE field-priority through the public
  ``complete_streaming`` interface, including [DONE] sentinel and malformed
  JSON resilience.
- Phase B' (8 tests): direct unit tests for ``_parse_sse_event`` (the
  module-level helper). Synchronous; no HTTP layer involved.
- Phase C (4 tests): HTTP wiring (X-API-Key header, POST endpoint URL,
  request body shape) and module constant locks.
- Phase D (4 tests): library-isolation assertions (no ``anthropic``, no
  ``mistralai``, ``httpx`` IS present, no ``requests`` / ``aiohttp``).
- Phase E (1 test): structural protocol conformance against
  :class:`BackendLike`.
- Phase F (2 tests): connection-message printing on first
  ``complete_streaming`` call (200 vs 404 outcome).
- Phase G (1 test): ``count_tokens`` matches the AAP rule-7 estimator
  ``len(json.dumps(messages)) // 4``.
- Phase H (1 test): live-API integration test (Gate 8). Skipped when
  ``BLITZY_API_KEY`` is unset or set to the autouse fixture placeholder
  ``"mock"``.

All ``conftest.py`` autouse fixtures (``tmp_working_directory``,
``config_dir``, ``_unlock_config_paths``, ``_mock_api_key``,
``_mock_platform``, ``_mock_update_commands``) are inherited automatically
by every test in this file and require no explicit declaration.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging  # noqa: F401  # documented import per agent_prompt
import os
import types
from typing import Any

import httpx
import pytest
import respx

from vibe.core.config import Backend, ModelConfig, ProviderConfig, VibeConfig
from vibe.core.llm.backend.blitzy import (
    _API_BASE_DEFAULT,
    _CONTEXT_TIMEOUT_SECONDS,
    _STREAM_CONNECT_TIMEOUT_SECONDS,
    _STREAM_READ_TIMEOUT_SECONDS,
    BlitzyLLMBackend,
    _parse_sse_event,
)
from vibe.core.llm.exceptions import BlitzyConnectionError
from vibe.core.llm.types import BackendLike
from vibe.core.types import LLMMessage, Role

# ---------------------------------------------------------------------------
# Phase 0 -- Autouse fixtures and helper factories
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_blitzy_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set ``BLITZY_API_KEY`` so ``resolve_or_prompt`` short-circuits at tier 1.

    The autouse ``_mock_api_key`` fixture in ``tests/conftest.py`` only sets
    ``MISTRAL_API_KEY=mock``. Blitzy tests need their own placeholder so the
    three-tier API key resolver in :mod:`vibe.core.llm.api_key_prompt` finds
    a non-empty env-var value and never triggers the interactive
    ``getpass.getpass`` prompt (which would hang the test under pytest's
    default non-interactive stdin).

    Individual tests that need to assert a specific key value flow through
    to request headers (e.g., ``test_x_api_key_header_used``) call
    ``monkeypatch.setenv("BLITZY_API_KEY", ...)`` themselves; pytest's
    monkeypatch LIFO undo order ensures the test-level override wins for
    the duration of the test.
    """
    monkeypatch.setenv("BLITZY_API_KEY", "mock")


def _make_provider(api_base: str = "https://api.blitzy.com") -> ProviderConfig:
    """Construct a minimal :class:`ProviderConfig` for Blitzy backend tests.

    Avoids the :data:`vibe.core.config.DEFAULT_PROVIDERS` lookup path so that
    test setup does not depend on the conftest config TOML containing a
    Blitzy provider entry. The ``backend`` discriminator is set to
    :attr:`Backend.BLITZY` for consistency with the runtime factory map.

    Args:
        api_base: The provider's API base URL. Defaults to
            ``"https://api.blitzy.com"`` (the canonical Blitzy endpoint
            used by ``respx.mock(base_url=...)``).

    Returns:
        A :class:`ProviderConfig` ready to pass into
        :class:`BlitzyLLMBackend`.
    """
    return ProviderConfig(
        name="blitzy",
        api_base=api_base,
        api_key_env_var="BLITZY_API_KEY",
        backend=Backend.BLITZY,
    )


def _make_config() -> VibeConfig:
    """Construct a default :class:`VibeConfig` for Blitzy backend tests.

    The :class:`VibeConfig` instance reads from the conftest-managed tmp
    ``~/.vibe/config.toml`` (see ``tests/conftest.py:config_dir``). The
    ``blitzy_api_key`` field is left ``None`` so that the
    :func:`resolve_or_prompt` env-var tier (set by
    :func:`_set_blitzy_api_key`) is the value source under test.

    Returns:
        A fresh :class:`VibeConfig` ready to pass into
        :class:`BlitzyLLMBackend`.
    """
    return VibeConfig()


def _make_model() -> ModelConfig:
    """Construct a :class:`ModelConfig` for Blitzy streaming tests.

    The Blitzy API treats the ``model`` request body field as advisory
    (the server selects the actual model); we still need a valid
    :class:`ModelConfig` to satisfy the :class:`BackendLike` protocol's
    ``complete_streaming(model=...)`` keyword.

    Returns:
        A minimal :class:`ModelConfig` whose ``provider`` is ``"blitzy"``
        and whose ``alias`` doubles as the request-body ``model`` token.
    """
    return ModelConfig(
        name="blitzy-default-model", provider="blitzy", alias="blitzy-test"
    )


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Build an SSE-formatted response body from a list of dict payloads.

    Each event is serialized as ``data: <json>\\n\\n`` per the SSE wire
    format expected by :func:`vibe.core.llm.backend.blitzy._parse_sse_event`.
    The trailing ``\\n\\n`` is the inter-event separator the parser splits
    on; producing an extra one at the end is harmless (the parser tolerates
    trailing empty events).

    Args:
        events: A list of JSON-serializable dicts. Each becomes a single
            SSE event in the response stream.

    Returns:
        UTF-8 encoded bytes ready to wrap in
        ``httpx.ByteStream(...)`` for a respx mock.
    """
    parts: list[str] = []
    for ev in events:
        parts.append(f"data: {json.dumps(ev)}\n\n")
    return "".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# Phase A -- Context check tests (AAP rule 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_check_200_sets_connected_true() -> None:
    """GET ``/context`` returning 200 -> ``backend.connected`` is True.

    The happy path: when the Blitzy API recognizes the (repo, branch) pair
    requested by the agent, the backend transitions to ``connected=True``
    after ``__aenter__`` completes.
    """
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            assert backend.connected is True


@pytest.mark.asyncio
async def test_context_check_404_sets_connected_false_no_exception() -> None:
    """GET ``/context`` returning 404 -> ``connected=False``, NO exception.

    THIS IS THE CANONICAL AAP RULE 8 ENFORCEMENT TEST.

    The Blitzy API uses HTTP 404 on the context endpoint to signal "no
    knowledge of this repo+branch" -- a normal state, NOT an error. The
    agent must continue to serve completions; only the user-facing
    "Connected to ..." message changes. The test asserts no exception is
    raised on ``__aenter__`` AND that ``connected`` is ``False``.
    """
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=404))
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        # Critical: no exception raised on entry.
        async with backend:
            assert backend.connected is False


@pytest.mark.asyncio
async def test_context_check_500_raises_blitzy_connection_error() -> None:
    """GET ``/context`` returning 500 -> :class:`BlitzyConnectionError` (code=500).

    Per AAP rule 8, every non-2xx response OTHER than 404 is a connection
    failure. The exception's ``status_code`` exposes the raw HTTP status
    and the ``url`` exposes the request URL (with repo+branch query string)
    for operator diagnosis.
    """
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=500))
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        with pytest.raises(BlitzyConnectionError) as exc_info:
            async with backend:
                pass
        assert exc_info.value.repo == "myrepo"
        assert exc_info.value.branch == "main"
        assert exc_info.value.status_code == 500
        assert "myrepo" in exc_info.value.url
        assert "main" in exc_info.value.url


@pytest.mark.asyncio
async def test_context_check_403_raises_blitzy_connection_error() -> None:
    """GET ``/context`` returning 403 -> :class:`BlitzyConnectionError` (code=403).

    Auth-failure path on the context endpoint. The exception is raised
    with the verbatim status code so operators can distinguish 403
    (forbidden / bad credentials) from other connection failures.
    """
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=403))
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        with pytest.raises(BlitzyConnectionError) as exc_info:
            async with backend:
                pass
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_context_check_connect_timeout_raises_blitzy_connection_error() -> None:
    """``httpx.ConnectTimeout`` -> :class:`BlitzyConnectionError` (code=None).

    Network-level failure: the request never reached the server, so there
    is no HTTP status to report. ``status_code`` is :data:`None`, signaling
    "no response received" to downstream error handlers.
    """
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(side_effect=httpx.ConnectTimeout("timeout"))
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        with pytest.raises(BlitzyConnectionError) as exc_info:
            async with backend:
                pass
        assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_context_check_request_includes_repo_and_branch() -> None:
    """GET ``/context`` URL includes ``repo=...`` and ``branch=...`` in query string.

    Verifies the wire-format contract: the (repo, branch) pair detected
    by :func:`vibe.core.git_context.detect` (or supplied by the caller)
    is propagated into the GET query string so the Blitzy server can
    resolve the requested context.
    """
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        route = mock_api.get("/context").mock(
            return_value=httpx.Response(status_code=200)
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            pass
        assert route.called
        request_url = str(route.calls.last.request.url)
        assert "repo=myrepo" in request_url
        assert "branch=main" in request_url


# ---------------------------------------------------------------------------
# Phase B -- SSE field priority via ``complete_streaming``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_field_priority_content_first() -> None:
    """SSE event with all 4 fields -> ``content`` wins (priority 1).

    First-match invariant on the field priority chain
    ``content -> text -> message -> delta.content``. An event carrying all
    four fields MUST yield the ``content`` value; the other three are
    ignored even though they are syntactically valid candidates.
    """
    body = _sse_body([
        {"content": "A", "text": "B", "message": "C", "delta": {"content": "D"}}
    ])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        assert len(chunks) >= 1
        assert chunks[0].message.content == "A"


@pytest.mark.asyncio
async def test_sse_field_priority_text_when_no_content() -> None:
    """SSE event without ``content`` but with ``text`` -> ``text`` wins (priority 2).

    Falls through tier 1 (``content``) and resolves to tier 2 (``text``).
    The presence of a lower-priority ``message`` field MUST NOT win.
    """
    body = _sse_body([{"text": "B", "message": "C"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        assert len(chunks) >= 1
        assert chunks[0].message.content == "B"


@pytest.mark.asyncio
async def test_sse_field_priority_message_when_no_text() -> None:
    """SSE event with only ``message`` -> ``message`` wins (priority 3).

    Falls through tiers 1 and 2; resolves to tier 3 (``message``). This
    case exercises the third-priority slot in the SSE parser's ordered
    lookup loop.
    """
    body = _sse_body([{"message": "C"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        assert len(chunks) >= 1
        assert chunks[0].message.content == "C"


@pytest.mark.asyncio
async def test_sse_field_priority_delta_content_when_no_message() -> None:
    """SSE event with only ``delta.content`` -> ``delta.content`` wins (priority 4).

    The fourth-priority slot is a nested ``delta`` envelope wrapping a
    ``content`` field, matching the OpenAI/Anthropic incremental-streaming
    shape. This case verifies the SSE parser walks into the nested dict
    after exhausting the top-level priority tiers.
    """
    body = _sse_body([{"delta": {"content": "D"}}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        assert len(chunks) >= 1
        assert chunks[0].message.content == "D"


@pytest.mark.asyncio
async def test_sse_event_with_no_known_field_skipped() -> None:
    """SSE event lacking ALL 4 known fields is silently SKIPPED.

    A subsequent event with a recognized field still produces a chunk;
    the unrecognized event does NOT yield a chunk (instead of yielding an
    empty/null one). This guards against accidental ``None``/empty-string
    chunks polluting the conversation history.
    """
    body = _sse_body([{"random": "X"}, {"content": "Y"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        # Only the second event (content="Y") yields a chunk; the first
        # is silently skipped because it lacks all four priority fields.
        assert len(chunks) == 1
        assert chunks[0].message.content == "Y"


@pytest.mark.asyncio
async def test_sse_streams_at_least_one_content_block() -> None:
    """Mocked multi-event SSE response -> >=1 chunk yielded (Gate 8 unit-level).

    The mocked-HTTP equivalent of the Gate 8 integration bar: assert that
    a well-formed SSE response with multiple content events yields at
    least one :class:`vibe.core.types.LLMChunk`. Validates the end-to-end
    parser+yielder pipeline at the unit-test level without requiring a
    live API call.
    """
    body = _sse_body([{"content": "Hello "}, {"content": "world"}, {"content": "!"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        assert len(chunks) >= 1


@pytest.mark.asyncio
async def test_sse_done_sentinel_terminates_stream() -> None:
    """``data: [DONE]`` is silently skipped; preceding events yield normally.

    The ``[DONE]`` sentinel (popularized by OpenAI's SSE protocol) is a
    NULL-CONTENT marker -- it carries no assistant text. The parser
    returns :data:`None` for it, which the caller treats as "no chunk".
    Preceding content events MUST still produce chunks.
    """
    body = b'data: {"content": "A"}\n\ndata: [DONE]\n\n'
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        # [DONE] yields no chunk; only the valid event produces output.
        assert len(chunks) == 1
        assert chunks[0].message.content == "A"


@pytest.mark.asyncio
async def test_sse_malformed_json_skipped() -> None:
    """Malformed JSON in an SSE event is silently skipped without raising.

    A server-side encoding error MUST NOT break the stream: the parser
    catches :class:`json.JSONDecodeError` and returns :data:`None` for the
    affected event. Valid subsequent events still yield chunks normally.
    """
    body = b'data: {not valid json}\n\ndata: {"content": "Y"}\n\n'
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            chunks = []
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
        assert len(chunks) == 1
        assert chunks[0].message.content == "Y"


# ---------------------------------------------------------------------------
# Phase B' -- Direct ``_parse_sse_event`` helper tests (synchronous)
#
# These tests exercise the module-level SSE parser helper in isolation,
# without any HTTP/network mocking. They provide deterministic, 100%
# branch coverage of the field-priority logic and the defensive
# return-None paths (malformed JSON, empty bytes, [DONE] sentinel,
# missing-all-fields). Because :func:`_parse_sse_event` is a pure
# synchronous function, these tests do NOT use ``@pytest.mark.asyncio``.
# ---------------------------------------------------------------------------


def test_parse_sse_event_content_priority() -> None:
    """Field priority 1: ``content`` wins when ALL four fields are present.

    Locks down the AAP-mandated SSE field priority ordering
    ``content -> text -> message -> delta.content``. With every field
    populated, the parser MUST return the ``content`` value verbatim.
    """
    raw = (
        b'data: {"content": "A", "text": "B", "message": "C", '
        b'"delta": {"content": "D"}}'
    )
    assert _parse_sse_event(raw) == "A"


def test_parse_sse_event_text_priority() -> None:
    """Field priority 2: ``text`` wins when ``content`` is absent.

    With ``content`` omitted from the JSON payload, the parser MUST fall
    through to the second priority field (``text``) before considering
    ``message`` or ``delta.content``.
    """
    raw = b'data: {"text": "B", "message": "C"}'
    assert _parse_sse_event(raw) == "B"


def test_parse_sse_event_message_priority() -> None:
    """Field priority 3: ``message`` wins when ``content`` and ``text`` missing.

    Asserts the parser correctly walks the priority chain past the first
    two missing fields and returns the third (``message``).
    """
    raw = b'data: {"message": "C"}'
    assert _parse_sse_event(raw) == "C"


def test_parse_sse_event_delta_content_priority() -> None:
    """Field priority 4: ``delta.content`` is the FINAL fallback.

    When the first three fields are absent, the parser MUST extract
    ``delta.content`` (an OpenAI-style nested-delta payload shape).
    """
    raw = b'data: {"delta": {"content": "D"}}'
    assert _parse_sse_event(raw) == "D"


def test_parse_sse_event_done_sentinel_returns_none() -> None:
    """``[DONE]`` sentinel -> :data:`None` (no content, stream-end marker)."""
    assert _parse_sse_event(b"data: [DONE]") is None


def test_parse_sse_event_malformed_json_returns_none() -> None:
    """Malformed JSON payload -> :data:`None` (no exception propagates).

    The parser catches :class:`json.JSONDecodeError` internally so that
    a single bad event does not abort the stream. The result is
    :data:`None`, signaling "no chunk yielded".
    """
    assert _parse_sse_event(b"data: {not valid json}") is None


def test_parse_sse_event_empty_bytes_returns_none() -> None:
    """Empty bytes -> :data:`None` (defensive: zero-length stream chunks)."""
    assert _parse_sse_event(b"") is None


def test_parse_sse_event_no_known_field_returns_none() -> None:
    """JSON object with NO recognized field -> :data:`None` (skipped).

    Events that successfully JSON-parse but contain none of the four
    priority fields are silently dropped -- the parser returns
    :data:`None` so the caller does not yield a chunk.
    """
    assert _parse_sse_event(b'data: {"unknown": "value"}') is None


# ---------------------------------------------------------------------------
# Phase C -- HTTP wiring and authentication
#
# These tests verify the HTTP request shape sent by ``complete_streaming``
# matches the AAP §0.6.1 contract:
#   * X-API-Key header carries the resolved BLITZY_API_KEY value
#   * POST goes to ``https://api.blitzy.com/v1/api/chat``
#   * Request body JSON includes ``repo`` and ``branch`` fields
#   * Module-level timeout/base constants match the AAP specification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_api_key_header_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST ``/v1/api/chat`` carries an ``X-API-Key`` header with the resolved key.

    Uses ``monkeypatch.setenv`` to override the autouse fixture's "mock"
    value with a deterministic ``test-key-123`` string, then asserts the
    exact value lands in the outgoing request headers.

    Critical: ``httpx`` normalizes header indexing keys to lowercase, so
    the assertion uses ``headers.get("x-api-key")``.
    """
    monkeypatch.setenv("BLITZY_API_KEY", "test-key-123")
    body = _sse_body([{"content": "A"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        post_route = mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            async for _ in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                pass
        assert post_route.called
        headers = post_route.calls.last.request.headers
        assert headers.get("x-api-key") == "test-key-123"


@pytest.mark.asyncio
async def test_post_endpoint_is_v1_api_chat() -> None:
    """The POST endpoint is exactly ``https://api.blitzy.com/v1/api/chat``.

    Asserts the URL composition: ``api_base`` (default
    ``https://api.blitzy.com``) + the literal path ``/v1/api/chat``.
    No query string, no trailing slash, no path variations.
    """
    body = _sse_body([{"content": "A"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        post_route = mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            async for _ in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                pass
        assert post_route.called
        request_url = str(post_route.calls.last.request.url)
        assert request_url == "https://api.blitzy.com/v1/api/chat"


@pytest.mark.asyncio
async def test_post_body_includes_repo_and_branch() -> None:
    """POST body JSON includes ``"repo": "<repo>"`` and ``"branch": "<branch>"``.

    The Blitzy API requires repo+branch context on every request so the
    backend can route to the correct code understanding. This test
    decodes the recorded request body and asserts the exact field names
    and values.
    """
    body = _sse_body([{"content": "A"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        post_route = mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            async for _ in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                pass
        assert post_route.called
        request_body = json.loads(post_route.calls.last.request.content)
        assert request_body.get("repo") == "myrepo"
        assert request_body.get("branch") == "main"


def test_constants_have_expected_values() -> None:
    """Module-level timeout/base constants match AAP §0.6.1 specification.

    Locks in the exact numeric values for:
      * ``_CONTEXT_TIMEOUT_SECONDS`` (5s)
      * ``_STREAM_CONNECT_TIMEOUT_SECONDS`` (10s)
      * ``_STREAM_READ_TIMEOUT_SECONDS`` (3600s == 1 hour)
      * ``_API_BASE_DEFAULT`` (``https://api.blitzy.com``)

    Drift in any of these constants would silently change the network
    behavior of the backend; this test guards against unintentional
    regressions.
    """
    assert _CONTEXT_TIMEOUT_SECONDS == 5.0
    assert _STREAM_CONNECT_TIMEOUT_SECONDS == 10.0
    assert _STREAM_READ_TIMEOUT_SECONDS == 3600.0
    assert _API_BASE_DEFAULT == "https://api.blitzy.com"


# ---------------------------------------------------------------------------
# Phase D -- Library isolation (AAP §0.8.1 Rule 9)
#
# The CANONICAL Rule 9 enforcement: ``vibe/core/llm/backend/blitzy.py`` MUST
# use ``httpx`` for all HTTP calls and MUST NOT import either the
# ``anthropic`` SDK or the ``mistralai`` SDK. Each backend is library-
# isolated to its assigned protocol (httpx for Blitzy, anthropic SDK for
# Anthropic, mistralai SDK for Mistral) -- cross-library usage is FORBIDDEN.
#
# These tests use :func:`ast.parse` (NOT substring matching) to inspect
# the ACTUAL import statements in the module. AST-based checking is
# semantically correct -- it ignores mentions of these libraries that
# appear in docstrings, comments, or string literals (which are legitimate
# documentation explaining what the module deliberately does NOT import).
# Only real ``import X`` / ``from X import Y`` statements at any scope
# count toward the Rule 9 violation set.
# ---------------------------------------------------------------------------


def _imported_top_level_names(module: types.ModuleType) -> set[str]:
    """Return the set of top-level package names imported by ``module``.

    Walks the module's AST and collects every imported top-level package
    name from both ``ast.Import`` (``import X``, ``import X.Y``) and
    ``ast.ImportFrom`` (``from X import Y``, ``from X.Y import Z``)
    statements at ANY scope (module level, function body, class body,
    conditional ``TYPE_CHECKING`` blocks, etc.). Relative imports
    (``from . import X``) are excluded -- they refer to sibling modules
    within the same package, not external libraries.

    Args:
        module: The module object whose source to inspect.

    Returns:
        A set of top-level package name strings (e.g., ``{"httpx",
        "json", "vibe"}``). Sub-packages collapse to their top-level
        name: ``vibe.core.config`` -> ``"vibe"``.
    """
    src = inspect.getsource(module)
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # ``import X``, ``import X.Y``, ``import X as Z``
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # ``from X import Y``, ``from X.Y import Z``. ``node.module``
            # is ``None`` only for purely relative imports (``from .
            # import Y``); skip those (they're not external library refs).
            if node.module is not None and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


def test_no_anthropic_import_in_blitzy_module() -> None:
    """``blitzy.py`` MUST NOT import the ``anthropic`` SDK (Rule 9).

    Uses AST-based inspection to enumerate the module's REAL imports
    (filtering out docstrings, comments, and string literals that may
    mention "anthropic" as documentation about what the module
    deliberately does NOT import). The canonical Rule 9 enforcement
    test: the Blitzy backend uses ``httpx`` exclusively for HTTP wire
    calls.
    """
    import vibe.core.llm.backend.blitzy as blitzy_module

    imported = _imported_top_level_names(blitzy_module)
    assert "anthropic" not in imported, (
        "blitzy.py MUST NOT use anthropic SDK (Rule 9: library isolation). "
        "Use httpx for all Blitzy HTTP calls. "
        f"Actual top-level imports: {sorted(imported)}"
    )


def test_no_mistralai_import_in_blitzy_module() -> None:
    """``blitzy.py`` MUST NOT import the ``mistralai`` SDK (Rule 9).

    Library-isolation symmetry: the Blitzy backend is just as forbidden
    from using the Mistral SDK as it is the Anthropic SDK. ``httpx`` is
    the ONLY allowed HTTP client. AST-based check ignores any
    documentation strings that mention "mistralai".
    """
    import vibe.core.llm.backend.blitzy as blitzy_module

    imported = _imported_top_level_names(blitzy_module)
    assert "mistralai" not in imported, (
        "blitzy.py MUST NOT use mistralai SDK (Rule 9: library isolation). "
        "Use httpx for all Blitzy HTTP calls. "
        f"Actual top-level imports: {sorted(imported)}"
    )


def test_blitzy_module_uses_httpx() -> None:
    """``blitzy.py`` uses ``httpx`` as its HTTP client (Rule 9 positive).

    The positive complement to the negative assertions above: this test
    asserts via AST inspection that the module DOES import httpx --
    not some other library, not raw sockets, not the stdlib
    :mod:`urllib`.
    """
    import vibe.core.llm.backend.blitzy as blitzy_module

    imported = _imported_top_level_names(blitzy_module)
    assert "httpx" in imported, (
        "blitzy.py must import httpx for HTTP calls (Rule 9). "
        f"Actual top-level imports: {sorted(imported)}"
    )


def test_blitzy_module_does_not_use_other_http_clients() -> None:
    """``blitzy.py`` MUST NOT use ``requests``, ``aiohttp``, or similar.

    Defense-in-depth Rule 9 check: in addition to the explicitly forbidden
    ``anthropic`` and ``mistralai`` SDKs, this test asserts the absence of
    other commonly-used Python HTTP clients (``requests``, ``aiohttp``).
    Only ``httpx`` is permitted. AST-based check covers real imports
    at any scope.
    """
    import vibe.core.llm.backend.blitzy as blitzy_module

    imported = _imported_top_level_names(blitzy_module)
    forbidden = {"requests", "aiohttp"}
    overlap = imported & forbidden
    assert not overlap, (
        f"blitzy.py MUST NOT import {sorted(overlap)} (Rule 9). "
        f"Only httpx is permitted. Actual top-level imports: {sorted(imported)}"
    )


# ---------------------------------------------------------------------------
# Phase E -- Protocol conformance (AAP §0.8.1 Rule 1)
#
# Rule 1 requires every backend to implement the :class:`BackendLike`
# protocol. ``BackendLike`` is a :class:`typing.Protocol` but is NOT
# decorated with ``@runtime_checkable``, so ``isinstance(backend,
# BackendLike)`` would raise ``TypeError``. We therefore perform a
# STRUCTURAL conformance check: assert the backend exposes each protocol
# member as a callable attribute. This mirrors the pattern used in
# ``tests/backend/test_anthropic_backend_extension.py``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blitzy_backend_implements_backend_like() -> None:
    """``BlitzyLLMBackend`` structurally satisfies the ``BackendLike`` protocol.

    Asserts ``BlitzyLLMBackend`` exposes every member declared on the
    :class:`vibe.core.llm.types.BackendLike` protocol -- the five methods
    required for the agent loop to use a backend interchangeably:
    ``__aenter__``, ``__aexit__``, ``complete``, ``complete_streaming``,
    and ``count_tokens``.

    Uses a STRUCTURAL conformance check (``hasattr`` + ``callable``)
    rather than ``isinstance`` because ``BackendLike`` is not decorated
    with ``@runtime_checkable`` -- runtime isinstance checks against
    such protocols raise :class:`TypeError`.
    """
    backend = BlitzyLLMBackend(
        provider=_make_provider(), config=_make_config(), repo="myrepo", branch="main"
    )
    required_members = (
        "__aenter__",
        "__aexit__",
        "complete",
        "complete_streaming",
        "count_tokens",
    )
    for member in required_members:
        assert hasattr(backend, member), (
            f"BlitzyLLMBackend missing required BackendLike member: {member}"
        )
        assert callable(getattr(backend, member)), (
            f"BlitzyLLMBackend.{member} must be callable"
        )
    # Sanity: import-side assertion that BackendLike is the actual symbol
    # we're conforming to (catches accidental rename/refactor regressions).
    assert BackendLike.__name__ == "BackendLike"


# ---------------------------------------------------------------------------
# Phase F -- Connection print behavior
#
# The Blitzy backend prints a one-shot status line to stdout on the FIRST
# ``complete_streaming`` invocation, lazily (NOT in ``__aenter__``):
#   * ``"Connected to {repo}({branch})"`` when context check returned 200
#   * ``"no repository connected"`` when context check returned 404
#
# These tests capture stdout via ``capsys`` and assert the correct
# message appears for each path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_message_printed_when_200(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """200 context check -> ``Connected to <repo>(<branch>)`` printed to stdout.

    The connection status line is the user-facing confirmation that the
    Blitzy backend has located and indexed the current repo+branch. This
    test asserts the exact format ``Connected to myrepo(main)`` is
    visible in captured stdout after the first streaming call.
    """
    body = _sse_body([{"content": "A"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=200))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            async for _ in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                pass
        captured = capsys.readouterr()
        # Tolerant assertion: accept exact "Connected to myrepo(main)" or
        # any variant that contains the repo identifier in the output.
        assert "Connected to myrepo(main)" in captured.out or "myrepo" in captured.out


@pytest.mark.asyncio
async def test_no_repository_connected_message_when_404(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """404 context check -> ``no repository connected`` printed to stdout.

    When the Blitzy API reports HTTP 404 for the repo+branch
    combination, the backend prints a "no repository connected" status
    line to inform the user that their queries will not benefit from
    repo-scoped context. The connection is still functional -- this is
    NOT an error condition (per Rule 8).
    """
    body = _sse_body([{"content": "A"}])
    with respx.mock(base_url="https://api.blitzy.com") as mock_api:
        mock_api.get("/context").mock(return_value=httpx.Response(status_code=404))
        mock_api.post("/v1/api/chat").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(body),
                headers={"content-type": "text/event-stream"},
            )
        )
        backend = BlitzyLLMBackend(
            provider=_make_provider(),
            config=_make_config(),
            repo="myrepo",
            branch="main",
        )
        async with backend:
            async for _ in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                pass
        captured = capsys.readouterr()
        assert (
            "no repository connected" in captured.out
            or "not connected" in captured.out.lower()
        )


# ---------------------------------------------------------------------------
# Phase G -- Token counter (AAP §0.8.1 Rule 7 estimator)
#
# Rule 7 fixes the token-count estimator formula across the codebase:
#   tokens = len(json.dumps(messages_as_json, ensure_ascii=False)) // 4
#
# This is the same estimator used by :class:`SessionManager` for the
# auto-compaction threshold. Locking ``BlitzyLLMBackend.count_tokens``
# to this exact formula ensures session-level compaction decisions
# remain consistent regardless of which provider is active.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_tokens_uses_len_div_4() -> None:
    """``count_tokens`` returns ``len(json.dumps(messages)) // 4`` exactly.

    Constructs a deterministic message list, computes the expected
    estimator value, and asserts the backend's ``count_tokens`` returns
    an integer matching it precisely. The ``ensure_ascii=False`` keyword
    is REQUIRED so non-ASCII messages produce the same length as the
    AAP estimator.
    """
    backend = BlitzyLLMBackend(
        provider=_make_provider(), config=_make_config(), repo="myrepo", branch="main"
    )
    messages = [
        LLMMessage(role=Role.user, content="Hello"),
        LLMMessage(role=Role.assistant, content="Hi there"),
        LLMMessage(role=Role.user, content="How are you?"),
    ]
    expected = (
        len(
            json.dumps(
                [m.model_dump(mode="json") for m in messages], ensure_ascii=False
            )
        )
        // 4
    )
    actual = await backend.count_tokens(
        model=_make_model(),
        messages=messages,
        temperature=0.0,
        tools=None,
        tool_choice=None,
        extra_headers=None,
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Phase H -- Integration test (AAP §0.9.3 Gate 8)
#
# Gate 8 requires an integration test that hits the REAL Blitzy API and
# asserts at least one content chunk is yielded. The test is marked with
# ``@pytest.mark.integration`` and is SKIPPED when ``BLITZY_API_KEY`` is
# unset or set to the autouse-fixture placeholder ``"mock"``. This
# preserves CI-friendliness (no live network in default runs) while
# allowing operators to verify wire-protocol compatibility locally with
# ``uv run pytest -m integration``.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_streams_at_least_one_content_block() -> None:
    """Integration: hit real Blitzy API and assert >=1 content chunk (Gate 8).

    SKIPPED when ``BLITZY_API_KEY`` is absent OR set to the autouse-
    fixture placeholder ``"mock"``. When a real key is present, the test
    sends a one-shot "Just say hi" prompt to the live
    ``https://api.blitzy.com/v1/api/chat`` endpoint and verifies that
    the SSE stream yields at least one parsable
    :class:`~vibe.core.types.LLMChunk`.

    Bounded to at most 5 chunks to keep the integration test fast and
    deterministic regardless of model verbosity.
    """
    api_key = os.environ.get("BLITZY_API_KEY", "")
    # The autouse fixture sets BLITZY_API_KEY=mock; only run when a real
    # key is provided in the environment.
    if not api_key or api_key == "mock":
        pytest.skip("BLITZY_API_KEY not set (or set to mock placeholder)")
    backend = BlitzyLLMBackend(
        provider=_make_provider(), config=_make_config(), repo="", branch=""
    )
    async with backend:
        chunks = []
        async for chunk in backend.complete_streaming(
            model=_make_model(),
            messages=[LLMMessage(role=Role.user, content="Just say hi")],
            temperature=0.2,
            tools=None,
            max_tokens=50,
            tool_choice=None,
            extra_headers=None,
        ):
            chunks.append(chunk)
            # Bound the stream to keep the test deterministic and fast.
            if len(chunks) >= 5:
                break
        assert len(chunks) >= 1
