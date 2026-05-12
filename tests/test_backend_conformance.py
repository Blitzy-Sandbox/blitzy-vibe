"""Protocol conformance tests for every registered LLM backend.

This file is the canonical Agent Action Plan (AAP) **Validation Gate 1**
realization (AAP section 0.9.1): every backend registered in
:data:`vibe.core.llm.backend.factory.BACKEND_FACTORY` is verified to
implement the :class:`vibe.core.llm.types.BackendLike` protocol surface.

Verbatim AAP rule 1 (section 0.8.1):

    "All three backends MUST implement the identical interface in
    ``vibe/core/llm/``; verified by isinstance/protocol conformance test
    for each."

Verbatim AAP Gate 1 (section 0.9.1):

    "All three backends pass isinstance/protocol conformance test; all
    abstract methods implemented; one test per backend."

The "three backends" the rule names are Blitzy, Mistral, and Anthropic.
This file parameterizes over ALL ``BACKEND_FACTORY`` entries (currently
five: Blitzy, Mistral, Generic, Anthropic, Claude Code) so that any
future registration is automatically conformance-checked without code
edits.

Structural-conformance rationale
================================

The :class:`vibe.core.llm.types.BackendLike` :class:`typing.Protocol`
is intentionally NOT decorated with :func:`typing.runtime_checkable`
(see ``vibe/core/llm/types.py`` -- preservation boundary; AAP section
0.2.2 forbids touching the LLM backend interface). A raw
``isinstance(backend, BackendLike)`` therefore raises ``TypeError:
Instance and class checks can only be used with @runtime_checkable
protocols``.

This module satisfies Gate 1 via *structural* conformance:

- ``__aenter__`` / ``__aexit__`` / ``complete`` / ``count_tokens`` MUST
  be ``async def`` coroutine functions (per
  :class:`BackendLike` typing).
- ``complete_streaming`` MUST be an ``async def`` async-generator
  factory (per the ``BackendLike.complete_streaming`` docstring at
  ``vibe/core/llm/types.py:L58-L60``: the method body is an
  async-generator, even though the function's ``def`` signature has no
  ``async`` keyword for typing reasons explained in the StackOverflow
  link cited there).
- Each method's signature MUST expose every keyword the protocol
  declares.

If a future PR adds ``@runtime_checkable`` to ``BackendLike``, the
``test_backend_isinstance_check`` test will tighten to a direct
``isinstance`` assertion automatically (it inspects
``_is_runtime_protocol`` and degrades gracefully otherwise).

All conftest autouse fixtures (``tmp_working_directory``,
``config_dir``, ``_unlock_config_paths``, ``_mock_api_key``,
``_mock_platform``, ``_mock_update_commands``) are inherited from
``tests/conftest.py`` and require no explicit import here.
"""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest
import respx

from vibe.core.config import Backend, ProviderConfig, VibeConfig
from vibe.core.llm.backend.factory import (
    _PROVIDER_STRING_TO_BACKEND,
    BACKEND_FACTORY,
    provider_string_to_backend,
)
from vibe.core.llm.types import BackendLike

# ---------------------------------------------------------------------------
# Phase A: Helpers
# ---------------------------------------------------------------------------
# Helper factories construct minimal, valid instances of the dependencies
# that backend ``__init__`` methods require. Each helper documents the
# specific shape it provides and why that shape satisfies every backend in
# ``BACKEND_FACTORY``.


def _make_dummy_provider(
    name: str = "test_provider", api_base: str = "https://api.example.com/v1"
) -> ProviderConfig:
    """Construct a minimal :class:`ProviderConfig` valid for ALL backends.

    The ``api_base`` is intentionally ``"https://api.example.com/v1"`` --
    the ``/v1`` suffix is required by :class:`MistralBackend`'s
    ``url_pattern = r"(https?://[^/]+)(/v.*)"`` validation. URLs without
    a ``/v<version>`` suffix fail Mistral construction with
    ``ValueError: Invalid API base URL``. The same shape is accepted by
    every other backend (Blitzy, Generic, Anthropic, Claude Code) which
    do not enforce any URL shape at construction time.

    The ``api_key_env_var`` is set to ``"TEST_API_KEY"`` so that the
    shared :func:`vibe.core.llm.api_key_prompt.resolve_or_prompt` (used
    by :class:`BlitzyLLMBackend` and :class:`AnthropicBackend`) finds the
    env value stubbed by :func:`_stub_provider_env_vars` and short-circuits
    at the env-var tier without triggering an interactive prompt.

    Args:
        name: The provider's logical name. Defaults to ``"test_provider"``.
        api_base: The provider's API base URL. Defaults to a Mistral-valid
            ``"https://api.example.com/v1"``.

    Returns:
        A :class:`ProviderConfig` ready to pass into any backend ``__init__``.
    """
    return ProviderConfig(name=name, api_base=api_base, api_key_env_var="TEST_API_KEY")


