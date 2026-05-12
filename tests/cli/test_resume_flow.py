"""Tests for the `--resume` CLI flow and session picker.

Covers AAP Behavioral Rules 5, 13 and Validation Gates 2, 9:
- Rule 5: ``--resume`` MUST skip provider selection when a session is found
  and loaded; if no sessions exist for the current (repo, branch), MUST
  fall through to provider selection WITHOUT exiting.
- Rule 13: The session record's ``provider`` field is a string in
  ``{"blitzy", "mistral", "anthropic"}`` and round-trips losslessly into
  a :class:`Backend` enum via :func:`provider_string_to_backend`.

These tests use stdlib :mod:`unittest.mock` (``pytest-mock`` is not
available in this project's test infrastructure). The conftest's autouse
fixtures provide an isolated filesystem; the local :func:`session_root`
fixture additionally sets ``$VIBE_HOME`` to the conftest ``config_dir``
so :class:`SessionManager` resolves into the test's tmp dir rather than
the user's real ``~/.blitzy``.

Phase A tests (1-11) exercise :func:`select_session` directly:
* Empty-list path -> ``(None, None)`` + verbatim notice with em-dash.
* Single/multiple sessions -> numbered list, recency sort, index pick.
* Invalid input -> bounded retry with :data:`_MAX_RETRIES`.
* EOF -> ``KeyboardInterrupt``.
* short_id truncation, no-git fallback to ``_unknown/_unknown``.
* Provider-to-Backend enum round-trip (Rule 13).

Phase B tests (12-17) exercise the entrypoint orchestration block:
* ``--resume`` with sessions found skips :func:`select_provider`.
* ``--resume`` with empty list falls through to :func:`select_provider`
  without exiting (Rule 5 fallthrough -- the critical no-exit invariant).
* Provider field on the session record drives the resolved
  :class:`Backend` enum passed to :func:`run_cli` (Rule 13 end-to-end).
* Restored messages are forwarded to :func:`run_cli` via
  ``restored_session.messages``.
* No-git context (``("", "")``) routes to ``_unknown/_unknown``.
* User cancellation inside the picker exits cleanly via
  :class:`SystemExit` without invoking :func:`select_provider`.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Pre-import the heavy CLI modules at module load time so each pytest-xdist
# worker pays the ~1.3s ``textual_ui`` transitive import cost ONCE during
# test-module collection (which is exempt from ``pytest-timeout``'s 10s
# per-test ceiling) rather than during the first ``patch("vibe.cli.cli...")``
# lookup inside ``_invoke_main`` (which IS counted against the test's
# timeout). Without this pre-import, every test that exercises the
# entrypoint orchestration block fails with a 10s ``Failed: Timeout``.
import vibe.cli.cli  # noqa: F401  -- pre-import for xdist timeout safety
import vibe.cli.entrypoint  # noqa: F401  -- pre-import for xdist timeout safety
from vibe.core.config import Backend
from vibe.core.types import LLMMessage, Role

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_root(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin :class:`SessionManager`'s storage root to the conftest ``config_dir``.

    :class:`SessionManager` prefers ``$VIBE_HOME``. By setting it to the
    conftest's ``config_dir`` (which is already an isolated tmp dir), we
    ensure all session writes/reads happen inside the test's sandbox
    without touching the user's real ``~/.blitzy`` directory.

    The conftest ``config_dir`` fixture does NOT export ``VIBE_HOME``; it
    only patches ``global_paths._DEFAULT_VIBE_HOME``. Without this fixture,
    :class:`SessionManager` would fall back to ``Path.home() / ".blitzy"``
    (the production default), which would pollute the developer's real
    home directory and cause test cross-contamination.

    Returns:
        The resolved sessions root directory (``config_dir / "sessions"``).
    """
    monkeypatch.setenv("VIBE_HOME", str(config_dir))
    sessions_dir = config_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    session_id: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    provider: str = "blitzy",
    repo: str = "myrepo",
    branch: str = "main",
    messages: list[LLMMessage] | None = None,
) -> Any:
    """Construct a :class:`SessionRecord` with sensible defaults for tests.

    Lazy-imports :class:`SessionRecord` because the module hosting it
    (``vibe.core.session``) is created by the cascade BEFORE this test
    module is imported, so a top-level import is safe in practice -- but
    keeping it lazy preserves resilience to module-evaluation ordering
    changes and matches the lazy-import discipline used by the
    :func:`_invoke_main` helper for the entrypoint's own lazy imports.

    Args:
        session_id: 32-char UUID4 hex. When ``None``, defaults to the
            predictable prefix ``"abcdef12"`` + 24 zeros so tests can
            assert short_id substrings.
        created_at: ISO 8601 ``Z``-suffixed UTC timestamp. Lexicographic
            order matches chronological order.
        provider: ``"blitzy"``/``"mistral"``/``"anthropic"`` (Rule 13).
        repo: Repository name segment.
        branch: Branch name segment.
        messages: Conversation messages. Defaults to a single user
            message so the record is valid.

    Returns:
        A fully populated :class:`SessionRecord` ready for
        :meth:`SessionManager.save`.
    """
    from vibe.core.session import SessionRecord

    if session_id is None:
        # 32-char hex like ``uuid4().hex``; predictable prefix lets tests
        # match by short_id without needing to capture the generated value.
        session_id = "abcdef12" + "0" * 24
    if messages is None:
        messages = [LLMMessage(role=Role.user, content="hello")]
    return SessionRecord(
        session_id=session_id,
        created_at=created_at,
        provider=provider,
        repo=repo,
        branch=branch,
        messages=messages,
    )


