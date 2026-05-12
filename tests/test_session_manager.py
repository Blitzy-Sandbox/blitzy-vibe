"""Unit tests for :class:`vibe.core.session.SessionManager`.

This file is the regression suite for the new session-persistence layer at
``vibe/core/session.py``. It exercises every behavioural branch of:

- :class:`SessionRecord` -- JSON shape, defaults, all six schema keys.
- :func:`new_session` -- UUID4 + ISO 8601 factory.
- :meth:`SessionManager.save` -- AAP Behavioural Rule 6
  (full-overwrite, ``~/.blitzy/sessions/{repo}/{branch}/`` path,
  ``_unknown`` fallback for empty git context).
- :meth:`SessionManager.load` -- session-id rglob, ``SessionNotFoundError``.
- :meth:`SessionManager.list_sessions` -- created_at-descending sort,
  malformed-file tolerance, empty-list-for-unknown-repo path (rule 5).
- :meth:`SessionManager.compact` -- AAP Behavioural Rule 7
  (``len(json.dumps(messages)) // 4`` threshold at 80% of token_limit,
  oldest-half summarisation, newest-half preserved verbatim).
- :func:`estimate_tokens` -- the canonical Rule 7 estimator formula.

Behavioural rules verified by this suite (from AAP section 0.8.1):

- **Rule 5**: ``--resume`` with no sessions falls through to provider
  selection (this file verifies the underlying empty-list contract via
  ``list_sessions``; the CLI integration is in
  ``tests/cli/test_resume_flow.py``).
- **Rule 6**: Session files MUST be written (full overwrite) after every
  turn and MUST be stored at ``~/.blitzy/sessions/{repo}/{branch}/``.
- **Rule 7**: Auto-compaction MUST trigger when
  ``len(json.dumps(messages)) // 4`` exceeds 80% of the active provider's
  configured context limit; MUST replace compacted messages with a single
  system summary; MUST preserve the most recent messages verbatim.
- **Rule 13**: ``SessionRecord.provider`` accepts exactly the three
  string tokens ``"blitzy"``, ``"mistral"``, ``"anthropic"`` (no
  orphaned strings).

All ``conftest.py`` autouse fixtures (``tmp_working_directory``,
``config_dir``, ``_unlock_config_paths``, ``_mock_api_key``,
``_mock_platform``, ``_mock_update_commands``) are inherited automatically
and require no explicit declaration here. The ``config_dir`` fixture is
the natural ``vibe_home`` for these tests: it is already a fresh
``.vibe``-shaped tmp directory per-test, isolated from the host
filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import uuid

import pytest

from vibe.core.llm.exceptions import SessionNotFoundError
from vibe.core.session import (
    SessionManager,
    SessionRecord,
    estimate_tokens,
    new_session,
)
from vibe.core.types import LLMMessage, Role

# ---------------------------------------------------------------------------
# Phase A -- Fixtures and Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_root(config_dir: Path) -> Path:
    """Return a tmp ``.vibe``-shaped root suitable as ``SessionManager``'s
    ``vibe_home`` argument.

    The conftest ``config_dir`` fixture is already a fresh ``.vibe`` tmp
    directory per-test (it also monkeypatches
    ``global_paths._DEFAULT_VIBE_HOME``). Pre-creating the ``sessions/``
    subdir mirrors the production layout so assertions of the form
    ``session_root / "sessions" / ...`` are unambiguous.
    """
    (config_dir / "sessions").mkdir(parents=True, exist_ok=True)
    return config_dir


def _make_message(role: Role, content: str) -> LLMMessage:
    """Build a single :class:`LLMMessage` with the requested role/content.

    Convenience helper that keeps the body of each test focused on the
    invariant under verification rather than on Pydantic constructor noise.
    """
    return LLMMessage(role=role, content=content)


def _make_record(
    provider: str = "blitzy",
    repo: str = "myrepo",
    branch: str = "main",
    messages: list[LLMMessage] | None = None,
    compacted_summary: str | None = None,
    created_at: str | None = None,
    session_id: str | None = None,
) -> SessionRecord:
    """Build a :class:`SessionRecord` with sensible defaults.

    Each parameter has a defaulted value matching the user-attached
    JSON example in AAP section 0.2.2, so callers need only specify the
    fields directly under test. Unspecified fields produce a fresh UUID4
    hex ``session_id`` and a current-UTC ``created_at`` so multiple
    records constructed in the same test do not collide.
    """
    return SessionRecord(
        session_id=session_id or uuid.uuid4().hex,
        created_at=created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        provider=provider,
        repo=repo,
        branch=branch,
        messages=messages or [],
        compacted_summary=compacted_summary,
    )


def _iso_z(dt: datetime) -> str:
    """Format ``dt`` as an ISO 8601 ``Z``-suffixed UTC string.

    Matches the format produced by :func:`new_session`
    (``%Y-%m-%dT%H:%M:%SZ``), which is the exact lexicographic-sort key
    :meth:`SessionManager.list_sessions` relies on. Tests that exercise
    ordering MUST use this helper to keep the test-side timestamps
    consistent with the production format.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# Phase B -- SessionRecord Construction