def _make_dummy_config() -> VibeConfig:
    """Construct a minimal :class:`VibeConfig` for backend instantiation.

    The conftest's autouse ``config_dir`` fixture has already initialized
    the temporary ``~/.vibe/config.toml`` and patched
    ``global_paths._DEFAULT_VIBE_HOME`` to point at it. A bare
    ``VibeConfig()`` therefore reads the conftest-managed base config
    (with the default ``mistral`` provider) plus this module's autouse
    env-var stubs.

    Returns:
        A fresh :class:`VibeConfig` instance suitable for handing off to
        :class:`BlitzyLLMBackend` and :class:`AnthropicBackend` (both of
        which require a ``config`` positional argument after the AAP
        extension).
    """
    return VibeConfig()


def _instantiate_backend(backend_cls: type) -> Any:
    """Construct a backend instance with introspected dummy arguments.

    Each backend in :data:`BACKEND_FACTORY` has a slightly different
    constructor signature. As of the AAP delivery the signatures are:

    - :class:`MistralBackend`: ``(provider, timeout=720.0)``
    - :class:`GenericBackend`: ``(*, client=None, provider, timeout=720.0)``
    - :class:`AnthropicBackend`: ``(provider, config, timeout=720.0)``
    - :class:`ClaudeCodeBackend`: ``(provider, timeout=720.0)``
    - :class:`BlitzyLLMBackend`: ``(provider, config, *, repo="", branch="")``

    This helper inspects the ``__init__`` signature and supplies
    ``provider`` and (optionally) ``config`` -- every other parameter
    has a default, so the call is well-formed regardless of the future
    addition of new backends with new optional keyword arguments.

    The helper does NOT enter the backend as a context manager; the
    Phase E lifecycle test does that explicitly under
    ``@pytest.mark.asyncio``.

    Args:
        backend_cls: The backend class to instantiate (a value from
            ``BACKEND_FACTORY.values()``).

    Returns:
        A constructed backend instance. The instance is fully initialized
        but not yet entered as an async context manager.
    """
    sig = inspect.signature(backend_cls.__init__)
    params = sig.parameters
    kwargs: dict[str, Any] = {}
    if "provider" in params:
        kwargs["provider"] = _make_dummy_provider()
    if "config" in params:
        kwargs["config"] = _make_dummy_config()
    return backend_cls(**kwargs)