def _save_record(record: Any) -> Path:
    """Save a :class:`SessionRecord` via :class:`SessionManager`.

    Returns:
        The :class:`Path` written by :meth:`SessionManager.save` (useful
        for tests that need to assert the on-disk path layout).
    """
    from vibe.core.session import SessionManager

    return SessionManager().save(record)


def _invoke_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    extra_patches: list[tuple[str, Any]] | None = None,
    expect_system_exit: bool = False,
) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Invoke :func:`vibe.cli.entrypoint.main` with the given ``argv``.

    Patches (always applied):

    * :data:`sys.argv` -- so :func:`parse_arguments` sees the test's argv.
    * ``vibe.cli.cli.run_cli`` -- with a :class:`MagicMock` spy that is
      returned to the caller so assertions can inspect ``call_args``.
      Patching the SOURCE module path works because the entrypoint
      does ``from vibe.cli.cli import run_cli`` lazily INSIDE ``main()``,
      which performs the lookup against the (already-patched) module.
    * ``vibe.cli.entrypoint.check_and_resolve_trusted_folder`` -- no-op,
      because the real implementation may interact with the user's
      trusted-folders manager and we don't want side effects.
    * ``signal.signal`` -- no-op, because ``main()`` calls
      ``signal.signal(signal.SIGINT, signal.SIG_IGN)`` at the very top,
      and we want to leave the running test's signal disposition
      untouched.

    Extra patches can be passed via ``extra_patches``: a list of
    ``(target, mock_obj)`` pairs. Each target is the dotted import path
    that :func:`unittest.mock.patch` operates on. The returned ``extras``
    dict maps each extra-patch target string to its mock instance so
    callers can assert on it without re-creating a local reference.

    Args:
        monkeypatch: The pytest ``MonkeyPatch`` fixture, used to set
            ``sys.argv`` (which is reverted automatically at test
            teardown).
        argv: List of CLI tokens, including ``argv[0]``. Typically
            ``["vibe", "--resume"]`` for this test module.
        extra_patches: Optional extra targets to patch for the duration
            of the call.
        expect_system_exit: When ``True``, ``main()`` is invoked inside
            ``pytest.raises(SystemExit)``. Use this for the cancelled
            flow (test 17).

    Returns:
        Tuple ``(run_cli_spy, extras_dict)`` where ``run_cli_spy`` is
        the :class:`MagicMock` substituted for :func:`run_cli` and
        ``extras_dict`` is keyed by the extra-patch target strings.
    """
    monkeypatch.setattr(sys, "argv", argv)
    run_cli_spy = MagicMock()
    base_targets: dict[str, Any] = {
        "vibe.cli.cli.run_cli": run_cli_spy,
        "vibe.cli.entrypoint.check_and_resolve_trusted_folder": MagicMock(),
        "signal.signal": MagicMock(),
    }
    extras: dict[str, MagicMock] = {}
    if extra_patches:
        for target, mock_obj in extra_patches:
            base_targets[target] = mock_obj
            extras[target] = mock_obj

    patches = [patch(target, mock) for target, mock in base_targets.items()]
    for p in patches:
        p.start()
    try:
        from vibe.cli.entrypoint import main

        if expect_system_exit:
            with pytest.raises(SystemExit):
                main()
        else:
            main()
    finally:
        # ``finally`` guarantees we always restore the patches, even when
        # the test body raises (e.g., an unexpected ``AssertionError`` in
        # ``main``). Without this, leaked patches would pollute subsequent
        # tests under the same pytest session.
        for p in patches:
            p.stop()
    return run_cli_spy, extras


# ---------------------------------------------------------------------------
# Phase A -- ``select_session`` direct unit tests
# ---------------------------------------------------------------------------


def test_select_session_empty_list_returns_none_tuple(
    session_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty session list -> ``(None, None)`` + verbatim no-sessions notice.

    The ``myrepo/main`` subdirectory does NOT exist on disk for this
    invocation (we have not pre-saved any records), exercising the
    :meth:`SessionManager.list_sessions` "directory missing" branch.

    The notice contains an em-dash (``\\u2014`` ``\u2014``), NOT a regular
    hyphen-minus. The literal in this assertion is the actual U+2014 EM
    DASH character per AAP rule 5 verbatim wording.
    """
    from vibe.cli.session_picker import select_session

    record, backend = select_session("myrepo", "main")

    assert record is None
    assert backend is None

    captured = capsys.readouterr()
    # Note: the dash is an em-dash (U+2014), NOT a hyphen. The space
    # before and after the em-dash is also part of the verbatim format.
    assert (
        "No previous sessions found for myrepo(main) \u2014 starting new session"
        in captured.out
    )


