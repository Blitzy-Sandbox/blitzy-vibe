from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console

from vibe.cli.bootstrap.env_snapshot import env_file_path, write_archie_env
from vibe.cli.bootstrap.shell_hook import install_shell_hook, is_hook_installed
from vibe.cli.bootstrap.steps import (
    BootstrapContext,
    deactivate_venv,
    find_or_create_venv,
    load_env_config_yaml,
    load_env_file,
    run_make_targets,
    run_tests,
    set_blitzy_env_path,
    set_local_development,
    set_postgres_port,
)
from vibe.cli.skills_sync import sync_skills_to_claude_code

console = Console()


def run_bootstrap(args: argparse.Namespace) -> None:
    """Orchestrate the 9-step bootstrap process."""
    env: str = args.environment
    console.print(f"\n[bold purple]Blitzy Bootstrap[/] — setting up [cyan]{env}[/] environment\n")

    before_env = dict(os.environ)

    ctx = BootstrapContext(
        environment=env,
        skip_make=args.skip_make,
        run_tests_flag=args.test,
        blitzy_env_path_arg=getattr(args, "blitzy_env_path", None),
    )

    steps = [
        ("Deactivating existing venv", lambda: deactivate_venv(ctx)),
        ("Finding or creating venv", lambda: find_or_create_venv(ctx)),
        ("Resolving Blitzy env path", lambda: set_blitzy_env_path(ctx)),
        ("Loading env file", lambda: load_env_file(ctx)),
        ("Loading env config YAML", lambda: load_env_config_yaml(ctx)),
        ("Setting Postgres port", lambda: set_postgres_port(ctx)),
        ("Setting LOCAL_DEVELOPMENT", lambda: set_local_development(ctx)),
        ("Running make targets", lambda: run_make_targets(ctx)),
        ("Running tests", lambda: run_tests(ctx)),
    ]

    for description, step_fn in steps:
        console.print(f"  [dim]→[/] {description}...")
        try:
            step_fn()
        except Exception as e:
            console.print(f"  [red]✗ Failed:[/] {e}")
            sys.exit(1)

    after_env = dict(os.environ)
    venv_path = os.environ.get("VIRTUAL_ENV")

    write_archie_env(before_env, after_env, venv_path)

    # Shell hook: auto-source env file after future bootstrap runs
    if is_hook_installed():
        console.print("\n  [green]Your venv will be activated automatically.[/]")
    else:
        installed = install_shell_hook()
        if not installed:
            ef = env_file_path()
            console.print(f"\n  To activate now:\n    [bold]source {ef}[/]")

    # Sync bundled skills to Claude Code
    console.print()
    sync_skills_to_claude_code()

    console.print("\n[bold green]Bootstrap complete.[/]\n")