# ===========================================================================


def test_session_record_round_trip_via_json() -> None:
    """SessionRecord -> JSON -> SessionRecord round trip preserves all fields.

    Verifies the AAP section 0.2.2 JSON shape is bidirectionally lossless.
    """
    msgs = [_make_message(Role.user, "hi"), _make_message(Role.assistant, "hello")]
    record = _make_record(messages=msgs, compacted_summary="prior summary")

    serialized = record.model_dump_json()
    revived = SessionRecord.model_validate_json(serialized)

    assert revived.session_id == record.session_id
    assert revived.created_at == record.created_at
    assert revived.provider == record.provider
    assert revived.repo == record.repo
    assert revived.branch == record.branch
    assert revived.compacted_summary == record.compacted_summary
    # Messages compared via model_dump(mode="json") to handle auto-generated
    # message_id parity without depending on Pydantic ``__eq__`` semantics.
    assert [m.model_dump(mode="json") for m in revived.messages] == [
        m.model_dump(mode="json") for m in record.messages
    ]


def test_session_record_compacted_summary_defaults_to_none() -> None:
    """``compacted_summary`` defaults to ``None`` when not supplied.

    Verifies the AAP section 0.2.2 schema literal ``"<str|null>"`` default.
    """
    record = SessionRecord(
        session_id=uuid.uuid4().hex,
        created_at=_iso_z(datetime.now(UTC)),
        provider="blitzy",
        repo="myrepo",
        branch="main",
        messages=[],
    )

    assert record.compacted_summary is None


def test_session_record_has_all_six_schema_keys() -> None:
    """SessionRecord.model_dump() exposes exactly the AAP-mandated keys.

    The AAP section 0.2.2 User Example lists seven top-level keys
    (``session_id``, ``created_at``, ``provider``, ``repo``, ``branch``,
    ``messages``, ``compacted_summary``). The test name retains the
    historical "six" wording for traceability, but asserts on all seven
    keys present in the schema as currently implemented.
    """
    record = _make_record()
    dumped = record.model_dump()

    expected_keys = {
        "session_id",
        "created_at",
        "provider",
        "repo",
        "branch",
        "messages",
        "compacted_summary",
    }
    assert set(dumped.keys()) == expected_keys


@pytest.mark.parametrize("provider", ["blitzy", "mistral", "anthropic"])
def test_session_record_provider_accepts_three_strings(provider: str) -> None:
    """Provider field accepts each of the AAP-canonical lowercase tokens.

    Rule 13: ``"blitzy" | "mistral" | "anthropic"`` are the exact strings
    used across ``--provider`` choices, ``BACKEND_FACTORY`` keys, and the
    ``SessionRecord.provider`` field. This test pins the
    ``SessionRecord.provider`` half of that invariant.
    """
    record = _make_record(provider=provider)

    assert record.provider == provider


# ===========================================================================
# Phase C -- new_session Factory
# ===========================================================================


def test_new_session_generates_uuid_session_id() -> None:
    """``new_session`` produces a valid UUID4 hex ``session_id``."""
    record = new_session("blitzy", "myrepo", "main")

    # ``uuid.UUID(<32-char hex>)`` MUST NOT raise for a well-formed hex.
    parsed = uuid.UUID(record.session_id)
    # And the value MUST round-trip through ``.hex`` -- this confirms the
    # session_id is hex (no hyphens), matching the AAP section 0.2.2 example.
    assert parsed.hex == record.session_id


def test_new_session_uses_iso_8601_timestamp() -> None:
    """``created_at`` is parseable as an ISO 8601 timestamp.

    ``datetime.fromisoformat`` accepts the ``Z`` suffix natively on
    Python 3.11+, so the assertion is both a parse-success check and an
    implicit format check.
    """
    record = new_session("blitzy", "myrepo", "main")

    # MUST parse without raising.
    parsed = datetime.fromisoformat(record.created_at)
    # And the parsed value must be timezone-aware (Z suffix preserved).
    assert parsed.tzinfo is not None


