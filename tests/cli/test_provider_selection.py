"""Tests for the LLM provider selection CLI flow.

Covers AAP Behavioral Rules 4, 10, 13 and Validation Gates 2, 9, 13:

* Rule 4: Provider selection MUST be displayed before any backend is
  instantiated. Verified by patching every entry in BACKEND_FACTORY with
  a spy and asserting no spy is called when the picker is cancelled.
* Rule 10: MissingAPIKeyError MUST be raised when the user declines the
  interactive API key prompt for any of {blitzy, mistral, anthropic}.
* Rule 13: The provider string set {"blitzy", "mistral", "anthropic"} MUST
  be consistent across argparse choices, the factory string-to-enum map,
  the Backend enum values, the *_api_key VibeConfig fields, and the
  ContextLimitsConfig field names.

These tests use stdlib :mod:`unittest.mock` (``pytest-mock`` is not
available in this project's test infrastructure). The autouse fixtures
from ``tests/conftest.py`` (``tmp_working_directory``, ``config_dir``,
``_unlock_config_paths``, ``_mock_api_key``, ``_mock_platform``,
``_mock_update_commands``) provide an isolated filesystem and environment
for every test in this module.

Phase A tests (1-11) exercise :func:`select_provider` directly: numeric
selections, token aliases, case-insensitivity, whitespace tolerance,
invalid-input reprompt, ``_MAX_RETRIES`` exhaustion, and EOF handling.

Phase B tests (12-15) exercise the entrypoint orchestration block: the
``--provider`` flag MUST skip the picker and pass the corresponding
:class:`Backend` enum to :func:`run_cli` (Gate 9 wiring verification).

Phase C tests (16-17) exercise argparse behavior: ``type=str.lower``
case normalization and ``choices=[...]`` unknown-value rejection.

Phase D test (18) is the canonical Rule 4 assertion: when the picker is
cancelled (``KeyboardInterrupt``), NO entry in :data:`BACKEND_FACTORY`
has its constructor called.

Phase E tests (19-21) verify Rule 10 on :func:`resolve_or_prompt`: when
both the env var and config field are absent and the user enters an
empty string at the interactive ``getpass`` prompt,
:class:`MissingAPIKeyError` is raised.

Phase F test (22) is the canonical Rule 13 four-set consistency check.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Pre-import the heavy CLI modules at module load time so each pytest-xdist
# worker pays the import cost ONCE during test-module collection (which is
# exempt from ``pytest-timeout``'s 10s per-test ceiling) rather than during
# the first ``patch("vibe.cli.cli...")`` lookup inside ``_invoke_main`` (which
# IS counted against the test's timeout). Without this pre-import, Phase B/D
# tests that exercise the entrypoint orchestration block fail with a 10s
# ``Failed: Timeout`` because the lazy import inside ``main()`` triggers
# heavy textual_ui transitive imports.
import vibe.cli.cli  # noqa: F401  -- pre-import for xdist timeout safety
import vibe.cli.entrypoint  # noqa: F401  -- pre-import for xdist timeout safety
from vibe.core.config import Backend, MissingAPIKeyError, VibeConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    extra_patches: list[tuple[str, Any]] | None = None,
    expect_system_exit: bool = False,
) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Invoke :func:`vibe.cli.entrypoint.main` with the given ``argv``.

    Patches (always applied for the duration of the call):

    * :data:`sys.argv` -- so :func:`parse_arguments` sees the test's argv.
      Restored automatically at test teardown by ``monkeypatch``.
    * ``vibe.cli.cli.run_cli`` -- substituted with a :class:`MagicMock` spy
      that is returned to the caller so assertions can inspect
      ``call_args``. Patching the SOURCE module path works because the
      entrypoint does ``from vibe.cli.cli import run_cli`` LAZILY INSIDE
      ``main()``, which performs the attribute lookup against the
      (already-patched) module.
    * ``vibe.cli.entrypoint.check_and_resolve_trusted_folder`` -- no-op,
      because the real implementation may interact with the user's
      trusted-folders manager and we don't want side effects.
    * ``signal.signal`` -- no-op, because ``main()`` calls
      ``signal.signal(signal.SIGINT, signal.SIG_IGN)`` at the very top,
      and we want to leave the running test's signal disposition
      untouched.

    Extra patches can be passed via ``extra_patches``: a list of
    ``(target, mock_obj)`` pairs. Each ``target`` is the dotted import
    path that :func:`unittest.mock.patch` operates on. The returned
    ``extras`` dict maps each extra-patch target string to its mock
    instance so callers can assert on it without re-creating a local
    reference.

    Args:
        monkeypatch: The pytest ``MonkeyPatch`` fixture, used to set
            :data:`sys.argv` (which is reverted automatically at test
            teardown).
        argv: List of CLI tokens, including ``argv[0]``. Typically
            ``["vibe", "--provider", "blitzy"]`` for this test module.
        extra_patches: Optional extra targets to patch for the duration
            of the call. Each tuple ``(target, mock_obj)`` is applied
            via :func:`unittest.mock.patch`.
        expect_system_exit: When ``True``, ``main()`` is invoked inside
            ``pytest.raises(SystemExit)``. Use this for the cancelled
            flow (Phase D test 18).

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
# Phase A -- ``select_provider()`` direct unit tests (tests 1-11)
# ---------------------------------------------------------------------------


def test_enter_selects_blitzy_default(capsys: pytest.CaptureFixture[str]) -> None:
    """Empty input (Enter pressed) -> :attr:`Backend.BLITZY` (default).

    Also verifies that the four-line prompt is rendered verbatim per the
    AAP user example, including the EXACT double-space spacing between
    "Blitzy" and "(default)" on line 2 (``[1] Blitzy  (default)``).
    """
    with patch("builtins.input", return_value=""):
        from vibe.cli.provider_picker import select_provider

        result = select_provider()

    assert result == Backend.BLITZY

    captured = capsys.readouterr()
    # All four prompt lines MUST appear verbatim. The double-space between
    # "Blitzy" and "(default)" is a literal-string assertion -- if the
    # picker's prompt text drifts to a single space, this assertion is
    # the canary.
    assert "Select LLM provider:" in captured.out
    assert "[1] Blitzy  (default)" in captured.out
    assert "[2] Mistral" in captured.out
    assert "[3] Anthropic" in captured.out


def test_input_1_selects_blitzy() -> None:
    """Digit input ``"1"`` -> :attr:`Backend.BLITZY`."""
    with patch("builtins.input", return_value="1"):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == Backend.BLITZY


def test_input_2_selects_mistral() -> None:
    """Digit input ``"2"`` -> :attr:`Backend.MISTRAL`."""
    with patch("builtins.input", return_value="2"):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == Backend.MISTRAL


def test_input_3_selects_anthropic() -> None:
    """Digit input ``"3"`` -> :attr:`Backend.ANTHROPIC`."""
    with patch("builtins.input", return_value="3"):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == Backend.ANTHROPIC


def test_input_blitzy_string_selects_blitzy() -> None:
    """Token input ``"blitzy"`` -> :attr:`Backend.BLITZY`.

    Verifies the picker accepts the lowercase provider name as an
    alias for the numeric option.
    """
    with patch("builtins.input", return_value="blitzy"):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == Backend.BLITZY


def test_input_mistral_uppercase_selects_mistral() -> None:
    """Uppercase token ``"MISTRAL"`` -> :attr:`Backend.MISTRAL`.

    Verifies case-insensitivity: the picker MUST lowercase the input
    before matching against the token map.
    """
    with patch("builtins.input", return_value="MISTRAL"):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == Backend.MISTRAL


def test_input_anthropic_with_whitespace_selects_anthropic() -> None:
    """Mixed-case ``"  Anthropic  "`` (leading/trailing whitespace) -> Anthropic.

    Verifies the picker strips surrounding whitespace AND lowercases the
    input before matching. Operators who accidentally include trailing
    spaces from a copy-paste MUST NOT be punished with an invalid-choice
    error.
    """
    with patch("builtins.input", return_value="  Anthropic  "):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == Backend.ANTHROPIC


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", Backend.BLITZY),
        ("2", Backend.MISTRAL),
        ("3", Backend.ANTHROPIC),
        ("blitzy", Backend.BLITZY),
        ("mistral", Backend.MISTRAL),
        ("anthropic", Backend.ANTHROPIC),
        ("BLITZY", Backend.BLITZY),
        ("Mistral", Backend.MISTRAL),
        ("  anthropic  ", Backend.ANTHROPIC),
    ],
)
def test_token_aliases_parametrize(raw: str, expected: Backend) -> None:
    """Comprehensive sweep over all accepted token forms.

    Each tuple ``(raw, expected)`` exercises one accepted input form:
    digits, lowercase names, uppercase/title-case variants, and
    whitespace-padded tokens. This single parametrized test serves as
    the regression sentinel for the picker's input-normalization logic.
    """
    with patch("builtins.input", return_value=raw):
        from vibe.cli.provider_picker import select_provider

        assert select_provider() == expected


def test_invalid_input_reprompts_then_accepts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid input reprompts; the next valid input is accepted.

    ``"foo"`` is not a valid token -> reprompt with the canonical
    "Invalid choice. Please enter 1, 2, or 3." message -> next input
    ``"1"`` MUST return :attr:`Backend.BLITZY` without raising.
    """
    with patch("builtins.input", side_effect=["foo", "1"]):
        from vibe.cli.provider_picker import select_provider

        result = select_provider()

    assert result == Backend.BLITZY

    captured = capsys.readouterr()
    # The exact wording is a fixed contract -- if the picker's error
    # message drifts, this assertion catches the regression.
    assert "Invalid choice. Please enter 1, 2, or 3." in captured.out


