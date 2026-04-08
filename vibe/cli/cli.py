from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from rich import print as rprint
from rich.console import Console

from vibe.cli.bootstrap import BootstrapContext
from vibe.cli.bootstrap.env_snapshot import env_file_path, repo_tag
from vibe.cli.bootstrap.steps import (
    deactivate_venv,
    find_or_create_venv,
    load_env_config_yaml,
    load_env_file,
    run_make_targets,
    set_blitzy_env_path,
    set_local_development,
    set_postgres_port,
)
from vibe.cli.textual_ui.app import run_textual_ui
from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config import (
    MissingAPIKeyError,
    MissingPromptFileError,
    VibeConfig,
    load_dotenv_values,
)
from vibe.core.paths.config_paths import CONFIG_FILE, HISTORY_FILE
from vibe.core.programmatic import run_programmatic
from vibe.core.session.session_loader import SessionLoader
from vibe.core.types import LLMMessage, OutputFormat, Role
from vibe.core.utils import ConversationLimitException, logger
from vibe.setup.onboarding import run_onboarding

console = Console()


def get_initial_agent_name(args: argparse.Namespace) -> str:
    if args.prompt is not None and args.agent == BuiltinAgentName.DEFAULT:
        return BuiltinAgentName.AUTO_APPROVE
    return args.agent


def get_prompt_from_stdin() -> str | None:
    if sys.stdin.isatty():
        return None
    try:
        if content := sys.stdin.read().strip():
            sys.stdin = sys.__stdin__ = open("/dev/tty")
            return content
    except KeyboardInterrupt:
        pass
    except OSError:
        return None

    return None


def load_config_or_exit() -> VibeConfig:
    try:
        return VibeConfig.load()
    except MissingAPIKeyError:
        run_onboarding()
        return VibeConfig.load()
    except MissingPromptFileError as e:
        rprint(f"[yellow]Invalid system prompt id: {e}[/]")
        sys.exit(1)
    except ValueError as e:
        rprint(f"[yellow]{e}[/]")
        sys.exit(1)


def bootstrap_config_files() -> None:
    if not CONFIG_FILE.path.exists():
        try:
            VibeConfig.save_updates(VibeConfig.create_default())
        except Exception as e:
            rprint(f"[yellow]Could not create default config file: {e}[/]")

    if not HISTORY_FILE.path.exists():
        try:
            HISTORY_FILE.path.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.path.write_text("Hello Blitzy!\n", "utf-8")
        except Exception as e:
            rprint(f"[yellow]Could not create history file: {e}[/]")


def _has_archie_bootstrap() -> bool:
    """Check if the current project has an archie-bootstrap script."""
    # Walk up from cwd looking for archie-bootstrap
    current = Path.cwd()
    while current != current.parent:
        if (current / "archie-bootstrap").exists():
            return True
        if (current / "Makefile").exists():
            # Check if Makefile has archie targets
            try:
                makefile_content = (current / "Makefile").read_text()
                if "archie" in makefile_content.lower():
                    return True
            except Exception:
                pass
        current = current.parent
    return False


def _get_bootstrap_timestamp_path() -> Path:
    """Get the path to the bootstrap timestamp file."""
    return env_file_path().with_suffix(".timestamp")


def _is_bootstrap_stale(hours: int = 24) -> bool:
    """Check if the bootstrap environment is stale."""
    env_path = env_file_path()
    timestamp_path = _get_bootstrap_timestamp_path()
    
    # If env file doesn't exist, bootstrap is needed
    if not env_path.exists():
        return True
    
    # If timestamp doesn't exist, consider it stale
    if not timestamp_path.exists():
        return True
    
    try:
        timestamp = float(timestamp_path.read_text().strip())
        age = datetime.now() - datetime.fromtimestamp(timestamp)
        return age > timedelta(hours=hours)
    except Exception:
        # If we can't read the timestamp, consider it stale
        return True


def _update_bootstrap_timestamp() -> None:
    """Update the bootstrap timestamp file."""
    timestamp_path = _get_bootstrap_timestamp_path()
    timestamp_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp_path.write_text(str(time.time()))


def _cleanup_stale_env_files(days: int = 30) -> None:
    """Clean up stale environment files from other projects."""
    archie_home = Path.home() / ".archie"
    if not archie_home.exists():
        return
    
    current_tag = repo_tag()
    cutoff = datetime.now() - timedelta(days=days)
    
    for env_file in archie_home.glob("*-env"):
        # Skip current project's env file
        if env_file.stem == f"{current_tag}-env":
            continue
        
        try:
            mtime = datetime.fromtimestamp(env_file.stat().st_mtime)
            if mtime < cutoff:
                env_file.unlink()
                # Also remove timestamp if it exists
                timestamp_file = env_file.with_suffix(".timestamp")
                if timestamp_file.exists():
                    timestamp_file.unlink()
        except Exception:
            pass


