from __future__ import annotations

import base64
from collections.abc import Callable
import os
import platform
import shutil
import subprocess

import pyperclip
from textual.app import App

_PREVIEW_MAX_LENGTH = 40


def _copy_osc52(text: str) -> None:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    osc52_seq = f"\033]52;c;{encoded}\a"
    if os.environ.get("TMUX"):
        osc52_seq = f"\033Ptmux;\033{osc52_seq}\033\\"

    with open("/dev/tty", "w") as tty:
        tty.write(osc52_seq)
        tty.flush()


def _copy_x11_clipboard(text: str) -> None:
    subprocess.run(
        ["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True
    )


def _copy_wayland_clipboard(text: str) -> None:
    subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=True)


def _has_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _get_copy_fns(app: App) -> list[Callable[[str], None]]:
    copy_fns: list[Callable[[str], None]] = [
        _copy_osc52,
        pyperclip.copy,
        app.copy_to_clipboard,
    ]
    if platform.system() == "Linux" and _has_cmd("wl-copy"):
        copy_fns = [_copy_wayland_clipboard, *copy_fns]
    if platform.system() == "Linux" and _has_cmd("xclip"):
        copy_fns = [_copy_x11_clipboard, *copy_fns]
    return copy_fns


def _shorten_preview(texts: list[str]) -> str:
    dense_text = "⏎".join(texts).replace("\n", "⏎")
    if len(dense_text) > _PREVIEW_MAX_LENGTH:
        return f"{dense_text[: _PREVIEW_MAX_LENGTH - 1]}…"
    return dense_text


def _gather_selected_texts(app: App) -> list[str]:
    """Collect selected text from all widgets with active selections."""
    selected_texts: list[str] = []
    for widget in app.query("*"):
        if not hasattr(widget, "text_selection") or not widget.text_selection:
            continue

        selection = widget.text_selection

        try:
            result = widget.get_selection(selection)
        except (OSError, ValueError, AttributeError, TypeError):
            continue

        if not result:
            continue

        selected_text, _ = result
        if selected_text.strip():
            selected_texts.append(selected_text)
    return selected_texts


def _try_clipboard_copy(
    text: str, copy_fns: list[Callable[[str], None]]
) -> bool:
    """Attempt to copy text using available clipboard strategies.

    Returns True if at least one strategy succeeded.
    """
    success = False
    for copy_fn in copy_fns:
        try:
            copy_fn(text)
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            success = True
    return success


def copy_selection_to_clipboard(app: App) -> None:
    """Copy the current text selection to the system clipboard."""
    selected_texts = _gather_selected_texts(app)
    if not selected_texts:
        return

    combined_text = "\n".join(selected_texts)
    if _try_clipboard_copy(combined_text, _get_copy_fns(app)):
        app.notify(
            f'"{_shorten_preview(selected_texts)}" copied to clipboard',
            severity="information",
            timeout=2,
            markup=False,
        )
    else:
        app.notify(
            "Failed to copy - no clipboard method available",
            severity="warning",
            timeout=3,
        )
