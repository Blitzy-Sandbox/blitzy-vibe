"""Coverage tests for the legacy code paths of ``vibe/core/llm/backend/anthropic_llm.py``.

This file complements ``tests/backend/test_anthropic_backend_extension.py``
(which covers the AAP-introduced surfaces — API-key chain,
``anthropic_model`` config field, library isolation, constructor
signature) with focused tests for the pre-existing code paths that the
AAP did *not* modify but that the file-wide coverage gate (Gate 10,
≥80%) requires us to exercise:

- :class:`AnthropicMapper` — request/response adapter:
    * ``prepare_messages`` for every :class:`Role` arm
      (system, user, assistant with text only, assistant with
      tool_calls, assistant with malformed tool args, tool result
      merging into a preceding user block, tool result starting a new
      user block)
    * ``prepare_tool`` for the tool-schema shape
    * ``prepare_tool_choice`` for every ``StrToolChoice`` arm and for
      the explicit-tool form
    * ``parse_response`` for text-only, thinking-only, tool_use-only,
      and combined-block responses
- :meth:`AnthropicBackend.complete` — non-streaming completion happy
  path and the two error funnels (``APIStatusError`` →
  ``BackendError`` via ``build_http_error``; ``APIConnectionError`` →
  ``BackendError`` via ``build_request_error``)
- :meth:`AnthropicBackend.complete_streaming` — the streaming event
  loop (``message_start`` → ``content_block_start`` →
  ``content_block_delta`` → ``message_delta``) and the same two error
  funnels
- :meth:`AnthropicBackend._on_content_block_start` — both the
  text-block branch (returns ``[]``) and the tool_use branch
- :meth:`AnthropicBackend._on_content_block_delta` — every delta type
  (text_delta, thinking_delta, input_json_delta into a registered
  tool, input_json_delta against an unknown index, unknown delta type)
- :meth:`AnthropicBackend.count_tokens` — system-only path,
  tools-included path, and the empty-config-anthropic_model fallback
  to ``model.name``

Every test is hermetic: no real Anthropic SDK calls are made. The
``anthropic.AsyncAnthropic`` client is patched out for the few tests
that need an actual client instance. ``ANTHROPIC_API_KEY`` is set via
``monkeypatch.setenv`` so the constructor's three-tier resolver
short-circuits on the env-var tier (per
``tests/conftest.py:_mock_api_key`` it is already set to ``"mock"`` —
we override it for explicit clarity).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from vibe.core.config import Backend, ModelConfig, ProviderConfig, VibeConfig
from vibe.core.llm.backend.anthropic_llm import AnthropicBackend, AnthropicMapper
from vibe.core.llm.exceptions import BackendError
from vibe.core.types import (
    AvailableFunction,
    AvailableTool,
    FunctionCall,
    LLMMessage,
    Role,
    ToolCall,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Helpers — shared providers, models, configs, mocked clients
# ---------------------------------------------------------------------------


def _provider() -> ProviderConfig:
    """Return a minimal Anthropic :class:`ProviderConfig`."""
    return ProviderConfig(
        name="anthropic",
        api_base="https://api.anthropic.com",
        api_key_env_var="ANTHROPIC_API_KEY",
        backend=Backend.ANTHROPIC,
    )


def _model(name: str = "claude-sonnet-4-6") -> ModelConfig:
    """Return a :class:`ModelConfig` for the Anthropic provider."""
    return ModelConfig(name=name, provider="anthropic", alias="anthropic-test")


def _config(anthropic_model: str | None = None) -> VibeConfig:
    """Return a :class:`VibeConfig`, optionally overriding ``anthropic_model``."""
    if anthropic_model is None:
        return VibeConfig()
    return VibeConfig(anthropic_model=anthropic_model)


def _fake_client() -> MagicMock:
    """Build a :class:`MagicMock` mimicking :class:`anthropic.AsyncAnthropic`.

    ``close`` is an :class:`AsyncMock` because the production
    ``__aexit__`` awaits it. ``messages.create``, ``messages.stream``,
    and ``messages.count_tokens`` are left as plain :class:`MagicMock`
    children so individual tests can replace them with their own stubs.
    """
    client = MagicMock()
    client.close = AsyncMock(return_value=None)
    return client


def _tool(name: str = "list_files", description: str = "List files") -> AvailableTool:
    """Return an :class:`AvailableTool` carrying a small JSON Schema."""
    return AvailableTool(
        type="function",
        function=AvailableFunction(
            name=name,
            description=description,
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
    )


# ---------------------------------------------------------------------------
# AnthropicMapper.prepare_messages — every Role arm
# ---------------------------------------------------------------------------


class TestAnthropicMapperPrepareMessages:
    """Cover every branch of :meth:`AnthropicMapper.prepare_messages`."""

    def test_system_role_becomes_top_level_system(self) -> None:
        """A ``Role.system`` message becomes the returned ``system`` string."""
        mapper = AnthropicMapper()
        system, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.system, content="be brief")
        ])
        assert system == "be brief"
        assert msgs == []

    def test_system_role_with_none_content_becomes_empty_string(self) -> None:
        """A ``None`` content on a system message yields the empty string."""
        mapper = AnthropicMapper()
        system, msgs = mapper.prepare_messages([LLMMessage(role=Role.system)])
        assert system == ""
        assert msgs == []

    def test_user_role_passes_through_as_user_message(self) -> None:
        """A user message is forwarded with ``role='user'`` and string content."""
        mapper = AnthropicMapper()
        system, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.user, content="hello")
        ])
        assert system is None
        assert msgs == [{"role": "user", "content": "hello"}]

    def test_user_role_with_none_content_uses_empty_string(self) -> None:
        """A user message with ``None`` content yields the empty string."""
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([LLMMessage(role=Role.user)])
        assert msgs == [{"role": "user", "content": ""}]

    def test_assistant_role_with_text_only(self) -> None:
        """An assistant message with text becomes a single ``text`` content block."""
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.assistant, content="response text")
        ])
        assert msgs == [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "response text"}],
            }
        ]

    def test_assistant_role_with_tool_calls(self) -> None:
        """Tool calls produce ``tool_use`` content blocks alongside text."""
        mapper = AnthropicMapper()
        tool_call = ToolCall(
            id="toolu_abc123",
            function=FunctionCall(name="read_file", arguments='{"path": "x"}'),
        )
        _, msgs = mapper.prepare_messages([
            LLMMessage(
                role=Role.assistant, content="invoking tool", tool_calls=[tool_call]
            )
        ])
        assert msgs[0]["role"] == "assistant"
        blocks = msgs[0]["content"]
        # The text block precedes the tool_use block per source ordering.
        assert blocks[0] == {"type": "text", "text": "invoking tool"}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["id"] == "toolu_abc123"
        assert blocks[1]["name"] == "read_file"
        assert blocks[1]["input"] == {"path": "x"}

    def test_assistant_role_with_malformed_tool_arguments(self) -> None:
        """Invalid JSON arguments fall back to an empty dict (not an exception)."""
        mapper = AnthropicMapper()
        bad_call = ToolCall(
            id="toolu_bad",
            function=FunctionCall(name="frob", arguments="not-valid-json{"),
        )
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.assistant, tool_calls=[bad_call])
        ])
        # JSONDecodeError is swallowed; ``input`` falls back to ``{}``.
        assert msgs[0]["content"][0]["input"] == {}
        assert msgs[0]["content"][0]["type"] == "tool_use"

    def test_assistant_role_with_none_tool_arguments(self) -> None:
        """``None`` arguments parse as the empty-object default."""
        mapper = AnthropicMapper()
        call = ToolCall(id="toolu_none", function=FunctionCall(name="t"))
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.assistant, tool_calls=[call])
        ])
        assert msgs[0]["content"][0]["input"] == {}

    def test_assistant_role_with_tool_call_missing_id_generates_uuid(self) -> None:
        """When ``ToolCall.id`` is ``None``, a synthetic ``toolu_`` id is used."""
        mapper = AnthropicMapper()
        call = ToolCall(id=None, function=FunctionCall(name="t", arguments="{}"))
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.assistant, tool_calls=[call])
        ])
        assert msgs[0]["content"][0]["id"].startswith("toolu_")

    def test_assistant_role_with_only_tool_calls_yields_no_text_block(self) -> None:
        """When ``content`` is empty/falsy, no leading ``text`` block is emitted."""
        mapper = AnthropicMapper()
        call = ToolCall(id="t1", function=FunctionCall(name="t", arguments="{}"))
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.assistant, content="", tool_calls=[call])
        ])
        # No text-only block precedes the tool_use.
        assert msgs[0]["content"][0]["type"] == "tool_use"
        assert len(msgs[0]["content"]) == 1

    def test_assistant_with_no_content_or_tool_calls_emits_empty_text_block(
        self,
    ) -> None:
        """A bare assistant message still produces a placeholder ``text`` block.

        The Anthropic API rejects assistant messages whose content array
        is empty; the production mapper protects against that by
        emitting ``[{"type": "text", "text": ""}]`` as a fallback.
        """
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([LLMMessage(role=Role.assistant)])
        assert msgs[0]["content"] == [{"type": "text", "text": ""}]

    def test_tool_result_starts_new_user_block_when_no_prior_user(self) -> None:
        """A ``Role.tool`` message creates a fresh ``user`` message when needed."""
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.tool, content="result-text", tool_call_id="toolu_xx")
        ])
        assert msgs == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_xx",
                        "content": "result-text",
                    }
                ],
            }
        ]

    def test_tool_result_with_missing_id_uses_empty_string(self) -> None:
        """A ``Role.tool`` message without ``tool_call_id`` falls back to ``""``."""
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([LLMMessage(role=Role.tool, content="r")])
        assert msgs[0]["content"][0]["tool_use_id"] == ""

    def test_consecutive_tool_results_merge_into_single_user_block(self) -> None:
        """Adjacent tool results are appended to the same user message."""
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.tool, content="a", tool_call_id="t1"),
            LLMMessage(role=Role.tool, content="b", tool_call_id="t2"),
        ])
        assert len(msgs) == 1
        # Both tool_results live inside the same user message's content list.
        assert len(msgs[0]["content"]) == 2
        assert msgs[0]["content"][0]["tool_use_id"] == "t1"
        assert msgs[0]["content"][1]["tool_use_id"] == "t2"

    def test_tool_result_appends_after_assistant_starts_new_user_block(self) -> None:
        """A tool result after a non-list-content user message creates a new block.

        The merge branch only fires when the previous message has a
        list-shaped content (i.e. it is itself a tool_result holder).
        A plain text-content user message is NOT eligible for merging.
        """
        mapper = AnthropicMapper()
        _, msgs = mapper.prepare_messages([
            LLMMessage(role=Role.user, content="hi"),
            LLMMessage(role=Role.tool, content="r", tool_call_id="t1"),
        ])
        # Two separate user-role messages: the original plus a new one for the result.
        assert len(msgs) == 2
        assert msgs[0]["content"] == "hi"
        assert isinstance(msgs[1]["content"], list)
        assert msgs[1]["content"][0]["type"] == "tool_result"

    def test_full_conversation_round_trip(self) -> None:
        """A realistic conversation (system+user+assistant+tool) preserves order."""
        mapper = AnthropicMapper()
        messages = [
            LLMMessage(role=Role.system, content="sys"),
            LLMMessage(role=Role.user, content="q1"),
            LLMMessage(
                role=Role.assistant,
                content="thinking",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        function=FunctionCall(name="search", arguments='{"q":"x"}'),
                    )
                ],
            ),
            LLMMessage(role=Role.tool, content="hit", tool_call_id="t1"),
            LLMMessage(role=Role.assistant, content="done"),
        ]
        system, msgs = mapper.prepare_messages(messages)
        assert system == "sys"
        # user, assistant(text+tool_use), user(tool_result), assistant(text)
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# AnthropicMapper.prepare_tool and prepare_tool_choice
# ---------------------------------------------------------------------------


class TestAnthropicMapperPrepareTool:
    """Cover :meth:`AnthropicMapper.prepare_tool`."""

    def test_prepare_tool_emits_anthropic_tool_schema(self) -> None:
        """``prepare_tool`` maps internal tool shape to Anthropic's schema keys."""
        mapper = AnthropicMapper()
        result = mapper.prepare_tool(_tool(name="list_files", description="lst"))
        assert result == {
            "name": "list_files",
            "description": "lst",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }

    def test_prepare_tool_handles_empty_description(self) -> None:
        """Empty descriptions are passed through as the empty string."""
        mapper = AnthropicMapper()
        tool = AvailableTool(
            function=AvailableFunction(
                name="x", description="", parameters={"type": "object"}
            )
        )
        result = mapper.prepare_tool(tool)
        assert result["description"] == ""


