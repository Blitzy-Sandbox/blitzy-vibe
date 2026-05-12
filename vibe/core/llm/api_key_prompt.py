"""Shared API key resolver for the Blitzy/Anthropic/Mistral backends.

Resolves keys via a three-tier chain: env var -> ``VibeConfig`` field ->
interactive ``getpass.getpass`` prompt. Empty prompt input raises
:class:`vibe.core.config.MissingAPIKeyError` (AAP rule 10). After a successful
interactive entry, the user is offered the option to persist the key to
``~/.blitzy/config.toml`` via ``tomli_w``.

CRITICAL -- AAP rule 2 (API key masking): immediately after acquisition from
ANY source, the resolved value is registered with
:func:`vibe.core.observability.register_sensitive` so that the global
``KEY_MASK_FILTER`` scrubs it from every subsequent log record, exception
message, and traceback. DO NOT bypass this registration.

This module is intentionally provider-agnostic: it does NOT import the
``anthropic``, ``httpx``, or ``mistralai`` SDKs. The provider identifier is
carried as a lowercase string (``"blitzy" | "mistral" | "anthropic"``) for
user-facing prompts and for :class:`MissingAPIKeyError` construction.
"""

from __future__ import annotations

import getpass
import logging
import os
import tomllib
from typing import TYPE_CHECKING

from pydantic import SecretStr
import tomli_w

from vibe.core.config import MissingAPIKeyError
from vibe.core.observability import register_sensitive
from vibe.core.paths.config_paths import CONFIG_FILE

if TYPE_CHECKING:
    from vibe.core.config import VibeConfig

# Module-level logger. All output through this logger flows through the
# global ``KEY_MASK_FILTER`` installed on the ``vibe`` namespace logger by
# :mod:`vibe.core.observability`. This module deliberately does NOT log the
# resolved API key value at any level -- the mask filter is a defense-in-depth
# safeguard, not a substitute for not logging secrets in the first place.
logger = logging.getLogger(__name__)


def resolve_or_prompt(
    provider: str, env_var: str | None, config_field: str, config: VibeConfig
) -> str:
    """Resolve an API key via env var -> config field -> interactive prompt.

    The resolution order is strict and exhaustive: each tier is tried in
    sequence, and a non-empty value at any tier short-circuits the remaining
    tiers. The very first thing the function does on a successful resolution
    is to register the value with the global mask filter via
    :func:`vibe.core.observability.register_sensitive`, ensuring that the
    secret is scrubbed from every subsequent log record, exception message,
    and traceback (AAP rule 2).

    Resolution tiers:

    1. ``os.getenv(env_var)`` -- the provider-specific environment variable
       (e.g., ``BLITZY_API_KEY``). Skipped when ``env_var`` is ``None`` or
       empty.
    2. ``getattr(config, config_field)`` -- a :class:`pydantic.SecretStr`,
       a plain ``str``, or ``None``. Empty values fall through to the next
       tier.
    3. Interactive ``getpass.getpass`` prompt. After a successful entry, the
       user is offered the option to persist the key to
       ``~/.blitzy/config.toml`` via ``tomli_w`` so subsequent invocations
       resolve from the config tier without re-prompting.

    Args:
        provider: Lowercase provider token (``"blitzy" | "mistral" |
            "anthropic"``). Used in the user-facing prompt and in
            :class:`MissingAPIKeyError` construction; never logged.
        env_var: Name of the environment variable to check first (e.g.,
            ``"BLITZY_API_KEY"``). May be ``None`` or empty to skip env
            lookup entirely.
        config_field: Name of the :class:`VibeConfig` attribute holding the
            key (e.g., ``"blitzy_api_key"``). The attribute value may be a
            :class:`pydantic.SecretStr`, a plain ``str``, or ``None``.
        config: The active :class:`VibeConfig` instance. Accessed via
            ``getattr`` so the function works with any subclass and tolerates
            a missing attribute gracefully (treats it as ``None``).

    Returns:
        The resolved API key as a plain string. The value has already been
        registered with the global mask filter at this point.

    Raises:
        MissingAPIKeyError: when the user provides empty input at the
            interactive prompt. AAP rule 10 mandates that the agent then
            exit; the CLI entrypoint catches this exception and terminates
            with a clear error message.
    """
    # Tier 1: Environment variable.
    #
    # ``os.getenv`` returns ``None`` for an unset variable and ``""`` for an
    # explicitly empty value. Both are falsy, so the ``if env_value:`` guard
    # cleanly falls through to the next tier in either case.
    if env_var:
        env_value = os.getenv(env_var)
        if env_value:
            register_sensitive(env_value)
            return env_value

    # Tier 2: Config field.
    #
    # The attribute on ``VibeConfig`` may be a ``SecretStr`` (the canonical
    # form for the three optional fields ``blitzy_api_key``,
    # ``anthropic_api_key``, ``mistral_api_key`` per the config update in
    # this delivery), a plain ``str`` (defensive -- some test fixtures may
    # bypass the Pydantic field type), or ``None`` (no config-side key set).
    # ``getattr(..., None)`` ensures we never raise ``AttributeError`` if a
    # caller passes a typo or a config object that does not expose the field.
    config_value = getattr(config, config_field, None)
    if isinstance(config_value, SecretStr):
        raw = config_value.get_secret_value()
        if raw:
            register_sensitive(raw)
            return raw
    elif isinstance(config_value, str) and config_value:
        # The truthiness check filters out both ``""`` and any other falsy
        # variant a non-canonical config object might supply.
        register_sensitive(config_value)
        return config_value

    # Tier 3: Interactive prompt.
    #
    # The leading ``print`` displays human-readable context (provider name
    # and abort instruction). ``getpass.getpass`` reads from stdin WITHOUT
    # echoing -- this is a baseline security measure independent of the
    # global mask filter (which protects subsequent log output but cannot
    # un-echo a value already written to the terminal).
    print(
        f"{provider.title()} API key not found in environment or config.\n"
        "Enter API key (or press Enter to abort):"
    )
    entered = getpass.getpass("> ").strip()
    if not entered:
        # AAP rule 10: declined entry -> agent exits via the CLI's exception
        # handler. The single-argument ``MissingAPIKeyError(provider)``
        # form is the canonical signal for this path; the legacy two-arg
        # form (env_key, provider_name) is reserved for the
        # ``_check_api_key`` validator's pre-prompt failures.
        raise MissingAPIKeyError(provider)
    register_sensitive(entered)

    # Optional persistence step. The "Save to config?" prompt uses ``input``
    # (NOT ``getpass``) because the y/N response is non-sensitive and showing
    # the keypress avoids confusing the operator into thinking the prompt
    # hung. ``EOFError`` covers the non-interactive case (piped stdin, CI)
    # where ``input`` would otherwise crash after reading EOF -- we treat it
    # as an implicit "no" and skip the save.
    try:
        save_choice = input("Save to ~/.blitzy/config.toml? [y/N] ").strip().lower()
    except EOFError:
        save_choice = ""
    if save_choice == "y":
        _save_key_to_config(config_field, entered)

    return entered


