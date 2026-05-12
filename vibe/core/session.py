"""Session persistence manager for per-repo/per-branch JSON sessions.

This module is a PEER addition to the existing ``vibe.core.session`` subpackage.
The existing subpackage writes async turn logs to ``~/.blitzy/logs/session/``;
this module manages a SEPARATE file family at
``~/.blitzy/sessions/{repo_name}/{branch_name}/{session_id}.json`` for the
``--resume`` feature defined in the AAP (Capability C).

Module-as-package coexistence
-----------------------------

The pre-existing ``vibe/core/session/`` directory is a namespace package
containing ``session_logger.py``, ``session_loader.py``, and
``session_migration.py`` for ACP/MCP turn logging. Without intervention, the
mere existence of ``vibe/core/session.py`` (a regular module) would shadow the
namespace package and break all
``from vibe.core.session.session_logger import ...`` imports throughout the
codebase (``vibe.core.agent_loop``, ``vibe.cli.cli``,
``vibe.cli.textual_ui.app``, and the corresponding test files).

To preserve those imports while also exposing the new
:class:`SessionManager`/:class:`SessionRecord` API on the ``vibe.core.session``
namespace, this module assigns its ``__path__`` to the existing directory.
This is a documented Python pattern (see :pep:`328` and the standard library
``os`` module, which uses the same technique to expose ``os.path``): a regular
module that sets ``__path__`` simultaneously behaves as a module (with its own
attributes) and as a package (allowing submodule imports). Both
``from vibe.core.session import SessionManager`` AND
``from vibe.core.session.session_logger import SessionLogger`` resolve
correctly, satisfying the AAP rule "MUST be a distinct addition that does not
collide with the existing ``vibe/core/session/`` subpackage" without modifying
the existing subpackage modules.

Storage root resolution order (AAP §0.6.1 Group 1):

1. ``VIBE_HOME`` env var, if set and non-empty (AAP-mandated highest
   precedence for the sessions root).
2. ``BLITZY_HOME`` env var, if set and non-empty (operator-convenience
   fallback that aligns with the rest of the codebase — notably
   ``vibe/core/paths/global_paths.py`` — so an operator who sets only one
   of the two env vars sees consistent session storage behavior).
3. ``~/.blitzy`` (``Path.home() / ".blitzy"``).

The two env vars coexist because the AAP explicitly named ``VIBE_HOME``
for the sessions root while the pre-existing codebase used ``BLITZY_HOME``
for the global config / log paths. Reading both (in the order above)
preserves the AAP precedence while preventing the operator-experience
divergence flagged by QA report finding Issue #2.

Compaction (rule 7): when ``len(json.dumps(messages)) // 4`` exceeds
``0.8 * token_limit``, the oldest half of the messages is replaced by a single
``Role.system`` summary message; the newest half is preserved verbatim.

Public surface
--------------

* :class:`SessionRecord` — Pydantic model matching the AAP §0.2.2 User
  Example JSON shape verbatim.
* :class:`SessionManager` — save/load/list/compact orchestrator.
* :func:`estimate_tokens` — user-mandated rule 7 estimator
  (``len(json.dumps(messages)) // 4``).
* :func:`new_session` — factory for fresh :class:`SessionRecord` instances
  with new UUID4 hex IDs and ISO 8601 ``Z``-suffixed UTC timestamps.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import uuid

from pydantic import BaseModel, ConfigDict

from vibe.core.llm.exceptions import SessionNotFoundError
from vibe.core.observability import increment, span
from vibe.core.types import LLMMessage, Role

# ---------------------------------------------------------------------------
# Module-as-package coexistence with ``vibe/core/session/`` namespace package
# ---------------------------------------------------------------------------
#
# Setting ``__path__`` makes this module also behave as a package for the
# purposes of submodule lookup. We point it at the sibling ``session``
# directory so that pre-existing imports such as
# ``from vibe.core.session.session_logger import SessionLogger`` continue to
# work unchanged.
#
# ``__file__`` is always set for a regular module loaded from a real source
# file, so the ``Path(__file__).with_suffix("")`` derivation is robust and
# does not depend on the current working directory.
__path__: list[str] = [str(Path(__file__).with_suffix(""))]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SessionRecord — JSON shape mirrors AAP §0.2.2 User Example verbatim
# ---------------------------------------------------------------------------


class SessionRecord(BaseModel):
    """Persisted session record.

    The JSON shape matches the AAP §0.2.2 User Example verbatim:

    .. code-block:: json

        {
          "session_id": "<uuid4>",
          "created_at": "<ISO 8601>",
          "provider": "<blitzy|mistral|anthropic>",
          "repo": "<str>",
          "branch": "<str>",
          "messages": [...],
          "compacted_summary": "<str|null>"
        }

    The field order below matches the JSON output order, which is the natural
    Pydantic v2 ``model_dump_json`` order, for human readability when an
    operator inspects a session file on disk.

    Attributes:
        session_id: 32-char lowercase hex UUID4, generated by
            :func:`uuid.uuid4` ``.hex`` (no hyphens).
        created_at: ISO 8601 UTC timestamp with explicit ``Z`` suffix
            (e.g., ``"2026-04-22T10:14:03Z"``). Stored as a string (not a
            timezone-aware :class:`datetime`) for JSON-friendliness and
            lexicographic sortability.
        provider: Lowercase provider identifier, one of ``"blitzy"``,
            ``"mistral"``, or ``"anthropic"``. MUST match
            ``Backend.<X>.value`` exactly (AAP rule 13).
        repo: Repository name from
            :func:`vibe.core.git_context.detect`. ``""`` when git context is
            unavailable.
        branch: Branch name from
            :func:`vibe.core.git_context.detect`. ``""`` when git context is
            unavailable or HEAD is detached.
        messages: Conversation messages as :class:`LLMMessage` instances.
        compacted_summary: Summary text from the most recent
            :meth:`SessionManager.compact` call, or ``None`` if compaction
            has never run for this session.

    The ``extra="ignore"`` config allows forward-compatible field additions
    without breaking the load path for older session files.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    created_at: str
    provider: str
    repo: str
    branch: str
    messages: list[LLMMessage]
    compacted_summary: str | None = None


