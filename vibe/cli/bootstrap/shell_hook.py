from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

console = Console()

_HOOK_MARKER = "# blitzy-bootstrap-hook"

_HOOK_BODY = """\

# blitzy-bootstrap-hook
blitzy() {
    if [[ "$1" == "bootstrap" ]]; then
        command blitzy "$@"
        local _blitzy_tag
        _blitzy_tag="$(basename "$PWD")"
        [[ -f "$HOME/.archie/${_blitzy_tag}-env" ]] && source "$HOME/.archie/${_blitzy_tag}-env"
    else
        command blitzy "$@"
    fi
}
"""


def _rc_file() -> Path | None:
    """Return the shell rc file path, or *None* for unsupported shells."""
    shell = os.environ.get("SHELL", "")
    if shell.endswith("zsh"):
        return Path.home() / ".zshrc"
    if shell.endswith("bash"):
        return Path.home() / ".bashrc"
    return None


def is_hook_installed() -> bool:
    """Check whether the blitzy bootstrap hook is already in the user's rc file."""
    rc = _rc_file()
    if rc is None or not rc.exists():
        return False
    return _HOOK_MARKER in rc.read_text(encoding="utf-8")


def install_shell_hook() -> bool:
    """Prompt the user and append the shell hook to their rc file.

    Returns *True* if the hook was installed (or was already present).
    """
    if is_hook_installed():
        return True

    rc = _rc_file()
    if rc is None:
        shell = os.environ.get("SHELL", "unknown")
        console.print(
            f"\n  [yellow]Unsupported shell ({shell}).[/]  "
            "Add the following function to your shell config manually:\n"
        )
        console.print(_HOOK_BODY, highlight=False)
        return False

    if not Confirm.ask(
        f"\n  Install blitzy bootstrap shell hook to [bold]{rc}[/]?",
        default=True,
        console=console,
    ):
        return False

    with rc.open("a", encoding="utf-8") as f:
        f.write(_HOOK_BODY)

    console.print(f"  [green]Hook installed.[/]  Run [bold]source {rc}[/] or open a new terminal to activate.")
    return True
