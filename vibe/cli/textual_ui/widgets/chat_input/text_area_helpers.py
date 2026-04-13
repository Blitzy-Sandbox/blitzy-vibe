"""Helper classes extracted from ``ChatTextArea`` for file-level method count reduction.

This module contains:
* **Composition helpers** (``CompletionHelper``, ``HistoryNavigator``, ``ModeManager``)
  — thin wrappers composed into ``ChatTextArea`` via ``__init__`` injection (R2).
* **Message classes** (``Submitted``, ``HistoryPrevious``, ``HistoryNext``,
  ``HistoryReset``, ``ModeChanged``) — originally inner classes of ``ChatTextArea``,
  moved to module level with ``namespace='chat_text_area'`` so that Textual's
  ``on_chat_text_area_*`` handler routing is preserved.

All classes are re-attached as class attributes of ``ChatTextArea`` so that
external callers (e.g. ``body.py``) can continue to reference them as
``ChatTextArea.Submitted``, ``ChatTextArea.ModeChanged``, etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.message import Message

if TYPE_CHECKING:
    from typing import Literal

    from vibe.cli.textual_ui.widgets.chat_input.text_area import ChatTextArea

    InputMode = Literal["!", "/", ">"]

__all__ = [
    "CompletionHelper",
    "HistoryNavigator",
    "HistoryNext",
    "HistoryPrevious",
    "HistoryReset",
    "ModeChanged",
    "ModeManager",
    "Submitted",
]


# ---------------------------------------------------------------------------
# Composition helpers (R2: composition over inheritance via __init__ injection)
# ---------------------------------------------------------------------------


class CompletionHelper:
    """Completion-related state and logic for ChatTextArea.

    Composed into ChatTextArea via ``__init__`` injection.  Actual logic
    lives in the module-level ``_completion_*`` functions in ``text_area.py``.
    """

    def __init__(self, text_area: ChatTextArea) -> None:
        self._text_area = text_area


class HistoryNavigator:
    """History navigation state and logic for ChatTextArea.

    Composed into ChatTextArea via ``__init__`` injection.  Actual logic
    lives in the module-level ``_history_*`` functions in ``text_area.py``.
    """

    def __init__(self, text_area: ChatTextArea) -> None:
        self._text_area = text_area


class ModeManager:
    """Input mode management for ChatTextArea.

    Composed into ChatTextArea via ``__init__`` injection.  Actual logic
    lives in the module-level ``_mode_*`` functions in ``text_area.py``.
    """

    def __init__(self, text_area: ChatTextArea) -> None:
        self._text_area = text_area


# ---------------------------------------------------------------------------
# Message classes — originally inner classes of ChatTextArea.
#
# Moved to module level with ``namespace='chat_text_area'`` so that
# Textual's handler-name derivation produces the same names as the
# original inner-class pattern (e.g. ``on_chat_text_area_submitted``).
# ---------------------------------------------------------------------------


class Submitted(Message, namespace="chat_text_area"):
    """Message posted when the user submits input."""

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__()


class HistoryPrevious(Message, namespace="chat_text_area"):
    """Message posted to request the previous history entry."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        super().__init__()


class HistoryNext(Message, namespace="chat_text_area"):
    """Message posted to request the next history entry."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        super().__init__()


class HistoryReset(Message, namespace="chat_text_area"):
    """Message sent when history navigation should be reset."""


class ModeChanged(Message, namespace="chat_text_area"):
    """Message sent when the input mode changes (>, !, /)."""

    def __init__(self, mode: InputMode) -> None:
        self.mode = mode
        super().__init__()
