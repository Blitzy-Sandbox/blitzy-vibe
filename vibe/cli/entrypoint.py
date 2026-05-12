from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys

from rich import print as rprint

from vibe import __version__
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.paths.config_paths import unlock_config_paths
from vibe.core.trusted_folders import has_trustable_content, trusted_folders_manager
from vibe.setup.trusted_folders.trust_folder_dialog import (
    TrustDialogQuitException,
    ask_trust_folder,
)

SUBCOMMANDS = {"bootstrap", "skills"}


def _parse_bootstrap_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blitzy bootstrap", description="Bootstrap a dev/qa/prod environment"
    )
    parser.add_argument(
        "environment", nargs="?", default="dev", choices=["dev", "qa", "prod"]
    )
    parser.add_argument("--test", action="store_true", help="Run tests after bootstrap")
    parser.add_argument("--skip-make", action="store_true", help="Skip make targets")
    parser.add_argument(
        "--blitzy-env-path", type=Path, help="Path to Blitzy env files directory"
    )
    args = parser.parse_args(sys.argv[2:])
    args.command = "bootstrap"
    return args


def _parse_skills_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blitzy skills", description="Manage Blitzy skills"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="sync",
        choices=["sync"],
        help="Action to perform (default: sync)",
    )
    args = parser.parse_args(sys.argv[2:])
    args.command = "skills"
    return args


def parse_arguments() -> argparse.Namespace:
    # If the first arg is a known subcommand, delegate to its parser.
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        if sys.argv[1] == "skills":
            return _parse_skills_args()
        return _parse_bootstrap_args()

    parser = argparse.ArgumentParser(
        description="Blitzy Agent CLI",
        epilog="subcommands:\n  bootstrap    Bootstrap dev environment\n  skills       Sync bundled skills to Claude Code\n\n"
        "Run `blitzy <subcommand> --help` for subcommand-specific options.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "initial_prompt",
        nargs="?",
        metavar="PROMPT",
        help="Initial prompt to start the interactive session with.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        nargs="?",
        const="",
        metavar="TEXT",
        help="Run in programmatic mode: send prompt, auto-approve all tools, "
        "output response, and exit.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        metavar="N",
        help="Maximum number of assistant turns "
        "(only applies in programmatic mode with -p).",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        metavar="DOLLARS",
        help="Maximum cost in dollars (only applies in programmatic mode with -p). "
        "Session will be interrupted if cost exceeds this limit.",
    )
    parser.add_argument(
        "--enabled-tools",
        action="append",
        metavar="TOOL",
        help="Enable specific tools. In programmatic mode (-p), this disables "
        "all other tools. "
        "Can use exact names, glob patterns (e.g., 'bash*'), or "
        "regex with 're:' prefix. Can be specified multiple times.",
    )
    parser.add_argument(
        "--output",
        type=str,
        choices=["text", "json", "streaming"],
        default="text",
        help="Output format for programmatic mode (-p): 'text' "
        "for human-readable (default), 'json' for all messages at end, "
        "'streaming' for newline-delimited JSON per message.",
    )
    parser.add_argument(
        "--agent",
        metavar="NAME",
        default=BuiltinAgentName.DEFAULT,
        help="Agent to use (builtin: default, plan, accept-edits, auto-approve, "
        "or custom from ~/.blitzy/agents/NAME.toml)",
    )
    parser.add_argument("--setup", action="store_true", help="Setup API key and exit")
    parser.add_argument(
        "--workdir",
        type=Path,
        metavar="DIR",
        help="Change to this directory before running",
    )

    continuation_group = parser.add_mutually_exclusive_group()
    continuation_group.add_argument(
        "-c",
        "--continue",
        action="store_true",
        dest="continue_session",
        help="Continue from the most recent saved session",
    )
    continuation_group.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previous session via the interactive session picker "
        "(scoped to the current repository and branch).",
    )

    # AAP Capability A (rule 13): the `--provider` flag is OUTSIDE the
    # mutually-exclusive `-c/--continue`/`--resume` group because it is
    # compatible with both -- when --resume is paired with --provider and the
    # session picker yields an empty list, the entrypoint orchestration block
    # falls through to provider selection and consumes `args.provider` to
    # bypass the interactive picker. ``type=str.lower`` normalizes the input
    # BEFORE argparse validates against ``choices``, so ``--provider BLITZY``
    # is accepted and stored as ``"blitzy"`` (the canonical lowercase token
    # that matches ``Backend.<X>.value`` and ``provider_string_to_backend``'s
    # key set -- AAP rule 13 single source of truth). ``default=None``
    # allows the orchestration block to unambiguously distinguish "not set"
    # from "explicitly chosen".
    parser.add_argument(
        "--provider",
        type=str.lower,
        choices=["blitzy", "mistral", "anthropic"],
        default=None,
        help="Skip the interactive provider prompt by specifying the LLM provider "
        "(case-insensitive). Mutually consistent with --resume's restored provider.",
    )

    parser.add_argument(
        "--force-bootstrap",
        action="store_true",
        help="Force environment bootstrap even if cached environment exists",
    )
    parser.add_argument(
        "--skip-auto-bootstrap",
        action="store_true",
        help="Skip automatic environment bootstrap",
    )

    args = parser.parse_args()
    args.command = None
    return args


