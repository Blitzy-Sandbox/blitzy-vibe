from __future__ import annotations

from typing import TYPE_CHECKING

from vibe.cli.textual_ui.widgets.approval_app import ApprovalApp
from vibe.core.types import ApprovalResponse
from vibe.core.utils import CancellationReason, get_user_cancellation_message

if TYPE_CHECKING:
    from vibe.cli.textual_ui.app import VibeApp

__all__ = ["ApprovalHandler"]


class ApprovalHandler:
    """Handles tool approval flow for the Vibe terminal UI.

    Extracted from VibeApp to reduce god class size.
    Composed into VibeApp via __init__ injection.
    """

    def __init__(self, app: VibeApp) -> None:
        self._app = app

    async def on_approval_granted(
        self, message: ApprovalApp.ApprovalGranted
    ) -> None:
        """Handle approval granted for a tool call."""
        if self._app._pending_approval and not self._app._pending_approval.done():
            self._app._pending_approval.set_result((ApprovalResponse.YES, None))

        await self._app._switch_to_input_app()

    async def on_approval_granted_always_tool(
        self, message: ApprovalApp.ApprovalGrantedAlwaysTool
    ) -> None:
        """Handle approval granted with 'always allow' for a tool."""
        self._app._set_tool_permission_always(
            message.tool_name, save_permanently=message.save_permanently
        )

        if self._app._pending_approval and not self._app._pending_approval.done():
            self._app._pending_approval.set_result((ApprovalResponse.YES, None))

        await self._app._switch_to_input_app()

    async def on_approval_rejected(
        self, message: ApprovalApp.ApprovalRejected
    ) -> None:
        """Handle rejection of a tool approval request."""
        if self._app._pending_approval and not self._app._pending_approval.done():
            feedback = str(
                get_user_cancellation_message(CancellationReason.OPERATION_CANCELLED)
            )
            self._app._pending_approval.set_result((ApprovalResponse.NO, feedback))

        await self._app._switch_to_input_app()

        if self._app._loading_widget and self._app._loading_widget.parent:
            await self._app._remove_loading_widget()
