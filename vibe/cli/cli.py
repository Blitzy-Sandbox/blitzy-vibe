from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
import signal
import sys
import time

from rich import print as rprint
from rich.console import Console

from vibe.cli.bootstrap import BootstrapContext
from vibe.cli.bootstrap.env_snapshot import env_file_path, repo_tag
from vibe.cli.bootstrap.steps import (
    deactivate_venv,
    find_or_create_venv,
    load_env_config_yaml,
    load_env_file,
    set_blitzy_env_path,
    set_local_development,
    set_postgres_port,
)
from vibe.cli.textual_ui.app import run_textual_ui
from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config import (
    Backend,
    MissingAPIKeyError,
    MissingPromptFileError,
    ProviderConfig,
    VibeConfig,
    load_dotenv_values,
)
from vibe.core.git_context import detect as detect_git_context
from vibe.core.llm.backend.factory import BACKEND_FACTORY
from vibe.core.llm.types import BackendLike
from vibe.core.middleware import AutoCompactMiddleware
from vibe.core.observability import set_correlation_id
from vibe.core.paths.config_paths import CONFIG_FILE, HISTORY_FILE
from vibe.core.programmatic import run_programmatic
from vibe.core.session import SessionManager, SessionRecord, new_session
from vibe.core.session.session_loader import (  # pyright: ignore[reportMissingImports]
    SessionLoader,
)
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
    """Hydrate ``agent_loop.messages`` with previously persisted messages.

    Two effects matter:

    1. **History append.** ``loaded_messages`` (with system messages dropped —
       :class:`AgentLoop` builds a fresh system prompt at construction time) is
       extended onto :attr:`AgentLoop.messages` so the conversation resumes
       where it left off.
    2. **Observer high-watermark advance** (AAP §0.6.1 Group 2 — "skips the
       empty-session initialization"). :class:`AgentLoop` increments
       ``_last_observed_message_index`` to ``1`` in its constructor when a
       ``message_observer`` is supplied, marking only the newly built system
       message as "already observed". After we extend ``agent_loop.messages``
       with the loaded history, the watermark is still ``1``, which would
       cause :meth:`AgentLoop._flush_new_messages` on the FIRST post-resume
       turn to re-emit observer notifications for every loaded message —
       resulting in duplicate appends into ``session_record.messages`` and a
       corrupt session file (AAP rule 6 violation in spirit).

       To prevent the duplication we advance the watermark to the new end of
       :attr:`AgentLoop.messages`, declaring the loaded history as
       "already observed". Subsequent flushes will only emit observer
       notifications for messages produced by the current run.

    This helper is intentionally in-scope (``vibe/cli/cli.py``) rather than
    modifying :class:`AgentLoop` itself, which lies outside the boundary
    directive (``vibe/core/llm/``, ``vibe/core/``, ``vibe/cli/``,
    ``pyproject.toml``). The two-line ``_last_observed_message_index``
    re-assignment is the minimal, surgical fix.
    """
    non_system_messages = [msg for msg in loaded_messages if msg.role != Role.system]
    agent_loop.messages.extend(non_system_messages)
    # AAP §0.6.1 Group 2 + Rule 6: declare every loaded historical message
    # as already observed so :meth:`AgentLoop._flush_new_messages` will only
    # emit observer notifications for messages produced by the current run.
    agent_loop._last_observed_message_index = len(agent_loop.messages)
    logger.info("Loaded %d messages from previous session", len(non_system_messages))


def _build_session_record(
    backend: Backend | None, restored_session: SessionRecord | None
) -> SessionRecord:
    """Build the SessionRecord for this run (AAP Capability C).

    Resolves (repo, branch) via the pure-Python git context detector (AAP
    rule 3: never raises). Restored session's (repo, branch) take precedence
    to preserve continuity when the user has switched branches between
    sessions. Returns a fresh SessionRecord with a UUID4 hex session_id
    when restored_session is None.
    """
    cwd_repo, cwd_branch = detect_git_context()
    if restored_session is not None:
        return restored_session
    # If backend is None (only possible in --setup mode, defensive), fall back
    # to Backend.BLITZY for the provider field.
    provider_value = backend.value if backend is not None else Backend.BLITZY.value
    return new_session(provider=provider_value, repo=cwd_repo, branch=cwd_branch)