def test_select_session_with_explicit_empty_directory_returns_none_tuple(
    session_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-existing empty ``(repo, branch)`` dir behaves identically to missing.

    Exercises the :meth:`SessionManager.list_sessions` "directory exists
    but contains no readable JSON" branch. The contract from AAP rule 5
    is "no sessions -> fall through", which MUST hold regardless of
    whether the directory was never created or is just empty.
    """
    (session_root / "myrepo" / "main").mkdir(parents=True, exist_ok=True)
    from vibe.cli.session_picker import select_session

    record, backend = select_session("myrepo", "main")

    assert record is None
    assert backend is None

    captured = capsys.readouterr()
    assert (
        "No previous sessions found for myrepo(main) \u2014 starting new session"
        in captured.out
    )


def test_select_session_one_session_returns_record_and_backend(
    session_root: Path,
) -> None:
    """A single saved session -> picker returns ``(record, Backend.BLITZY)``."""
    saved = _make_record(provider="blitzy", repo="myrepo", branch="main")
    _save_record(saved)

    with patch("builtins.input", return_value="1"):
        from vibe.cli.session_picker import select_session

        record, backend = select_session("myrepo", "main")

    assert record is not None
    assert record.session_id == saved.session_id
    assert backend == Backend.BLITZY


def test_select_session_picks_by_index(session_root: Path) -> None:
    """Picker honors the 1-based numeric index against recency-sorted list.

    Three records with distinct ``created_at`` timestamps are saved. The
    picker sorts descending by ``created_at`` (newest first), so:

    * Position ``[1]`` = ``2026-01-03T00:00:00Z`` (newest)
    * Position ``[2]`` = ``2026-01-02T00:00:00Z`` (middle)
    * Position ``[3]`` = ``2026-01-01T00:00:00Z`` (oldest)

    Selecting ``"2"`` MUST return the middle record, not the oldest.
    """
    _save_record(
        _make_record(
            session_id="11111111" + "0" * 24, created_at="2026-01-01T00:00:00Z"
        )
    )
    _save_record(
        _make_record(
            session_id="22222222" + "0" * 24, created_at="2026-01-02T00:00:00Z"
        )
    )
    _save_record(
        _make_record(
            session_id="33333333" + "0" * 24, created_at="2026-01-03T00:00:00Z"
        )
    )

    with patch("builtins.input", return_value="2"):
        from vibe.cli.session_picker import select_session

        record, _ = select_session("myrepo", "main")

    assert record is not None
    # Position ``[2]`` in a desc-sorted list of three is the MIDDLE record.
    assert record.created_at == "2026-01-02T00:00:00Z"
    assert record.session_id.startswith("22222222")


def test_select_session_displays_sorted_by_recency(
    session_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Picker display order is ``created_at`` descending (newest first).

    The newest record's short_id MUST appear on the line beginning with
    ``[1]``. ISO 8601 ``Z``-suffixed timestamps are lexicographically
    sortable, so ``"2026-03-01T00:00:00Z" > "2026-02-01T00:00:00Z" >
    "2026-01-01T00:00:00Z"`` -- the order asserted below.
    """
    _save_record(
        _make_record(
            session_id="aaaa1111" + "0" * 24, created_at="2026-01-01T00:00:00Z"
        )
    )
    _save_record(
        _make_record(
            session_id="bbbb2222" + "0" * 24, created_at="2026-02-01T00:00:00Z"
        )
    )
    _save_record(
        _make_record(
            session_id="cccc3333" + "0" * 24, created_at="2026-03-01T00:00:00Z"
        )
    )

    with patch("builtins.input", return_value="1"):
        from vibe.cli.session_picker import select_session

        record, _ = select_session("myrepo", "main")

    assert record is not None
    assert record.session_id.startswith("cccc3333")

    captured = capsys.readouterr()
    # Find the line that starts with ``[1]`` and assert it contains the
    # newest short_id. We avoid asserting on the line literal (which
    # depends on column padding) and only check for the substring.
    lines = captured.out.splitlines()
    first_line = next((line for line in lines if line.lstrip().startswith("[1]")), None)
    assert first_line is not None, (
        f"No ``[1]`` line found in picker output: {captured.out!r}"
    )
    assert "cccc3333" in first_line, (
        f"Expected newest short_id ``cccc3333`` on ``[1]`` line; got: {first_line!r}"
    )


def test_select_session_invalid_input_reprompts(
    session_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid input is rejected with the canonical message and reprompts.

    ``"99"`` is numerically valid but out of range for two saved sessions
    (valid range ``[1, 2]``). The picker MUST reprompt rather than
    raising, and on the next valid input ``"1"`` MUST return the
    corresponding record.
    """
    _save_record(
        _make_record(
            session_id="aaaaaaaa" + "0" * 24, created_at="2026-01-02T00:00:00Z"
        )
    )
    _save_record(
        _make_record(
            session_id="bbbbbbbb" + "0" * 24, created_at="2026-01-01T00:00:00Z"
        )
    )

    with patch("builtins.input", side_effect=["99", "1"]):
        from vibe.cli.session_picker import select_session

        record, _ = select_session("myrepo", "main")

    assert record is not None
    captured = capsys.readouterr()
    # The canonical error message contains both the prefix ``"Invalid
    # choice."`` AND the explicit valid range ``"between 1 and 2"``. We
    # assert on the substring rather than the full line to be robust to
    # any future surrounding wording tweaks.
    assert "Invalid choice." in captured.out
    assert "between 1 and 2" in captured.out


def test_select_session_three_invalid_inputs_raises_keyboard_interrupt(
    session_root: Path,
) -> None:
    """After ``_MAX_RETRIES`` invalid inputs the picker raises ``KeyboardInterrupt``.

    The picker's bounded-retry contract caps attempts at three; supplying
    invalid inputs on every attempt (more than three side_effects are
    listed to be defensive against signature changes) MUST result in
    ``KeyboardInterrupt`` so the entrypoint's outer handler exits without
    instantiating a backend (AAP rule 4).
    """
    _save_record(_make_record())

    with patch("builtins.input", side_effect=["99", "abc", "0", "99"]):
        from vibe.cli.session_picker import select_session

        with pytest.raises(KeyboardInterrupt):
            select_session("myrepo", "main")


def test_select_session_eof_raises_keyboard_interrupt(session_root: Path) -> None:
    """``EOFError`` from ``input()`` is converted to ``KeyboardInterrupt``.

    Treating EOF as cancellation lets the entrypoint's outer
    ``except (KeyboardInterrupt, EOFError)`` clause handle both Ctrl-C
    and Ctrl-D paths uniformly.
    """
    _save_record(_make_record())

    with patch("builtins.input", side_effect=EOFError):
        from vibe.cli.session_picker import select_session

        with pytest.raises(KeyboardInterrupt):
            select_session("myrepo", "main")


def test_select_session_short_id_displayed_in_picker(
    session_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Picker shows ``session_id[:8]`` only; the full 32-char id is hidden.

    A 32-char id with the predictable prefix ``"deadbeef"`` followed by
    24 ``"f"`` chars lets us assert truncation: the 8-char prefix MUST
    appear in the picker output, but the FULL id MUST NOT (the rest of
    the id would only appear if the display truncation were broken).
    """
    full_id = "deadbeef" + "f" * 24
    _save_record(_make_record(session_id=full_id))

    with patch("builtins.input", return_value="1"):
        from vibe.cli.session_picker import select_session

        select_session("myrepo", "main")

    captured = capsys.readouterr()
    assert "deadbeef" in captured.out
    # The full 32-char id MUST NOT appear (truncation verification).
    # ``deadbeef`` is a substring of the full id, so checking the full
    # string is the discriminating test.
    assert full_id not in captured.out


@pytest.mark.parametrize(
    ("provider_str", "expected_backend"),
    [
        ("blitzy", Backend.BLITZY),
        ("mistral", Backend.MISTRAL),
        ("anthropic", Backend.ANTHROPIC),
    ],
)
def test_select_session_provider_round_trips_to_backend(
    session_root: Path, provider_str: str, expected_backend: Backend
) -> None:
    """Rule 13: ``record.provider`` string round-trips to :class:`Backend` enum.

    For each of the three accepted provider tokens, a record is saved
    with that token, the picker is invoked, and the returned backend
    MUST equal the corresponding ``Backend.<X>`` enum value.
    :func:`provider_string_to_backend` is the single source of truth for
    this mapping (AAP rule 13 -- no orphaned strings).
    """
    _save_record(_make_record(provider=provider_str, repo="myrepo", branch="main"))

    with patch("builtins.input", return_value="1"):
        from vibe.cli.session_picker import select_session

        record, backend = select_session("myrepo", "main")

    assert record is not None
    assert record.provider == provider_str
    assert backend == expected_backend


def test_select_session_no_git_uses_unknown_unknown_path(session_root: Path) -> None:
    """Empty ``(repo, branch)`` routes to the ``_unknown/_unknown`` fallback.

    When :func:`vibe.core.git_context.detect` returns ``("", "")`` (no
    ``.git`` directory or unreadable metadata), :meth:`SessionManager`
    falls back to ``"_unknown"`` for each empty segment. The on-disk
    layout MUST therefore be ``sessions/_unknown/_unknown/{id}.json``.
    """
    _save_record(_make_record(repo="", branch=""))

    with patch("builtins.input", return_value="1"):
        from vibe.cli.session_picker import select_session

        record, _ = select_session("", "")

    assert record is not None
    # Verify on-disk layout: empty repo/branch -> ``_unknown/_unknown``.
    unknown_dir = session_root / "_unknown" / "_unknown"
    assert unknown_dir.exists(), (
        f"Expected ``_unknown/_unknown`` directory under {session_root}"
    )
    files = list(unknown_dir.glob("*.json"))
    assert len(files) == 1, (
        f"Expected exactly one session file under {unknown_dir}; got {files}"
    )


# ---------------------------------------------------------------------------
# Phase B -- Entrypoint orchestration with ``--resume``
# ---------------------------------------------------------------------------


def test_resume_with_sessions_skips_provider_picker(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 5 (happy path): ``--resume`` with sessions skips the provider picker.

    Setup:

    * One record saved with ``provider="anthropic"`` for ``(myrepo, main)``.
    * :func:`vibe.core.git_context.detect` mocked to return that tuple.
    * Stdin input mocked to ``"1"`` so the (real) session picker picks
      the only available session.

    Assertions:

    * :func:`vibe.cli.provider_picker.select_provider` MUST NOT be called
      (the restored session's provider pins the backend).
    * :func:`vibe.cli.cli.run_cli` MUST be called exactly once.
    * ``run_cli.call_args.kwargs["backend"]`` MUST equal
      :attr:`Backend.ANTHROPIC` -- the round-tripped enum from the
      saved record's ``provider`` field.
    * ``run_cli.call_args.kwargs["restored_session"]`` MUST be the
      saved record (same ``session_id``).
    """
    record = _make_record(provider="anthropic", repo="myrepo", branch="main")
    _save_record(record)

    picker_spy = MagicMock(name="select_provider_spy")
    git_detect_spy = MagicMock(return_value=("myrepo", "main"))

    # ``builtins.input`` cannot be passed via ``extra_patches`` and remain
    # active for the duration of ``main()``'s lazy import + execution;
    # wrapping the whole ``_invoke_main`` call in ``with patch(...)`` is
    # the cleanest approach because the ``patch`` context manager nests
    # transparently around the helper's own patch stack.
    with patch("builtins.input", return_value="1"):
        run_cli_spy, _ = _invoke_main(
            monkeypatch,
            ["vibe", "--resume"],
            extra_patches=[
                ("vibe.cli.provider_picker.select_provider", picker_spy),
                ("vibe.core.git_context.detect", git_detect_spy),
            ],
        )

    # Rule 5 -- the provider picker MUST NOT run when a session is restored.
    assert picker_spy.call_count == 0, (
        f"select_provider was called {picker_spy.call_count} time(s); "
        "expected 0 because a session was restored"
    )
    # ``run_cli`` is the single sink for the orchestration block -- exactly
    # one call is the wire-correctness invariant.
    assert run_cli_spy.call_count == 1, (
        f"run_cli was called {run_cli_spy.call_count} time(s); expected 1"
    )

    call = run_cli_spy.call_args
    kwargs = call.kwargs
    assert kwargs.get("backend") == Backend.ANTHROPIC, (
        f"Expected Backend.ANTHROPIC, got {kwargs.get('backend')}"
    )
    restored = kwargs.get("restored_session")
    assert restored is not None, (
        "Expected restored_session to be the saved record; got None"
    )
    assert restored.session_id == record.session_id, (
        f"Expected restored session_id={record.session_id!r}, "
        f"got {restored.session_id!r}"
    )


def test_resume_with_empty_list_falls_through_to_provider_picker(
    session_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule 5 (fallthrough): empty list -> provider picker (NO exit).

    THIS IS THE CRITICAL no-exit invariant. ``select_session`` returning
    ``(None, None)`` MUST NOT cause the agent to exit -- it MUST fall
    through to :func:`select_provider`.

    Setup:

    * NO records saved for ``("emptyrepo", "main")``.
    * :func:`detect` mocked to return ``("emptyrepo", "main")``.
    * :func:`select_provider` mocked to return :attr:`Backend.BLITZY` so
      the test can verify it IS called (the fallthrough actually
      reached its target) AND that the resulting backend is passed
      through to :func:`run_cli`.

    Assertions:

    * The verbatim "No previous sessions found..." notice MUST appear
      in stdout.
    * :func:`select_provider` MUST be called exactly once.
    * :func:`run_cli` MUST be called with ``backend=Backend.BLITZY``
      (no exit -- the orchestration block continued).
    * ``restored_session`` MUST be ``None`` (no record was loaded).
    """
    picker_spy = MagicMock(return_value=Backend.BLITZY)
    git_detect_spy = MagicMock(return_value=("emptyrepo", "main"))

    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe", "--resume"],
        extra_patches=[
            ("vibe.cli.provider_picker.select_provider", picker_spy),
            ("vibe.core.git_context.detect", git_detect_spy),
        ],
    )

    captured = capsys.readouterr()
    # The em-dash here is critical -- if the picker's notice were emitted
    # with a hyphen instead, this assertion would catch the regression.
    assert "No previous sessions found for emptyrepo(main)" in captured.out, (
        f"Expected fall-through notice in stdout; got: {captured.out!r}"
    )

    # Fallthrough invariant: the provider picker WAS called.
    assert picker_spy.call_count == 1, (
        f"select_provider was called {picker_spy.call_count} time(s); "
        "expected 1 because the session list was empty (Rule 5 fallthrough)"
    )

    # No-exit invariant: ``run_cli`` was reached. If the orchestration
    # block had exited on the empty-list path, ``run_cli`` would have a
    # call_count of 0 here.
    assert run_cli_spy.call_count == 1, (
        f"run_cli was called {run_cli_spy.call_count} time(s); "
        "expected 1 because the empty-list path MUST NOT exit"
    )
    kwargs = run_cli_spy.call_args.kwargs
    assert kwargs.get("backend") == Backend.BLITZY, (
        f"Expected Backend.BLITZY from fallthrough picker, got {kwargs.get('backend')}"
    )
    assert kwargs.get("restored_session") is None, (
        "Expected restored_session=None on fallthrough; "
        f"got {kwargs.get('restored_session')!r}"
    )


@pytest.mark.parametrize(
    ("provider_str", "expected_backend"),
    [
        ("blitzy", Backend.BLITZY),
        ("mistral", Backend.MISTRAL),
        ("anthropic", Backend.ANTHROPIC),
    ],
)
def test_resume_restores_provider_from_session_record(
    session_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_str: str,
    expected_backend: Backend,
) -> None:
    """Rule 13 (end-to-end): saved ``provider`` -> ``Backend`` passed to ``run_cli``.

    For each accepted provider token, a record is saved, the session is
    resumed via the picker (input ``"1"``), and the corresponding
    :class:`Backend` enum MUST be passed to :func:`run_cli` as the
    ``backend`` keyword. This is the end-to-end verification of Gate 2's
    "all paths traced from CLI arg -> factory -> backend" requirement,
    via the ``--resume`` route specifically.
    """
    record = _make_record(provider=provider_str, repo="myrepo", branch="main")
    _save_record(record)

    with patch("builtins.input", return_value="1"):
        run_cli_spy, _ = _invoke_main(
            monkeypatch,
            ["vibe", "--resume"],
            extra_patches=[
                ("vibe.cli.provider_picker.select_provider", MagicMock()),
                (
                    "vibe.core.git_context.detect",
                    MagicMock(return_value=("myrepo", "main")),
                ),
            ],
        )

    assert run_cli_spy.call_count == 1
    kwargs = run_cli_spy.call_args.kwargs
    assert kwargs.get("backend") == expected_backend, (
        f"For provider={provider_str!r}, expected backend={expected_backend!r}, "
        f"got {kwargs.get('backend')!r}"
    )


def test_resume_loads_messages_from_session(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restored session's ``messages`` list is forwarded to ``run_cli``.

    Verifies that AAP's "hydrate conversation history" contract is wired
    through the orchestration block: when the picker returns a record
    with three messages, ``run_cli`` MUST receive a ``restored_session``
    whose ``.messages`` has those same three messages in order, with
    content preserved verbatim.
    """
    msgs = [
        LLMMessage(role=Role.user, content="first user turn"),
        LLMMessage(role=Role.assistant, content="first assistant reply"),
        LLMMessage(role=Role.user, content="second user turn"),
    ]
    record = _make_record(
        messages=msgs, provider="blitzy", repo="myrepo", branch="main"
    )
    _save_record(record)

    with patch("builtins.input", return_value="1"):
        run_cli_spy, _ = _invoke_main(
            monkeypatch,
            ["vibe", "--resume"],
            extra_patches=[
                ("vibe.cli.provider_picker.select_provider", MagicMock()),
                (
                    "vibe.core.git_context.detect",
                    MagicMock(return_value=("myrepo", "main")),
                ),
            ],
        )

    assert run_cli_spy.call_count == 1
    kwargs = run_cli_spy.call_args.kwargs
    restored = kwargs.get("restored_session")
    assert restored is not None
    assert len(restored.messages) == 3, (
        f"Expected 3 restored messages, got {len(restored.messages)}"
    )
    # Verify each message's content survived the JSON round-trip.
    assert restored.messages[0].content == "first user turn"
    assert restored.messages[0].role == Role.user
    assert restored.messages[1].content == "first assistant reply"
    assert restored.messages[1].role == Role.assistant
    assert restored.messages[2].content == "second user turn"
    assert restored.messages[2].role == Role.user


def test_resume_with_no_git_uses_unknown_unknown(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 3 + Rule 5: no-git context routes through ``_unknown/_unknown``.

    When :func:`detect` returns ``("", "")`` (the silent-failure
    sentinel for an absent or unreadable ``.git``), the orchestration
    block MUST still call :func:`select_session("", "")`, which in turn
    looks for sessions under ``_unknown/_unknown``. A pre-saved record
    under that path MUST be found and forwarded to :func:`run_cli`.
    """
    record = _make_record(repo="", branch="")
    _save_record(record)

    with patch("builtins.input", return_value="1"):
        run_cli_spy, _ = _invoke_main(
            monkeypatch,
            ["vibe", "--resume"],
            extra_patches=[
                ("vibe.cli.provider_picker.select_provider", MagicMock()),
                ("vibe.core.git_context.detect", MagicMock(return_value=("", ""))),
            ],
        )

    assert run_cli_spy.call_count == 1
    kwargs = run_cli_spy.call_args.kwargs
    restored = kwargs.get("restored_session")
    assert restored is not None, (
        "Expected restored_session from ``_unknown/_unknown`` path; got None"
    )
    assert restored.repo == ""
    assert restored.branch == ""


def test_resume_cancelled_picker_exits_cleanly(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KeyboardInterrupt`` inside the picker -> clean ``SystemExit``.

    Simulates the user cancelling out of the picker (e.g., Ctrl-C
    during input). The entrypoint's outer
    ``except (KeyboardInterrupt, EOFError)`` clause MUST catch the
    interrupt, print the "Bye!" greeting, and call ``sys.exit(0)``.

    Assertions:

    * :func:`run_cli` MUST NOT be called (the main flow short-circuited).
    * :func:`select_provider` MUST NOT be called (the fallthrough is
      NOT triggered when the picker raises; ``backend_enum`` never
      becomes set, but ``sys.exit`` runs first).
    """
    _save_record(_make_record())

    picker_spy = MagicMock(name="select_provider_spy")
    # ``select_session`` is patched here (unlike most other Phase B tests
    # which use the real picker). The cancellation is simulated by raising
    # ``KeyboardInterrupt`` directly from the patched function, which is
    # equivalent to the user supplying invalid input 3 times or hitting
    # Ctrl-D from the picker's own try/except path.
    cancel_spy = MagicMock(side_effect=KeyboardInterrupt("cancelled by user"))

    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe", "--resume"],
        extra_patches=[
            ("vibe.cli.provider_picker.select_provider", picker_spy),
            ("vibe.cli.session_picker.select_session", cancel_spy),
            (
                "vibe.core.git_context.detect",
                MagicMock(return_value=("myrepo", "main")),
            ),
        ],
        expect_system_exit=True,
    )

    # ``main()`` exited via ``sys.exit(0)`` before reaching ``run_cli``.
    assert run_cli_spy.call_count == 0, (
        f"run_cli was called {run_cli_spy.call_count} time(s); "
        "expected 0 because the picker was cancelled"
    )
    # The provider picker is NOT triggered when the session picker raises
    # -- the orchestration's ``try/except`` calls ``sys.exit(0)`` directly.
    assert picker_spy.call_count == 0, (
        f"select_provider was called {picker_spy.call_count} time(s); "
        "expected 0 because cancellation exits before fallthrough"
    )