def test_new_session_starts_with_empty_messages() -> None:
    """A fresh session has an empty ``messages`` list and no summary."""
    record = new_session("blitzy", "myrepo", "main")

    assert record.messages == []
    assert record.compacted_summary is None


def test_new_session_returns_two_distinct_uuids_on_consecutive_calls() -> None:
    """Two consecutive ``new_session`` calls yield different ``session_id`` values.

    UUID4 collisions are statistically negligible; this test guards against
    a regression where the factory is reduced to a constant or memoised.
    """
    record_a = new_session("blitzy", "myrepo", "main")
    record_b = new_session("blitzy", "myrepo", "main")

    assert record_a.session_id != record_b.session_id


# ===========================================================================
# Phase D -- SessionManager.save -- AAP Rule 6
# ===========================================================================


def test_save_writes_full_record_to_repo_branch_path(session_root: Path) -> None:
    """Rule 6: ``save`` writes the JSON file at the documented path.

    Path layout: ``{vibe_home}/sessions/{repo}/{branch}/{session_id}.json``
    (AAP section 0.5.3). All six (now seven) JSON keys must be present
    after reading the file back.
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(repo="myrepo", branch="main", provider="blitzy")

    written = mgr.save(record)

    expected = (
        session_root / "sessions" / "myrepo" / "main" / f"{record.session_id}.json"
    )
    assert written == expected
    assert expected.exists()

    data = json.loads(expected.read_text(encoding="utf-8"))
    assert set(data.keys()) == {
        "session_id",
        "created_at",
        "provider",
        "repo",
        "branch",
        "messages",
        "compacted_summary",
    }


def test_save_creates_parent_directories(session_root: Path) -> None:
    """``save`` creates intermediate directories with ``mkdir(parents=True)``.

    The first save for a previously unseen ``(repo, branch)`` pair MUST
    succeed even when the ``repo/`` and ``branch/`` directories do not
    already exist.
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(repo="brand_new_repo", branch="feature/x")

    # Sanity precondition: the target subdirs must not exist yet.
    assert not (session_root / "sessions" / "brand_new_repo").exists()

    mgr.save(record)

    assert (session_root / "sessions" / "brand_new_repo").is_dir()
    assert (session_root / "sessions" / "brand_new_repo" / "feature").is_dir()
    assert (session_root / "sessions" / "brand_new_repo" / "feature" / "x").is_dir()


