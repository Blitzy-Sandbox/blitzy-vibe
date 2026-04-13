from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from enum import StrEnum, auto
import time
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from vibe.core.llm.format import ResolvedMessage, ResolvedToolCall
from vibe.core.tools.base import (
    BaseTool,
    InvokeContext,
    ToolError,
    ToolPermission,
    ToolPermissionError,
)
from vibe.core.tools.manager import NoSuchToolError
from vibe.core.types import (
    ApprovalResponse,
    AsyncApprovalCallback,
    LLMMessage,
    Role,
    SyncApprovalCallback,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
)
from vibe.core.utils import (
    TOOL_ERROR_TAG,
    CancellationReason,
    get_user_cancellation_message,
)

if TYPE_CHECKING:
    from vibe.core.agent_loop import AgentLoop

__all__ = ["ToolDecision", "ToolExecutionResponse", "ToolExecutor"]


class ToolExecutionResponse(StrEnum):
    SKIP = auto()
    EXECUTE = auto()


class ToolDecision(BaseModel):
    verdict: ToolExecutionResponse
    feedback: str | None = None


class ToolExecutor:
    """Handles tool call execution, extracted from AgentLoop.

    Composed into AgentLoop via ``__init__`` injection (R2: composition
    over inheritance).  All methods were moved verbatim from
    ``AgentLoop`` with only ``self.`` → ``self._loop.`` reference changes
    for AgentLoop-owned attributes.
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Moved verbatim from AgentLoop._handle_tool_calls — process resolved
    # tool calls, executing each tool and yielding events.
    async def handle_tool_calls(
        self, resolved: ResolvedMessage
    ) -> AsyncGenerator[ToolCallEvent | ToolResultEvent | ToolStreamEvent]:
        for failed in resolved.failed_calls:
            error_msg = f"<{TOOL_ERROR_TAG}>{failed.tool_name}: {failed.error}</{TOOL_ERROR_TAG}>"

            yield ToolResultEvent(
                tool_name=failed.tool_name,
                tool_class=None,
                error=error_msg,
                tool_call_id=failed.call_id,
            )

            self._loop.stats.tool_calls_failed += 1
            self._loop.messages.append(
                self._loop.format_handler.create_failed_tool_response_message(
                    failed, error_msg
                )
            )

        for tool_call in resolved.tool_calls:
            yield ToolCallEvent(
                tool_name=tool_call.tool_name,
                tool_class=tool_call.tool_class,
                args=tool_call.validated_args,
                tool_call_id=tool_call.call_id,
            )

            try:
                tool_instance = self._loop.tool_manager.get(tool_call.tool_name)
            except (OSError, ImportError, KeyError, NoSuchToolError) as exc:
                error_msg = f"Error getting tool '{tool_call.tool_name}': {exc}"
                yield ToolResultEvent(
                    tool_name=tool_call.tool_name,
                    tool_class=tool_call.tool_class,
                    error=error_msg,
                    tool_call_id=tool_call.call_id,
                )
                self._append_tool_response(tool_call, error_msg)
                continue

            decision = await self._should_execute_tool(
                tool_instance, tool_call.validated_args, tool_call.call_id
            )

            if decision.verdict == ToolExecutionResponse.SKIP:
                self._loop.stats.tool_calls_rejected += 1
                skip_reason = decision.feedback or str(
                    get_user_cancellation_message(
                        CancellationReason.TOOL_SKIPPED, tool_call.tool_name
                    )
                )

                yield ToolResultEvent(
                    tool_name=tool_call.tool_name,
                    tool_class=tool_call.tool_class,
                    skipped=True,
                    skip_reason=skip_reason,
                    tool_call_id=tool_call.call_id,
                )
                self._append_tool_response(tool_call, skip_reason)
                continue

            self._loop.stats.tool_calls_agreed += 1

            try:
                start_time = time.perf_counter()
                result_model = None

                async for item in tool_instance.invoke(
                    ctx=InvokeContext(
                        tool_call_id=tool_call.call_id,
                        approval_callback=self._loop.approval_callback,
                        agent_manager=self._loop.agent_manager,
                        user_input_callback=self._loop.user_input_callback,
                    ),
                    **tool_call.args_dict,
                ):
                    if isinstance(item, ToolStreamEvent):
                        yield item
                    else:
                        result_model = item

                duration = time.perf_counter() - start_time

                if result_model is None:
                    raise ToolError("Tool did not yield a result")

                text = "\n".join(
                    f"{k}: {v}" for k, v in result_model.model_dump().items()
                )
                self._append_tool_response(tool_call, text)

                yield ToolResultEvent(
                    tool_name=tool_call.tool_name,
                    tool_class=tool_call.tool_class,
                    result=result_model,
                    duration=duration,
                    tool_call_id=tool_call.call_id,
                )

                self._loop.stats.tool_calls_succeeded += 1

            except asyncio.CancelledError:
                cancel = str(
                    get_user_cancellation_message(CancellationReason.TOOL_INTERRUPTED)
                )
                yield ToolResultEvent(
                    tool_name=tool_call.tool_name,
                    tool_class=tool_call.tool_class,
                    error=cancel,
                    tool_call_id=tool_call.call_id,
                )
                self._append_tool_response(tool_call, cancel)
                raise

            except (ToolError, ToolPermissionError) as exc:
                error_msg = f"<{TOOL_ERROR_TAG}>{tool_instance.get_name()} failed: {exc}</{TOOL_ERROR_TAG}>"

                yield ToolResultEvent(
                    tool_name=tool_call.tool_name,
                    tool_class=tool_call.tool_class,
                    error=error_msg,
                    tool_call_id=tool_call.call_id,
                )

                if isinstance(exc, ToolPermissionError):
                    self._loop.stats.tool_calls_agreed -= 1
                    self._loop.stats.tool_calls_rejected += 1
                else:
                    self._loop.stats.tool_calls_failed += 1
                self._append_tool_response(tool_call, error_msg)
                continue

    def fill_missing_tool_responses(self) -> None:
        """Ensure every assistant tool_call has a matching tool response message.

        Moved verbatim from ``AgentLoop._fill_missing_tool_responses``,
        then refactored to extract ``_fill_gap_for_message`` to reduce
        nesting depth (R8 compliance — no ``# noqa`` suppression).
        """
        i = 1
        while i < len(self._loop.messages):
            msg = self._loop.messages[i]

            if msg.role != "assistant" or not msg.tool_calls:
                i += 1
                continue

            expected_responses = len(msg.tool_calls)
            if expected_responses > 0:
                self._fill_gap_for_message(i, msg, expected_responses)
                i = i + 1 + expected_responses
            else:
                i += 1

    def _fill_gap_for_message(
        self, msg_idx: int, msg: LLMMessage, expected_responses: int
    ) -> None:
        """Insert placeholder tool responses for any missing replies.

        Extracted from ``fill_missing_tool_responses`` to eliminate deep
        nesting (previously 5+ levels).
        """
        actual_responses = 0
        j = msg_idx + 1
        while j < len(self._loop.messages) and self._loop.messages[j].role == "tool":
            actual_responses += 1
            j += 1

        if actual_responses >= expected_responses:
            return

        insertion_point = msg_idx + 1 + actual_responses
        assert msg.tool_calls is not None  # guarded by caller

        for call_idx in range(actual_responses, expected_responses):
            tool_call_data = msg.tool_calls[call_idx]

            empty_response = LLMMessage(
                role=Role.tool,
                tool_call_id=tool_call_data.id or "",
                name=(tool_call_data.function.name or "")
                if tool_call_data.function
                else "",
                content=str(
                    get_user_cancellation_message(CancellationReason.TOOL_NO_RESPONSE)
                ),
            )

            self._loop.messages.insert(insertion_point, empty_response)
            insertion_point += 1

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _append_tool_response(self, tool_call: ResolvedToolCall, text: str) -> None:
        """Append a tool response message to the conversation history.

        Moved verbatim from ``AgentLoop._append_tool_response``.
        """
        self._loop.messages.append(
            LLMMessage.model_validate(
                self._loop.format_handler.create_tool_response_message(tool_call, text)
            )
        )

    async def _should_execute_tool(
        self, tool: BaseTool, args: BaseModel, tool_call_id: str
    ) -> ToolDecision:
        """Determine whether a tool should be executed based on permissions.

        Moved verbatim from ``AgentLoop._should_execute_tool``.
        """
        if self._loop.auto_approve:
            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)

        allowlist_denylist_result = tool.check_allowlist_denylist(args)
        if allowlist_denylist_result == ToolPermission.ALWAYS:
            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)
        elif allowlist_denylist_result == ToolPermission.NEVER:
            denylist_patterns = tool.config.denylist
            denylist_str = ", ".join(repr(pattern) for pattern in denylist_patterns)
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback=f"Tool '{tool.get_name()}' blocked by denylist: [{denylist_str}]",
            )

        tool_name = tool.get_name()
        perm = self._loop.tool_manager.get_tool_config(tool_name).permission

        if perm is ToolPermission.ALWAYS:
            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)
        if perm is ToolPermission.NEVER:
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback=f"Tool '{tool_name}' is permanently disabled",
            )

        return await self._ask_approval(tool_name, args, tool_call_id)

    async def _ask_approval(
        self, tool_name: str, args: BaseModel, tool_call_id: str
    ) -> ToolDecision:
        """Request user approval for tool execution via the approval callback.

        Moved verbatim from ``AgentLoop._ask_approval``.
        """
        if not self._loop.approval_callback:
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback="Tool execution not permitted.",
            )
        if asyncio.iscoroutinefunction(self._loop.approval_callback):
            async_callback = cast(AsyncApprovalCallback, self._loop.approval_callback)
            response, feedback = await async_callback(tool_name, args, tool_call_id)
        else:
            sync_callback = cast(SyncApprovalCallback, self._loop.approval_callback)
            response, feedback = sync_callback(tool_name, args, tool_call_id)

        match response:
            case ApprovalResponse.YES:
                return ToolDecision(
                    verdict=ToolExecutionResponse.EXECUTE, feedback=feedback
                )
            case ApprovalResponse.NO:
                return ToolDecision(
                    verdict=ToolExecutionResponse.SKIP, feedback=feedback
                )

    def _ensure_assistant_after_tools(self) -> None:
        """Append a placeholder assistant message if the last message is a tool response.

        Moved verbatim from ``AgentLoop._ensure_assistant_after_tools``.
        """
        MIN_MESSAGE_SIZE = 2
        if len(self._loop.messages) < MIN_MESSAGE_SIZE:
            return

        last_msg = self._loop.messages[-1]
        if last_msg.role is Role.tool:
            empty_assistant_msg = LLMMessage(role=Role.assistant, content="Understood.")
            self._loop.messages.append(empty_assistant_msg)
