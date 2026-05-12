"""Interactive picker for resuming a saved session.

This module exposes :func:`select_session`, the interactive prompt shown at
CLI startup when ``--resume`` is supplied.  It lists the saved sessions for
the current ``(repo, branch)`` (most recent first) and returns the chosen
record together with its provider as a :class:`~vibe.core.config.Backend`.

Per AAP section 0.8.1 rule 5:

* **Sessions found** -> returns ``(SessionRecord, Backend)``.  The caller MUST
  skip provider selection because the restored session pins the backend.
* **No sessions found** -> prints a notice and returns ``(None, None)``.  The
  caller MUST fall through to :func:`vibe.cli.provider_picker.select_provider`
  rather than exiting.

The mapping from each record's ``provider`` string to a :class:`Backend` enum
is performed via
:func:`vibe.core.llm.backend.factory.provider_string_to_backend` to satisfy
rule 13 (no orphaned strings -- the same map used by ``--provider``).

The picker is intentionally minimal and does not depend on any rich rendering
library, the structured logger, or the git context detector.  The
``(repo, branch)`` tuple is passed in by the entrypoint, which has already
called :func:`vibe.core.git_context.detect` once.  No backend constructor runs
before this picker returns -- rule 5 forbids it on the sessions-found path
(provider is restored from the session record) and rule 4 forbids it on the
fall-through path (the caller invokes :func:`select_provider` next).
"""

from __future__ import annotations

from vibe.core.config import Backend
from vibe.core.llm.backend.factory import provider_string_to_backend
from vibe.core.session import SessionManager, SessionRecord

# Bounded retry limit per AAP section 0.6.1 ("bounded retry").  After this many
# invalid attempts the picker raises :class:`KeyboardInterrupt` so that startup
# aborts cleanly and the entrypoint's outer handler exits without instantiating
# a backend.  The value matches :data:`vibe.cli.provider_picker._MAX_RETRIES`
# for cross-picker UX consistency.
_MAX_RETRIES = 3

# Header line printed BEFORE the numbered list of saved sessions.  Wording is
# fixed per AAP section 0.6.3 and is asserted by the integration tests.
_HEADER = "Select a session to resume:"

# Error message template rendered after each invalid input.  ``n`` is the
# number of sessions in the list (so the operator sees the exact valid range,
# e.g. ``"between 1 and 5"``).
_INVALID_MESSAGE_TEMPLATE = "Invalid choice. Please enter a number between 1 and {n}."


