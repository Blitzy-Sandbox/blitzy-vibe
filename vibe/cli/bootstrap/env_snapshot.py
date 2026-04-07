from __future__ import annotations

from pathlib import Path

from rich.console import Console

console = Console()

ARCHIE_HOME = Path.home() / ".archie"

# Keys that are noisy or session-specific and should be excluded from the snapshot.
_EXCLUDED_KEYS = frozenset({
    "_",
    "SHLVL",
    "OLDPWD",
    "PWD",
    "TERM_SESSION_ID",
    "TMPDIR",
})


def repo_tag() -> str:
    """Return a tag derived from the current working directory name."""
    return Path.cwd().name


def env_file_path() -> Path:
    """Return the global env snapshot path: ``~/.archie/<repo-tag>-env``."""
    return ARCHIE_HOME / f"{repo_tag()}-env"


def write_archie_env(
    before: dict[str, str],
    after: dict[str, str],
    venv_path: str | None,
) -> None:
    """Diff two environment snapshots and write to ``~/.archie/<tag>-env``."""
    lines: list[str] = []

    for key in sorted(after):
        if key in _EXCLUDED_KEYS:
            continue
        after_val = after[key]
        before_val = before.get(key)
        if before_val != after_val:
            lines.append(f'export {key}="{after_val}"')

    if venv_path:
        lines.append('source "$VIRTUAL_ENV/bin/activate" 2>/dev/null')

    ARCHIE_HOME.mkdir(parents=True, exist_ok=True)
    env_file = env_file_path()
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    console.print(f"\n  [green]Wrote environment snapshot to[/] {env_file}")
    console.print(f"  [dim]{len(lines)} line(s) in snapshot[/]")