class TestAnthropicMapperPrepareToolChoice:
    """Cover every arm of :meth:`AnthropicMapper.prepare_tool_choice`."""

    def test_tool_choice_auto(self) -> None:
        """``"auto"`` maps to ``{"type": "auto"}``."""
        assert AnthropicMapper().prepare_tool_choice("auto") == {"type": "auto"}

    def test_tool_choice_none(self) -> None:
        """``"none"`` maps to ``{"type": "none"}``."""
        assert AnthropicMapper().prepare_tool_choice("none") == {"type": "none"}

    def test_tool_choice_any(self) -> None:
        """``"any"`` maps to ``{"type": "any"}``."""
        assert AnthropicMapper().prepare_tool_choice("any") == {"type": "any"}

    def test_tool_choice_required_maps_to_any(self) -> None:
        """``"required"`` is normalized to ``"any"`` for Anthropic."""
        assert AnthropicMapper().prepare_tool_choice("required") == {"type": "any"}

    def test_tool_choice_unknown_string_falls_back_to_auto(self) -> None:
        """An unknown literal value falls back to ``"auto"`` as a safe default.

        The ``case _:`` arm in ``prepare_tool_choice`` guards against
        future :class:`StrToolChoice` Literal additions accidentally
        producing a ``KeyError`` at runtime.
        """
        # type: ignore[arg-type] — we intentionally test the fallback arm.
        assert AnthropicMapper().prepare_tool_choice("bogus") == {  # type: ignore[arg-type]
            "type": "auto"
        }

    def test_tool_choice_explicit_tool_name(self) -> None:
        """An ``AvailableTool`` forces the model to call that specific tool."""
        tool = _tool(name="forced_tool")
        result = AnthropicMapper().prepare_tool_choice(tool)
        assert result == {"type": "tool", "name": "forced_tool"}


