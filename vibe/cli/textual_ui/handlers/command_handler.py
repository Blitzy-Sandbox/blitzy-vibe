from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vibe.cli.textual_ui.widgets.messages import (
    ErrorMessage,
    UserCommandMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from vibe.cli.textual_ui.app import VibeApp

__all__ = ["CommandHandler"]


class CommandHandler:
    """Handles slash command dispatch for the Vibe terminal UI.

    Extracted from VibeApp._handle_command() to reduce god class size.
    Composed into VibeApp via __init__ injection (R2).

    Slash-command implementations (_show_help, _show_status, _show_config,
    _show_log_path) are moved verbatim from VibeApp with only
    ``self.`` → ``self._app.`` reference changes (R1: extraction over rewrite).
    """

    def __init__(self, app: VibeApp) -> None:
        self._app = app

    async def handle_command(self, user_input: str) -> bool:
        """Dispatch a user input string to a registered command if it matches.

        Returns True if a command was found and executed, False otherwise.
        Looks up handler on self (CommandHandler) first, then falls back to
        the VibeApp instance for commands that remain on the app.
        """
        if command := self._app.commands.find_command(user_input):
            await self._app._mount_and_scroll(UserMessage(user_input))
            handler = getattr(self, command.handler, None) or getattr(
                self._app, command.handler
            )
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
            return True
        return False

    def _get_skill_entries(self) -> list[tuple[str, str]]:
        """Return available user-invocable skill names and descriptions."""
        if not self._app.agent_loop:
            return []
        return [
            (f"/{name}", info.description)
            for name, info in self._app.agent_loop.skill_manager.available_skills.items()
            if info.user_invocable
        ]

    # ------------------------------------------------------------------
    # Slash-command implementations (moved verbatim from VibeApp — R1)
    # ------------------------------------------------------------------

    async def _show_help(self) -> None:
        """Display keyboard shortcuts and available commands."""
        help_text = self._app.commands.get_help_text()
        await self._app._mount_and_scroll(UserCommandMessage(help_text))

    async def _show_status(self) -> None:
        """Display agent statistics."""
        stats = self._app.agent_loop.stats
        status_text = f"""## Agent Statistics

- **Steps**: {stats.steps:,}
- **Session Prompt Tokens**: {stats.session_prompt_tokens:,}
- **Session Completion Tokens**: {stats.session_completion_tokens:,}
- **Session Total LLM Tokens**: {stats.session_total_llm_tokens:,}
- **Last Turn Tokens**: {stats.last_turn_total_tokens:,}
- **Cost**: ${stats.session_cost:.4f}
"""
        await self._app._mount_and_scroll(UserCommandMessage(status_text))

    async def _show_config(self) -> None:
        """Switch to the configuration app in the bottom panel."""
        # Compare against StrEnum value to avoid runtime import of BottomApp
        if self._app._current_bottom_app == "config":
            return
        await self._app._switch_to_config_app()

    async def _show_log_path(self) -> None:
        """Show the path to the current interaction log directory."""
        if not self._app.agent_loop.session_logger.enabled:
            await self._app._mount_and_scroll(
                ErrorMessage(
                    "Session logging is disabled in configuration.",
                    collapsed=self._app._tools_collapsed,
                )
            )
            return

        try:
            log_path = str(self._app.agent_loop.session_logger.session_dir)
            await self._app._mount_and_scroll(
                UserCommandMessage(
                    f"## Current Log Directory\n\n`{log_path}`\n\nYou can send this directory to share your interaction."
                )
            )
        except (OSError, AttributeError) as e:
            await self._app._mount_and_scroll(
                ErrorMessage(
                    f"Failed to get log path: {e}",
                    collapsed=self._app._tools_collapsed,
                )
            )