def _save_key_to_config(field_name: str, value: str) -> None:
    """Write ``field_name = value`` to the top level of ~/.blitzy/config.toml.

    Reads the existing TOML (or starts from an empty dict when the file is
    absent), sets the field, and writes back via :func:`tomli_w.dump` in
    binary mode. Silently ignores every error path -- AAP rule 2 forbids
    logging anything that could expose ``value`` via a captured traceback,
    so this function NEVER emits log output and NEVER re-raises.

    The key is written as a top-level TOML key (``blitzy_api_key = "..."``)
    rather than under an ``[api_keys]`` section, matching the layout of the
    corresponding :class:`VibeConfig` fields so that
    :class:`pydantic_settings.TomlConfigSettingsSource` reads it on the next
    CLI invocation without re-prompting.

    Args:
        field_name: The :class:`VibeConfig` attribute name (and the literal
            top-level TOML key) -- one of ``"blitzy_api_key"``,
            ``"anthropic_api_key"``, ``"mistral_api_key"``.
        value: The resolved API key. NEVER logged; silently dropped on error.
    """
    # ``CONFIG_FILE.path`` raises ``RuntimeError("Config path is locked")``
    # until :func:`vibe.core.paths.config_paths.unlock_config_paths` has been
    # called. In production the CLI entrypoint unlocks it early; in tests
    # the lock may still be in effect. We silently no-op -- the in-memory
    # resolution result returned by the caller is unaffected.
    try:
        path = CONFIG_FILE.path
    except RuntimeError:
        return

    try:
        # Read the existing TOML if it exists. ``tomllib.load`` requires
        # binary mode per the stdlib API.
        if path.exists():
            with path.open("rb") as fh_read:
                data = tomllib.load(fh_read)
        else:
            data = {}
        # Defensive fallback: ``tomllib`` always returns a dict at the
        # document root, but if a file was hand-edited to contain something
        # exotic we don't want to crash. Replace with an empty dict and
        # continue.
        if not isinstance(data, dict):
            data = {}
        # Set the top-level key. This deliberately overwrites any existing
        # value (the user explicitly chose to save a new key).
        data[field_name] = value
        # Ensure the parent directory exists. ``mkdir(parents=True,
        # exist_ok=True)`` is idempotent and required for first-run users
        # whose ``~/.blitzy/`` has not been created yet.
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``tomli_w.dump`` requires binary mode -- the same pattern is used
        # by ``VibeConfig.dump_config`` elsewhere in the codebase.
        with path.open("wb") as fh_write:
            tomli_w.dump(data, fh_write)
    except (OSError, tomllib.TOMLDecodeError):
        # AAP rule 2: DO NOT log the failure. A traceback or formatted
        # exception message could echo ``value`` (the API key) via some
        # downstream handler that has not yet seen the masked value
        # registered above. The next CLI invocation will simply re-prompt
        # the user -- correctness is preserved without leaking the secret.
        return