# ---------------------------------------------------------------------------
# AnthropicMapper.parse_response — extract content/reasoning/tool_calls
# ---------------------------------------------------------------------------


class TestAnthropicMapperParseResponse:
    """Cover the response-parsing branches of :meth:`AnthropicMapper.parse_response`."""

    def test_parse_response_text_only(self) -> None:
        """A response of pure ``text`` blocks concatenates to a single string."""
        mapper = AnthropicMapper()
        block1 = MagicMock()
        block1.type = "text"
        block1.text = "Hello "
        block2 = MagicMock()
        block2.type = "text"
        block2.text = "world"
        response = MagicMock()
        response.content = [block1, block2]
        content, reasoning, tool_calls = mapper.parse_response(response)
        assert content == "Hello world"
        assert reasoning is None
        assert tool_calls is None

    def test_parse_response_thinking_only(self) -> None:
        """``thinking`` blocks populate ``reasoning_content`` only."""
        mapper = AnthropicMapper()
        block = MagicMock()
        block.type = "thinking"
        block.thinking = "internal reasoning"
        response = MagicMock()
        response.content = [block]
        content, reasoning, tool_calls = mapper.parse_response(response)
        assert content == ""
        assert reasoning == "internal reasoning"
        assert tool_calls is None

    def test_parse_response_tool_use_only(self) -> None:
        """A pure ``tool_use`` response produces a single :class:`ToolCall`."""
        mapper = AnthropicMapper()
        block = MagicMock()
        block.type = "tool_use"
        block.id = "toolu_42"
        block.name = "lookup"
        block.input = {"q": "value"}
        response = MagicMock()
        response.content = [block]
        content, reasoning, tool_calls = mapper.parse_response(response)
        assert content == ""
        assert reasoning is None
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0].id == "toolu_42"
        assert tool_calls[0].function.name == "lookup"
        # Arguments are JSON-encoded for backend-uniform consumption.
        arguments = tool_calls[0].function.arguments
        assert arguments is not None
        assert json.loads(arguments) == {"q": "value"}
        # ``index`` reflects position within ``response.content``.
        assert tool_calls[0].index == 0

    def test_parse_response_combined_blocks(self) -> None:
        """Text + thinking + tool_use combine correctly into one result tuple."""
        mapper = AnthropicMapper()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "preamble"
        thinking_block = MagicMock()
        thinking_block.type = "thinking"
        thinking_block.thinking = "weighing options"
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "t1"
        tool_block.name = "x"
        tool_block.input = {}
        response = MagicMock()
        response.content = [text_block, thinking_block, tool_block]
        content, reasoning, tool_calls = mapper.parse_response(response)
        assert content == "preamble"
        assert reasoning == "weighing options"
        assert tool_calls is not None
        assert len(tool_calls) == 1
        # ``index`` reflects the position in ``response.content`` (not the
        # position among tool_use blocks alone).
        assert tool_calls[0].index == 2


