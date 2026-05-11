"""Pure-Python git context detector.

Reads ``.git/HEAD`` and ``.git/config`` to extract ``(repo_name, branch_name)``
for the current working directory. NEVER raises an exception — returns
``("", "")`` silently on any I/O, parse, or unicode error.

This is intentionally subprocess-free: it does NOT shell out to ``git``, so it
works in environments where ``git`` is not on ``PATH`` (containers, restricted
shells) and the behavioral contract that "MUST NOT raise" is trivially
satisfied (AAP §0.8.1 rule 3).

The single public entrypoint is :func:`detect`. The helpers
:func:`_read_branch` and :func:`_read_repo_name` are module-private.

Typical usage::

    from vibe.core.git_context import detect

    repo, branch = detect()
    if not repo and not branch:
        # No git context available — fall back to "_unknown/_unknown"
        ...
"""

from __future__ import annotations

import configparser
from pathlib import Path


def detect(cwd: Path | None = None) -> tuple[str, str]:
    """Detect ``(repo_name, branch_name)`` from a working directory.

    Reads ``cwd/.git/HEAD`` and ``cwd/.git/config`` directly (no subprocess
    invocation). Returns ``("", "")`` silently on any I/O or parse error
    (rule 3: MUST NOT raise).

    Args:
        cwd: Directory to inspect. Defaults to :func:`Path.cwd`.

    Returns:
        Tuple of ``(repo_name, branch_name)``. Both fields are strings; either
        may be empty when detection fails or is partial (e.g., detached HEAD
        yields a branch of ``""`` even when the repo name is detectable, and a
        repo without an ``origin`` remote yields an empty repo name even on a
        named branch).
    """
    try:
        base = Path(cwd) if cwd is not None else Path.cwd()
    except OSError:
        return ("", "")

    git_dir = base / ".git"
    # ``.git`` may not exist, or may be a file (for submodules/worktrees); we
    # only support the canonical directory layout per the AAP. ``is_dir`` is
    # safe — it returns ``False`` (instead of raising) for missing paths and
    # non-directory files.
    try:
        if not git_dir.is_dir():
            return ("", "")
    except OSError:
        # Defensive: ``is_dir`` can raise on permission/IO errors on some
        # filesystems (e.g., FUSE mounts).
        return ("", "")

    branch = _read_branch(git_dir / "HEAD")
    repo = _read_repo_name(git_dir / "config")
    return (repo, branch)


def _read_branch(head_path: Path) -> str:
    """Parse ``.git/HEAD`` to extract the current branch name.

    The ``HEAD`` file contains either:

    * ``ref: refs/heads/<branch>`` (attached HEAD — the common case).
    * A 40-char SHA hex string (detached HEAD).

    Returns the branch name for attached HEAD or ``""`` for detached HEAD,
    a missing file, or any parse/I/O error.

    Args:
        head_path: Path to ``.git/HEAD``.

    Returns:
        Branch name string, or ``""`` on detached HEAD or any error.
    """
    try:
        content = head_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""

    # Attached HEAD: "ref: refs/heads/<branch>"
    if content.startswith("ref:"):
        ref = content[len("ref:") :].strip()
        if not ref:
            return ""
        # Git permits "/" inside branch names (e.g., "feature/foo/bar"), so we
        # strip the canonical "refs/heads/" prefix when present rather than
        # naively splitting on the last "/".
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            return ref[len(prefix) :]
        # Non-heads ref (e.g., tag or remote-tracking) — return the last path
        # segment as a sensible fallback.
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
        return ref

    # Detached HEAD: 40-char SHA hex (or any non-ref payload). No branch name.
    return ""


def _read_repo_name(config_path: Path) -> str:
    """Parse ``.git/config`` to extract the origin remote's repository name.

    Looks for the ``[remote "origin"]`` section and its ``url`` field, strips
    a trailing ``.git``, and returns the final path segment (after the last
    ``/`` or ``:`` to support SSH-form URLs).

    Returns ``""`` on missing file, missing section, missing url, or any
    parse/I/O error.

    Args:
        config_path: Path to ``.git/config``.

    Returns:
        Repository name string, or ``""`` on any error or missing remote.
    """
    try:
        content = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""

    # Use ``RawConfigParser`` so URL values containing literal ``%`` characters
    # (rare but legal in git URLs) cannot trigger ``InterpolationError`` at
    # value-access time — satisfying rule 3 robustly.
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(content)
    except configparser.Error:
        return ""

    # ``configparser`` exposes the quoted-subsection header as a single key:
    # ``'remote "origin"'`` (with the embedded double quotes). Both a missing
    # section and a missing ``url`` key raise ``KeyError`` on access.
    try:
        url = parser['remote "origin"']["url"].strip()
    except (KeyError, configparser.Error):
        return ""

    return _strip_url_to_repo_name(url)


def _strip_url_to_repo_name(url: str) -> str:
    """Reduce a git remote URL to its final-segment repository name.

    Strips a trailing ``.git`` and trailing slashes, then takes the substring
    after the rightmost ``/`` or ``:`` separator. Handles HTTPS, SSH, and bare
    filesystem-path URL forms uniformly.

    Args:
        url: Already-stripped remote URL (the caller has run ``.strip()``).

    Returns:
        The repository name, or ``""`` for an empty input.
    """
    if not url:
        return ""

    # Strip trailing ".git" (and trailing slash, defensively).
    if url.endswith(".git"):
        url = url[: -len(".git")]
    url = url.rstrip("/")
    if not url:
        return ""

    # SSH-form: "git@github.com:user/repo"  -> rightmost separator is "/".
    # SSH-form sans path: "git@host:repo"   -> rightmost separator is ":".
    # HTTPS-form: "https://github.com/user/repo" -> rightmost separator is "/".
    # Bare path: "/srv/git/repo"            -> rightmost separator is "/".
    # Combining ``rfind("/")`` and ``rfind(":")`` and taking the rightmost
    # handles all forms uniformly.
    last_slash = url.rfind("/")
    last_colon = url.rfind(":")
    cut = max(last_slash, last_colon)
    # ``cut < 0`` means no separator at all — return the whole url (probably
    # malformed but arguably the best we can do without inventing extra
    # error states).
    return url if cut < 0 else url[cut + 1 :]