# ---------------------------------------------------------------------------
# Phase B: Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_provider_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub provider-specific env vars to skip the interactive prompt tier.

    AAP rule 10 mandates that :func:`resolve_or_prompt` raises
    :class:`MissingAPIKeyError` when the user declines the interactive
    prompt. Pytest runs in a non-interactive context, so a triggered
    ``getpass.getpass`` call would hang the test (no TTY, no EOF, no
    pre-supplied input).

    The conftest's autouse ``_mock_api_key`` fixture only sets
    ``MISTRAL_API_KEY=mock``; this module-scoped fixture additionally
    stubs:

    - ``TEST_API_KEY`` -- referenced by :func:`_make_dummy_provider` so
      tier 1 of :func:`resolve_or_prompt` finds a non-empty value when
      the backend's ``api_key_env_var`` is ``"TEST_API_KEY"``.
    - ``BLITZY_API_KEY`` -- the canonical Blitzy env var; the
      :class:`BlitzyLLMBackend` constructor falls back to this name when
      the provider config's ``api_key_env_var`` is empty.
    - ``ANTHROPIC_API_KEY`` -- the canonical Anthropic env var; the
      :class:`AnthropicBackend` constructor's
      :func:`resolve_or_prompt` call uses the same fallback pattern.
    - ``MISTRAL_API_KEY`` -- re-asserted (it's already set by conftest)
      for symmetry and self-documentation; ``monkeypatch.setenv`` is
      idempotent.

    Args:
        monkeypatch: Pytest's MonkeyPatch utility; env-var changes are
            automatically rolled back at test teardown.
    """
    monkeypatch.setenv("TEST_API_KEY", "test-dummy")
    monkeypatch.setenv("BLITZY_API_KEY", "blitzy-dummy")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-dummy")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-dummy")


@pytest.fixture(params=list(BACKEND_FACTORY.items()), ids=lambda p: p[0].value)
def backend_factory_entry(request: pytest.FixtureRequest) -> tuple[Backend, type]:
    """Yield each ``(Backend, backend_class)`` pair from BACKEND_FACTORY.

    Pytest's parametrize machinery converts ``BACKEND_FACTORY.items()``
    into one test invocation per registered backend, with the test ID
    being the lowercase ``Backend`` enum value (e.g.,
    ``test_backend_has_complete_method[blitzy]``).

    This matches AAP Gate 1's "one test per backend" requirement: each
    structural-conformance test runs once per registered backend, so the
    test suite output enumerates exactly which backend (by name) passed
    or failed each check.

    Args:
        request: The Pytest :class:`pytest.FixtureRequest` carrying the
            current parameter from the ``params`` list.

    Returns:
        A tuple ``(backend_enum_member, backend_class)``.
    """
    return request.param


# ---------------------------------------------------------------------------
# Phase C: Backend Factory Discovery
# ---------------------------------------------------------------------------
# These tests verify the SHAPE of ``BACKEND_FACTORY`` itself -- the
# registry must contain the rule-1 trio and must map each ``Backend``
# enum value to a distinct class.


def test_backend_factory_contains_blitzy_entry() -> None:
    """``Backend.BLITZY`` MUST appear in ``BACKEND_FACTORY``.

    This is the gate that catches a missing factory registration -- if a
    future refactor removes the Blitzy entry from
    ``vibe/core/llm/backend/factory.py`` while the rest of the AAP
    machinery (provider picker, session JSON ``provider`` field,
    ``--provider blitzy``) still references it, the inconsistency would
    cause a ``KeyError`` at runtime on the first lookup. This test
    surfaces the inconsistency in the test suite instead.
    """
    assert Backend.BLITZY in BACKEND_FACTORY


def test_backend_factory_contains_all_required_backends() -> None:
    """``BACKEND_FACTORY`` MUST cover the AAP rule-1 trio at minimum.

    AAP rule 1 (verbatim): "All three backends MUST implement the
    identical interface in ``vibe/core/llm/``". The "three backends" are
    Blitzy, Mistral, and Anthropic. Additional entries (Generic,
    Claude Code) are acceptable and do not violate the rule.

    The assertion uses a subset check rather than equality so that the
    factory may add new providers in the future without breaking this
    test.
    """
    required = {Backend.BLITZY, Backend.MISTRAL, Backend.ANTHROPIC}
    assert required.issubset(BACKEND_FACTORY.keys()), (
        f"BACKEND_FACTORY missing required backends. "
        f"Required: {sorted(b.value for b in required)}; "
        f"Present: {sorted(b.value for b in BACKEND_FACTORY)}"
    )


def test_backend_factory_value_classes_are_distinct() -> None:
    """Each registered backend class MUST be a distinct type.

    No two ``Backend`` enum members should map to the same backend
    class -- if a refactor accidentally aliased two enum values to the
    same class (e.g., copy-paste in the dict literal), the agent's
    provider-selection plumbing would silently degrade because a
    provider would dispatch to the wrong backend implementation.
    """
    classes = list(BACKEND_FACTORY.values())
    assert len(classes) == len(set(classes)), (
        f"BACKEND_FACTORY contains duplicate backend classes: {classes}"
    )


# ---------------------------------------------------------------------------
# Phase D: Protocol Conformance (Rule 1, Gate 1)
# ---------------------------------------------------------------------------
# Per the module docstring, ``BackendLike`` is NOT decorated with
# ``@runtime_checkable``, so a bare ``isinstance(backend, BackendLike)``
# raises ``TypeError``. We therefore verify conformance STRUCTURALLY:
# every method declared on the Protocol must exist on the backend with
# the correct kind (coroutine function or async-generator function) and
# expose every keyword the Protocol declares.
#
# The single ``test_backend_isinstance_check`` test below additionally
# performs the literal isinstance assertion IF ``BackendLike`` is
# runtime-checkable -- this future-proofs the suite so it tightens
# automatically when/if the Protocol decoration is added.


def _is_runtime_checkable(protocol_cls: type) -> bool:
    """Return True if ``protocol_cls`` is decorated with ``@runtime_checkable``.

    Python's ``typing.Protocol`` machinery sets the private attribute
    ``_is_runtime_protocol`` to ``True`` on the class when
    :func:`typing.runtime_checkable` decorates it. Reading the attribute
    via :func:`getattr` with a ``False`` default tolerates both branches
    (decorated and undecorated) without import-time errors.

    Args:
        protocol_cls: The Protocol class to inspect.

    Returns:
        ``True`` if the Protocol is runtime-checkable; ``False`` otherwise.
    """
    return bool(getattr(protocol_cls, "_is_runtime_protocol", False))


def test_backend_isinstance_check(backend_factory_entry: tuple[Backend, type]) -> None:
    """``isinstance(backend, BackendLike)`` MUST hold when runtime-checkable.

    This test honours AAP Gate 1's verbatim wording
    ("isinstance/protocol conformance test") whenever
    :class:`BackendLike` carries the ``@runtime_checkable`` decoration.

    When the Protocol is NOT runtime-checkable (the current state, per
    the module docstring), this test is skipped with a clear message;
    the structural-conformance tests below carry the conformance burden.

    Args:
        backend_factory_entry: The ``(Backend, backend_class)`` pair
            currently under test (one invocation per registered backend).
    """
    if not _is_runtime_checkable(BackendLike):
        pytest.skip(
            "BackendLike is not @runtime_checkable; "
            "structural conformance is verified by the remaining tests"
        )
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    # pyright cannot statically prove ``BackendLike`` is runtime-checkable
    # here because the guard above is a runtime check. Suppress the
    # warning explicitly; the ``_is_runtime_checkable`` guard is the
    # actual safety net.
    assert isinstance(backend, BackendLike), (  # pyright: ignore[reportArgumentType]
        f"{backend_cls.__name__} does not satisfy BackendLike protocol"
    )


def test_backend_can_be_instantiated(
    backend_factory_entry: tuple[Backend, type],
) -> None:
    """Each backend class MUST be constructible from a dummy provider/config.

    A backend that cannot be instantiated cannot conform to the
    Protocol -- the test fails fast with a clear ``__init__``-side
    error so the developer learns *why* construction failed (rather
    than seeing a downstream ``AttributeError`` in a later test).
    """
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    assert backend is not None
    assert isinstance(backend, backend_cls)


def test_backend_has_aenter_method(backend_factory_entry: tuple[Backend, type]) -> None:
    """``__aenter__`` MUST be defined as an ``async def`` coroutine function.

    Per ``BackendLike.__aenter__`` at ``vibe/core/llm/types.py:L20``,
    the method MUST be ``async def``. :func:`inspect.iscoroutinefunction`
    returns ``True`` for ``async def`` methods at the class level
    (bound to a class) and at the instance level (bound to an instance);
    we use the bound-method form for clarity.
    """
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    assert hasattr(backend, "__aenter__"), (
        f"{backend_cls.__name__} is missing __aenter__"
    )
    assert inspect.iscoroutinefunction(backend.__aenter__), (
        f"{backend_cls.__name__}.__aenter__ is not an async coroutine function"
    )


def test_backend_has_aexit_method(backend_factory_entry: tuple[Backend, type]) -> None:
    """``__aexit__`` MUST be ``async def`` and accept the three exception params.

    Per ``BackendLike.__aexit__`` at ``vibe/core/llm/types.py:L21-L26``,
    the method MUST accept three positional parameters after ``self``:
    ``exc_type``, ``exc_val``, ``exc_tb``. Python's
    :keyword:`async with` machinery passes these arguments by position
    when leaving the context manager.
    """
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    assert hasattr(backend, "__aexit__"), f"{backend_cls.__name__} is missing __aexit__"
    assert inspect.iscoroutinefunction(backend.__aexit__), (
        f"{backend_cls.__name__}.__aexit__ is not an async coroutine function"
    )
    sig = inspect.signature(backend.__aexit__)
    params = list(sig.parameters)
    # Bound-method signature: exc_type, exc_val, exc_tb (no ``self``).
    assert len(params) == 3, (
        f"{backend_cls.__name__}.__aexit__ signature expects 3 params "
        f"(exc_type, exc_val, exc_tb); got {params}"
    )


def test_backend_has_complete_method(
    backend_factory_entry: tuple[Backend, type],
) -> None:
    """``complete`` MUST be ``async def`` and expose every protocol keyword.

    Per ``BackendLike.complete`` at ``vibe/core/llm/types.py:L28-L56``,
    the method MUST accept seven keyword-only parameters:
    ``model``, ``messages``, ``temperature``, ``tools``,
    ``max_tokens``, ``tool_choice``, ``extra_headers``.
    Each backend's ``complete`` may add OPTIONAL parameters with
    defaults, but it MUST NOT remove or rename any of the seven.
    """
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    assert hasattr(backend, "complete"), f"{backend_cls.__name__} is missing complete"
    assert inspect.iscoroutinefunction(backend.complete), (
        f"{backend_cls.__name__}.complete is not an async coroutine function"
    )
    required_kwargs = {
        "model",
        "messages",
        "temperature",
        "tools",
        "max_tokens",
        "tool_choice",
        "extra_headers",
    }
    sig = inspect.signature(backend.complete)
    actual_params = set(sig.parameters)
    missing = required_kwargs - actual_params
    assert not missing, (
        f"{backend_cls.__name__}.complete is missing required parameters: "
        f"{sorted(missing)}"
    )


def test_backend_has_complete_streaming_method(
    backend_factory_entry: tuple[Backend, type],
) -> None:
    """``complete_streaming`` MUST be an async-generator with protocol kwargs.

    Per ``BackendLike.complete_streaming`` at
    ``vibe/core/llm/types.py:L58-L90``, the method body is an
    async-generator yielding :class:`LLMChunk` instances. The function
    signature is intentionally declared without an ``async`` keyword to
    work around Python's type-inference of async-generator return types
    (per the StackOverflow link in the protocol docstring).

    :func:`inspect.isasyncgenfunction` returns ``True`` for any
    ``async def`` function whose body contains a ``yield`` -- i.e.,
    every concrete implementation of the protocol method.

    The same seven keyword parameters as ``complete`` are required:
    ``model``, ``messages``, ``temperature``, ``tools``,
    ``max_tokens``, ``tool_choice``, ``extra_headers``.
    """
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    assert hasattr(backend, "complete_streaming"), (
        f"{backend_cls.__name__} is missing complete_streaming"
    )
    # Each concrete implementation should be an ``async def`` with a
    # ``yield`` in its body -- ``inspect.isasyncgenfunction`` is the
    # canonical check. Per the protocol docstring at
    # ``vibe/core/llm/types.py:L58-L60``, this is the expected shape
    # (NOT ``inspect.iscoroutinefunction``).
    assert inspect.isasyncgenfunction(backend.complete_streaming), (
        f"{backend_cls.__name__}.complete_streaming is not an async-generator function"
    )
    required_kwargs = {
        "model",
        "messages",
        "temperature",
        "tools",
        "max_tokens",
        "tool_choice",
        "extra_headers",
    }
    sig = inspect.signature(backend.complete_streaming)
    actual_params = set(sig.parameters)
    missing = required_kwargs - actual_params
    assert not missing, (
        f"{backend_cls.__name__}.complete_streaming is missing required parameters: "
        f"{sorted(missing)}"
    )


def test_backend_has_count_tokens_method(
    backend_factory_entry: tuple[Backend, type],
) -> None:
    """``count_tokens`` MUST be ``async def`` and expose every protocol kwarg.

    Per ``BackendLike.count_tokens`` at ``vibe/core/llm/types.py:L92-L120``,
    the method MUST accept six keyword parameters:
    ``model``, ``messages``, ``temperature`` (with default ``0.0``),
    ``tools``, ``tool_choice`` (with default ``None``),
    ``extra_headers``.

    The estimator's return type is ``int``; the implementation may be
    static (e.g., ``len(json.dumps(messages)) // 4`` for Blitzy) or
    network-based (e.g., Anthropic SDK's ``messages.count_tokens``).
    """
    _backend_enum, backend_cls = backend_factory_entry
    backend = _instantiate_backend(backend_cls)
    assert hasattr(backend, "count_tokens"), (
        f"{backend_cls.__name__} is missing count_tokens"
    )
    assert inspect.iscoroutinefunction(backend.count_tokens), (
        f"{backend_cls.__name__}.count_tokens is not an async coroutine function"
    )
    required_kwargs = {
        "model",
        "messages",
        "temperature",
        "tools",
        "tool_choice",
        "extra_headers",
    }
    sig = inspect.signature(backend.count_tokens)
    actual_params = set(sig.parameters)
    missing = required_kwargs - actual_params
    assert not missing, (
        f"{backend_cls.__name__}.count_tokens is missing required parameters: "
        f"{sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Phase E: Async Context Manager Lifecycle (Smoke)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_can_be_entered_and_exited(
    backend_factory_entry: tuple[Backend, type],
) -> None:
    """Each backend MUST support ``async with backend: ...`` without raising.

    This is a structural smoke check for the
    :class:`AsyncContextManager` half of the :class:`BackendLike`
    Protocol. The full handshake of every backend is exercised by
    dedicated test files (``test_blitzy_backend.py``,
    ``test_anthropic_backend_extension.py``, etc.); this test just
    verifies that the context manager round-trip completes without
    raising for ANY registered backend.

    :class:`BlitzyLLMBackend` performs a one-shot ``GET /context`` HTTP
    call inside :meth:`BlitzyLLMBackend.__aenter__` -- AAP rule 8 -- so
    Blitzy is special-cased: a :mod:`respx` mock intercepts the call
    against the canonical ``https://api.blitzy.com`` base URL and
    returns ``200`` + ``{"connected": true}`` (the happy-path branch).
    For every other backend, ``__aenter__`` only constructs the
    underlying SDK client (no network I/O), so no mocking is required.

    Branching on the enum value (``Backend.BLITZY``) rather than the
    class name (``"BlitzyLLMBackend"``) is intentional -- it keeps the
    coupling at the registry layer and survives any future renaming
    of the concrete class.

    Args:
        backend_factory_entry: The ``(Backend, backend_class)`` pair
            currently under test.
    """
    backend_enum, backend_cls = backend_factory_entry
    if backend_enum is Backend.BLITZY:
        # Use the canonical Blitzy api_base so the URL the backend
        # constructs (``{api_base}/context?...``) matches the respx
        # mock route literally.
        with respx.mock(base_url="https://api.blitzy.com") as mock_api:
            mock_api.get("/context").mock(
                return_value=httpx.Response(status_code=200, json={"connected": True})
            )
            backend = backend_cls(
                provider=_make_dummy_provider(api_base="https://api.blitzy.com"),
                config=_make_dummy_config(),
            )
            async with backend:
                # Body of ``async with`` is intentionally empty: the
                # check is just "does the round-trip complete without
                # raising?". Any error raised by either ``__aenter__``
                # or ``__aexit__`` will fail the test with an unraised
                # exception; pytest's traceback points to the source.
                pass
    else:
        backend = _instantiate_backend(backend_cls)
        async with backend:
            pass


# ---------------------------------------------------------------------------
# Phase F: Provider String Set Consistency (Rule 13)
# ---------------------------------------------------------------------------
# AAP rule 13: the ``--provider`` accepted values, ``BACKEND_FACTORY``
# string keys, session JSON ``provider`` values, and ``VibeConfig``
# ``*_api_key`` field suffixes MUST be exactly the same lowercase set.
# These tests pin the invariants visible from this test file's scope
# (the factory map keys and the string-to-enum helper).


def test_backend_enum_values_include_rule1_trio() -> None:
    """The ``BACKEND_FACTORY`` keys' string values MUST include the trio.

    AAP rule 1 names Blitzy, Mistral, and Anthropic as the three
    backends that must implement the identical interface. AAP rule 13
    further requires that the lowercase string set ``{"blitzy",
    "mistral", "anthropic"}`` be consistently referenced across every
    site that names a provider (``--provider``, session JSON,
    ``[context_limits]`` table, etc.).

    This test pins one of those sites: the factory map's enum-value
    strings.
    """
    expected = {"blitzy", "mistral", "anthropic"}
    actual = {b.value for b in BACKEND_FACTORY.keys()}
    assert expected.issubset(actual), (
        f"BACKEND_FACTORY enum values missing AAP rule-1 trio. "
        f"Expected subset: {sorted(expected)}; got: {sorted(actual)}"
    )


def test_provider_string_to_backend_maps_blitzy() -> None:
    """``provider_string_to_backend("blitzy") == Backend.BLITZY``.

    The string-to-enum helper is the canonical AAP rule-13 translator
    used by ``vibe/cli/entrypoint.py`` to convert the lowercase
    ``--provider blitzy`` argparse string into the
    :class:`Backend.BLITZY` enum member that keys
    :data:`BACKEND_FACTORY`.
    """
    assert provider_string_to_backend("blitzy") is Backend.BLITZY


def test_provider_string_to_backend_maps_mistral() -> None:
    """``provider_string_to_backend("mistral") == Backend.MISTRAL``."""
    assert provider_string_to_backend("mistral") is Backend.MISTRAL


def test_provider_string_to_backend_maps_anthropic() -> None:
    """``provider_string_to_backend("anthropic") == Backend.ANTHROPIC``."""
    assert provider_string_to_backend("anthropic") is Backend.ANTHROPIC


def test_provider_string_to_backend_raises_on_unknown() -> None:
    """Unknown provider names MUST raise ``KeyError``.

    Per the docstring in ``vibe/core/llm/backend/factory.py``,
    ``provider_string_to_backend`` raises ``KeyError`` for any string
    outside ``{"blitzy", "mistral", "anthropic"}``. The CLI entrypoint
    relies on this error to detect typos and unrecognized flag values
    (and exits with a clear error message rather than constructing the
    wrong backend silently).
    """
    with pytest.raises(KeyError):
        provider_string_to_backend("nonexistent_provider")


def test_provider_string_to_backend_underlying_map_exactly_rule1_trio() -> None:
    """``_PROVIDER_STRING_TO_BACKEND`` keys MUST be exactly the trio.

    This is the strictest invariant of AAP rule 13: the underlying map
    must contain *exactly* the three lowercase provider tokens that the
    user may select. Any extra key (e.g., ``"generic"``, ``"claude_code"``)
    would expose an internal-only backend to the
    ``--provider`` flag, which violates the AAP scoping. Any missing
    key would break the rule-1 trio coverage.
    """
    expected = {"blitzy", "mistral", "anthropic"}
    actual = set(_PROVIDER_STRING_TO_BACKEND.keys())
    assert actual == expected, (
        f"_PROVIDER_STRING_TO_BACKEND keys do not match AAP rule-13 trio. "
        f"Expected: {sorted(expected)}; got: {sorted(actual)}"
    )


def test_provider_string_to_backend_factory_round_trip() -> None:
    """Every string-to-enum mapping MUST resolve to a registered factory class.

    Closes the loop on the AAP rule-13 invariant: each lowercase
    provider token maps to a :class:`Backend` enum member, which keys
    a backend class in :data:`BACKEND_FACTORY`. The chain
    ``string -> enum -> class`` must be defined for every accepted
    string. If any link is missing, the CLI entrypoint would crash
    with a ``KeyError`` mid-startup rather than at flag-parse time.
    """
    for provider_name in _PROVIDER_STRING_TO_BACKEND:
        backend_enum = provider_string_to_backend(provider_name)
        assert backend_enum in BACKEND_FACTORY, (
            f"provider_string_to_backend({provider_name!r}) -> "
            f"{backend_enum!r} is not registered in BACKEND_FACTORY"
        )
        backend_cls = BACKEND_FACTORY[backend_enum]
        # Class must be a type, not a sentinel or function.
        assert isinstance(backend_cls, type), (
            f"BACKEND_FACTORY[{backend_enum!r}] is not a class: {backend_cls!r}"
        )
