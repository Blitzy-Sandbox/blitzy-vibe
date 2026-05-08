from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
import types
from typing import TYPE_CHECKING

from vibe.core.types import (
    AvailableTool,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
)

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig


def is_claude_code_authenticated() -> bool:
    """Return True if `claude auth status` reports loggedIn=true."""
    import subprocess

    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn"))
    except Exception:
        return False


def run_claude_login() -> bool:
    """Run `claude auth login` interactively. Returns True on success."""
    import subprocess

    try:
        result = subprocess.run(["claude", "auth", "login"])
        return result.returncode == 0
    except FileNotFoundError:
        return False


class ClaudeCodeBackend:
    """LLM backend that delegates to the `claude` CLI.

    Uses the user's existing Claude Code authentication (keychain/OAuth),
    so no separate ANTHROPIC_API_KEY is required.

    Each backend instance tracks a session ID so that multi-turn
    conversations resume the same Claude Code session.
    """

    def __init__(self, provider: ProviderConfig, timeout: float = 720.0) -> None:
        self._provider = provider
        self._timeout = timeout
        self._session_id: str | None = None

    async def __aenter__(self) -> ClaudeCodeBackend:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _last_user_message(self, messages: list[LLMMessage]) -> str:
        for msg in reversed(messages):
            if msg.role == Role.user:
                return msg.content or ""
        return ""

    def _system_prompt(self, messages: list[LLMMessage]) -> str | None:
        for msg in messages:
            if msg.role == Role.system:
                return msg.content or None
        return None

    def _build_cmd(
        self, model: ModelConfig, prompt: str, system: str | None
    ) -> list[str]:
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            model.name,
        ]
        if self._session_id:
            cmd += ["--resume", self._session_id]
        if system:
            cmd += ["--append-system-prompt", system]
        return cmd

    # ------------------------------------------------------------------
    # Streaming — core method
    # ------------------------------------------------------------------

    async def complete_streaming(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
    ) -> AsyncGenerator[LLMChunk, None]:
        prompt = self._last_user_message(messages)
        system = self._system_prompt(messages)
        cmd = self._build_cmd(model, prompt, system)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        input_tokens = 0
        output_tokens = 0
        assert proc.stdout is not None

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            # Capture session id for multi-turn resume
            if etype == "system" and event.get("subtype") == "init":
                self._session_id = event.get("session_id")

            # stream_event wraps the raw Anthropic streaming events
            elif etype == "stream_event":
                inner = event.get("event", {})
                itype = inner.get("type")

                if itype == "message_start":
                    usage = inner.get("message", {}).get("usage", {})
                    input_tokens = usage.get("input_tokens", 0) + usage.get(
                        "cache_read_input_tokens", 0
                    )

                elif itype == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield LLMChunk(
                            message=LLMMessage(
                                role=Role.assistant, content=delta.get("text", "")
                            ),
                            usage=None,
                        )
                    elif delta.get("type") == "thinking_delta":
                        yield LLMChunk(
                            message=LLMMessage(
                                role=Role.assistant,
                                content="",
                                reasoning_content=delta.get("thinking", ""),
                            ),
                            usage=None,
                        )
                    # input_json_delta (tool arg chunks) are intentionally ignored:
                    # Claude Code handles tool calls internally; blitzy-vibe only
                    # needs the final text output.

                # content_block_start tool_use events are also ignored for the same reason.

                elif itype == "message_delta":
                    usage = inner.get("usage", {})
                    output_tokens = usage.get("output_tokens", 0)
                    yield LLMChunk(
                        message=LLMMessage(role=Role.assistant, content=""),
                        usage=LLMUsage(
                            prompt_tokens=input_tokens, completion_tokens=output_tokens
                        ),
                    )

        await proc.wait()

    # ------------------------------------------------------------------
    # Non-streaming wrapper
    # ------------------------------------------------------------------

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
    ) -> LLMChunk:
        result: LLMChunk | None = None
        async for chunk in self.complete_streaming(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        ):
            result = chunk if result is None else result + chunk
        return result or LLMChunk(
            message=LLMMessage(role=Role.assistant, content=""), usage=LLMUsage()
        )

    async def count_tokens(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        tools: list[AvailableTool] | None = None,
        tool_choice: StrToolChoice | AvailableTool | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> int:
        # Rough estimate — claude CLI doesn't expose token counting directly
        return sum(len(m.content or "") for m in messages) // 4