def test_save_full_overwrite_after_every_turn(session_root: Path) -> None:
    """Rule 6: ``save`` is a FULL OVERWRITE, not an append.

    Save once with one message; save again (same session_id) with five
    messages; assert the on-disk file has five messages, not six.
    """
    mgr = SessionManager(vibe_home=session_root)
    session_id = uuid.uuid4().hex

    record_one = _make_record(
        session_id=session_id, messages=[_make_message(Role.user, "first")]
    )
    mgr.save(record_one)

    record_five = _make_record(
        session_id=session_id,
        messages=[_make_message(Role.user, f"msg-{i}") for i in range(5)],
    )
    written = mgr.save(record_five)

    data = json.loads(written.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 5


def test_save_returns_path(session_root: Path) -> None:
    """``save`` returns the ``Path`` to the written file for caller assertions."""
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record()

    written = mgr.save(record)

    assert isinstance(written, Path)
    assert written.exists()
    assert written.name == f"{record.session_id}.json"


def test_save_uses_unknown_for_empty_repo_branch(session_root: Path) -> None:
    """Empty git-context ``("", "")`` falls back to ``_unknown/_unknown/``.

    Covers the AAP section 0.5.3 fallback: when
    :func:`vibe.core.git_context.detect` returns ``("", "")`` (rule 3
    silent-failure contract), the persistence layer must substitute
    ``"_unknown"`` for each empty segment so the file lands at a stable,
    well-known path.
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(repo="", branch="")

    written = mgr.save(record)

    expected = (
        session_root
        / "sessions"
        / "_unknown"
        / "_unknown"
        / f"{record.session_id}.json"
    )
    assert written == expected
    assert expected.exists()


def test_save_uses_unknown_for_one_empty_segment(session_root: Path) -> None:
    """``"_unknown"`` substitution is applied independently per-segment.

    When only the branch is empty (e.g., detached HEAD with a parseable
    origin remote), the repo segment must retain its value and only the
    branch segment falls back to ``"_unknown"``. This pinpoints the
    per-segment behaviour documented in
    ``SessionManager._path_for``.
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(repo="real_repo", branch="")

    written = mgr.save(record)

    expected = (
        session_root
        / "sessions"
        / "real_repo"
        / "_unknown"
        / f"{record.session_id}.json"
    )
    assert written == expected


def test_save_serializes_messages_correctly(session_root: Path) -> None:
    """Saved messages retain ``role`` and ``content`` after disk round trip.

    The test is tolerant of optional fields (``tool_calls``,
    ``reasoning_content``, etc.) being either omitted by Pydantic or
    present as ``null``; only ``role`` and ``content`` are asserted on.
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(
        messages=[
            _make_message(Role.system, "you are helpful"),
            _make_message(Role.user, "ping"),
            _make_message(Role.assistant, "pong"),
        ]
    )

    written = mgr.save(record)
    data = json.loads(written.read_text(encoding="utf-8"))

    assert len(data["messages"]) == 3
    assert data["messages"][0]["role"] == "system"
    assert data["messages"][0]["content"] == "you are helpful"
    assert data["messages"][1]["role"] == "user"
    assert data["messages"][1]["content"] == "ping"
    assert data["messages"][2]["role"] == "assistant"
    assert data["messages"][2]["content"] == "pong"


# ===========================================================================
# Phase E -- SessionManager.load
# ===========================================================================


def test_load_round_trips_a_session(session_root: Path) -> None:
    """Save followed by load returns a record equal to the original.

    Comparison uses ``model_dump(mode="json")`` to avoid depending on
    Pydantic ``__eq__`` semantics for nested ``LLMMessage`` instances
    (which carry auto-generated ``message_id`` UUIDs).
    """
    mgr = SessionManager(vibe_home=session_root)
    original = _make_record(
        messages=[
            _make_message(Role.user, "hello"),
            _make_message(Role.assistant, "hi"),
        ],
        compacted_summary="earlier summary",
    )
    mgr.save(original)

    loaded = mgr.load(original.session_id)

    assert loaded.model_dump(mode="json") == original.model_dump(mode="json")


def test_load_raises_session_not_found_for_unknown_id(session_root: Path) -> None:
    """``load`` raises ``SessionNotFoundError`` for an unknown session_id.

    The exception MUST carry the requested ``session_id`` so the CLI
    layer (and human operators reading stack traces) can identify which
    session failed to load.
    """
    mgr = SessionManager(vibe_home=session_root)

    with pytest.raises(SessionNotFoundError) as exc_info:
        mgr.load("nonexistent-session-id")

    # The exception MUST carry the requested session_id, per AAP
    # section 0.5.1: ``SessionNotFoundError(session_id: str)``.
    assert exc_info.value.session_id == "nonexistent-session-id"
    # And it must surface in the string representation for tracebacks.
    assert "nonexistent-session-id" in str(exc_info.value)


def test_load_raises_session_not_found_when_sessions_root_missing(
    config_dir: Path,
) -> None:
    """``load`` returns ``SessionNotFoundError`` when the sessions root is missing.

    A brand-new ``vibe_home`` without a ``sessions/`` subdirectory must
    NOT crash with ``FileNotFoundError``; the loader must convert the
    absence to the documented domain-specific exception.
    """
    # Use config_dir directly (NOT session_root which pre-creates sessions/).
    mgr = SessionManager(vibe_home=config_dir)

    with pytest.raises(SessionNotFoundError):
        mgr.load("any-id")


def test_load_can_find_session_across_repo_branches(session_root: Path) -> None:
    """``load`` finds a session_id regardless of which ``(repo, branch)`` directory it lives in.

    The CLI ``--resume <id>`` path (legacy SESSION_ID form) is allowed to
    look up by id alone without knowing the originating repo/branch
    context, because UUID4 hex strings are unique with overwhelming
    probability. Implementation uses ``rglob`` under the sessions root.
    """
    mgr = SessionManager(vibe_home=session_root)
    record_a = _make_record(repo="repo1", branch="main")
    record_b = _make_record(repo="repo2", branch="feature/x")

    mgr.save(record_a)
    mgr.save(record_b)

    loaded_a = mgr.load(record_a.session_id)
    loaded_b = mgr.load(record_b.session_id)

    assert loaded_a.session_id == record_a.session_id
    assert loaded_a.repo == "repo1"
    assert loaded_b.session_id == record_b.session_id
    assert loaded_b.repo == "repo2"
    assert loaded_b.branch == "feature/x"


# ===========================================================================
# Phase F -- SessionManager.list_sessions
# ===========================================================================


def test_list_sessions_returns_empty_for_unknown_repo_branch(
    session_root: Path,
) -> None:
    """Unknown ``(repo, branch)`` yields ``[]`` -- NEVER raises.

    Rule 5: the ``--resume`` picker MUST fall through to provider
    selection (not exit) when no sessions exist. The empty-list contract
    here is the unit-level precondition for that CLI-level behaviour.
    """
    mgr = SessionManager(vibe_home=session_root)

    result = mgr.list_sessions("unknown_repo", "unknown_branch")

    assert result == []


def test_list_sessions_returns_empty_for_unknown_root(config_dir: Path) -> None:
    """Even when the ``sessions/`` root itself doesn't exist, returns ``[]``.

    The ``config_dir`` fixture provides a fresh ``.vibe`` directory but
    does NOT pre-create ``sessions/`` (only the explicit ``session_root``
    fixture does). This test pins the most-pessimistic empty-state
    contract.
    """
    mgr = SessionManager(vibe_home=config_dir)

    assert mgr.list_sessions("any_repo", "any_branch") == []


def test_list_sessions_sorted_by_created_at_descending(session_root: Path) -> None:
    """Records returned by ``list_sessions`` are sorted newest-first.

    Three records are written with distinct timestamps ``now-2h``,
    ``now-1h``, ``now``; the returned order must be ``now``, ``now-1h``,
    ``now-2h``.
    """
    mgr = SessionManager(vibe_home=session_root)
    now = datetime.now(UTC)

    record_old = _make_record(
        repo="r", branch="b", created_at=_iso_z(now - timedelta(hours=2))
    )
    record_mid = _make_record(
        repo="r", branch="b", created_at=_iso_z(now - timedelta(hours=1))
    )
    record_new = _make_record(repo="r", branch="b", created_at=_iso_z(now))

    mgr.save(record_old)
    mgr.save(record_mid)
    mgr.save(record_new)

    result = mgr.list_sessions("r", "b")

    assert len(result) == 3
    assert result[0].session_id == record_new.session_id
    assert result[1].session_id == record_mid.session_id
    assert result[2].session_id == record_old.session_id


def test_list_sessions_filters_by_repo_branch(session_root: Path) -> None:
    """``list_sessions`` returns only records under the requested ``(repo, branch)``.

    Two records under ``(repo1, main)`` and one record under
    ``(repo2, main)`` must yield length-2 and length-1 results
    respectively -- the filter MUST be repo-aware.
    """
    mgr = SessionManager(vibe_home=session_root)

    repo1_a = _make_record(repo="repo1", branch="main")
    repo1_b = _make_record(repo="repo1", branch="main")
    repo2_a = _make_record(repo="repo2", branch="main")

    mgr.save(repo1_a)
    mgr.save(repo1_b)
    mgr.save(repo2_a)

    result_repo1 = mgr.list_sessions("repo1", "main")
    result_repo2 = mgr.list_sessions("repo2", "main")

    repo1_ids = {r.session_id for r in result_repo1}
    repo2_ids = {r.session_id for r in result_repo2}
    assert repo1_ids == {repo1_a.session_id, repo1_b.session_id}
    assert repo2_ids == {repo2_a.session_id}


def test_list_sessions_ignores_non_json_files(session_root: Path) -> None:
    """Non-``.json`` files and malformed JSON files are silently skipped.

    Partial writes from a crashed CLI process or operator-edited typos
    must NOT break the picker UI. The implementation logs at DEBUG and
    continues; this test verifies the user-visible result (the valid
    record still appears).
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(repo="r", branch="b")
    mgr.save(record)

    target_dir = session_root / "sessions" / "r" / "b"
    # Stray non-JSON file (e.g., an editor backup).
    (target_dir / "notes.txt").write_text("not a session", encoding="utf-8")
    # Malformed JSON masquerading as a session file.
    (target_dir / "garbage.json").write_text("{not valid json", encoding="utf-8")
    # Schema-invalid JSON (parses but fails Pydantic validation).
    (target_dir / "wrong_shape.json").write_text(
        json.dumps({"foo": "bar"}), encoding="utf-8"
    )

    result = mgr.list_sessions("r", "b")

    assert len(result) == 1
    assert result[0].session_id == record.session_id


def test_list_sessions_empty_directory_returns_empty(session_root: Path) -> None:
    """A pre-existing but empty ``(repo, branch)`` directory yields ``[]``.

    Directly creating the target directory exercises the "exists and is
    a directory, but no JSON files inside" branch of
    ``list_sessions`` -- distinct from the "directory missing" branch
    covered by ``test_list_sessions_returns_empty_for_unknown_repo_branch``.
    """
    target_dir = session_root / "sessions" / "empty_repo" / "empty_branch"
    target_dir.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(vibe_home=session_root)

    result = mgr.list_sessions("empty_repo", "empty_branch")

    assert result == []


# ===========================================================================
# Phase G -- SessionManager.compact -- AAP Rule 7
# ===========================================================================


def test_estimate_tokens_uses_len_div_4_formula() -> None:
    """``estimate_tokens`` is exactly ``len(json.dumps(msgs)) // 4``.

    The user-mandated rule 7 estimator is verified byte-for-byte against
    a manually computed reference value, with the identical serialisation
    semantics (``mode="json"``, ``ensure_ascii=False``).
    """
    messages = [
        _make_message(Role.user, "hello world"),
        _make_message(Role.assistant, "greetings, traveller"),
    ]

    expected = (
        len(
            json.dumps(
                [m.model_dump(mode="json") for m in messages], ensure_ascii=False
            )
        )
        // 4
    )

    assert estimate_tokens(messages) == expected


def test_estimate_tokens_empty_list_returns_zero() -> None:
    """An empty messages list yields ``estimate_tokens == 0`` (well, almost).

    ``json.dumps([])`` returns ``"[]"`` (length 2). Floor division by 4
    therefore yields 0. This anchors the lower bound of the formula.
    """
    assert estimate_tokens([]) == 0


def test_compaction_triggers_above_80_percent_threshold(session_root: Path) -> None:
    """Rule 7: compaction triggers when usage > 80% of ``token_limit``.

    Constructs 10 messages of ~400-char content (verified empirically to
    yield ``estimate_tokens > 800`` for ``token_limit=1000`` -> threshold
    800); asserts the returned record has fewer messages than the input
    AND the first message carries the mocked summary.
    """
    mgr = SessionManager(vibe_home=session_root)
    long_content = "x" * 400
    messages = [
        _make_message(
            Role.user if i % 2 == 0 else Role.assistant, f"{long_content}-{i}"
        )
        for i in range(10)
    ]
    record = _make_record(messages=messages)
    original_count = len(record.messages)

    # Precondition: token estimate MUST exceed the 800-token threshold so
    # the test exercises the compaction path. If the estimator semantics
    # ever change such that this precondition fails, the test would
    # otherwise become a silent no-op -- this assertion guards against
    # that regression.
    assert estimate_tokens(record.messages) > int(1000 * 0.8)

    complete_fn = MagicMock(return_value="SUMMARY_OF_OLDEST_HALF")

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    # The mock MUST have been called exactly once (the summarisation pass).
    assert complete_fn.call_count == 1
    # Message count must have dropped (one summary + newest half).
    assert len(result.messages) < original_count
    # The first message MUST be a system-role summary with the mocked text.
    assert result.messages[0].role == Role.system
    assert result.messages[0].content == "SUMMARY_OF_OLDEST_HALF"


def test_compaction_does_not_trigger_below_threshold(session_root: Path) -> None:
    """Rule 7: usage <= 80% of ``token_limit`` is a no-op.

    Short messages (estimate < 800) MUST leave the record unchanged AND
    MUST NOT invoke ``complete_fn``. The MagicMock's ``call_count`` is
    the primary assertion; the implementation may either return the same
    object reference unchanged or a structurally-equal new object, so
    the messages-equality check uses model_dump for tolerance.
    """
    mgr = SessionManager(vibe_home=session_root)
    messages = [
        _make_message(Role.user if i % 2 == 0 else Role.assistant, f"msg-{i}")
        for i in range(10)
    ]
    record = _make_record(messages=messages)

    # Precondition: token estimate MUST be below the 800-token threshold.
    assert estimate_tokens(record.messages) < int(1000 * 0.8)

    complete_fn = MagicMock(return_value="UNUSED_SUMMARY")
    snapshot_messages = [m.model_dump(mode="json") for m in record.messages]
    snapshot_summary = record.compacted_summary

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    # complete_fn MUST NOT be called below threshold.
    assert complete_fn.call_count == 0
    # The record must be unchanged.
    assert [m.model_dump(mode="json") for m in result.messages] == snapshot_messages
    assert result.compacted_summary == snapshot_summary


def test_compaction_replaces_oldest_half_with_system_summary(
    session_root: Path,
) -> None:
    """Rule 7: oldest half is replaced by a single system summary message.

    With 10 messages and a very small ``token_limit`` (forcing
    compaction), the final layout MUST be:

    - One system-role message carrying the mocked summary.
    - The newest 5 messages preserved verbatim.

    Total messages after compaction: 1 + 5 = 6.
    """
    mgr = SessionManager(vibe_home=session_root)
    long_content = "x" * 400
    messages = [
        _make_message(
            Role.user if i % 2 == 0 else Role.assistant, f"{long_content}-{i}"
        )
        for i in range(10)
    ]
    record = _make_record(messages=messages)

    complete_fn = MagicMock(return_value="SUMMARY")

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    assert len(result.messages) == 6
    assert result.messages[0].role == Role.system
    assert result.messages[0].content == "SUMMARY"
    # The summary is also stored at the top level per the schema.
    assert result.compacted_summary == "SUMMARY"


def test_recent_messages_preserved_verbatim(session_root: Path) -> None:
    """Rule 7: the newest half is preserved BYTE-FOR-BYTE.

    Constructs 10 messages with distinct, identifiable contents
    ``"msg-0"``, ``"msg-1"``, ..., ``"msg-9"`` and a content prefix long
    enough to push the estimate above threshold. After compaction, the
    final five messages MUST be ``msg-5``, ``msg-6``, ``msg-7``,
    ``msg-8``, ``msg-9`` in that exact order with their contents
    unchanged.
    """
    mgr = SessionManager(vibe_home=session_root)
    # Use a 500-char prefix so estimate_tokens > 800 even with the small
    # ``msg-N`` suffix; the suffix is what we assert on for identity.
    prefix = "p" * 500
    messages = [
        _make_message(Role.user if i % 2 == 0 else Role.assistant, f"{prefix}msg-{i}")
        for i in range(10)
    ]
    record = _make_record(messages=messages)

    complete_fn = MagicMock(return_value="DOES_NOT_MATTER")

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    # Index 0 is the summary; indices 1..5 are the preserved newest half.
    preserved = result.messages[1:]
    # The newest half consists of messages with indices 5..9 in the
    # original list, preserved verbatim.
    expected_contents = [f"{prefix}msg-{i}" for i in range(5, 10)]
    expected_roles = [Role.user if i % 2 == 0 else Role.assistant for i in range(5, 10)]
    assert [m.content for m in preserved] == expected_contents
    assert [m.role for m in preserved] == expected_roles


def test_compaction_sets_compacted_summary_field(session_root: Path) -> None:
    """Rule 7: ``compacted_summary`` is set to the summariser's output."""
    mgr = SessionManager(vibe_home=session_root)
    long_content = "x" * 400
    messages = [
        _make_message(
            Role.user if i % 2 == 0 else Role.assistant, f"{long_content}-{i}"
        )
        for i in range(10)
    ]
    record = _make_record(messages=messages)

    complete_fn = MagicMock(return_value="Hello, this is the summary text.")

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    assert result.compacted_summary == "Hello, this is the summary text."
    assert isinstance(result.compacted_summary, str)
    assert result.compacted_summary != ""


def test_compaction_with_odd_message_count(session_root: Path) -> None:
    """Odd-count message lists compact with a documented rounding rule.

    For 9 messages and Python's ``len // 2`` midpoint convention:
    ``midpoint = 9 // 2 = 4``. Oldest 4 are summarised, newest 5 are
    preserved verbatim -> final count: ``1 + 5 = 6``.

    The test is tolerant of the alternative ``1 + 4 = 5`` split if a
    future implementation rounds the other direction; it asserts only
    that ALL preserved messages still appear verbatim (the explicit
    Rule 7 invariant) regardless of where the cut falls.
    """
    mgr = SessionManager(vibe_home=session_root)
    prefix = "p" * 500
    messages = [
        _make_message(Role.user if i % 2 == 0 else Role.assistant, f"{prefix}msg-{i}")
        for i in range(9)
    ]
    record = _make_record(messages=messages)

    complete_fn = MagicMock(return_value="SUMMARY")

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    # The first message MUST be the summary regardless of split.
    assert result.messages[0].role == Role.system
    assert result.messages[0].content == "SUMMARY"

    # Total count is 1 (summary) + newest_half preserved.
    preserved = result.messages[1:]
    preserved_contents = [m.content for m in preserved]

    # The preserved messages MUST be a contiguous tail of the original
    # message list. Locate them by content match.
    original_contents = [m.content for m in messages]
    # Determine the split point implied by the actual result.
    if not preserved_contents:
        pytest.fail("Compaction discarded all preserved messages")
    split_index = original_contents.index(preserved_contents[0])
    # All preserved messages must equal the original tail starting at
    # ``split_index``, in order.
    assert preserved_contents == original_contents[split_index:]
    # And the count must be 6 (1+5) or 5 (1+4) -- the two valid rounding
    # outcomes for a 9-message list.
    assert len(result.messages) in {5, 6}


def test_compaction_with_zero_or_one_message_is_noop(session_root: Path) -> None:
    """``midpoint == 0`` short-circuits compaction.

    A single message (``len // 2 == 0``) cannot be meaningfully split
    into "oldest half" and "newest half"; the compactor must skip
    summarisation rather than discard the only message into an empty
    summary. This is the documented short-circuit at
    ``SessionManager.compact`` line ``if midpoint == 0: return session``.
    """
    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(messages=[_make_message(Role.user, "x" * 100_000)])

    # Make sure the precondition (above threshold) is met so the only
    # short-circuit reachable is the midpoint==0 branch.
    assert estimate_tokens(record.messages) > int(1000 * 0.8)

    complete_fn = MagicMock(return_value="UNUSED")

    result = mgr.compact(record, token_limit=1000, complete_fn=complete_fn)

    # complete_fn MUST NOT be called: the midpoint==0 branch returns
    # before invoking the summariser.
    assert complete_fn.call_count == 0
    # And the single message must be preserved.
    assert len(result.messages) == 1


def test_compaction_complete_fn_receives_oldest_half(session_root: Path) -> None:
    """The summariser is invoked with the OLDEST HALF of the messages.

    Pins the implementation contract that ``complete_fn`` receives a
    list whose tail equals ``record.messages[:midpoint]`` (the
    summarisation system prompt is prepended; that prompt is the only
    message that is NOT part of the oldest half).
    """
    mgr = SessionManager(vibe_home=session_root)
    prefix = "p" * 500
    messages = [
        _make_message(Role.user if i % 2 == 0 else Role.assistant, f"{prefix}msg-{i}")
        for i in range(10)
    ]
    record = _make_record(messages=messages)

    captured: dict[str, Any] = {}

    def fake_complete(passed_messages: list[LLMMessage]) -> str:
        captured["messages"] = passed_messages
        return "SUMMARY"

    mgr.compact(record, token_limit=1000, complete_fn=fake_complete)

    passed = captured["messages"]
    # The passed list is the summarisation prompt followed by the oldest
    # half. The oldest half is the first half of the original messages
    # (indices 0..4 for a 10-message list).
    oldest_half_contents = [m.content for m in messages[:5]]
    # The summarisation prompt is the FIRST entry; the oldest half
    # follows immediately. The prompt is implementation-specific, so we
    # check only the trailing slice.
    passed_oldest = [m.content for m in passed[-5:]]
    assert passed_oldest == oldest_half_contents


# ===========================================================================
# Phase H -- Integration with VIBE_HOME env var
# ===========================================================================


def test_session_manager_default_root_uses_vibe_home_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an explicit ``vibe_home`` arg, ``$VIBE_HOME`` is honoured.

    The implementation precedence is:
    1. Explicit ``vibe_home=...`` constructor argument (highest).
    2. ``$VIBE_HOME`` env var, if set and non-empty after strip.
    3. ``~/.blitzy`` (production default).

    This test pins precedence rule (2): with no constructor argument
    and ``$VIBE_HOME`` set to a tmp path, saves must land under that
    tmp path, NOT under the host ``~/.blitzy``.
    """
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))

    mgr = SessionManager()
    record = _make_record(repo="r", branch="b")

    written = mgr.save(record)

    expected = tmp_path / "sessions" / "r" / "b" / f"{record.session_id}.json"
    assert written == expected
    assert expected.exists()


def test_session_manager_explicit_vibe_home_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_root: Path
) -> None:
    """Explicit ``vibe_home`` constructor argument WINS over ``$VIBE_HOME``.

    Precedence rule (1) above: even when ``$VIBE_HOME`` points elsewhere,
    an explicit ``vibe_home=...`` kwarg routes all saves to the
    explicitly-named directory.
    """
    # Set the env var to a path we want IGNORED.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("VIBE_HOME", str(elsewhere))

    mgr = SessionManager(vibe_home=session_root)
    record = _make_record(repo="r", branch="b")

    written = mgr.save(record)

    # The save MUST land under session_root (explicit arg), NOT under
    # the env var's path.
    expected = session_root / "sessions" / "r" / "b" / f"{record.session_id}.json"
    assert written == expected
    assert expected.exists()
    # And NOT under the elsewhere path.
    assert not (
        elsewhere / "sessions" / "r" / "b" / f"{record.session_id}.json"
    ).exists()
