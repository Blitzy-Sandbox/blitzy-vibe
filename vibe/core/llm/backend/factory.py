from __future__ import annotations

from vibe.core.config import Backend
from vibe.core.llm.backend.anthropic_llm import AnthropicBackend
from vibe.core.llm.backend.blitzy import BlitzyLLMBackend
from vibe.core.llm.backend.claude_code_llm import ClaudeCodeBackend
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.llm.backend.mistral import MistralBackend

BACKEND_FACTORY = {
    Backend.BLITZY: BlitzyLLMBackend,
    Backend.MISTRAL: MistralBackend,
    Backend.GENERIC: GenericBackend,
    Backend.ANTHROPIC: AnthropicBackend,
    Backend.CLAUDE_CODE: ClaudeCodeBackend,
}


_PROVIDER_STRING_TO_BACKEND: dict[str, Backend] = {
    "blitzy": Backend.BLITZY,
    "mistral": Backend.MISTRAL,
    "anthropic": Backend.ANTHROPIC,
}


def provider_string_to_backend(name: str) -> Backend:
    """Map a lowercase provider string to the corresponding ``Backend`` enum.

    Used by the CLI entrypoint to translate ``--provider blitzy|mistral|anthropic``
    into a factory key. AAP rule 13: the input string set MUST be exactly
    ``{"blitzy", "mistral", "anthropic"}`` -- this map is the SINGLE source of
    truth; any other site needing the same mapping MUST import this function
    rather than duplicating the dict.

    Args:
        name: The provider name (case-insensitive; leading/trailing whitespace
            is stripped).

    Returns:
        The corresponding :class:`~vibe.core.config.Backend` enum value.

    Raises:
        KeyError: If ``name`` is not one of the three accepted strings.
    """
    key = name.strip().lower()
    if key not in _PROVIDER_STRING_TO_BACKEND:
        raise KeyError(
            f"Unknown provider {name!r}; "
            f"expected one of {sorted(_PROVIDER_STRING_TO_BACKEND)}"
        )
    return _PROVIDER_STRING_TO_BACKEND[key]