# ---------------------------------------------------------------------------
# AnthropicBackend._on_content_block_start / _on_content_block_delta
# ---------------------------------------------------------------------------


def _make_backend(monkeypatch: pytest.MonkeyPatch) -> AnthropicBackend:
    """Construct an :class:`AnthropicBackend` with a deterministic API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return AnthropicBackend(provider=_provider(), config=_config())


class TestContentBlockStart:
    """Cover :meth:`AnthropicBackend._on_content_block_start`."""

    def test_text_block_start_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``content_block_start`` for a ``text`` block produces no chunks."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.content_block.type = "text"
        chunks = backend._on_content_block_start(event, {})
        assert chunks == []

    def test_tool_use_block_start_returns_initial_chunk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``content_block_start`` for a ``tool_use`` block emits a tool_call chunk."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.content_block.type = "tool_use"
        event.content_block.id = "toolu_77"
        event.content_block.name = "do_it"
        event.index = 5
        current: dict[int, dict] = {}
        chunks = backend._on_content_block_start(event, current)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.message.role == Role.assistant
        assert chunk.message.content == ""
        assert chunk.message.tool_calls is not None
        assert chunk.message.tool_calls[0].id == "toolu_77"
        assert chunk.message.tool_calls[0].function.name == "do_it"
        assert chunk.message.tool_calls[0].function.arguments == ""
        # Index in the ToolCall is the position in our running list, not the SDK index.
        assert chunk.message.tool_calls[0].index == 0
        # The internal mapping is updated with the SDK-level index 5.
        assert 5 in current
        assert current[5]["id"] == "toolu_77"


class TestContentBlockDelta:
    """Cover every branch of :meth:`AnthropicBackend._on_content_block_delta`."""

    def test_text_delta_emits_text_chunk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``text_delta`` produces an assistant chunk with the delta as ``content``."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.delta.type = "text_delta"
        event.delta.text = "partial"
        chunks = backend._on_content_block_delta(event, {})
        assert len(chunks) == 1
        assert chunks[0].message.content == "partial"
        assert chunks[0].message.role == Role.assistant

    def test_thinking_delta_emits_reasoning_chunk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``thinking_delta`` populates ``reasoning_content`` (not ``content``)."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.delta.type = "thinking_delta"
        event.delta.thinking = "reasoning..."
        chunks = backend._on_content_block_delta(event, {})
        assert len(chunks) == 1
        assert chunks[0].message.content == ""
        assert chunks[0].message.reasoning_content == "reasoning..."

    def test_input_json_delta_for_registered_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``input_json_delta`` for a registered tool emits a tool_call delta."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.delta.type = "input_json_delta"
        event.delta.partial_json = '{"k":'
        event.index = 7
        current: dict[int, dict] = {7: {"id": "toolu_9", "name": "tn", "index": 0}}
        chunks = backend._on_content_block_delta(event, current)
        assert len(chunks) == 1
        tool_calls = chunks[0].message.tool_calls
        assert tool_calls is not None
        tc = tool_calls[0]
        assert tc.id == "toolu_9"
        assert tc.function.arguments == '{"k":'
        assert tc.function.name is None  # mid-stream: name is left blank

    def test_input_json_delta_for_unknown_index_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``input_json_delta`` against an unknown index is dropped silently."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.delta.type = "input_json_delta"
        event.delta.partial_json = "{}"
        event.index = 99
        # No entry for index 99 in the registered tool-call map.
        chunks = backend._on_content_block_delta(event, {})
        assert chunks == []

    def test_unknown_delta_type_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown ``delta.type`` produces no chunks (defensive default arm)."""
        backend = _make_backend(monkeypatch)
        event = MagicMock()
        event.delta.type = "future_delta_type"
        chunks = backend._on_content_block_delta(event, {})
        assert chunks == []