# ---------------------------------------------------------------------------
# estimate_tokens — user-mandated rule 7 estimator
# ---------------------------------------------------------------------------


def estimate_tokens(messages: list[LLMMessage]) -> int:
    """Estimate token count via the user-mandated rule 7 formula.

    Returns ``len(json.dumps(messages)) // 4``, where each :class:`LLMMessage`
    is serialized via its Pydantic ``model_dump(mode="json")``. The
    ``mode="json"`` argument ensures nested fields such as ``tool_calls`` and
    the ``Role`` enum serialize to plain JSON-compatible types (strings,
    dicts, lists, numbers, ``None``).

    ``ensure_ascii=False`` is passed to :func:`json.dumps` so the produced
    string's character count tracks the raw UTF-8 byte-length the user would
    intuit when applying the "approximately 4 chars per token" rule of thumb
    to non-ASCII content. (With ``ensure_ascii=True``, a single non-ASCII
    code point can expand to up to six ASCII bytes of ``\\uXXXX`` escape,
    which would inflate the estimate misleadingly.)

    Args:
        messages: List of :class:`LLMMessage` instances to measure.

    Returns:
        An ``int`` token estimate (``len // 4``) suitable for direct
        comparison against an integer ``token_limit`` or its ``0.8`` fraction.

    Notes:
        The formula is intentionally cheap (no tokenizer dependency, no
        provider-specific calibration). It is acknowledged as an estimate;
        the AAP explicitly mandates this exact expression in rule 7, so it is
        preserved verbatim for predictability.
    """
    serialized = json.dumps(
        [m.model_dump(mode="json") for m in messages], ensure_ascii=False
    )
    return len(serialized) // 4


# ---------------------------------------------------------------------------
# SessionManager — save/load/list/compact orchestrator
# ---------------------------------------------------------------------------