def test_three_invalid_inputs_raises_keyboard_interrupt() -> None:
    """After ``_MAX_RETRIES`` invalid inputs the picker raises ``KeyboardInterrupt``.

    The bounded-retry contract caps attempts at three; supplying invalid
    inputs on every attempt MUST result in ``KeyboardInterrupt`` so the
    entrypoint's outer handler exits without instantiating a backend
    (AAP rule 4). More than three side_effects are listed defensively
    so the test does not depend on whether the implementation stops at
    3 or 4 -- ``KeyboardInterrupt`` after exhaustion is the only
    invariant.
    """
    with patch("builtins.input", side_effect=["bad1", "bad2", "bad3", "bad4"]):
        from vibe.cli.provider_picker import select_provider

        with pytest.raises(KeyboardInterrupt):
            select_provider()


def test_eof_error_raises_keyboard_interrupt() -> None:
    """``EOFError`` from ``input()`` is converted to ``KeyboardInterrupt``.

    Treating EOF as cancellation lets the entrypoint's outer
    ``except (KeyboardInterrupt, EOFError)`` clause handle Ctrl-D and
    piped-empty-stdin paths uniformly with Ctrl-C cancellation.
    """
    with patch("builtins.input", side_effect=EOFError):
        from vibe.cli.provider_picker import select_provider

        with pytest.raises(KeyboardInterrupt):
            select_provider()