def select_session(
    repo: str, branch: str
) -> tuple[SessionRecord | None, Backend | None]:
    """Interactive picker for resuming a saved session in ``(repo, branch)``.

    Args:
        repo: Repository name detected from the working directory's ``.git``
            metadata.  Empty string when git context is unavailable.
        branch: Branch name detected from ``.git/HEAD``.  Empty string when
            unavailable (e.g., detached HEAD) or when ``repo`` is empty.

    Returns:
        Tuple ``(SessionRecord, Backend)`` when the user picks a session, or
        ``(None, None)`` when no sessions exist (AAP rule 5 -- caller MUST
        fall through to provider selection in this case).

    Raises:
        KeyboardInterrupt: When the user supplies invalid input on every
            attempt (bounded to :data:`_MAX_RETRIES`) or sends Ctrl-C/Ctrl-D.

    Notes:
        Sessions are sorted by ``created_at`` descending (most recent first)
        by the underlying :meth:`SessionManager.list_sessions` call -- the
        picker does NOT re-sort.  The printed columns are:

        * ``short_id``: the first 8 chars of ``record.session_id``.
        * ``created_at``: the ISO 8601 string stored on the record.
        * ``provider``: the lowercase provider string (``blitzy``/``mistral``
          /``anthropic``).
        * ``{N} messages``: ``len(record.messages)``.

        The picker accepts only digit input ``1``..``N``.  Empty input is
        invalid (unlike the provider picker, there is no sensible "default
        session" to fall back to).  The user must either pick a number or
        abort the picker with Ctrl-C.
    """
    # Use the default storage root resolution (``$VIBE_HOME`` env var or
    # ``~/.blitzy``).  Tests inject ``VIBE_HOME`` into the environment to
    # redirect to a tmpdir; the picker does not need to be aware of that
    # detail.
    manager = SessionManager()
    sessions = manager.list_sessions(repo, branch)

    if not sessions:
        # AAP rule 5 -- fall-through path: print the verbatim notice from
        # section 0.6.1 (em-dash, not hyphen-minus) and return the sentinel
        # tuple ``(None, None)`` so the caller can detect this case and
        # proceed to :func:`vibe.cli.provider_picker.select_provider`.
        print(
            f"No previous sessions found for {repo}({branch}) "
            f"\u2014 starting new session"
        )
        return (None, None)

    # Render the numbered list.  The two-space separator between columns
    # matches the AAP section 0.6.3 user example exactly:
    #   ``[1] a1b2c3d4  2026-04-22T10:14:03Z  anthropic   23 messages``.
    # ``record.provider:<10`` left-pads provider to 10 chars so the message
    # count column stays aligned regardless of provider name length
    # ("anthropic" is 9 chars; "blitzy" and "mistral" are shorter).
    # ``msg_count:>3`` right-aligns the count in a 3-char field.
    print(_HEADER)
    for i, record in enumerate(sessions, start=1):
        short_id = record.session_id[:8]
        msg_count = len(record.messages)
        # Singular/plural for grammatical correctness ("1 message" vs
        # "5 messages").  The AAP example only shows plural counts, but the
        # extra polish costs nothing and avoids the "1 messages" eyesore.
        msg_word = "message" if msg_count == 1 else "messages"
        print(
            f"[{i}] {short_id}  {record.created_at}  "
            f"{record.provider:<10} {msg_count:>3} {msg_word}"
        )

    for _ in range(_MAX_RETRIES):
        try:
            # ``input("> ")`` writes ``> `` to stdout (without a newline) and
            # reads a single line, stripping the trailing newline.  The
            # prompt matches the provider picker for visual consistency.
            raw = input("> ")
        except EOFError as exc:
            # Ctrl-D or piped empty stdin -- treat as cancellation.  Convert
            # to ``KeyboardInterrupt`` so the entrypoint has a single
            # exception type to catch for both interactive cancellation
            # paths (this one and the explicit Ctrl-C path).
            raise KeyboardInterrupt("Session selection cancelled (EOF)") from exc

        # Strip surrounding whitespace; do NOT lowercase -- the session
        # picker accepts only digits, so there is no case to fold.
        token = raw.strip()
        if not token:
            # Empty input -- invalid.  Unlike the provider picker, there is
            # no "default session" semantic for Enter; the user MUST pick a
            # number or abort with Ctrl-C.  Silently picking the most recent
            # session would surprise a user who pressed Enter by accident.
            print(_INVALID_MESSAGE_TEMPLATE.format(n=len(sessions)))
            continue

        try:
            choice = int(token)
        except ValueError:
            # Non-numeric input -- reprompt with the explicit valid range
            # message.  ``int()`` raises ``ValueError`` for non-numeric
            # tokens, which is the only failure mode for this conversion.
            print(_INVALID_MESSAGE_TEMPLATE.format(n=len(sessions)))
            continue

        if not (1 <= choice <= len(sessions)):
            # Numeric but out of range (``0``, negative numbers, ``99``
            # when only 3 sessions exist, etc.).  Reprompt.
            print(_INVALID_MESSAGE_TEMPLATE.format(n=len(sessions)))
            continue

        # Happy path: translate the 1-based UI index to a 0-based list
        # index and resolve the record's provider string to its
        # :class:`Backend` enum value through the canonical mapper.  Rule
        # 13: ``provider_string_to_backend`` is the SINGLE source of truth
        # for the ``{"blitzy", "mistral", "anthropic"}`` token set.  A
        # malformed record with an unknown provider surfaces a ``KeyError``
        # -- that condition is a bug in the writer (or a corrupted file),
        # not a user input error, so we intentionally do NOT catch it.
        selected = sessions[choice - 1]
        backend = provider_string_to_backend(selected.provider)
        return (selected, backend)

    # All ``_MAX_RETRIES`` attempts were invalid.  Abort startup cleanly --
    # the entrypoint's outer ``try/except`` catches ``KeyboardInterrupt`` and
    # exits without instantiating a backend.
    raise KeyboardInterrupt(
        f"Session selection failed after {_MAX_RETRIES} invalid attempts"
    )


__all__ = ["select_session"]
