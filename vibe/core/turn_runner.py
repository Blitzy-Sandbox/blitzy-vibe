"""LLM turn orchestration extracted from AgentLoop.

This module contains the ``TurnRunner`` class, which handles single LLM turn
orchestration logic previously inlined in ``AgentLoop``.  It is composed into
``AgentLoop`` via ``__init__`` injection (R2 — composition over inheritance).

All methods were moved **verbatim** from ``AgentLoop`` (R1 — extraction over
rewrite) with ``self.<attr>`` references remapped to ``self._loop.<attr>`` so
they access the parent loop's state through the back-reference.

Circular-import safety: ``AgentLoop`` is imported only under
``TYPE_CHECKING`` (PEP 563 deferred annotations), so there is no runtime
import cycle between ``agent_loop`` → ``turn_runner`` → ``agent_loop``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from vibe.core.types import (
    AssistantEvent,
    BaseEvent,
    ReasoningEvent,
)

if TYPE_CHECKING:
    from vibe.core.agent_loop import AgentLoop

__all__ = ["TurnRunner"]


class TurnRunner:
    """Handles single LLM turn orchestration, extracted from AgentLoop.

    Composed into ``AgentLoop`` via ``__init__`` injection (R2).  The owning
    ``AgentLoop`` instance is stored as ``_loop`` and provides access to:

    * ``backend`` — LLM backend
    * ``config`` — session configuration
    * ``messages`` — conversation message history
    * ``format_handler`` — API tool format handler
    * ``tool_manager`` — tool registry / manager
    * ``enable_streaming`` — whether streaming is enabled
    * ``stats`` — session statistics
    * ``session_id`` — current session identifier

    Public methods:

    * :meth:`perform_turn` — orchestrate a full LLM turn (stream/non-stream
      + tool call processing).
    * :meth:`stream_events` — stream ``AssistantEvent`` / ``ReasoningEvent``
      chunks from the LLM backend.
    * :meth:`get_assistant_event` — obtain a single non-streaming assistant
      response.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, loop: AgentLoop) -> None:
        """Initialise with a back-reference to the owning AgentLoop.

        Args:
            loop: The parent ``AgentLoop`` providing backend, config,
                messages, format_handler, tool_manager, and stats.
        """
        self._loop = loop

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def perform_turn(self) -> AsyncGenerator[BaseEvent, None]:
        """Orchestrate a single LLM turn including streaming and tool calls.

        Extracted verbatim from ``AgentLoop._perform_llm_turn()``.

        Delegates to :meth:`stream_events` or :meth:`get_assistant_event` for
        LLM interaction, then parses the response for tool calls and delegates
        tool execution back to the owning ``AgentLoop`` (which in turn
        delegates to ``ToolExecutor``).

        Yields:
            ``BaseEvent`` subclass instances — ``AssistantEvent``,
            ``ReasoningEvent``, ``ToolCallEvent``, ``ToolResultEvent``, and
            ``ToolStreamEvent`` — as the turn progresses.
        """
        if self._loop.enable_streaming:
            async for event in self.stream_events():
                yield event
        else:
            assistant_event = await self.get_assistant_event()
            if assistant_event.content:
                yield assistant_event

        last_message = self._loop.messages[-1]

        parsed = self._loop.format_handler.parse_message(last_message)
        resolved = self._loop.format_handler.resolve_tool_calls(
            parsed, self._loop.tool_manager
        )

        if not resolved.tool_calls and not resolved.failed_calls:
            return

        async for event in self._loop._handle_tool_calls(resolved):
            yield event

    async def stream_events(
        self,
    ) -> AsyncGenerator[AssistantEvent | ReasoningEvent]:
        """Stream assistant and reasoning events from the LLM backend.

        Extracted verbatim from ``AgentLoop._stream_assistant_events()``.

        Content and reasoning chunks are accumulated in internal buffers and
        yielded in batches (``BATCH_SIZE = 5`` chunks) to reduce per-event
        overhead.  When the stream switches between content and reasoning, the
        current buffer is flushed immediately so consumers see interleaved
        events in the correct order.

        Yields:
            ``AssistantEvent`` for content chunks and ``ReasoningEvent`` for
            reasoning/chain-of-thought chunks.
        """
        content_buffer = ""
        reasoning_buffer = ""
        chunks_with_content = 0
        chunks_with_reasoning = 0
        message_id: str | None = None
        BATCH_SIZE = 5

        async for chunk in self._loop._chat_streaming():
            if message_id is None:
                message_id = chunk.message.message_id

            if chunk.message.reasoning_content:
                if content_buffer:
                    yield AssistantEvent(
                        content=content_buffer, message_id=message_id
                    )
                    content_buffer = ""
                    chunks_with_content = 0

                reasoning_buffer += chunk.message.reasoning_content
                chunks_with_reasoning += 1

                if chunks_with_reasoning >= BATCH_SIZE:
                    yield ReasoningEvent(
                        content=reasoning_buffer, message_id=message_id
                    )
                    reasoning_buffer = ""
                    chunks_with_reasoning = 0

            if chunk.message.content:
                if reasoning_buffer:
                    yield ReasoningEvent(
                        content=reasoning_buffer, message_id=message_id
                    )
                    reasoning_buffer = ""
                    chunks_with_reasoning = 0

                content_buffer += chunk.message.content
                chunks_with_content += 1

                if chunks_with_content >= BATCH_SIZE:
                    yield AssistantEvent(
                        content=content_buffer, message_id=message_id
                    )
                    content_buffer = ""
                    chunks_with_content = 0

        if reasoning_buffer:
            yield ReasoningEvent(content=reasoning_buffer, message_id=message_id)

        if content_buffer:
            yield AssistantEvent(content=content_buffer, message_id=message_id)

    async def get_assistant_event(self) -> AssistantEvent:
        """Get a non-streaming assistant response from the LLM backend.

        Extracted verbatim from ``AgentLoop._get_assistant_event()``.

        Returns:
            ``AssistantEvent`` with the complete LLM response content and the
            message identifier assigned by the backend.
        """
        llm_result = await self._loop._chat()
        return AssistantEvent(
            content=llm_result.message.content or "",
            message_id=llm_result.message.message_id,
        )