def run_auto_bootstrap() -> bool:
    """Run a minimal, non-interactive bootstrap. Returns True if successful."""
    try:
        before_env = dict(os.environ)
        
        ctx = BootstrapContext(
            environment="dev",
            skip_make=True,  # Keep it fast
            run_tests_flag=False,
            blitzy_env_path_arg=None,
        )
        
        # Run minimal bootstrap steps
        steps = [
            deactivate_venv,
            find_or_create_venv,
            set_blitzy_env_path,
            load_env_file,
            load_env_config_yaml,
            set_postgres_port,
            set_local_development,
        ]
        
        for step_fn in steps:
            step_fn(ctx)
        
        # Update timestamp on success
        _update_bootstrap_timestamp()
        
        # Clean up old env files
        _cleanup_stale_env_files()
        
        return True
    except Exception as e:
        logger.warning(f"Auto-bootstrap failed: {e}")
        return False


def load_session(
    args: argparse.Namespace, config: VibeConfig
) -> list[LLMMessage] | None:
    if not args.continue_session and not args.resume:
        return None

    if not config.session_logging.enabled:
        rprint(
            "[red]Session logging is disabled. "
            "Enable it in config to use --continue or --resume[/]"
        )
        sys.exit(1)

    session_to_load = None
    if args.continue_session:
        session_to_load = SessionLoader.find_latest_session(config.session_logging)
        if not session_to_load:
            rprint(
                f"[red]No previous sessions found in "
                f"{config.session_logging.save_dir}[/]"
            )
            sys.exit(1)
    else:
        session_to_load = SessionLoader.find_session_by_id(
            args.resume, config.session_logging
        )
        if not session_to_load:
            rprint(
                f"[red]Session '{args.resume}' not found in "
                f"{config.session_logging.save_dir}[/]"
            )
            sys.exit(1)

    try:
        loaded_messages, _ = SessionLoader.load_session(session_to_load)
        return loaded_messages
    except Exception as e:
        rprint(f"[red]Failed to load session: {e}[/]")
        sys.exit(1)


def _load_messages_from_previous_session(
    agent_loop: AgentLoop, loaded_messages: list[LLMMessage]
) -> None:
    non_system_messages = [msg for msg in loaded_messages if msg.role != Role.system]
    agent_loop.messages.extend(non_system_messages)
    logger.info("Loaded %d messages from previous session", len(non_system_messages))


def run_cli(args: argparse.Namespace) -> None:
    load_dotenv_values()
    bootstrap_config_files()
    
    # Auto-bootstrap check
    bootstrap_status = None
    if _has_archie_bootstrap() and not getattr(args, 'skip_auto_bootstrap', False):
        force_bootstrap = getattr(args, 'force_bootstrap', False)
        
        if force_bootstrap or _is_bootstrap_stale():
            console.print("[dim]Preparing environment...[/]")
            if run_auto_bootstrap():
                bootstrap_status = "✓ Environment ready"
            else:
                bootstrap_status = "⚠ Environment setup incomplete"
        else:
            bootstrap_status = "✓ Using cached environment"

    if args.setup:
        run_onboarding()
        sys.exit(0)

    try:
        initial_agent_name = get_initial_agent_name(args)
        config = load_config_or_exit()

        if args.enabled_tools:
            config.enabled_tools = args.enabled_tools

        loaded_messages = load_session(args, config)

        stdin_prompt = get_prompt_from_stdin()
        if args.prompt is not None:
            # Restore default SIGINT so Ctrl+C works in programmatic mode
            # (entrypoint ignores SIGINT to avoid uv process-manager race).
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            programmatic_prompt = args.prompt or stdin_prompt
            if not programmatic_prompt:
                print(
                    "Error: No prompt provided for programmatic mode", file=sys.stderr
                )
                sys.exit(1)
            output_format = OutputFormat(
                args.output if hasattr(args, "output") else "text"
            )

            try:
                final_response = run_programmatic(
                    config=config,
                    prompt=programmatic_prompt,
                    max_turns=args.max_turns,
                    max_price=args.max_price,
                    output_format=output_format,
                    previous_messages=loaded_messages,
                    agent_name=initial_agent_name,
                )
                if final_response:
                    print(final_response)
                sys.exit(0)
            except ConversationLimitException as e:
                print(e, file=sys.stderr)
                sys.exit(1)
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            agent_loop = AgentLoop(
                config, agent_name=initial_agent_name, enable_streaming=True
            )

            if loaded_messages:
                _load_messages_from_previous_session(agent_loop, loaded_messages)

            run_textual_ui(
                agent_loop=agent_loop,
                initial_prompt=args.initial_prompt or stdin_prompt,
                bootstrap_status=bootstrap_status,
            )

    except (KeyboardInterrupt, EOFError):
        rprint("\n[dim]Bye![/]")
        sys.exit(0)