# ---------------------------------------------------------------------------
# Phase B -- ``--provider`` flag end-to-end via ``main()`` (tests 12-15)
# ---------------------------------------------------------------------------


def test_entrypoint_path_blitzy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--provider blitzy`` -> ``run_cli`` called with ``backend=Backend.BLITZY``.

    Gate 9 wiring verification: the argparse flag MUST translate to the
    correct :class:`Backend` enum via :func:`provider_string_to_backend`
    and reach :func:`run_cli` as the ``backend`` keyword argument. The
    interactive provider picker MUST NOT be invoked.
    """
    picker_spy = MagicMock(name="select_provider_spy")
    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe", "--provider", "blitzy"],
        extra_patches=[("vibe.cli.provider_picker.select_provider", picker_spy)],
    )

    # The picker MUST NOT be called when ``--provider`` is supplied.
    assert picker_spy.call_count == 0, (
        f"select_provider was called {picker_spy.call_count} time(s); "
        "expected 0 because --provider was supplied"
    )
    # ``run_cli`` is the single sink for the orchestration block.
    assert run_cli_spy.call_count == 1, (
        f"run_cli was called {run_cli_spy.call_count} time(s); expected 1"
    )

    kwargs = run_cli_spy.call_args.kwargs
    assert kwargs.get("backend") == Backend.BLITZY, (
        f"Expected Backend.BLITZY, got {kwargs.get('backend')}"
    )
    assert kwargs.get("restored_session") is None, (
        "Expected restored_session=None when --provider is used without "
        f"--resume; got {kwargs.get('restored_session')!r}"
    )


def test_entrypoint_path_mistral(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--provider mistral`` -> ``run_cli`` called with ``backend=Backend.MISTRAL``."""
    picker_spy = MagicMock(name="select_provider_spy")
    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe", "--provider", "mistral"],
        extra_patches=[("vibe.cli.provider_picker.select_provider", picker_spy)],
    )

    assert picker_spy.call_count == 0
    assert run_cli_spy.call_count == 1
    kwargs = run_cli_spy.call_args.kwargs
    assert kwargs.get("backend") == Backend.MISTRAL, (
        f"Expected Backend.MISTRAL, got {kwargs.get('backend')}"
    )
    assert kwargs.get("restored_session") is None


