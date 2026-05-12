"""Unit tests for :func:`vibe.core.git_context.detect`.

This suite exercises every behavioral branch of the pure-Python git context
detector, with particular attention to **AAP behavioral Rule 3** (verbatim):

    Git context detection MUST NOT raise exceptions when ``.git`` is absent;
    MUST return ``("", "")`` silently; verified by unit test in temp dir
    without ``.git``.

The detector reads ``.git/HEAD`` and ``.git/config`` directly (no ``git``
subprocess invocation) and returns ``(repo_name, branch_name)``. Every I/O,
parse, or unicode failure path is required to return ``("", "")`` silently.

The autouse :func:`tmp_working_directory` fixture in ``tests/conftest.py``
monkeypatches the per-test current working directory to a freshly created
temporary directory, guaranteeing no inherited ``.git/`` state from the
repository the suite runs inside.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.git_context import detect

# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _write_attached_head(git_dir: Path, branch: str) -> None:
    """Write an attached-HEAD ``HEAD`` file pointing at ``refs/heads/<branch>``."""
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def _write_detached_head(git_dir: Path, sha: str) -> None:
    """Write a detached-HEAD ``HEAD`` file containing a raw 40-char SHA."""
    (git_dir / "HEAD").write_text(f"{sha}\n", encoding="utf-8")


def _write_config_with_origin(git_dir: Path, url: str) -> None:
    """Write a minimal ``.git/config`` containing the given ``origin`` URL."""
    content = f'[remote "origin"]\n\turl = {url}\n'
    (git_dir / "config").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Rule 3 — silent-failure contract: no .git/ present
# ---------------------------------------------------------------------------


def test_no_git_directory_returns_empty_tuple(tmp_working_directory: Path) -> None:
    """Rule 3: detect() returns ('', '') silently when .git is absent."""
    # The autouse fixture guarantees the cwd is a fresh temp dir with no .git.
    assert not (tmp_working_directory / ".git").exists()

    result = detect()

    assert result == ("", "")


# ---------------------------------------------------------------------------
# Happy-path: attached HEAD + origin remote
# ---------------------------------------------------------------------------


def test_attached_head_parses_branch_name(tmp_working_directory: Path) -> None:
    """Attached HEAD with refs/heads/main yields branch 'main' and repo 'repo'."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    assert detect() == ("repo", "main")


def test_attached_head_with_feature_branch(tmp_working_directory: Path) -> None:
    """Branch names containing '/' (e.g., feature/x) are preserved verbatim."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "feature/my-branch")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    repo, branch = detect()

    assert branch == "feature/my-branch"
    assert repo == "repo"


# ---------------------------------------------------------------------------
# Detached HEAD: branch must be empty, repo still detectable
# ---------------------------------------------------------------------------


def test_detached_head_returns_empty_branch(tmp_working_directory: Path) -> None:
    """Detached HEAD (raw 40-char SHA) yields branch='' and repo still parsed."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_detached_head(git_dir, "a" * 40)
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    repo, branch = detect()

    assert branch == ""
    assert repo == "repo"


# ---------------------------------------------------------------------------
# Rule 3 — malformed HEAD content must not raise
# ---------------------------------------------------------------------------


def test_malformed_head_does_not_raise(tmp_working_directory: Path) -> None:
    """Rule 3: malformed HEAD content does not raise; branch silently empty."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("this is not a HEAD\n", encoding="utf-8")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    # detect() MUST NOT raise — the assertion exercises the no-raise contract
    # by simply unpacking the return value.
    repo, branch = detect()

    assert branch == ""
    # Config remains parseable so the repo name is still recovered.
    assert repo == "repo"


# ---------------------------------------------------------------------------
# Remote URL forms — repo name extraction from each canonical form
# ---------------------------------------------------------------------------


def test_origin_url_https_form(tmp_working_directory: Path) -> None:
    """HTTPS URL with trailing .git yields the final path segment as repo."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    repo, _ = detect()

    assert repo == "repo"


def test_origin_url_ssh_form(tmp_working_directory: Path) -> None:
    """SSH-form URL (git@host:owner/repo.git) yields repo='repo'."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "git@github.com:owner/repo.git")

    repo, _ = detect()

    assert repo == "repo"


def test_origin_url_no_dot_git_suffix(tmp_working_directory: Path) -> None:
    """URL without a trailing .git is parsed correctly."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo")

    repo, _ = detect()

    assert repo == "repo"


def test_origin_url_with_trailing_slash(tmp_working_directory: Path) -> None:
    """URL with a trailing slash has the slash stripped before name extraction."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo/")

    repo, _ = detect()

    assert repo == "repo"


# ---------------------------------------------------------------------------
# Missing remote section / missing config file
# ---------------------------------------------------------------------------


def test_missing_remote_origin_returns_empty_repo(tmp_working_directory: Path) -> None:
    """When [remote "origin"] is absent, repo='' and branch is still parsed."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    # Write a config that has only the [core] section — no [remote "origin"].
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
    )

    repo, branch = detect()

    assert repo == ""
    assert branch == "main"