def _resolve_loaded_messages(
    args: argparse.Namespace, restored_session: SessionRecord | None, config: VibeConfig
) -> list[LLMMessage] | None:
    """Resolve which message history (if any) to hydrate into the AgentLoop.

    Preference order:
    1. When ``restored_session`` is provided (via the --resume picker at the
       entrypoint), use its messages (system messages dropped — re-added by
       AgentLoop construction).
    2. When ``args.continue_session`` is True (the legacy -c/--continue flag),
       fall back to the legacy session subsystem via :func:`load_session`.
    3. Otherwise, return None (fresh conversation).
    """
    if restored_session is not None:
        return [m for m in restored_session.messages if m.role != Role.system]
    if args.continue_session:
        return load_session(args, config)
    return None


def _install_provider_aware_compact(
    agent_loop: AgentLoop, backend: Backend | None, config: VibeConfig
) -> None:
    """Replace the legacy AutoCompactMiddleware with the provider-aware form.

    Per AAP rule 11, the auto-compact threshold for the user-selectable
    providers (Blitzy, Mistral, Anthropic) MUST be derived from the
    configurable context limits at 80% of the active provider's limit. The
    legacy ``AutoCompactMiddleware(threshold)`` instance added by
    ``AgentLoop._setup_middleware`` is replaced in place; other middleware
    (TurnLimit, PriceLimit, ContextWarning, PlanAgent) is preserved.
    """
    if backend is None or backend.value not in {"blitzy", "mistral", "anthropic"}:
        return
    new_compact = AutoCompactMiddleware(
        provider=backend, context_limits=config.context_limits
    )
    for i, mw in enumerate(agent_loop.middleware_pipeline.middlewares):
        if isinstance(mw, AutoCompactMiddleware):
            agent_loop.middleware_pipeline.middlewares[i] = new_compact
            break


def _maybe_run_auto_bootstrap(args: argparse.Namespace) -> str | None:
    """Run automatic environment bootstrap when applicable, returning status.

    Returns the human-readable bootstrap status (used by the TUI) or None
    when no bootstrap step ran. The decision rules are unchanged from the
    inline form: bootstrap runs only when an ``archie-bootstrap`` script (or
    Makefile target) is present and ``--skip-auto-bootstrap`` is not set.
    """
    if not _has_archie_bootstrap() or getattr(args, "skip_auto_bootstrap", False):
        return None

    force_bootstrap = getattr(args, "force_bootstrap", False)
    if force_bootstrap or _is_bootstrap_stale():
        console.print("[dim]Preparing environment...[/]")
        if run_auto_bootstrap():
            return "✓ Environment ready"
        return "⚠ Environment setup incomplete"
    return "✓ Using cached environment"