def test_entrypoint_path_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--provider anthropic`` -> ``run_cli`` called with ``backend=Backend.ANTHROPIC``."""
    picker_spy = MagicMock(name="select_provider_spy")
    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe", "--provider", "anthropic"],
        extra_patches=[("vibe.cli.provider_picker.select_provider", picker_spy)],
    )

    assert picker_spy.call_count == 0
    assert run_cli_spy.call_count == 1
    kwargs = run_cli_spy.call_args.kwargs
    assert kwargs.get("backend") == Backend.ANTHROPIC, (
        f"Expected Backend.ANTHROPIC, got {kwargs.get('backend')}"
    )
    assert kwargs.get("restored_session") is None


def test_entrypoint_calls_picker_when_no_provider_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``--provider`` and no ``--resume`` -> the picker IS invoked.

    Verifies the orchestration block's default branch: when neither
    flag is supplied, :func:`select_provider` is called exactly once and
    its return value is forwarded to :func:`run_cli` as the ``backend``
    keyword. This is the canonical "rule 4 happy path" -- the picker is
    the gatekeeper before any backend constructor runs.
    """
    picker_spy = MagicMock(return_value=Backend.BLITZY)
    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe"],
        extra_patches=[("vibe.cli.provider_picker.select_provider", picker_spy)],
    )

    # The picker MUST be called exactly once when no provider flag is set.
    assert picker_spy.call_count == 1, (
        f"select_provider was called {picker_spy.call_count} time(s); "
        "expected 1 because neither --provider nor --resume was supplied"
    )
    assert run_cli_spy.call_count == 1
    kwargs = run_cli_spy.call_args.kwargs
    # The picker's return value MUST reach ``run_cli`` unchanged.
    assert kwargs.get("backend") == Backend.BLITZY, (
        f"Expected Backend.BLITZY (picker's return value), got {kwargs.get('backend')}"
    )


# ---------------------------------------------------------------------------
# Phase C -- ``--provider`` argparse case-insensitivity + validation
# ---------------------------------------------------------------------------