def check_and_resolve_trusted_folder() -> None:
    try:
        cwd = Path.cwd()
    except FileNotFoundError:
        rprint(
            "[red]Error: Current working directory no longer exists.[/]\n"
            "[yellow]The directory you started blitzy from has been deleted. "
            "Please change to an existing directory and try again, "
            "or use --workdir to specify a working directory.[/]"
        )
        sys.exit(1)

    if not has_trustable_content(cwd) or cwd.resolve() == Path.home().resolve():
        return

    is_folder_trusted = trusted_folders_manager.is_trusted(cwd)

    if is_folder_trusted is not None:
        return

    try:
        is_folder_trusted = ask_trust_folder(cwd)
    except (KeyboardInterrupt, EOFError, TrustDialogQuitException):
        sys.exit(0)
    except Exception as e:
        rprint(f"[yellow]Error showing trust dialog: {e}[/]")
        return

    if is_folder_trusted is True:
        trusted_folders_manager.add_trusted(cwd)
    elif is_folder_trusted is False:
        trusted_folders_manager.add_untrusted(cwd)


def main() -> None:
    # Ignore SIGINT at the process level so that `uv run` (or any parent process
    # manager) doesn't race against a dying child when Ctrl+C is pressed.
    # Textual handles Ctrl+C through terminal raw-mode keyboard input, not via
    # SIGINT, so interactive mode is unaffected.  For programmatic mode (-p),
    # the KeyboardInterrupt handler in run_cli still works because we restore
    # the default SIGINT disposition there.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    args = parse_arguments()

    if args.command == "bootstrap":
        from vibe.cli.bootstrap import run_bootstrap

        run_bootstrap(args)
        sys.exit(0)

    if args.command == "skills":
        from vibe.cli.skills_sync import sync_skills_to_claude_code

        sync_skills_to_claude_code()
        sys.exit(0)

    if args.workdir:
        workdir = args.workdir.expanduser().resolve()
        if not workdir.is_dir():
            rprint(
                f"[red]Error: --workdir does not exist or is not a directory: {workdir}[/]"
            )
            sys.exit(1)
        os.chdir(workdir)

    is_interactive = args.prompt is None
    if is_interactive:
        check_and_resolve_trusted_folder()
    unlock_config_paths()

    # --- AAP Capability A + C: provider/session selection (rules 4, 5, 13) ---
    # MUST happen BEFORE any backend constructor is called (rule 4).  The
    # entrypoint resolves a ``Backend`` enum value and (optionally) a
    # ``SessionRecord``; ``run_cli`` consumes both as keyword arguments and
    # is the sole site that instantiates the backend via
    # :data:`vibe.core.llm.backend.factory.BACKEND_FACTORY`.
    #
    # Variable scope: ``restored_session`` and ``backend_enum`` are declared
    # OUTSIDE the ``if not args.setup:`` block so they remain defined (as
    # ``None``) when ``--setup`` is passed.  This avoids ``UnboundLocalError``
    # at the ``run_cli(args, backend=...)`` call site.  ``--setup`` skips
    # both pickers because ``run_cli`` calls ``run_onboarding()`` and exits
    # before any backend logic runs (see ``vibe/cli/cli.py`` setup branch).
    #
    # The ``from __future__ import annotations`` directive at the top of
    # this module makes all type annotations strings at runtime, so the
    # ``SessionRecord | None`` / ``Backend | None`` annotations below do not
    # require the runtime types to be imported eagerly -- only the runtime
    # references inside the ``if not args.setup:`` block actually evaluate
    # the names ``SessionRecord`` and ``Backend``.
    restored_session: SessionRecord | None = None
    backend_enum: Backend | None = None

    if not args.setup:
        # Lazy imports: deferred to ``main()`` so that subcommand dispatches
        # (``bootstrap``, ``skills``) that ``sys.exit(0)`` earlier in this
        # function pay zero import cost for the LLM/session machinery, and
        # so that any future circular-import risk via the CLI module graph
        # is contained to this single function.
        from vibe.cli.provider_picker import select_provider
        from vibe.cli.session_picker import select_session
        from vibe.core.config import Backend
        from vibe.core.git_context import detect as detect_git_context
        from vibe.core.llm.backend.factory import provider_string_to_backend
        from vibe.core.session import SessionRecord

        if args.resume:
            # Rule 5: ``--resume`` opens the interactive session picker
            # scoped to the current ``(repo, branch)``.  ``detect_git_context``
            # NEVER raises (rule 3 silent-failure contract): an absent or
            # unreadable ``.git`` directory yields ``("", "")``, which the
            # session manager maps to the ``_unknown/_unknown`` storage path.
            repo, branch = detect_git_context()
            try:
                restored_session, backend_enum = select_session(repo, branch)
            except (KeyboardInterrupt, EOFError):
                # Both pickers convert Ctrl-D/EOF to ``KeyboardInterrupt``
                # internally, but defensive ``EOFError`` catch costs nothing
                # and protects against any future picker refactor.  The
                # ``"\n[dim]Bye![/]"`` greeting matches the existing exit
                # pattern in ``run_cli``'s outer ``except`` block for visual
                # consistency.
                rprint("\n[dim]Bye![/]")
                sys.exit(0)
            # Rule 5 fallthrough: when ``select_session`` returns
            # ``(None, None)`` (the empty-list sentinel), ``backend_enum`` is
            # ``None`` and execution proceeds to the provider-selection
            # branch below WITHOUT exiting.

        if backend_enum is None:
            # Covers two cases uniformly:
            #   1. ``--resume`` was NOT passed (skipped session selection).
            #   2. ``--resume`` was passed but the session list was empty
            #      (rule 5 fallthrough).
            # In both, ``--provider`` (if set) wins over the interactive
            # picker.  ``provider_string_to_backend`` is the canonical
            # rule-13 mapper -- its key set is the SINGLE source of truth
            # for the ``{"blitzy", "mistral", "anthropic"}`` token universe.
            if args.provider:
                backend_enum = provider_string_to_backend(args.provider)
            else:
                try:
                    backend_enum = select_provider()
                except (KeyboardInterrupt, EOFError):
                    rprint("\n[dim]Bye![/]")
                    sys.exit(0)

    from vibe.cli.cli import run_cli

    run_cli(args, backend=backend_enum, restored_session=restored_session)


if __name__ == "__main__":
    main()
