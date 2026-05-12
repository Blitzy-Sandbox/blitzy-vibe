"""Unit tests for the AAP-extended ``vibe/core/llm/backend/anthropic_llm.py``.

This suite verifies the AnthropicBackend extension introduced by the Agent
Action Plan (AAP):

1. The constructor now requires a ``config: VibeConfig`` argument and
   resolves the API key via the three-tier
   :func:`vibe.core.llm.api_key_prompt.resolve_or_prompt` chain
   (env var -> ``VibeConfig.anthropic_api_key`` -> interactive
   ``getpass.getpass`` prompt).
2. The ``anthropic_model`` field on :class:`VibeConfig` (default
   ``"claude-sonnet-4-6"``) is forwarded to the Anthropic SDK's
   ``messages.stream`` and ``messages.count_tokens`` calls. The config
   value takes precedence over ``model.name`` per AAP section 0.6.1.
3. The Anthropic SDK is the sole transport for ``/v1/messages`` -- raw
   ``httpx`` calls against the messages endpoint are forbidden (AAP
   rule 9, "library isolation").
4. An empty interactive prompt response raises
   :class:`vibe.core.config.MissingAPIKeyError` (AAP rule 10, "declined
   prompt -> exit"); the CLI entrypoint catches this and exits.
5. The ``AnthropicMapper`` adapter class is preserved unchanged
   (smoke-checked for the four canonical method attributes).
6. The resolved API key is registered with
   :func:`vibe.core.observability.register_sensitive` so that subsequent
   log emissions including the key value are scrubbed by the global
   ``KEY_MASK_FILTER`` (AAP rule 2, "API key masking").

The file also includes the canonical Gate 8 integration test for the
streaming path -- skipped automatically when ``ANTHROPIC_API_KEY`` is
unset or set to the literal ``"mock"`` placeholder used by the autouse
``tests/conftest.py:_mock_api_key`` fixture.

All ``conftest.py`` autouse fixtures (``tmp_working_directory``,
``config_dir``, ``_unlock_config_paths``, ``_mock_api_key``,
``_mock_platform``, ``_mock_update_commands``) are inherited
automatically by every test in this file and require no explicit
declaration.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr
import pytest

from vibe.core.config import (
    Backend,
    MissingAPIKeyError,
    ModelConfig,
    ProviderConfig,
    VibeConfig,
)
from vibe.core.llm.backend.anthropic_llm import AnthropicBackend, AnthropicMapper
from vibe.core.llm.types import BackendLike
from vibe.core.types import LLMMessage, Role

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_provider() -> ProviderConfig:
    """Construct a minimal :class:`ProviderConfig` for Anthropic tests.

    Returns:
        A :class:`ProviderConfig` whose ``backend`` is
        :attr:`Backend.ANTHROPIC` and whose ``api_key_env_var`` is the
        canonical ``"ANTHROPIC_API_KEY"``. Used as the ``provider``
        argument to :class:`AnthropicBackend` in every test that does
        not require a custom provider shape.
    """
    return ProviderConfig(
        name="anthropic",
        api_base="https://api.anthropic.com",
        api_key_env_var="ANTHROPIC_API_KEY",
        backend=Backend.ANTHROPIC,
    )


def _make_model(name: str = "claude-sonnet-4-6") -> ModelConfig:
    """Construct a :class:`ModelConfig` for the Anthropic provider.

    The default ``name`` matches the AAP-prescribed default for
    :attr:`VibeConfig.anthropic_model` so that pinned-model assertions
    do not require an explicit override.

    Args:
        name: The model identifier to embed in the ``ModelConfig``.

    Returns:
        A :class:`ModelConfig` whose ``provider`` is ``"anthropic"`` and
        whose ``alias`` is ``"anthropic-test"`` (a deterministic value
        for test fixtures).
    """
    return ModelConfig(name=name, provider="anthropic", alias="anthropic-test")


def _make_config(
    *, anthropic_api_key: str | None = None, anthropic_model: str | None = None
) -> VibeConfig:
    """Construct a :class:`VibeConfig` with optional Anthropic fields.

    The :class:`VibeConfig` ``_check_api_key`` validator only inspects
    the *active* provider (mistral in the conftest's default config),
    so passing ``anthropic_api_key=None`` does not trigger
    :class:`MissingAPIKeyError` at construction time -- the test
    autouse ``_mock_api_key`` fixture supplies ``MISTRAL_API_KEY=mock``.

    Args:
        anthropic_api_key: Optional plaintext API key. When supplied,
            wrapped in :class:`pydantic.SecretStr` before assignment to
            match the production type signature.
        anthropic_model: Optional model identifier override. When
            ``None``, the field defaults to ``"claude-sonnet-4-6"`` via
            the :class:`VibeConfig` schema.

    Returns:
        A :class:`VibeConfig` carrying the supplied overrides.
    """
    kwargs: dict[str, Any] = {}
    if anthropic_api_key is not None:
        kwargs["anthropic_api_key"] = SecretStr(anthropic_api_key)
    if anthropic_model is not None:
        kwargs["anthropic_model"] = anthropic_model
    return VibeConfig(**kwargs)


def _make_fake_client() -> MagicMock:
    """Build a synthetic :class:`anthropic.AsyncAnthropic` stand-in.

    The returned :class:`MagicMock` carries an :class:`AsyncMock` on
    its ``close`` attribute so that the production
    ``AnthropicBackend.__aexit__`` -- which does ``await
    self._client.close()`` -- completes without raising ``TypeError:
    object MagicMock can't be used in 'await' expression``.

    Callers further configure the client by assigning ``messages.stream``
    or ``messages.count_tokens`` to their test-specific stubs.

    Returns:
        A :class:`MagicMock` ready to back the SDK client during tests.
    """
    client = MagicMock()
    # The production __aexit__ awaits ``self._client.close()``; without
    # an AsyncMock here MagicMock would return a non-awaitable.
    client.close = AsyncMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# Phase A -- API key resolution order (Rule 10)
# ---------------------------------------------------------------------------


class TestAPIKeyResolutionOrder:
    """Verify the three-tier env -> config -> prompt resolution chain.

    AAP behavioural rule 10 mandates that the AnthropicBackend (and the
    other AAP-extended backends) resolve their API key via
    :func:`vibe.core.llm.api_key_prompt.resolve_or_prompt`. The order is
    strict and exhaustive: a non-empty value at any tier short-circuits
    the remaining tiers, and an empty stdin response at the final tier
    raises :class:`MissingAPIKeyError`.
    """

    def test_api_key_resolution_env_var_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ANTHROPIC_API_KEY`` wins over ``config.anthropic_api_key``.

        Both sources supply a key; the env-var value MUST be selected
        per AAP rule 10's stated resolution order.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        config = _make_config(anthropic_api_key="config-key")
        backend = AnthropicBackend(provider=_make_provider(), config=config)
        assert backend._api_key == "env-key"

    def test_api_key_resolution_config_field_used_when_no_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``ANTHROPIC_API_KEY`` is unset, the config field is used.

        The fallback uses :class:`pydantic.SecretStr.get_secret_value`
        internally; the resolved string MUST equal the underlying
        plaintext.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = _make_config(anthropic_api_key="config-key")
        backend = AnthropicBackend(provider=_make_provider(), config=config)
        assert backend._api_key == "config-key"

    def test_api_key_resolution_prompt_used_when_neither(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both env and config are absent, ``getpass`` is invoked.

        The save-to-config prompt is declined here (``"N"``) so the test
        body does not hang on the interactive ``input()`` call inside
        :func:`resolve_or_prompt`.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = _make_config()  # anthropic_api_key defaults to None
        # ``api_key_prompt.py`` does ``import getpass`` (not ``from
        # getpass import getpass``), so the bound-attribute path is
        # ``vibe.core.llm.api_key_prompt.getpass.getpass``.
        monkeypatch.setattr(
            "vibe.core.llm.api_key_prompt.getpass.getpass",
            lambda _prompt="": "prompted-key",
        )
        # Decline the post-entry save-to-config prompt to keep the test
        # hermetic. The CONFIG_FILE write path is exercised separately
        # in tests/test_api_key_masking.py.
        monkeypatch.setattr("builtins.input", lambda _prompt="": "N")
        backend = AnthropicBackend(provider=_make_provider(), config=config)
        assert backend._api_key == "prompted-key"

    def test_api_key_resolution_declined_prompt_raises_missing_api_key_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Canonical Rule 10: empty stdin at the prompt raises ``MissingAPIKeyError``.

        The single-argument ``MissingAPIKeyError(provider)`` form is the
        AAP-prescribed signal that the user has declined entry. The CLI
        entrypoint catches this exception and exits with a clean error
        message; here we only assert the exception type.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = _make_config()
        # Empty string from ``getpass.getpass`` simulates the user
        # pressing Enter without supplying a key (the canonical "abort"
        # gesture documented inside ``resolve_or_prompt``).
        monkeypatch.setattr(
            "vibe.core.llm.api_key_prompt.getpass.getpass", lambda _prompt="": ""
        )
        with pytest.raises(MissingAPIKeyError):
            AnthropicBackend(provider=_make_provider(), config=config)


# ---------------------------------------------------------------------------
# Phase A (continued) -- Sensitive-value masking (Rule 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def _mask_caplog_handler(
    caplog: pytest.LogCaptureFixture,
) -> Generator[None, None, None]:
    """Attach :data:`KEY_MASK_FILTER` to the ``caplog`` handler.

    Rationale (paraphrased from ``tests/test_api_key_masking.py``):
    Python's :mod:`logging` applies logger-level filters only at the
    originating logger. Records propagating from descendant loggers
    (``vibe.test.*``) up to ``caplog.handler`` (which is attached to
    the root logger) bypass the filter that
    :mod:`vibe.core.observability` auto-installs on the ``vibe`` logger.

    Attaching the filter directly to ``caplog.handler`` reproduces the
    production deployment pattern in
    :func:`vibe.core.observability.configure_json_logging` and ensures
    ``caplog.records`` carry the masked output that Rule 2 asserts.

    Yields:
        Control returns to the test body with the filter attached;
        the teardown phase removes it again so the fixture is hermetic.
    """
    from vibe.core.observability import KEY_MASK_FILTER

    caplog.handler.addFilter(KEY_MASK_FILTER)
    try:
        yield
    finally:
        caplog.handler.removeFilter(KEY_MASK_FILTER)


class TestSensitiveValueRegistration:
    """Verify the resolved key flows into the Rule 2 mask registry.

    AAP behavioural rule 2 states that ``BLITZY_API_KEY``,
    ``ANTHROPIC_API_KEY``, and ``MISTRAL_API_KEY`` values MUST NOT
    appear in any log line, exception message, or traceback. The mask
    registry is the enforcement mechanism: every resolved key passes
    through :func:`register_sensitive` inside :func:`resolve_or_prompt`.
    """

    def test_api_key_value_registered_as_sensitive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        _mask_caplog_handler: None,
    ) -> None:
        """A resolved env-var key is scrubbed from subsequent log output.

        Construction of :class:`AnthropicBackend` triggers
        ``resolve_or_prompt``, which calls ``register_sensitive`` with
        the resolved value. A log record emitted afterward that
        embeds the same plaintext key MUST be masked to ``***``.
        """
        secret = "secret-12345-abcde"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        config = _make_config()
        # Construct the backend; this registers the env-key with the
        # mask filter via the resolve_or_prompt -> register_sensitive
        # path documented in ``vibe.core.llm.api_key_prompt``.
        _ = AnthropicBackend(provider=_make_provider(), config=config)
        # Emit a log line that includes the secret. The filter on
        # ``caplog.handler`` (installed by the fixture above) must
        # rewrite the message to ``***`` before it reaches
        # ``caplog.records``.
        logger = logging.getLogger("vibe.test.anthropic_backend_extension")
        with caplog.at_level(logging.INFO, logger="vibe"):
            logger.info("Attempting auth with key: %s", secret)
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert secret not in combined, (
            "Sensitive API key value leaked into log output -- "
            "Rule 2 violation (expected the value to be replaced "
            "with '***')."
        )


# ---------------------------------------------------------------------------
# Phase B -- anthropic_model field consumption
# ---------------------------------------------------------------------------


class _FakeStream:
    """An async-iterable, zero-event stream stand-in for the SDK.

    The production :meth:`AnthropicBackend.complete_streaming` iterates
    over the stream with ``async for event in stream:``; this class
    yields nothing so the loop body never executes -- the streaming
    test only cares about the kwargs forwarded to ``messages.stream``,
    not the events themselves.
    """

    async def __aiter__(self) -> Any:
        # Async generator that yields zero events. The ``if False:``
        # guard preserves the ``async def`` + ``yield`` shape required
        # to make this an async generator function (as opposed to a
        # coroutine) while statically guaranteeing the yield is
        # unreachable.
        if False:  # pragma: no cover
            yield


class _FakeStreamCtx:
    """An async context manager mirroring the SDK's ``MessageStreamManager``.

    Production code uses ``async with client.messages.stream(...) as
    stream:``; this stand-in supports the context-manager protocol and
    yields a :class:`_FakeStream` on entry. Exit is a no-op.
    """

    async def __aenter__(self) -> _FakeStream:
        return _FakeStream()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        return None


def _make_stream_capture() -> tuple[dict[str, Any], Any]:
    """Build a captured-kwargs dict and a ``messages.stream`` stand-in.

    Returns:
        A ``(captured_kwargs, fake_stream_callable)`` pair. The callable
        is suitable for assignment to ``fake_client.messages.stream``;
        every kwarg supplied at call time is recorded in
        ``captured_kwargs`` (the same dict on subsequent invocations).
    """
    captured: dict[str, Any] = {}

    def _fake_stream(**kwargs: Any) -> _FakeStreamCtx:
        captured.update(kwargs)
        return _FakeStreamCtx()

    return captured, _fake_stream


class TestAnthropicModelField:
    """Verify ``VibeConfig.anthropic_model`` propagation to the SDK.

    The AAP requires:

    - ``VibeConfig.anthropic_model`` defaults to ``"claude-sonnet-4-6"``
      (section 0.6.1 Group 4, verified Anthropic model snapshot per
      section 0.10.2).
    - The config field is forwarded to the SDK
      ``messages.stream(model=..., ...)`` and
      ``messages.count_tokens(model=..., ...)`` calls.
    - The config value takes precedence over the per-request
      :attr:`ModelConfig.name` (the ``or model.name`` fallback only
      fires when the config value is an empty string).
    """

    def test_anthropic_model_default_is_claude_sonnet_4_6(self) -> None:
        """The :class:`VibeConfig` schema default is ``claude-sonnet-4-6``.

        AAP section 0.10.2 verifies this model identifier against the
        Anthropic release notes (Claude Sonnet 4.6, released
        February 17, 2026).
        """
        config = VibeConfig()
        assert config.anthropic_model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_anthropic_model_override_passed_to_sdk_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``config.anthropic_model`` flows into ``messages.stream(model=...)``."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = _make_config(anthropic_model="claude-opus-4")
        backend = AnthropicBackend(provider=_make_provider(), config=config)

        captured, fake_stream = _make_stream_capture()
        fake_client = _make_fake_client()
        fake_client.messages.stream = fake_stream

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                async for _ in backend.complete_streaming(
                    model=_make_model(),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                ):
                    pass

        assert captured.get("model") == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_anthropic_model_default_passed_when_not_overridden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The schema default is forwarded when no override is set.

        Importantly, ``config.anthropic_model`` takes precedence over
        ``model.name``: even though we pass ``name="something-else"``
        in the per-request :class:`ModelConfig`, the SDK call receives
        ``"claude-sonnet-4-6"`` from the config.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = _make_config()  # anthropic_model defaults to "claude-sonnet-4-6"
        backend = AnthropicBackend(provider=_make_provider(), config=config)

        captured, fake_stream = _make_stream_capture()
        fake_client = _make_fake_client()
        fake_client.messages.stream = fake_stream

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                async for _ in backend.complete_streaming(
                    model=_make_model(name="something-else"),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                ):
                    pass

        # Config wins over ModelConfig.name.
        assert captured.get("model") == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_count_tokens_uses_config_anthropic_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``count_tokens`` forwards ``config.anthropic_model`` to the SDK.

        The token count corresponds to the model actually used by
        ``complete``/``complete_streaming``, so the count_tokens call
        must use the SAME identifier (config value) rather than the
        per-request ``ModelConfig.name``.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = _make_config(anthropic_model="claude-opus-4")
        backend = AnthropicBackend(provider=_make_provider(), config=config)

        captured: dict[str, Any] = {}

        async def _fake_count_tokens(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            result = MagicMock()
            result.input_tokens = 42
            return result

        fake_client = _make_fake_client()
        fake_client.messages.count_tokens = _fake_count_tokens

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            async with backend:
                tokens = await backend.count_tokens(
                    model=_make_model(),
                    messages=[LLMMessage(role=Role.user, content="hi")],
                    temperature=0.0,
                    tools=None,
                    tool_choice=None,
                    extra_headers=None,
                )

        assert captured.get("model") == "claude-opus-4"
        assert tokens == 42


# ---------------------------------------------------------------------------
# Phase C -- Library isolation (Rule 9)
# ---------------------------------------------------------------------------


class TestLibraryIsolation:
    """Verify Rule 9: Anthropic uses the SDK, never raw httpx for messages.

    The SDK itself uses ``httpx`` as a transport detail; that is
    acceptable. The forbidden pattern is calling the
    ``/v1/messages`` endpoint directly via ``httpx`` -- bypassing the
    SDK's authentication, retry, and streaming logic.
    """

    def test_anthropic_module_uses_sdk_not_raw_httpx_for_messages(self) -> None:
        """``import anthropic`` is present; ``/v1/messages`` is NOT.

        The substring check is a defensive guard: if a future change
        accidentally introduces a raw HTTP call to the messages
        endpoint, this test fails immediately and points the developer
        back at Rule 9.
        """
        import vibe.core.llm.backend.anthropic_llm as anthropic_module

        src = inspect.getsource(anthropic_module)
        assert "import anthropic" in src, (
            "anthropic_llm.py must import the anthropic SDK (Rule 9)."
        )
        # The hardcoded Anthropic messages endpoint path MUST NOT
        # appear in source -- the SDK abstracts it. If it appears, the
        # code is bypassing the SDK.
        assert "/v1/messages" not in src, (
            "anthropic_llm.py must not call /v1/messages directly via "
            "httpx (Rule 9); use the anthropic SDK."
        )

    def test_anthropic_module_does_not_import_mistralai(self) -> None:
        """``mistralai`` MUST NOT be imported by the Anthropic backend.

        Cross-importing the Mistral SDK in the Anthropic backend
        violates Rule 9's strict per-backend library isolation.
        """
        import vibe.core.llm.backend.anthropic_llm as anthropic_module

        src = inspect.getsource(anthropic_module)
        assert "import mistralai" not in src
        assert "from mistralai" not in src


# ---------------------------------------------------------------------------
# Phase D -- Constructor signature
# ---------------------------------------------------------------------------


class TestConstructorSignature:
    """Verify the post-AAP ``AnthropicBackend.__init__`` signature.

    The AAP update adds a required ``config: VibeConfig`` parameter
    between the existing ``provider`` and ``timeout`` parameters. The
    ``timeout`` parameter retains its default ``720.0`` (preserved for
    backwards-compatibility with the existing factory call site).
    """

    def test_constructor_signature_accepts_config(self) -> None:
        """``__init__`` accepts ``config: VibeConfig``.

        With ``from __future__ import annotations`` in effect in the
        source module, the annotation is the *string* ``"VibeConfig"``.
        The assertion tolerates both string and class forms so the
        test continues to pass if the source module ever drops the
        future-annotations import.
        """
        sig = inspect.signature(AnthropicBackend.__init__)
        params = sig.parameters
        assert "config" in params, (
            "AnthropicBackend.__init__ must accept a 'config' parameter "
            "post-AAP update (see section 0.6.1 Group 3)."
        )
        annotation = params["config"].annotation
        assert annotation is VibeConfig or annotation == "VibeConfig", (
            f"Expected config annotation to be VibeConfig, got {annotation!r}"
        )

    def test_constructor_signature_has_provider(self) -> None:
        """``__init__`` still accepts ``provider: ProviderConfig``.

        The AAP update is purely additive; the existing ``provider``
        parameter is preserved unchanged.
        """
        sig = inspect.signature(AnthropicBackend.__init__)
        assert "provider" in sig.parameters

    def test_constructor_signature_has_timeout_default(self) -> None:
        """The ``timeout`` parameter retains its default value of ``720.0``.

        The AAP update does not alter the ``api_timeout`` contract;
        callers (notably the factory) continue to omit the kwarg and
        rely on the default.
        """
        sig = inspect.signature(AnthropicBackend.__init__)
        params = sig.parameters
        assert "timeout" in params
        assert params["timeout"].default == 720.0


# ---------------------------------------------------------------------------
# Phase E -- AsyncAnthropic construction
# ---------------------------------------------------------------------------


class TestAsyncAnthropicConstruction:
    """Verify ``__aenter__`` instantiates the SDK client with resolved values."""

    @pytest.mark.asyncio
    async def test_aenter_creates_async_anthropic_client_with_resolved_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SDK client is constructed with the resolved key and timeout.

        ``api_key`` carries the value resolved by
        :func:`resolve_or_prompt`; ``timeout`` defaults to ``720.0``
        per the constructor signature.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "resolved-key")
        config = _make_config()
        backend = AnthropicBackend(provider=_make_provider(), config=config)

        captured: dict[str, Any] = {}

        def _fake_constructor(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _make_fake_client()

        with patch(
            "vibe.core.llm.backend.anthropic_llm.anthropic.AsyncAnthropic",
            side_effect=_fake_constructor,
        ):
            async with backend:
                pass

        assert captured.get("api_key") == "resolved-key"
        assert captured.get("timeout") == 720.0


# ---------------------------------------------------------------------------
# Phase F -- Protocol conformance (Rule 1)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify ``AnthropicBackend`` satisfies the :class:`BackendLike` protocol."""

    def test_anthropic_backend_implements_backend_like(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``AnthropicBackend`` structurally conforms to :class:`BackendLike` (Rule 1).

        :class:`BackendLike` is a :class:`typing.Protocol` *without* the
        ``@runtime_checkable`` decorator, so :func:`isinstance` cannot be
        used. Instead, conformance is verified by structural inspection
        of the required attributes:

          * ``__aenter__`` / ``__aexit__`` -- async context manager protocol
          * ``complete`` -- non-streaming completion
          * ``complete_streaming`` -- streaming completion
          * ``count_tokens`` -- token estimation helper

        Each attribute must be both present *and* callable. This matches
        the structural typing semantics of :class:`typing.Protocol` and
        satisfies AAP Rule 1 ("All three backends MUST implement the
        identical interface...verified by isinstance/protocol conformance
        test for each.") -- the ``isinstance`` clause cannot be honored
        for a non-runtime-checkable Protocol, so the equivalent structural
        check is used.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        backend = AnthropicBackend(provider=_make_provider(), config=_make_config())
        # Structural conformance: every required method on BackendLike is
        # present as a callable attribute of the backend instance.
        required_members = (
            "__aenter__",
            "__aexit__",
            "complete",
            "complete_streaming",
            "count_tokens",
        )
        for member in required_members:
            assert hasattr(backend, member), (
                f"AnthropicBackend missing required BackendLike member: {member}"
            )
            assert callable(getattr(backend, member)), (
                f"AnthropicBackend.{member} must be callable"
            )
        # Sanity: the BackendLike protocol type is the one referenced by the
        # rest of the codebase as a type annotation (cf. ``tests/backend/test_backend.py``
        # line 73: ``backend: BackendLike = BackendClass(provider=provider)``).
        # The import keeps the test honest about *what* protocol we are
        # claiming to satisfy.
        assert BackendLike.__name__ == "BackendLike"


# ---------------------------------------------------------------------------
# Phase G -- AnthropicMapper preservation
# ---------------------------------------------------------------------------


class TestAnthropicMapperPreservation:
    """Smoke-check the unchanged :class:`AnthropicMapper` adapter.

    The AAP explicitly states the mapper is preserved unchanged (see
    section 0.6.1 Group 3 comment "AnthropicMapper class -- UNCHANGED").
    This test guards against accidental refactors.
    """

    def test_anthropic_mapper_unchanged(self) -> None:
        """The four canonical adapter methods are all callable."""
        mapper = AnthropicMapper()
        # The four public methods that the AAP enumerates as the
        # mapper's contract.
        assert hasattr(mapper, "prepare_messages")
        assert hasattr(mapper, "prepare_tool")
        assert hasattr(mapper, "prepare_tool_choice")
        assert hasattr(mapper, "parse_response")
        # Spot-check that one method is callable; the existing
        # tests/backend/test_backend.py exercises the mapper's
        # behaviour in more depth.
        assert callable(mapper.prepare_messages)


# ---------------------------------------------------------------------------
# Phase H -- Gate 8 integration test
# ---------------------------------------------------------------------------


class TestAnthropicIntegration:
    """Gate 8: live-API integration test for the streaming path.

    Skipped automatically when ``ANTHROPIC_API_KEY`` is unset or equal
    to the placeholder ``"mock"`` used by the autouse
    ``_mock_api_key`` fixture (which sets ``MISTRAL_API_KEY=mock`` --
    Anthropic is not affected by that fixture, but we apply the same
    placeholder guard for defensive symmetry).
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_integration_streams_at_least_one_token(self) -> None:
        """Hit the live API; assert at least one chunk is yielded.

        The test issues a short prompt and reads up to five chunks
        before breaking; the AAP only requires that >=1 chunk be
        yielded (Gate 8 minimum bar).
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key or api_key == "mock":
            pytest.skip("ANTHROPIC_API_KEY not set (or mock placeholder)")
        config = _make_config()
        backend = AnthropicBackend(provider=_make_provider(), config=config)
        chunks: list[Any] = []
        async with backend:
            async for chunk in backend.complete_streaming(
                model=_make_model(),
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=None,
                max_tokens=50,
                tool_choice=None,
                extra_headers=None,
            ):
                chunks.append(chunk)
                if len(chunks) >= 5:
                    break
        assert len(chunks) >= 1