def _build_backend_instance(
    backend: Backend | None, config: VibeConfig
) -> BackendLike | None:
    """Construct a :class:`BackendLike` from the user-selected ``Backend`` enum.

    Resolves the orchestration gap identified in checkpoint review finding
    C5-CRIT-01: the entrypoint correctly forwards ``backend=Backend.<X>`` to
    :func:`run_cli`, but the previous implementation never propagated that
    enum into :class:`AgentLoop`'s ``backend`` constructor parameter -- so
    :meth:`AgentLoop._select_backend` was always invoked and dispatched the
    LEGACY ``BACKEND_FACTORY[provider.backend](provider=provider,
    timeout=timeout)`` call, which is INCOMPATIBLE with the new
    :class:`BlitzyLLMBackend` and :class:`AnthropicBackend` constructor
    signatures.

    This helper closes the gap entirely within the in-scope ``cli.py``
    boundary (AAP §0.7.1 UPDATE) without modifying :mod:`vibe.core.agent_loop`
    (REFERENCE / preservation boundary). The constructed instance is then
    passed via ``AgentLoop(..., backend=backend_instance)``, which makes
    :class:`AgentLoop`'s pre-existing ``self.backend_factory = lambda:
    backend or self._select_backend()`` (``agent_loop.py:L127``) short-circuit
    to the supplied instance and BYPASS the legacy factory call site
    (AAP §0.5.4 flow step Z: "Instantiate backend via factory" -- the
    factory is now invoked in this helper, not inside ``AgentLoop``).

    The factory lookup goes through :data:`BACKEND_FACTORY` (rather than
    importing the backend classes directly) for two reasons:

    1. It matches AAP §0.5.4's startup flow diagram exactly ("Z[Instantiate
       backend via factory]"), keeping the factory as the SINGLE source of
       truth for which class implements which backend.
    2. The Rule 4 canonical test
       ``test_no_backend_constructor_called_when_picker_cancelled``
       (``tests/cli/test_provider_selection.py:L492``) patches every entry
       in :data:`BACKEND_FACTORY` with :class:`MagicMock` spies and asserts
       all ``call_count == 0`` when the picker is cancelled. Using the
       factory dict here ensures that future production-mode tests
       observing backend instantiation see the same call surface that
       Rule 4 covers.

    Per-backend dispatch:

    * :attr:`Backend.BLITZY`: ``BlitzyLLMBackend(provider, config, *,
      repo, branch)`` where ``(repo, branch)`` comes from
      :func:`detect_git_context` (AAP rule 3 -- silent failure returns
      ``("", "")`` which the backend forwards as URL parameters).
    * :attr:`Backend.ANTHROPIC`: ``AnthropicBackend(provider, config,
      timeout)`` with ``timeout=config.api_timeout``.
    * :attr:`Backend.MISTRAL`: ``MistralBackend(provider, timeout)`` --
      legacy two-arg signature preserved unchanged (AAP boundary directive:
      "Mistral backend: untouched, preserved as-is").
    * Any other enum value (programmatic callers, future additions):
      returns ``None`` to delegate back to
      :meth:`AgentLoop._select_backend`, preserving compatibility with
      the existing :data:`BACKEND_FACTORY` entries that are not
      user-selectable via ``--provider`` (Rule 13 restricts the
      user-facing token set to ``{blitzy, mistral, anthropic}``).

    Args:
        backend: The :class:`Backend` enum resolved by the entrypoint's
            orchestration block (from ``--provider``, the interactive
            provider picker, or the restored session's ``provider``
            field). ``None`` indicates the legacy path (``--setup``,
            or future programmatic invocations that do not pass through
            the picker); the caller is expected to fall through to
            :meth:`AgentLoop._select_backend`.
        config: The active :class:`VibeConfig` instance. Both the
            provider lookup (``config.providers`` iteration) and the
            timeout (``config.api_timeout``) come from this object.

    Returns:
        A ready-to-use :class:`BackendLike` instance (caller does NOT
        need to ``__aenter__``; :class:`AgentLoop` handles the context
        manager protocol around every LLM call site). Returns ``None``
        when:

        * ``backend`` is ``None`` (no enum to dispatch on), OR
        * No :class:`ProviderConfig` in ``config.providers`` has
          ``backend == backend`` (defensive: a misconfigured
          ``~/.blitzy/config.toml`` could omit a provider entry), OR
        * The enum is :attr:`Backend.GENERIC` or
          :attr:`Backend.CLAUDE_CODE` (neither is user-selectable; the
          legacy factory call inside :meth:`AgentLoop._select_backend`
          still supports them via the original two-arg signature).

    Raises:
        MissingAPIKeyError: Propagated from the backend constructor's
            internal :func:`resolve_or_prompt` call (AAP rule 10 -- the
            user declined the interactive API key prompt). The CLI
            entrypoint's outer ``except`` clause catches this and exits
            with a clear error message.

    Note:
        The helper does NOT enter the backend's async context manager.
        :class:`AgentLoop` uses ``async with self.backend as backend:``
        inside every LLM call site (e.g.
        ``agent_loop.py:L578, L625, L854``), so the same instance can
        be entered repeatedly across turns -- :meth:`__aenter__` opens
        a fresh HTTP client per turn and :meth:`__aexit__` closes it,
        matching the existing per-call lifecycle.
    """
    if backend is None:
        return None

    # Locate the ProviderConfig whose ``backend`` field matches the enum.
    # ``config.providers`` is the active provider list (defaults from
    # ``DEFAULT_PROVIDERS`` unless overridden in ``~/.blitzy/config.toml``).
    provider_config: ProviderConfig | None = None
    for provider in config.providers:
        if provider.backend == backend:
            provider_config = provider
            break

    if provider_config is None:
        # Defensive fallback: a malformed user config could omit a
        # provider entry that the picker still admits. Logging at WARNING
        # surfaces the misconfiguration without crashing; ``None`` here
        # delegates backend selection to ``AgentLoop._select_backend``
        # (which uses ``active_model.provider`` and the legacy signature).
        logger.warning(
            "No provider with backend=%r found in config.providers; "
            "falling back to active-model provider selection. "
            "Add a provider entry with backend=%r to ~/.blitzy/config.toml "
            "to use --provider %s.",
            backend.value,
            backend.value,
            backend.value,
        )
        return None

    # Look up the backend class via the canonical factory. ``BACKEND_FACTORY``
    # is the SINGLE source of truth (AAP §0.5.4 step Z) and test patches
    # against this dict are observed at this call site (Rule 4 invariant).
    backend_cls = BACKEND_FACTORY[backend]

    # Per-backend signature dispatch. The three user-selectable backends
    # (Rule 13) each have their own signature shape; the dispatch is
    # explicit (rather than ``**kwargs`` magic) so static type checkers
    # can verify the call shape and future readers can trace the contract.
    if backend == Backend.BLITZY:
        # BlitzyLLMBackend.__init__(provider, config, *, repo, branch).
        # ``detect_git_context`` NEVER raises (rule 3 silent-failure
        # contract); absent ``.git`` yields ``("", "")`` which the backend
        # forwards as empty URL parameters (``/context?repo=&branch=``).
        repo, branch = detect_git_context()
        return backend_cls(provider_config, config, repo=repo, branch=branch)

    if backend == Backend.ANTHROPIC:
        # AnthropicBackend.__init__(provider, config, timeout=720.0).
        # ``config.api_timeout`` is the existing configurable timeout
        # (default 720.0s); matches the value formerly passed by
        # ``AgentLoop._select_backend``.
        return backend_cls(provider_config, config, config.api_timeout)

    if backend == Backend.MISTRAL:
        # MistralBackend.__init__(provider, timeout=720.0) -- legacy
        # signature, preserved unchanged per AAP boundary directive
        # ("Mistral backend: untouched, preserved as-is"). The backend
        # does not consume ``config`` directly; its API key resolution
        # remains env-var-only as in the pre-feature codebase.
        return backend_cls(provider_config, config.api_timeout)

    # Backend.GENERIC and Backend.CLAUDE_CODE are intentionally not
    # dispatched here -- they are NOT user-selectable via ``--provider``
    # (Rule 13), and their constructors still use the legacy two-arg
    # ``(provider, timeout)`` signature served by
    # ``AgentLoop._select_backend``. Returning ``None`` cleanly delegates.
    return None