# ---------------------------------------------------------------------------
# AnthropicBackend.complete — non-streaming happy path and error funnels
# ---------------------------------------------------------------------------


def _build_anthropic_message(
    text: str = "ok", input_tokens: int = 12, output_tokens: int = 34
) -> MagicMock:
    """Construct a :class:`MagicMock` mimicking ``anthropic.types.Message``.

    The mock carries one ``text`` content block plus a usage object with
    the integer token counts the production code reads at the end of
    :meth:`complete`.
    """
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return msg


class TestCompleteHappyPath:
    """Cover the happy path of :meth:`AnthropicBackend.complete`."""

    @pytest.mark.asyncio
    async def test_complete_returns_llmchunk_with_text_and_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful ``complete`` returns text + token usage in an :class:`LLMChunk`."""
        backend = _make_backend(monkeypatch)
        fake_client = _fake_client()
        fake_message = _build_anthropic_message(
            text="hi there", input_tokens=10, output_tokens=20
        )
        fake_client.messages.create = AsyncMock(return_value=fake_message)

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                result = await backend.complete(
                    model=_model(),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.0,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                )

        assert result.message.role == Role.assistant
        assert result.message.content == "hi there"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 20

    @pytest.mark.asyncio
    async def test_complete_forwards_tools_and_tool_choice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``tools`` and ``tool_choice`` are forwarded into ``messages.create`` kwargs."""
        backend = _make_backend(monkeypatch)
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _build_anthropic_message()

        fake_client = _fake_client()
        fake_client.messages.create = _capture

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                await backend.complete(
                    model=_model(),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.0,
                    tools=[_tool()],
                    max_tokens=None,
                    tool_choice="auto",
                    extra_headers={"X-Trace": "abc"},
                )

        assert "tools" in captured and len(captured["tools"]) == 1
        assert captured["tool_choice"] == {"type": "auto"}
        # ``extra_headers`` round-trips into the SDK kwargs.
        assert captured["extra_headers"] == {"X-Trace": "abc"}


