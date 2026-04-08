from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console

from vibe.core.paths.global_paths import BUNDLED_SKILLS_DIR

console = Console()

CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def sync_skills_to_claude_code() -> None:
    """Symlink bundled skills into ``~/.claude/skills/`` for Claude Code discovery."""
    bundled = BUNDLED_SKILLS_DIR.path
    if not bundled.is_dir():
        console.print("  [yellow]Bundled skills directory not found, skipping sync.[/]")
        return

    CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    skill_dirs = sorted(d for d in bundled.iterdir() if d.is_dir() and (d / "SKILL.md").exists())
    created = 0
    updated = 0
    unchanged = 0

    for skill_dir in skill_dirs:
        link = CLAUDE_SKILLS_DIR / skill_dir.name
        target = skill_dir.resolve()

        if link.is_symlink():
            if link.resolve() == target:
                unchanged += 1
                continue
            # Stale symlink — update it
            link.unlink()
            updated += 1
        elif link.exists():
            # Non-symlink file/dir exists — skip to avoid data loss
            console.print(f"  [yellow]Skipping {link.name}: non-symlink already exists at {link}[/]")
            continue
        else:
            created += 1

        os.symlink(target, link)

    # Clean up symlinks pointing to skills that no longer exist in bundled
    bundled_names = {d.name for d in skill_dirs}
    removed = 0
    for entry in CLAUDE_SKILLS_DIR.iterdir():
        if entry.is_symlink() and entry.name not in bundled_names:
            # Only remove if it points into our bundled dir
            try:
                link_target = entry.resolve()
            except OSError:
                link_target = None
            if link_target and str(bundled.resolve()) in str(link_target):
                entry.unlink()
                removed += 1

    parts = []
    if created:
        parts.append(f"{created} new")
    if updated:
        parts.append(f"{updated} updated")
    if removed:
        parts.append(f"{removed} removed")
    if unchanged:
        parts.append(f"{unchanged} unchanged")

    summary = ", ".join(parts) if parts else "no skills found"
    console.print(f"  [green]Synced skills to Claude Code:[/] {summary}")
    console.print(f"  [dim]{CLAUDE_SKILLS_DIR}[/]")
