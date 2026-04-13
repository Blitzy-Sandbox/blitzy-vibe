"""Protocol classes for breaking circular imports in vibe.core.

This module defines Protocol subclasses that abstract the contracts
used across vibe/core modules. Modules reference these protocols
under TYPE_CHECKING instead of importing concrete implementations,
breaking the circular dependency chain.

RULE R3: This module MUST contain ONLY typing.Protocol subclasses,
typing.TypeAlias definitions, and stdlib type imports. ZERO internal
vibe.* runtime imports are permitted.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import types
from typing import Any, Protocol, runtime_checkable

__all__ = ["BackendLike", "ConfigLike", "ToolLike", "ToolManagerLike"]


@runtime_checkable
class BackendLike(Protocol):
    """Protocol for dependency-injectable LLM backends.

    Mirrors vibe.core.llm.types.BackendLike but uses only stdlib types,
    ensuring this module remains import-pure (R3). Concrete implementations
    live in vibe.core.llm.backend and satisfy this contract structurally.
    """

    async def __aenter__(self) -> BackendLike: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None: ...

    async def complete(
        self,
        *,
        model: Any,
        messages: list[Any],
        temperature: float,
        tools: list[Any] | None,
        max_tokens: int | None,
        tool_choice: Any | None,
        extra_headers: dict[str, str] | None,
    ) -> Any: ...

    def complete_streaming(
        self,
        *,
        model: Any,
        messages: list[Any],
        temperature: float,
        tools: list[Any] | None,
        max_tokens: int | None,
        tool_choice: Any | None,
        extra_headers: dict[str, str] | None,
    ) -> AsyncGenerator[Any, None]: ...

    async def count_tokens(
        self,
        *,
        model: Any,
        messages: list[Any],
        temperature: float,
        tools: list[Any] | None,
        tool_choice: Any | None,
        extra_headers: dict[str, str] | None,
    ) -> int: ...


@runtime_checkable
class ToolLike(Protocol):
    """Protocol abstracting the BaseTool public interface.

    Used by cross-module consumers that need to reference tool capabilities
    without importing the concrete BaseTool class, breaking the circular
    dependency chain between types.py, tools/base.py, config.py, and
    tools/manager.py.
    """

    @classmethod
    def get_name(cls) -> str: ...

    @classmethod
    def get_parameters(cls) -> dict[str, Any]: ...

    @classmethod
    def get_tool_prompt(cls) -> str | None: ...

    def invoke(
        self, ctx: Any | None = None, **raw: Any,
    ) -> AsyncGenerator[Any, None]: ...

    def check_allowlist_denylist(self, args: Any) -> Any | None: ...


@runtime_checkable
class ConfigLike(Protocol):
    """Protocol abstracting VibeConfig field access.

    Provides the minimal contract needed by cross-module consumers
    for configuration reading without importing the full VibeConfig
    settings class and its heavy dependency chain.
    """

    @property
    def auto_approve(self) -> bool: ...

    @property
    def active_model(self) -> str: ...

    @property
    def auto_compact_threshold(self) -> int: ...

    def get_active_model(self) -> Any: ...

    def get_provider_for_model(self, model: Any) -> Any: ...


@runtime_checkable
class ToolManagerLike(Protocol):
    """Protocol abstracting ToolManager access.

    Decouples modules that need tool discovery and instantiation
    from the concrete ToolManager implementation and its transitive
    import chain.
    """

    @property
    def available_tools(self) -> dict[str, Any]: ...

    def get(self, tool_name: str) -> Any: ...

    def get_tool_config(self, tool_name: str) -> Any: ...

    def invalidate_tool(self, tool_name: str) -> None: ...

    def reset_all(self) -> None: ...
