from __future__ import annotations

import asyncio

from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config import VibeConfig
from vibe.core.llm.types import BackendLike
from vibe.core.output_formatters import create_formatter
from vibe.core.types import AssistantEvent, LLMMessage, OutputFormat, Role
from vibe.core.utils import ConversationLimitException, logger


def run_programmatic(
    config: VibeConfig,
    prompt: str,
    max_turns: int | None = None,
    max_price: float | None = None,
    output_format: OutputFormat = OutputFormat.TEXT,
    previous_messages: list[LLMMessage] | None = None,
    agent_name: str = BuiltinAgentName.AUTO_APPROVE,
    backend: BackendLike | None = None,
) -> str | None:
    """Run the agent in non-interactive (one-shot batch) mode.

    Accepts an optional ``backend`` instance constructed by the CLI
    orchestration block (``vibe.cli.cli._build_backend_instance``).
    When supplied, the instance is forwarded into :class:`AgentLoop`'s
    ``backend`` parameter, which short-circuits its default
    ``_select_backend()`` fallback (AAP §0.5.4 step Z) -- this closes
    review finding C5-CRIT-01 for the ``-p`` / programmatic execution
    path so that ``vibe -p "..." --provider blitzy|anthropic`` works
    end-to-end at runtime.

    The default ``backend=None`` preserves backward compatibility for
    existing callers (and the unit test suite at
    ``tests/test_cli_programmatic_preload.py``) that mock
    :data:`BACKEND_FACTORY` and rely on ``AgentLoop._select_backend`` to
    pick up the patched factory entry.

    Args:
        config: Active :class:`VibeConfig` instance. Used by AgentLoop
            for system prompt construction, model resolution, and
            pricing/middleware configuration.
        prompt: The user's one-shot prompt text. Required (non-empty
            validation is performed at the CLI layer).
        max_turns: Optional cap on assistant turns. ``None`` disables
            the limit. AgentLoop raises
            :class:`ConversationLimitException` when the cap is
            exceeded.
        max_price: Optional cap on session cost in USD. ``None``
            disables the limit.
        output_format: How to format streamed assistant events for
            stdout. See :class:`OutputFormat`.
        previous_messages: Optional message history to seed the
            conversation with (e.g., from ``--continue``). System
            messages in the input are dropped because :class:`AgentLoop`
            constructs a fresh system prompt at initialization.
        agent_name: Initial agent profile name. Defaults to
            ``BuiltinAgentName.AUTO_APPROVE`` (programmatic mode
            implies tool auto-approval).
        backend: Optional pre-constructed :class:`BackendLike`
            instance. When supplied (typical CLI flow:
            ``--provider blitzy|mistral|anthropic`` resolves to this
            instance via :func:`_build_backend_instance`), AgentLoop
            uses it verbatim. When ``None`` (default; tests, legacy
            callers), AgentLoop falls back to
            :meth:`AgentLoop._select_backend`.

    Returns:
        The final assistant response text (formatted per
        ``output_format``), or ``None`` if no response was produced.
    """
    formatter = create_formatter(output_format)

    agent_loop = AgentLoop(
        config,
        agent_name=agent_name,
        message_observer=formatter.on_message_added,
        max_turns=max_turns,
        max_price=max_price,
        backend=backend,
        enable_streaming=False,
    )
    logger.info("USER: %s", prompt)

    async def _async_run() -> str | None:
        if previous_messages:
            non_system_messages = [
                msg for msg in previous_messages if not (msg.role == Role.system)
            ]
            agent_loop.messages.extend(non_system_messages)
            logger.info(
                "Loaded %d messages from previous session", len(non_system_messages)
            )

        async for event in agent_loop.act(prompt):
            formatter.on_event(event)
            if isinstance(event, AssistantEvent) and event.stopped_by_middleware:
                raise ConversationLimitException(event.content)

        return formatter.finalize()

    return asyncio.run(_async_run())