class TestCompleteErrorPaths:
    """Cover the two error funnels in :meth:`AnthropicBackend.complete`."""

    @pytest.mark.asyncio
    async def test_complete_raises_backend_error_on_api_status_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``APIStatusError`` is reshaped into a :class:`BackendError`."""
        backend = _make_backend(monkeypatch)
        fake_client = _fake_client()
        response = httpx.Response(
            status_code=429,
            headers={"content-type": "application/json"},
            text='{"error":{"message":"rate limited"}}',
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        api_err = anthropic.APIStatusError("rate limited", response=response, body=None)
        fake_client.messages.create = AsyncMock(side_effect=api_err)

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                with pytest.raises(BackendError) as exc_info:
                    await backend.complete(
                        model=_model(),
                        messages=[LLMMessage(role=Role.user, content="hi")],
                        temperature=0.0,
                        tools=None,
                        max_tokens=None,
                        tool_choice=None,
                        extra_headers=None,
                    )

        # The reshaped error carries the upstream HTTP status code.
        assert exc_info.value.status == 429
        assert exc_info.value.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_complete_raises_backend_error_on_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``APIConnectionError`` is reshaped into a :class:`BackendError`."""
        backend = _make_backend(monkeypatch)
        fake_client = _fake_client()
        connect_err = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        fake_client.messages.create = AsyncMock(side_effect=connect_err)

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                with pytest.raises(BackendError) as exc_info:
                    await backend.complete(
                        model=_model(),
                        messages=[LLMMessage(role=Role.user, content="hi")],
                        temperature=0.0,
                        tools=None,
                        max_tokens=None,
                        tool_choice=None,
                        extra_headers=None,
                    )

        # Connection errors carry no upstream status code.
        assert exc_info.value.status is None
        assert exc_info.value.provider == "anthropic"


# ---------------------------------------------------------------------------
# AnthropicBackend.complete_streaming — event loop and error funnels
# ---------------------------------------------------------------------------


