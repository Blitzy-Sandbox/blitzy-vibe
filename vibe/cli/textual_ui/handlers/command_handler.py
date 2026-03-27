from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vibe.cli.textual_ui.widgets.messages import UserMessage

if TYPE_CHECKING:
    from vibe.cli.textual_ui.app import VibeApp

__all__ = ["CommandHandler"]


class CommandHandler:
    """Handles slash command dispatch for the Vibe terminal UI.

    Extracted from VibeApp._handle_command() to reduce god class size.
    Composed into VibeApp via __init__ injection (R2).
    """

    def __init__(self, app: VibeApp) -> None:
        self._app = app

    async def handle_command(self, user_input: str) -> bool:
        """Dispatch a user input string to a registered command if it matches.

        Returns True if a command was found and executed, False otherwise.
        """
        if command := self._app.commands.find_command(user_input):
            await self._app._mount_and_scroll(UserMessage(user_input))
            handler = getattr(self._app, command.handler)
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
            return True
        return False