def test_provider_flag_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--provider BLITZY`` -> ``args.provider == "blitzy"``.

    Verifies that argparse normalizes the input via ``type=str.lower``
    BEFORE validating against ``choices=["blitzy", "mistral",
    "anthropic"]``. Without ``type=str.lower``, ``--provider BLITZY``
    would fail argparse validation because ``"BLITZY"`` is not in the
    choices list.
    """
    monkeypatch.setattr(sys, "argv", ["vibe", "--provider", "BLITZY"])
    from vibe.cli.entrypoint import parse_arguments

    args = parse_arguments()
    assert args.provider == "blitzy", (
        f"Expected args.provider=='blitzy' (lowercased), got {args.provider!r}"
    )


def test_provider_flag_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--provider gpt`` -> ``SystemExit`` with "invalid choice" error.

    Verifies that argparse rejects unknown provider strings -- the
    rule-13 invariant that the accepted token set is exactly
    ``{"blitzy", "mistral", "anthropic"}`` is enforced by argparse
    itself, not by downstream code. ``gpt`` is the canonical "wrong
    provider" example that would round-trip through
    :func:`provider_string_to_backend` as a ``KeyError`` if argparse
    let it through.
    """
    monkeypatch.setattr(sys, "argv", ["vibe", "--provider", "gpt"])
    from vibe.cli.entrypoint import parse_arguments

    with pytest.raises(SystemExit):
        parse_arguments()

    # argparse writes the error to stderr; assert on substring rather
    # than full line because the exact format depends on Python version.
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err.lower() or "gpt" in captured.err.lower(), (
        f"Expected argparse 'invalid choice' message in stderr; got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Phase D -- Rule 4: No backend constructor before picker returns (test 18)
# ---------------------------------------------------------------------------


def test_no_backend_constructor_called_when_picker_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical Rule 4 assertion: cancelled picker -> NO backend instantiated.

    Setup:

    * Every entry in :data:`BACKEND_FACTORY` is replaced with a
      :class:`MagicMock` spy keyed by the :class:`Backend` enum member.
    * :func:`select_provider` is patched to raise ``KeyboardInterrupt``
      on its very first call (simulating Ctrl-C at the picker).
    * No ``--provider`` and no ``--resume`` flags, so the orchestration
      block MUST invoke the picker.

    Invariants verified:

    * ``main()`` MUST exit via ``sys.exit(0)`` (the entrypoint's outer
      ``except (KeyboardInterrupt, EOFError)`` clause).
    * NO backend class in :data:`BACKEND_FACTORY` MUST have been
      instantiated (``call_count == 0`` for every spy).
    * :func:`run_cli` MUST NOT be called (the orchestration block
      short-circuited via ``sys.exit`` before reaching it).

    This is the SINGLE most important assertion for AAP rule 4: it
    proves that the picker is the gatekeeper for backend instantiation.
    """
    from vibe.core.llm.backend.factory import BACKEND_FACTORY

    fake_factory = {
        enum_member: MagicMock(name=f"{enum_member.value}_backend_class")
        for enum_member in BACKEND_FACTORY
    }
    picker_spy = MagicMock(side_effect=KeyboardInterrupt)

    run_cli_spy, _ = _invoke_main(
        monkeypatch,
        ["vibe"],
        extra_patches=[
            ("vibe.cli.provider_picker.select_provider", picker_spy),
            ("vibe.core.llm.backend.factory.BACKEND_FACTORY", fake_factory),
        ],
        expect_system_exit=True,
    )

    # Rule 4 assertion: NO backend class was instantiated.
    for enum_member, mock_cls in fake_factory.items():
        assert mock_cls.call_count == 0, (
            f"Backend class for {enum_member.value!r} was instantiated "
            f"({mock_cls.call_count} time(s)) despite picker cancellation; "
            "AAP rule 4 forbids any backend constructor from running before "
            "the picker returns successfully"
        )
    # And ``run_cli`` was never reached either.
    assert run_cli_spy.call_count == 0, (
        f"run_cli was called {run_cli_spy.call_count} time(s); "
        "expected 0 because picker cancellation exits before run_cli"
    )
    # The picker itself MUST have been invoked once (otherwise the
    # cancellation handler would not have fired).
    assert picker_spy.call_count == 1, (
        f"select_provider was called {picker_spy.call_count} time(s); "
        "expected 1 (the cancellation path)"
    )


# ---------------------------------------------------------------------------
# Phase E -- Rule 10: Declined API key raises MissingAPIKeyError (tests 19-21)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "env_var", "config_field"),
    [
        ("blitzy", "BLITZY_API_KEY", "blitzy_api_key"),
        ("mistral", "MISTRAL_API_KEY", "mistral_api_key"),
        ("anthropic", "ANTHROPIC_API_KEY", "anthropic_api_key"),
    ],
)
def test_declined_key_raises_missing_api_key_error(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str, config_field: str
) -> None:
    """Rule 10: declined interactive API key prompt raises ``MissingAPIKeyError``.

    For each of the three accepted providers, verifies the
    :func:`resolve_or_prompt` contract: when the env var is absent, the
    config field is ``None``, and the user enters an empty string at
    the ``getpass`` prompt, :class:`MissingAPIKeyError` is raised. AAP
    rule 10 mandates that the agent then exit; the CLI entrypoint
    catches this exception and terminates with a clear error message
    (not exercised here -- this is the unit-level verification).

    Critical setup detail: the ``MISTRAL_API_KEY`` env var is set by
    the autouse ``_mock_api_key`` fixture in ``tests/conftest.py``, so
    we MUST explicitly ``delenv`` it for the mistral parametrization
    to actually reach the interactive prompt. The other two env vars
    are not set by the conftest fixture but we ``delenv`` defensively
    in case any external environment variable bleeds through.

    Args:
        monkeypatch: pytest fixture for env-var manipulation.
        provider: Lowercase provider token (``"blitzy"``/``"mistral"``/
            ``"anthropic"``).
        env_var: Provider-specific env-var name to clear.
        config_field: :class:`VibeConfig` attribute name to verify is
            ``None`` after :meth:`VibeConfig.load`.
    """
    # Clear the provider-specific env var. Autouse ``_mock_api_key``
    # sets ``MISTRAL_API_KEY=mock`` -- without this delenv, the mistral
    # parametrization would short-circuit at tier 1 and the prompt
    # would never fire.
    monkeypatch.delenv(env_var, raising=False)

    # Loading :class:`VibeConfig` requires the conftest's mock mistral
    # provider + ``MISTRAL_API_KEY=mock``; however, for the mistral
    # parametrization we just cleared that env var. Re-set it ONLY for
    # the config-load step so the ``_check_api_key`` validator does not
    # complain about the active mistral provider's missing key. The
    # in-memory config is then used to verify ``resolve_or_prompt``'s
    # behavior when ``config.<provider>_api_key`` is ``None`` and the
    # env var is unset.
    if provider == "mistral":
        monkeypatch.setenv("MISTRAL_API_KEY", "mock")
    config = VibeConfig.load()
    if provider == "mistral":
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    # Sanity check: the corresponding config field MUST be ``None`` (or
    # falsy). The autouse conftest config does NOT set ``*_api_key``
    # fields. If a future cascade adds them, this assertion catches the
    # drift before the prompt assertions below.
    assert getattr(config, config_field) is None, (
        f"Expected config.{config_field} to be None for this test; "
        f"got {getattr(config, config_field)!r}"
    )

    from vibe.core.llm.api_key_prompt import resolve_or_prompt

    # ``getpass.getpass`` returns the user's typed input. Empty string
    # signals declined entry. ``builtins.input`` is patched defensively
    # so any post-prompt "Save to config? [y/N]" call doesn't crash --
    # though in practice the empty-input path raises BEFORE the save
    # prompt is reached.
    with (
        patch("getpass.getpass", return_value=""),
        patch("builtins.input", return_value=""),
        pytest.raises(MissingAPIKeyError) as exc_info,
    ):
        resolve_or_prompt(provider, env_var, config_field, config)

    # The provider token MUST appear in the exception's string
    # representation -- both ``MissingAPIKeyError(provider)`` and
    # ``MissingAPIKeyError(env_key, provider_name)`` signatures
    # include the provider name in the formatted message.
    assert provider in str(exc_info.value).lower(), (
        f"Expected {provider!r} in str(exc); got {str(exc_info.value)!r}"
    )


# ---------------------------------------------------------------------------
# Phase F -- Rule 13: Provider string set consistency (test 22)
# ---------------------------------------------------------------------------


def test_provider_string_set_consistency() -> None:
    """Rule 13: provider string set MUST be consistent across four sources.

    The four sources are:

    1. :class:`Backend` enum values for the three user-facing providers.
    2. :func:`provider_string_to_backend` factory helper -- round-trips
       each string to the corresponding :class:`Backend` enum value
       AND raises :class:`KeyError` on unknown strings.
    3. :class:`VibeConfig` ``*_api_key`` field suffixes.
    4. :class:`ContextLimitsConfig` field names.

    All four sets MUST equal ``{"blitzy", "mistral", "anthropic"}``.

    Gate 13 (Registration-Invocation Pairing) invariant: any drift in
    one source without a matching update to the other three would
    produce orphaned strings -- e.g., a session record's ``provider``
    field that no longer maps to a backend, or a context-limit override
    that silently ignores a renamed provider.
    """
    expected = {"blitzy", "mistral", "anthropic"}

    # --- Source 1: ``Backend`` enum values for the three providers ----
    # We assert on the three user-facing members explicitly (and not on
    # ``{b.value for b in Backend}``) because ``Backend`` ALSO includes
    # ``GENERIC`` and ``CLAUDE_CODE`` -- those are internal/programmatic
    # backends, NOT user-selectable via ``--provider`` (see the rule-13
    # comment in ``vibe/core/llm/backend/factory.py``).
    backend_values = {
        Backend.BLITZY.value,
        Backend.MISTRAL.value,
        Backend.ANTHROPIC.value,
    }
    assert backend_values == expected, (
        f"Backend enum values for user-facing providers: {backend_values} != {expected}"
    )

    # --- Source 2: ``provider_string_to_backend`` round-trip ----------
    from vibe.core.llm.backend.factory import provider_string_to_backend

    for name in expected:
        backend = provider_string_to_backend(name)
        assert backend.value == name, (
            f"provider_string_to_backend({name!r}) returned {backend!r}; "
            f"expected the corresponding Backend enum whose value=={name!r}"
        )
    # Unknown strings MUST raise ``KeyError`` -- this is the
    # gatekeeper that prevents orphaned strings from silently
    # propagating downstream.
    with pytest.raises(KeyError):
        provider_string_to_backend("gpt")

    # --- Source 3: ``VibeConfig`` ``*_api_key`` field suffixes --------
    api_key_suffixes = {
        field_name.removesuffix("_api_key")
        for field_name in VibeConfig.model_fields
        if field_name.endswith("_api_key")
    }
    assert api_key_suffixes == expected, (
        f"VibeConfig *_api_key field suffixes: {api_key_suffixes} != {expected}"
    )

    # --- Source 4: ``ContextLimitsConfig`` field names ----------------
    from vibe.core.config import ContextLimitsConfig

    context_limits_fields = set(ContextLimitsConfig.model_fields.keys())
    assert context_limits_fields == expected, (
        f"ContextLimitsConfig field names: {context_limits_fields} != {expected}"
    )