class _StreamEventBuilder:
    """Compact helper for constructing synthetic SDK stream events."""

    @staticmethod
    def message_start(input_tokens: int) -> MagicMock:
        e = MagicMock()
        e.type = "message_start"
        e.message.usage.input_tokens = input_tokens
        return e

    @staticmethod
    def text_delta(text: str) -> MagicMock:
        e = MagicMock()
        e.type = "content_block_delta"
        e.delta.type = "text_delta"
        e.delta.text = text
        return e

    @staticmethod
    def message_delta(output_tokens: int) -> MagicMock:
        e = MagicMock()
        e.type = "message_delta"
        e.usage.output_tokens = output_tokens
        return e

    @staticmethod
    def content_block_start_text() -> MagicMock:
        e = MagicMock()
        e.type = "content_block_start"
        e.content_block.type = "text"
        return e


class _ProgrammableStream:
    """An async iterable that yields a programmer-provided event list."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aiter__(self) -> Any:
        for e in self._events:
            yield e


class _ProgrammableStreamCtx:
    """Async context manager wrapping :class:`_ProgrammableStream` for the SDK shape."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self) -> _ProgrammableStream:
        return _ProgrammableStream(self._events)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        return None


class TestCompleteStreamingHappyPath:
    """Cover the event-dispatch loop of :meth:`AnthropicBackend.complete_streaming`."""

    @pytest.mark.asyncio
    async def test_streaming_yields_text_chunks_and_final_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typical stream (start → 2 text_deltas → message_delta) yields 3 chunks.

        The final chunk carries the prompt/completion token counts from
        the closing ``message_delta`` event.
        """
        backend = _make_backend(monkeypatch)
        events = [
            _StreamEventBuilder.message_start(input_tokens=8),
            _StreamEventBuilder.text_delta("hel"),
            _StreamEventBuilder.text_delta("lo"),
            _StreamEventBuilder.message_delta(output_tokens=2),
        ]

        fake_client = _fake_client()
        fake_client.messages.stream = lambda **_kwargs: _ProgrammableStreamCtx(events)

        chunks = []
        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                async for chunk in backend.complete_streaming(
                    model=_model(),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.0,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                ):
                    chunks.append(chunk)

        # Two text deltas + the synthesized final usage chunk.
        assert len(chunks) == 3
        assert chunks[0].message.content == "hel"
        assert chunks[1].message.content == "lo"
        assert chunks[2].usage is not None
        assert chunks[2].usage.prompt_tokens == 8
        assert chunks[2].usage.completion_tokens == 2

    @pytest.mark.asyncio
    async def test_streaming_handles_text_content_block_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``content_block_start`` for a text block is forwarded but yields nothing.

        The branch is exercised purely for coverage of the
        ``elif event.type == "content_block_start"`` arm in
        :meth:`_iter_anthropic_stream_chunks`.
        """
        backend = _make_backend(monkeypatch)
        events = [
            _StreamEventBuilder.message_start(input_tokens=1),
            _StreamEventBuilder.content_block_start_text(),
            _StreamEventBuilder.message_delta(output_tokens=0),
        ]

        fake_client = _fake_client()
        fake_client.messages.stream = lambda **_kwargs: _ProgrammableStreamCtx(events)

        chunks = []
        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                async for chunk in backend.complete_streaming(
                    model=_model(),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.0,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                ):
                    chunks.append(chunk)

        # Only the final usage chunk is emitted — the text block start
        # produces no consumer-visible output.
        assert len(chunks) == 1
        assert chunks[0].usage is not None


class TestCompleteStreamingErrorPaths:
    """Cover the two error funnels in :meth:`complete_streaming`."""

    @pytest.mark.asyncio
    async def test_streaming_raises_backend_error_on_api_status_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``APIStatusError`` mid-stream is reshaped to :class:`BackendError`."""
        backend = _make_backend(monkeypatch)
        response = httpx.Response(
            status_code=500,
            headers={"content-type": "application/json"},
            text='{"error":{"message":"oops"}}',
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        api_err = anthropic.APIStatusError("oops", response=response, body=None)

        def _raising_stream(**_kwargs: Any) -> Any:
            raise api_err

        fake_client = _fake_client()
        fake_client.messages.stream = _raising_stream

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                with pytest.raises(BackendError) as exc_info:
                    async for _ in backend.complete_streaming(
                        model=_model(),
                        messages=[LLMMessage(role=Role.user, content="hi")],
                        temperature=0.0,
                        tools=None,
                        max_tokens=None,
                        tool_choice=None,
                        extra_headers=None,
                    ):
                        pass

        assert exc_info.value.status == 500
        assert exc_info.value.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_streaming_raises_backend_error_on_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``APIConnectionError`` mid-stream is reshaped to :class:`BackendError`."""
        backend = _make_backend(monkeypatch)
        connect_err = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

        def _raising_stream(**_kwargs: Any) -> Any:
            raise connect_err

        fake_client = _fake_client()
        fake_client.messages.stream = _raising_stream

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                with pytest.raises(BackendError) as exc_info:
                    async for _ in backend.complete_streaming(
                        model=_model(),
                        messages=[LLMMessage(role=Role.user, content="hi")],
                        temperature=0.0,
                        tools=None,
                        max_tokens=None,
                        tool_choice=None,
                        extra_headers=None,
                    ):
                        pass

        assert exc_info.value.status is None