def test_missing_config_file_does_not_raise(tmp_working_directory: Path) -> None:
    """Rule 3: missing .git/config does not raise; repo silently empty."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    # Intentionally do NOT create a config file.

    repo, branch = detect()

    assert repo == ""
    assert branch == "main"


# ---------------------------------------------------------------------------
# Rule 3 — unreadable .git/HEAD must not raise
# ---------------------------------------------------------------------------


def test_unreadable_head_returns_empty_tuple(
    tmp_working_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 3: unreadable HEAD (OSError on read) yields ('', '') silently."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    def _raise_oserror(*args, **kwargs) -> str:
        raise OSError("permission denied")

    # Monkeypatching Path.read_text affects BOTH .git/HEAD and .git/config
    # reads. The detector must catch the OSError in each helper and return
    # the empty-tuple sentinel without ever raising.
    monkeypatch.setattr(Path, "read_text", _raise_oserror)

    result = detect()

    assert result == ("", "")


# ---------------------------------------------------------------------------
# Additional edge cases for robustness (Rule 3 coverage)
# ---------------------------------------------------------------------------


def test_empty_ref_after_prefix_returns_empty_branch(
    tmp_working_directory: Path,
) -> None:
    """Rule 3: HEAD with bare ``ref:`` (no payload) yields branch='' silently."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    # ``ref:`` prefix followed by whitespace only — should not raise and must
    # produce an empty branch name.
    (git_dir / "HEAD").write_text("ref:   \n", encoding="utf-8")

    repo, branch = detect()

    assert branch == ""
    assert repo == ""


def test_non_heads_ref_returns_last_path_segment(tmp_working_directory: Path) -> None:
    """HEAD pointing to a non-heads ref (e.g., tag) yields the last segment."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    # A non-heads ref (tag form). The detector returns the final segment.
    (git_dir / "HEAD").write_text("ref: refs/tags/v1.0.0\n", encoding="utf-8")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    repo, branch = detect()

    assert branch == "v1.0.0"
    assert repo == "repo"


def test_malformed_config_does_not_raise(tmp_working_directory: Path) -> None:
    """Rule 3: unparseable .git/config does not raise; repo silently empty."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    # Garbage that the configparser cannot parse (key= line outside any section).
    (git_dir / "config").write_text(
        "this is not = a valid ini file\n", encoding="utf-8"
    )

    repo, branch = detect()

    assert repo == ""
    assert branch == "main"


def test_empty_origin_url_returns_empty_repo(tmp_working_directory: Path) -> None:
    """Origin section present with empty url yields repo='' (no separator)."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    # Origin section with an empty (whitespace-only) url field.
    (git_dir / "config").write_text('[remote "origin"]\n\turl =   \n', encoding="utf-8")

    repo, branch = detect()

    assert repo == ""
    assert branch == "main"


def test_non_heads_ref_without_slash_returns_ref_verbatim(
    tmp_working_directory: Path,
) -> None:
    """HEAD with a single-token ref (no '/') returns the ref text as branch."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    # An unusual but legal ref payload — neither "refs/heads/..." nor a path
    # with a slash. The detector returns the ref verbatim as a fallback.
    (git_dir / "HEAD").write_text("ref: HEAD\n", encoding="utf-8")

    _repo, branch = detect()

    assert branch == "HEAD"


def test_origin_url_dot_git_only_returns_empty_repo(
    tmp_working_directory: Path,
) -> None:
    """Origin URL of just '.git' yields repo='' (empty after suffix strip)."""
    git_dir = tmp_working_directory / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    # URL is exactly ".git" — after stripping the trailing ".git" suffix, the
    # remaining url is empty, so repo='' silently.
    _write_config_with_origin(git_dir, ".git")

    repo, branch = detect()

    assert repo == ""
    assert branch == "main"


def test_explicit_cwd_argument_is_honored(tmp_working_directory: Path) -> None:
    """Passing an explicit cwd argument overrides Path.cwd() lookup."""
    # Set up a fully populated repo at an arbitrary subpath that is NOT the
    # current working directory.
    other_dir = tmp_working_directory / "elsewhere"
    other_dir.mkdir()
    git_dir = other_dir / ".git"
    git_dir.mkdir()
    _write_attached_head(git_dir, "main")
    _write_config_with_origin(git_dir, "https://github.com/owner/repo.git")

    # The autouse fixture leaves Path.cwd() pointing at tmp_working_directory,
    # which has no .git/. Without an explicit cwd, detect() would return
    # ("", ""). With cwd=other_dir, it must read from there instead.
    assert detect() == ("", "")
    assert detect(cwd=other_dir) == ("repo", "main")


# ---------------------------------------------------------------------------
# Return-type invariants
# ---------------------------------------------------------------------------


def test_returns_tuple_type(tmp_working_directory: Path) -> None:
    """detect() always returns a 2-element tuple of strings."""
    result = detect()

    assert isinstance(result, tuple)
    assert len(result) == 2
    repo, branch = result
    assert isinstance(repo, str)
    assert isinstance(branch, str)
