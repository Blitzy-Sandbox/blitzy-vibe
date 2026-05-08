from __future__ import annotations

from vibe.core.config import Backend
from vibe.core.llm.backend.anthropic_llm import AnthropicBackend
from vibe.core.llm.backend.claude_code_llm import ClaudeCodeBackend
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.llm.backend.mistral import MistralBackend

BACKEND_FACTORY = {
    Backend.MISTRAL: MistralBackend,
    Backend.GENERIC: GenericBackend,
    Backend.ANTHROPIC: AnthropicBackend,
    Backend.CLAUDE_CODE: ClaudeCodeBackend,
}