# ---------------------------------------------------------------------------
# AnthropicBackend.count_tokens — system-only and tools-included paths
# ---------------------------------------------------------------------------


class TestCountTokens:
    """Cover :meth:`AnthropicBackend.count_tokens` kwargs assembly."""

    @pytest.mark.asyncio
    async def test_count_tokens_includes_system_kwarg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A system message in the conversation produces a ``system`` SDK kwarg."""
        backend = _make_backend(monkeypatch)
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            result = MagicMock()
            result.input_tokens = 100
            return result

        fake_client = _fake_client()
        fake_client.messages.count_tokens = _capture

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                tokens = await backend.count_tokens(
                    model=_model(),
                    messages=[
                        LLMMessage(role=Role.system, content="sys"),
                        LLMMessage(role=Role.user, content="q"),
                    ],
                )

        assert tokens == 100
        # ``system`` is forwarded as a top-level kwarg, not embedded in messages.
        assert captured.get("system") == "sys"

    @pytest.mark.asyncio
    async def test_count_tokens_forwards_tools_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``tools`` are supplied, ``count_tokens`` forwards them too."""
        backend = _make_backend(monkeypatch)
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            result = MagicMock()
            result.input_tokens = 50
            return result

        fake_client = _fake_client()
        fake_client.messages.count_tokens = _capture

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                tokens = await backend.count_tokens(
                    model=_model(),
                    messages=[LLMMessage(role=Role.user, content="q")],
                    tools=[_tool()],
                )

        assert tokens == 50
        assert "tools" in captured
        assert len(captured["tools"]) == 1
        # The Anthropic tool schema (not the internal AvailableTool) is forwarded.
        assert captured["tools"][0]["name"] == "list_files"

    @pytest.mark.asyncio
    async def test_count_tokens_uses_model_name_when_config_anthropic_model_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty ``config.anthropic_model`` falls back to ``model.name``.

        The ``or model.name`` fallback in ``count_tokens`` only fires
        when ``config.anthropic_model`` is an empty string. This test
        exercises that defensive branch.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        # ``VibeConfig`` allows ``anthropic_model`` to be an empty string;
        # the production code's ``or`` fallback engages in this case.
        config = VibeConfig(anthropic_model="")
        backend = AnthropicBackend(provider=_provider(), config=config)

        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            result = MagicMock()
            result.input_tokens = 1
            return result

        fake_client = _fake_client()
        fake_client.messages.count_tokens = _capture

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                await backend.count_tokens(
                    model=_model(name="model-from-registry"),
                    messages=[LLMMessage(role=Role.user, content="q")],
                )

        # ``model.name`` wins when ``config.anthropic_model`` is empty.
        assert captured.get("model") == "model-from-registry"


# ---------------------------------------------------------------------------
# AnthropicBackend._get_client — lazy client construction branch
# ---------------------------------------------------------------------------


class TestGetClient:
    """Cover :meth:`AnthropicBackend._get_client` lazy construction."""

    def test_get_client_constructs_when_not_entered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling ``_get_client`` outside ``async with`` constructs a fresh client.

        This branch protects ``count_tokens``-style calls that may not
        always be invoked inside an ``async with backend`` block.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        backend = AnthropicBackend(provider=_provider(), config=_config())

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=_fake_client(),
        ) as mock_ctor:
            client_a = backend._get_client()
            client_b = backend._get_client()

        # The constructor is called exactly once; subsequent invocations
        # reuse the cached client instance.
        assert mock_ctor.call_count == 1
        assert client_a is client_b