def _make_message_observer(
    session_record: SessionRecord, session_manager: SessionManager
) -> Callable[[LLMMessage], None]:
    """Build the per-turn save callback wired into AgentLoop.message_observer.

    AgentLoop invokes the callback for every new message appended to history.
    We mirror the message into the SessionRecord and full-overwrite the JSON
    file (AAP rule 6 — save after every turn).
    """

    def _on_message_added(msg: LLMMessage) -> None:
        session_record.messages.append(msg)
        try:
            session_manager.save(session_record)
        except OSError as exc:
            # Saving must not crash the user-facing loop; log and continue.
            logger.warning(
                "Failed to save session %s: %s", session_record.session_id, exc
            )

    return _on_message_added


def run_cli(
    args: argparse.Namespace,
    backend: Backend | None = None,
    restored_session: SessionRecord | None = None,
) -> None:
    """Top-level CLI orchestration entrypoint.

    Resolves config, builds a :class:`SessionRecord` (rule 3 git context
    detection — never raises), binds the per-session correlation ID, and
    branches into one of two execution modes:

    * **Interactive mode** (default — no ``--prompt``): wires the per-turn
      session-save observer into :class:`AgentLoop` (AAP rule 6 — full
      overwrite after every turn), installs the provider-aware
      :class:`AutoCompactMiddleware` (AAP rule 11 — 80% of the active
      provider's context limit), hydrates any restored conversation history,
      and hands off to the Textual UI.

    * **Programmatic mode** (``--prompt <text>``): runs the agent as a
      one-shot batch (``run_programmatic``). **Programmatic mode does
      INTENTIONALLY NOT persist session JSON** under
      ``~/.blitzy/sessions/{repo}/{branch}/`` because (a) it is a one-shot
      batch invocation where on-disk persistence has no resume target, and
      (b) the AAP §0.5.5 per-turn save flow is specified for the interactive
      Textual UI loop. The :class:`SessionRecord` is still built (for the
      correlation ID binding at line below) but the
      :func:`_make_message_observer` callback is NOT wired into the
      programmatic path. Operators who need durable history for batch jobs
      can pipe stdout through their own logging pipeline; this matches the
      ergonomics of ``--prompt`` already in use.

    Args:
        args: Parsed CLI arguments from :mod:`argparse`. Notable fields:
            ``prompt`` (programmatic-mode trigger), ``initial_prompt``
            (interactive seed), ``continue_session`` /
            ``resume`` (legacy session re-load flags), ``agent``
            (initial agent name), ``setup`` (run onboarding wizard).
        backend: Pre-resolved :class:`Backend` enum from the
            ``--provider`` flag or the interactive provider picker
            (entrypoint orchestration — see AAP §0.5.4). ``None`` falls
            back to the model registry's default provider via
            :meth:`AgentLoop._select_backend`.
        restored_session: A :class:`SessionRecord` selected from the
            ``--resume`` interactive picker. When supplied, the
            conversation history is hydrated from
            ``restored_session.messages`` (with the system message
            dropped — :class:`AgentLoop` constructs a fresh one) and
            provider selection is skipped (AAP rule 5). ``None`` for
            fresh sessions.

    Returns:
        ``None``. Exits the process via :func:`sys.exit` on
        :class:`KeyboardInterrupt`, :class:`EOFError`, missing prompt,
        or runtime errors raised during programmatic execution.
    """
    load_dotenv_values()
    bootstrap_config_files()

    # Auto-bootstrap check (logic preserved; extracted to helper for clarity).
    bootstrap_status = _maybe_run_auto_bootstrap(args)

    if args.setup:
        run_onboarding()
        sys.exit(0)

    try:
        initial_agent_name = get_initial_agent_name(args)
        config = load_config_or_exit()

        # --- AAP Capability C: Session persistence wiring (rules 2, 3, 6) ---
        session_record = _build_session_record(backend, restored_session)
        # Bind the per-session correlation ID for structured logging (rule 2).
        set_correlation_id(session_record.session_id)
        # Saves are full-overwrite per turn (rule 6).
        session_manager = SessionManager()

        if args.enabled_tools:
            config.enabled_tools = args.enabled_tools

        loaded_messages = _resolve_loaded_messages(args, restored_session, config)

        # --- AAP §0.5.4 step Z: Instantiate backend via factory ---------
        # Construct the BackendLike instance from the resolved Backend enum
        # BEFORE handing off to AgentLoop / run_programmatic. This closes
        # checkpoint review finding C5-CRIT-01: without this step, AgentLoop's
        # ``self.backend_factory = lambda: backend or self._select_backend()``
        # (agent_loop.py:L127) would invoke the legacy factory signature
        # ``BACKEND_FACTORY[provider.backend](provider=provider, timeout=...)``
        # which is INCOMPATIBLE with the new ``BlitzyLLMBackend(provider,
        # config, *, repo, branch)`` and ``AnthropicBackend(provider,
        # config, timeout)`` signatures introduced by this feature delivery.
        #
        # The helper returns ``None`` for ``backend is None`` (legacy paths
        # like setup-mode or callers that bypass the picker), which preserves
        # the original ``AgentLoop._select_backend`` fallback behavior.
        backend_instance = _build_backend_instance(backend, config)

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
                    # AAP §0.5.4 step Z (C5-CRIT-01 fix): forward the
                    # user-selected backend instance into the programmatic
                    # path so ``vibe -p "..." --provider blitzy`` invokes
                    # the correct backend at the LLM call site rather than
                    # silently falling back to the active-model provider.
                    backend=backend_instance,
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
            # AAP rule 6 — per-turn save hook via AgentLoop.message_observer.
            on_message_added = _make_message_observer(session_record, session_manager)
            agent_loop = AgentLoop(
                config,
                agent_name=initial_agent_name,
                message_observer=on_message_added,
                # AAP §0.5.4 step Z (C5-CRIT-01 fix): pass the user-selected
                # backend instance so ``AgentLoop`` short-circuits its
                # default ``_select_backend()`` (which would otherwise use
                # the legacy two-arg signature incompatible with the new
                # BlitzyLLMBackend / AnthropicBackend constructors). When
                # ``backend_instance`` is None (legacy callers, --setup
                # mode), AgentLoop falls back to ``_select_backend()`` as
                # before -- preserving backward compatibility.
                backend=backend_instance,
                enable_streaming=True,
            )

            # AAP rule 11 — provider-aware AutoCompactMiddleware (80% threshold).
            _install_provider_aware_compact(agent_loop, backend, config)

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