class SessionManager:
    """Per-repo/per-branch JSON session manager.

    Manages session files at
    ``{vibe_home}/sessions/{repo_name}/{branch_name}/{session_id}.json``. The
    storage root resolution order is:

    1. The constructor argument ``vibe_home`` (highest precedence — used by
       tests for hermetic ``tmp_path`` injection).
    2. The ``VIBE_HOME`` environment variable, if set and non-empty after
       stripping whitespace (AAP §0.6.1 mandated env var for sessions root).
    3. The ``BLITZY_HOME`` environment variable, if set and non-empty
       (operator-convenience fallback that aligns with
       ``vibe/core/paths/global_paths.py`` — addresses QA report Issue #2
       so operators setting only one of the two env vars get consistent
       behavior).
    4. ``~/.blitzy`` (``Path.home() / ".blitzy"``) as the production default.

    Each session is a single JSON file written by full overwrite after every
    turn (AAP rule 6). Compaction collapses the oldest-half messages into a
    single :class:`Role.system` summary message when the conversation exceeds
    80% of the active provider's configured context limit (AAP rule 7).

    Thread safety:
        Methods are not synchronized. The CLI is single-threaded per session;
        if multiple managers operate concurrently against the same session ID
        the last write wins (no locking is performed because session files
        are scoped per ``(repo, branch, session_id)`` and only one CLI
        instance is expected per such tuple).
    """

    def __init__(self, vibe_home: Path | None = None) -> None:
        """Construct a manager rooted at the resolved Vibe home directory.

        Args:
            vibe_home: Explicit override for the Vibe home directory. When
                ``None``, falls back to ``$VIBE_HOME`` (AAP-mandated), then
                ``$BLITZY_HOME`` (operator-convenience fallback consistent
                with ``vibe/core/paths/global_paths.py``), then
                ``~/.blitzy``. This argument is the primary mechanism by
                which tests inject a temporary directory without monkey
                patching :func:`Path.home`.
        """
        if vibe_home is not None:
            self._home = Path(vibe_home)
        else:
            # Resolution order (AAP §0.6.1 + QA report Issue #2 mitigation):
            # 1. VIBE_HOME (AAP-mandated for sessions root).
            # 2. BLITZY_HOME (consistent with the rest of the codebase —
            #    notably vibe/core/paths/global_paths.py uses this name).
            # 3. ~/.blitzy default.
            # Reading both env vars (with VIBE_HOME winning when both are
            # set, preserving AAP precedence) prevents the operator-UX gap
            # flagged by QA where setting only one env var produced
            # inconsistent results between sessions storage and other home-
            # rooted artifacts.
            env_home = os.environ.get("VIBE_HOME", "").strip()
            if not env_home:
                env_home = os.environ.get("BLITZY_HOME", "").strip()
            self._home = Path(env_home) if env_home else Path.home() / ".blitzy"
        self._sessions_root = self._home / "sessions"

    # ---- internal helpers ------------------------------------------------

    def _path_for(self, repo: str, branch: str, session_id: str) -> Path:
        """Compute the JSON file path for a given ``(repo, branch, session_id)``.

        When ``repo`` or ``branch`` is an empty string (the
        :func:`vibe.core.git_context.detect` "git context unavailable"
        sentinel — AAP rule 3 and §0.5.3), the corresponding path segment
        falls back to ``"_unknown"``. The two fields are handled
        independently because git context detection can succeed for the repo
        name while failing for the branch (detached HEAD) and vice versa.

        Args:
            repo: Repository name segment (possibly ``""``).
            branch: Branch name segment (possibly ``""``).
            session_id: Session UUID4 hex.

        Returns:
            Absolute path to the session JSON file (not necessarily
            existing).

        Notes:
            No path-segment sanitization is performed beyond the empty-string
            check. Git refs and remote names are constrained by Git's own
            naming rules (no path separators, no NUL bytes) and the inputs
            to this method come from :func:`vibe.core.git_context.detect`,
            which only forwards values that have parsed successfully out of
            ``.git/HEAD`` and ``.git/config``.
        """
        safe_repo = repo if repo else "_unknown"
        safe_branch = branch if branch else "_unknown"
        return self._sessions_root / safe_repo / safe_branch / f"{session_id}.json"

    # ---- public API ------------------------------------------------------

    def save(self, session: SessionRecord) -> Path:
        """Persist ``session`` to disk by full overwrite (AAP rule 6).

        The target path is computed by :meth:`_path_for` from
        ``session.repo``, ``session.branch``, and ``session.session_id``.
        Parent directories are created on demand
        (``mkdir(parents=True, exist_ok=True)``); the file is opened in
        ``"w"`` mode for full overwrite, which is explicitly required by
        rule 6 ("written (full overwrite) after every turn").

        The serialized JSON is indented to two spaces for human readability
        when an operator inspects the file on disk; indentation does not
        affect the JSON shape and is recovered transparently by
        :meth:`load` and :meth:`list_sessions`.

        AAP observability rule + ``docs/observability/dashboard.json``: the
        ``session.save`` span name is dashboard-required ("Span breakdown
        per session" trace view). The span is recorded around the directory
        creation + write so operators can see per-turn save latency in the
        dashboard. ``message_count`` and ``bytes_written`` are recorded as
        attrs for payload-size triage.

        Args:
            session: The session record to persist.

        Returns:
            The absolute :class:`Path` written, useful for tests that assert
            on the on-disk layout.
        """
        with span(
            "session.save",
            session_id=session.session_id,
            provider=session.provider,
            repo=session.repo,
            branch=session.branch,
        ) as span_attrs:
            span_attrs["message_count"] = len(session.messages)
            target = self._path_for(session.repo, session.branch, session.session_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = session.model_dump_json(indent=2)
            with target.open("w", encoding="utf-8") as f:
                f.write(payload)
            span_attrs["bytes_written"] = len(payload.encode("utf-8"))

            # Filesystem hardening (QA checkpoint #7 Issue #3): tighten the
            # on-disk permission of the session JSON file to ``0o600`` (owner
            # read/write only). Session files contain the full conversation
            # history -- user prompts, tool outputs, and assistant responses
            # -- which may include sensitive personal, business, or source-
            # code context typed into the agent. The default mode produced
            # by ``target.open("w")`` honors the process umask (typically
            # ``0o022`` on Linux), yielding ``0o644`` (world-readable). On
            # multi-user workstations and shared developer environments this
            # would expose every operator's chat history to any local user
            # with read access to ``~/.blitzy/sessions/``. Explicitly
            # chmod'ing both the file (``0o600``) and the immediate parent
            # branch directory (``0o700``) closes this gap.
            #
            # We chmod the leaf file plus the three nested directories
            # (``{branch}``, ``{repo}``, ``sessions``) that this manager
            # owns and creates via ``mkdir(parents=True)`` above. We do NOT
            # chmod ``self._home`` (e.g. ``~/.blitzy/``) because that
            # directory is shared with other tools (logs, config) and may
            # legitimately already exist with operator-chosen permissions
            # that we should not silently override.
            #
            # All chmod calls are wrapped in ``try / except OSError`` to
            # preserve the function's no-raise contract (consistent with AAP
            # rule 6 -- saves happen on every turn and MUST NOT crash the
            # CLI if the filesystem rejects a mode change). Filesystems that
            # do not support permission bits (e.g. FAT32, some FUSE mounts,
            # certain Windows-mounted shares) raise ``OSError`` from chmod;
            # the session content is already persisted at that point, so a
            # permission-tightening failure is purely a hardening miss, not
            # a data-loss event.
            try:
                os.chmod(target, 0o600)
            except OSError:
                # Best-effort; file is already written. Skip directory
                # tightening too -- if leaf chmod failed, the directory
                # chmod is unlikely to succeed and adds noise without value.
                span_attrs["outcome"] = "ok"
                return target
            # Tighten the three manager-owned directories we created via
            # ``mkdir(parents=True)``. We walk from leaf to root so a
            # mid-chain failure (e.g., a parent that already exists and is
            # not owned by us) does not abort tightening of the inner dirs
            # that we definitely just created.
            for ancestor in (
                target.parent,  # .../sessions/{repo}/{branch}/
                target.parent.parent,  # .../sessions/{repo}/
                target.parent.parent.parent,  # .../sessions/
            ):
                try:
                    os.chmod(ancestor, 0o700)
                except OSError:
                    # Best-effort; continue to the next ancestor. We
                    # intentionally do NOT break out of the loop because
                    # the failure may be specific to one directory (e.g.,
                    # a pre-existing operator-chmod'd dir higher up in the
                    # tree) while inner directories that this save just
                    # created can still be tightened successfully.
                    continue
            span_attrs["outcome"] = "ok"
            return target

    def load(self, session_id: str) -> SessionRecord:
        """Find and load the session record matching ``session_id``.

        Searches every ``(repo, branch)`` subdirectory under the sessions
        root because callers that hold only the session ID
        (e.g., ``blitzy --resume <id>``) may not know the repo/branch
        context the session was created under. UUID4 hex strings are unique
        with overwhelming probability, so exactly one file is expected to
        match.

        Args:
            session_id: UUID4 hex (32 characters) identifying the session.

        Returns:
            The loaded :class:`SessionRecord`.

        Raises:
            SessionNotFoundError: When no file named ``{session_id}.json``
                exists anywhere beneath the sessions root, or when the
                sessions root itself does not exist (AAP §0.5.1).
        """
        if not self._sessions_root.exists():
            raise SessionNotFoundError(session_id)
        matches = list(self._sessions_root.rglob(f"{session_id}.json"))
        if not matches:
            raise SessionNotFoundError(session_id)
        with matches[0].open("r", encoding="utf-8") as f:
            data = f.read()
        return SessionRecord.model_validate_json(data)

    def list_sessions(self, repo: str, branch: str) -> list[SessionRecord]:
        """Enumerate session records for the given ``(repo, branch)``.

        Records are sorted by ``created_at`` descending (most recent first).
        Sorting by the ISO 8601 ``Z``-suffixed UTC string is
        lexicographically correct because :func:`new_session` produces
        zero-padded fixed-width components (``%Y-%m-%dT%H:%M:%SZ``) — the
        natural string ordering coincides with chronological ordering.

        Files that fail to parse are silently skipped with a DEBUG log
        entry. This keeps the picker UI stable in the face of:

        * Partial writes from a crashed CLI process.
        * Schema drift across CLI versions.
        * Manual operator edits that introduce JSON syntax errors.

        Args:
            repo: Repository name (``""`` falls back to ``"_unknown"``).
            branch: Branch name (``""`` falls back to ``"_unknown"``).

        Returns:
            List of :class:`SessionRecord` instances, sorted by ``created_at``
            descending. Returns ``[]`` when the directory does not exist,
            is not a directory, or contains no readable JSON sessions
            (AAP rule 5: the picker falls through to provider selection).
        """
        safe_repo = repo if repo else "_unknown"
        safe_branch = branch if branch else "_unknown"
        directory = self._sessions_root / safe_repo / safe_branch
        if not directory.exists() or not directory.is_dir():
            return []

        records: list[SessionRecord] = []
        for entry in directory.iterdir():
            # Skip non-JSON entries (e.g., editor backup files, dotfiles)
            # and any non-regular files (directories, symlinks to dirs).
            if entry.suffix != ".json" or not entry.is_file():
                continue
            try:
                with entry.open("r", encoding="utf-8") as f:
                    data = f.read()
                records.append(SessionRecord.model_validate_json(data))
            except (OSError, ValueError) as exc:
                # ``ValueError`` covers both ``json.JSONDecodeError`` (a
                # ``ValueError`` subclass) and Pydantic ``ValidationError``
                # (also a ``ValueError`` subclass in Pydantic v2). Using
                # this single tuple avoids importing ``json`` or
                # ``pydantic.ValidationError`` solely for the except clause.
                # A bare ``except`` is intentionally NOT used; it would
                # mask genuine bugs (e.g., ``KeyboardInterrupt``).
                logger.debug("Skipping malformed session file %s: %s", entry, exc)
                continue
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def compact(
        self,
        session: SessionRecord,
        token_limit: int,
        complete_fn: Callable[[list[LLMMessage]], str],
    ) -> SessionRecord:
        """Compact ``session`` when token usage exceeds 80% of ``token_limit``.

        Implements the AAP rule 7 compaction strategy:

        1. Compute current usage with :func:`estimate_tokens`.
        2. If usage ``<= 0.8 * token_limit``, return ``session`` unchanged
           (no-op fast path).
        3. Split messages at the midpoint ``len(messages) // 2``.
        4. Pass the oldest half (prefixed with a summarization system
           prompt) to ``complete_fn`` and receive the summary text.
        5. Replace the oldest half with a single :class:`Role.system`
           message containing the summary; append the newest half
           VERBATIM (no transformation).
        6. Set ``session.compacted_summary`` to the summary text and return
           the updated record.

        The midpoint cutoff is the canonical "oldest half" definition. When
        the message count is ``0`` or ``1``, ``len // 2 == 0``, so the
        compaction is skipped to avoid summarizing an empty slice (which
        would discard the single remaining message into a summary of
        nothing).

        The ``session`` argument is mutated in place (``session.messages``
        is replaced with a freshly built list; ``session.compacted_summary``
        is assigned). The same object is returned for caller convenience
        and to enable expression-level chaining
        (``record = manager.compact(record, ...)``).

        AAP observability rule + ``docs/observability/dashboard.json``: the
        ``session.compact`` span name is dashboard-required ("Span breakdown
        per session" trace view + "Compaction count" metric panel). The
        span wraps the FULL compaction flow (threshold check, summarization
        LLM call via ``complete_fn``, and message-list replacement) so the
        recorded duration reflects the end-to-end compaction cost including
        the embedded LLM round-trip. The ``compacted`` boolean attr
        distinguishes triggered compactions from no-op fast-path returns.

        Args:
            session: The session record to compact (mutated in place).
            token_limit: The active provider's configured context limit in
                tokens. The threshold is ``0.8 * token_limit`` per rule 7.
            complete_fn: Callable accepting a ``list[LLMMessage]`` and
                returning the assistant's response text as a ``str``. The
                implementation is expected to pass the active backend's
                bound ``complete()`` method (or an adapter that converts an
                :class:`LLMChunk` to its content string). The intentionally
                minimal signature decouples this manager from the
                :class:`BackendLike` protocol.

        Returns:
            The (possibly mutated) :class:`SessionRecord`.
        """
        with span(
            "session.compact",
            session_id=session.session_id,
            provider=session.provider,
            token_limit=token_limit,
        ) as span_attrs:
            threshold = 0.8 * token_limit
            current = estimate_tokens(session.messages)
            span_attrs["estimated_tokens"] = current
            span_attrs["threshold"] = threshold
            span_attrs["message_count_before"] = len(session.messages)
            if current <= threshold:
                span_attrs["compacted"] = False
                span_attrs["outcome"] = "below_threshold"
                return session

            midpoint = len(session.messages) // 2
            if midpoint == 0:
                # Zero or one message — nothing meaningful to compact.
                span_attrs["compacted"] = False
                span_attrs["outcome"] = "too_few_messages"
                return session

            oldest_half = session.messages[:midpoint]
            newest_half = session.messages[midpoint:]

            summarization_prompt = LLMMessage(
                role=Role.system,
                content=(
                    "Summarize the following conversation in 200 words or less, "
                    "preserving key facts, tool calls, and decisions. The summary "
                    "will replace these messages in the conversation history."
                ),
            )
            summary_text = complete_fn([summarization_prompt, *oldest_half])
            summary_message = LLMMessage(role=Role.system, content=summary_text)

            # Replace (rather than mutate-in-place via index assignment) so
            # any external references to the OLD list are not silently
            # corrupted.
            session.messages = [summary_message, *newest_half]
            session.compacted_summary = summary_text
            span_attrs["compacted"] = True
            span_attrs["message_count_after"] = len(session.messages)
            span_attrs["summary_length"] = len(summary_text)
            span_attrs["outcome"] = "ok"
            # AAP §0.5.5 per-turn flow + dashboard.json metric_panels[1]
            # "Compaction count": emit the named ``compactions_triggered``
            # counter only when a real compaction was performed. The
            # below_threshold and too_few_messages fast-paths above return
            # without incrementing so the metric reflects ACTUAL
            # compactions, not threshold evaluations.
            increment("compactions_triggered")
            return session


# ---------------------------------------------------------------------------
# new_session — convenience factory for fresh SessionRecord instances
# ---------------------------------------------------------------------------


def new_session(provider: str, repo: str, branch: str) -> SessionRecord:
    """Build a fresh :class:`SessionRecord` ready for the first turn.

    Generates a 32-char lowercase hex UUID4 (``uuid.uuid4().hex``) as the
    ``session_id`` and an ISO 8601 UTC timestamp with explicit ``Z`` suffix
    as ``created_at``. The timestamp format ``%Y-%m-%dT%H:%M:%SZ`` is
    lexicographically sortable, which is the invariant
    :meth:`SessionManager.list_sessions` relies on to sort records by
    recency without parsing dates.

    Args:
        provider: Lowercase provider identifier
            (``"blitzy"`` / ``"mistral"`` / ``"anthropic"``) — written
            verbatim into the record.
        repo: Repository name from
            :func:`vibe.core.git_context.detect` (may be ``""``).
        branch: Branch name from
            :func:`vibe.core.git_context.detect` (may be ``""``).

    Returns:
        A fresh :class:`SessionRecord` with an empty ``messages`` list and
        ``compacted_summary=None``.

    Notes:
        :func:`datetime.utcnow` is intentionally NOT used; it is deprecated
        in Python 3.12+. Using :func:`datetime.now` with the
        :data:`datetime.UTC` constant (a Python 3.11+ alias for
        ``timezone.utc``) and an explicit ``Z`` suffix keeps the timestamp
        unambiguous and forward-compatible.
    """
    now_utc = datetime.now(UTC)
    created_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return SessionRecord(
        session_id=uuid.uuid4().hex,
        created_at=created_at,
        provider=provider,
        repo=repo,
        branch=branch,
        messages=[],
        compacted_summary=None,
    )
