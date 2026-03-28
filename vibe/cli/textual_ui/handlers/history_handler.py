from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widget import Widget

from vibe.cli.textual_ui.widgets.messages import AssistantMessage, UserMessage
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage, ToolResultMessage
from vibe.core.types import LLMMessage, Role

if TYPE_CHECKING:
    from vibe.cli.textual_ui.app import VibeApp

__all__ = ["HistoryHandler"]


class HistoryHandler:
    """Handles session history rebuild for the Vibe terminal UI.

    Extracted from VibeApp to reduce god class size.
    Composed into VibeApp via __init__ injection.
    """

    def __init__(self, app: VibeApp) -> None:
        self._app = app

    async def rebuild_history(self) -> None:
        """Rebuild the chat transcript from persisted messages.

        Skips if only system messages exist or if messages are already displayed.
        """
        if all(msg.role == Role.system for msg in self._app.agent_loop.messages):
            return

        messages_area = self._app.query_one("#messages")
        # Don't rebuild if messages are already displayed
        if messages_area.children:
            return

        tool_call_map: dict[str, str] = {}

        with self._app.batch_update():
            for msg in self._app.agent_loop.messages:
                if msg.role == Role.system:
                    continue

                match msg.role:
                    case Role.user:
                        if msg.content:
                            await messages_area.mount(UserMessage(msg.content))

                    case Role.assistant:
                        await self.mount_history_assistant_message(
                            msg, messages_area, tool_call_map
                        )

                    case Role.tool:
                        tool_name = msg.name or tool_call_map.get(
                            msg.tool_call_id or "", "tool"
                        )
                        await messages_area.mount(
                            ToolResultMessage(
                                tool_name=tool_name,
                                content=msg.content,
                                collapsed=self._app._tools_collapsed,
                            )
                        )

    async def mount_history_assistant_message(
        self,
        msg: LLMMessage,
        messages_area: Widget,
        tool_call_map: dict[str, str],
    ) -> None:
        """Mount an assistant message from history, including tool calls."""
        if msg.content:
            widget = AssistantMessage(msg.content)
            await messages_area.mount(widget)
            await widget.write_initial_content()
            await widget.stop_stream()

        if not msg.tool_calls:
            return

        for tool_call in msg.tool_calls:
            tool_name = tool_call.function.name or "unknown"
            if tool_call.id:
                tool_call_map[tool_call.id] = tool_name

            await messages_area.mount(ToolCallMessage(tool_name=tool_name))
