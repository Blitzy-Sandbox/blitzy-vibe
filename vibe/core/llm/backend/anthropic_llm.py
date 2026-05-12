from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import types
from typing import TYPE_CHECKING
from uuid import uuid4

import anthropic
import httpx

from vibe.core.llm.api_key_prompt import resolve_or_prompt
from vibe.core.llm.exceptions import BackendErrorBuilder
from vibe.core.types import (
    AvailableTool,
    FunctionCall,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
    ToolCall,
)

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig, VibeConfig


class AnthropicMapper:
    def prepare_messages(
        self, messages: list[LLMMessage]
    ) -> tuple[str | None, list[dict]]:
        """Convert internal messages to Anthropic format.

        Returns (system_prompt, messages_list).
        Anthropic requires system as a top-level param (not in the messages array),
        and tool results must be in user-role messages as tool_result content blocks.
        """
        system: str | None = None
        result: list[dict] = []

        for msg in messages:
            match msg.role:
                case Role.system:
                    system = msg.content or ""

                case Role.user:
                    result.append({"role": "user", "content": msg.content or ""})

                case Role.assistant:
                    content: list[dict] = []
                    # Omit reasoning/thinking blocks — we don't store the signature
                    # required by Anthropic to replay thinking in multi-turn sessions.
                    if msg.content:
                        content.append({"type": "text", "text": msg.content})
                    for tc in msg.tool_calls or []:
                        try:
                            input_data = json.loads(tc.function.arguments or "{}")
                        except (json.JSONDecodeError, TypeError):
                            input_data = {}
                        content.append({
                            "type": "tool_use",
                            "id": tc.id or f"toolu_{uuid4().hex[:8]}",
                            "name": tc.function.name or "",
                            "input": input_data,
                        })
                    result.append({
                        "role": "assistant",
                        "content": content or [{"type": "text", "text": ""}],
                    })

                case Role.tool:
                    tool_result: dict = {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": msg.content or "",
                    }
                    # Merge consecutive tool results into a single user message.
                    if (
                        result
                        and result[-1]["role"] == "user"
                        and isinstance(result[-1]["content"], list)
                    ):
                        result[-1]["content"].append(tool_result)
                    else:
                        result.append({"role": "user", "content": [tool_result]})

        return system, result

    def prepare_tool(self, tool: AvailableTool) -> dict:
        return {
            "name": tool.function.name,
            "description": tool.function.description or "",
            "input_schema": tool.function.parameters
            or {"type": "object", "properties": {}},
        }

    def prepare_tool_choice(self, tool_choice: StrToolChoice | AvailableTool) -> dict:
        if isinstance(tool_choice, str):
            match tool_choice:
                case "auto":
                    return {"type": "auto"}
                case "none":
                    return {"type": "none"}
                case "any" | "required":
                    return {"type": "any"}
                case _:
                    return {"type": "auto"}
        return {"type": "tool", "name": tool_choice.function.name}

    def parse_response(
        self, response: anthropic.types.Message
    ) -> tuple[str, str | None, list[ToolCall] | None]:
        """Extract (content, reasoning_content, tool_calls) from a complete message."""
        content = ""
        reasoning_content: str | None = None
        tool_calls: list[ToolCall] = []

        for index, block in enumerate(response.content):
            if block.type == "text":
                content += block.text
            elif block.type == "thinking":
                reasoning_content = (reasoning_content or "") + block.thinking
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        function=FunctionCall(
                            name=block.name,
                            arguments=json.dumps(block.input, ensure_ascii=False),
                        ),
                        index=index,
                    )
                )

        return content, reasoning_content, tool_calls or None


class AnthropicBackend:
    def __init__(
        self, provider: ProviderConfig, config: VibeConfig, timeout: float = 720.0
    ) -> None:
        self._client: anthropic.AsyncAnthropic | None = None
        self._provider = provider
        self._config = config
        self._mapper = AnthropicMapper()
        # API key resolution via the shared three-tier chain
        # (env var -> VibeConfig field -> interactive getpass prompt).
        # ``resolve_or_prompt`` registers the resolved value with the global
        # ``KEY_MASK_FILTER`` for log scrubbing (AAP rule 2) and either
        # returns a guaranteed non-empty string OR raises
        # ``MissingAPIKeyError("anthropic")`` when the user declines the
        # interactive prompt (AAP rule 10 -- the CLI entrypoint catches
        # this and exits with a clear message).
        #
        # ``self._provider.api_key_env_var`` may be an empty string for
        # exotic provider configurations; the ``or "ANTHROPIC_API_KEY"``
        # fallback is a defensive default that matches the canonical
        # Anthropic environment variable name. ``VibeConfig.anthropic_model``
        # is consumed in ``_build_kwargs``/``count_tokens`` below.
        self._api_key = resolve_or_prompt(
            "anthropic",
            self._provider.api_key_env_var or "ANTHROPIC_API_KEY",
            "anthropic_api_key",
            config,
        )
        self._timeout = timeout

    async def __aenter__(self) -> AnthropicBackend:
        self._client = anthropic.AsyncAnthropic(
            api_key=self._api_key, timeout=self._timeout
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=self._api_key, timeout=self._timeout
            )
        return self._client

    def _build_kwargs(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
    ) -> tuple[str | None, list[dict], dict]:
        system, prepared_messages = self._mapper.prepare_messages(messages)

        # The operator-selected ``VibeConfig.anthropic_model`` (default
        # ``"claude-sonnet-4-6"``, verified per AAP section 0.10.2) takes
        # precedence over the per-model registry name. The ``or model.name``
        # fallback protects against an explicit empty-string override.
        kwargs: dict = {
            "model": self._config.anthropic_model or model.name,
            "messages": prepared_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 16000,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._mapper.prepare_tool(t) for t in tools]
            if tool_choice:
                kwargs["tool_choice"] = self._mapper.prepare_tool_choice(tool_choice)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        return system, prepared_messages, kwargs

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
        _, _, kwargs = self._build_kwargs(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        )

        try:
            response = await self._get_client().messages.create(**kwargs)
            content, reasoning_content, tool_calls = self._mapper.parse_response(
                response
            )
            return LLMChunk(
                message=LLMMessage(
                    role=Role.assistant,
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls,
                ),
                usage=LLMUsage(
                    prompt_tokens=response.usage.input_tokens,
                    completion_tokens=response.usage.output_tokens,
                ),
            )

        except anthropic.APIStatusError as e:
            raise BackendErrorBuilder.build_http_error(
                provider=self._provider.name,
                endpoint=self._provider.api_base,
                response=e.response,
                headers=e.response.headers,
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e
        except (anthropic.APIConnectionError, httpx.RequestError) as e:
            raise BackendErrorBuilder.build_request_error(
                provider=self._provider.name,
                endpoint=self._provider.api_base,
                error=e,  # type: ignore[arg-type]
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e

    def _on_content_block_start(
        self, event: object, current_tool_calls: dict[int, dict]
    ) -> list[LLMChunk]:
        """Handle content_block_start events; register tool_use blocks."""
        if event.content_block.type != "tool_use":  # type: ignore[union-attr]
            return []
        tc_index = len(current_tool_calls)
        current_tool_calls[event.index] = {  # type: ignore[union-attr]
            "id": event.content_block.id,  # type: ignore[union-attr]
            "name": event.content_block.name,  # type: ignore[union-attr]
            "index": tc_index,
        }
        return [
            LLMChunk(
                message=LLMMessage(
                    role=Role.assistant,
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=event.content_block.id,  # type: ignore[union-attr]
                            function=FunctionCall(
                                name=event.content_block.name,  # type: ignore[union-attr]
                                arguments="",
                            ),
                            index=tc_index,
                        )
                    ],
                ),
                usage=None,
            )
        ]

    def _on_content_block_delta(
        self, event: object, current_tool_calls: dict[int, dict]
    ) -> list[LLMChunk]:
        """Handle content_block_delta events; dispatch by delta type."""
        delta = event.delta  # type: ignore[union-attr]
        if delta.type == "text_delta":
            return [
                LLMChunk(
                    message=LLMMessage(role=Role.assistant, content=delta.text),
                    usage=None,
                )
            ]
        if delta.type == "thinking_delta":
            return [
                LLMChunk(
                    message=LLMMessage(
                        role=Role.assistant,
                        content="",
                        reasoning_content=delta.thinking,
                    ),
                    usage=None,
                )
            ]
        if delta.type == "input_json_delta" and event.index in current_tool_calls:  # type: ignore[union-attr]
            tc = current_tool_calls[event.index]  # type: ignore[union-attr]
            return [
                LLMChunk(
                    message=LLMMessage(
                        role=Role.assistant,
                        content="",
                        tool_calls=[
                            ToolCall(
                                id=tc["id"],
                                function=FunctionCall(
                                    name=None, arguments=delta.partial_json
                                ),
                                index=tc["index"],
                            )
                        ],
                    ),
                    usage=None,
                )
            ]
        return []

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
        _, _, kwargs = self._build_kwargs(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        )

        try:
            input_tokens = 0
            # Maps stream block index → {id, name, index (in tool_calls list)}
            current_tool_calls: dict[int, dict] = {}

            async with self._get_client().messages.stream(**kwargs) as stream:
                async for event in stream:
                    chunks: list[LLMChunk] = []
                    if event.type == "message_start":
                        input_tokens = event.message.usage.input_tokens
                    elif event.type == "content_block_start":
                        chunks = self._on_content_block_start(event, current_tool_calls)
                    elif event.type == "content_block_delta":
                        chunks = self._on_content_block_delta(event, current_tool_calls)
                    elif event.type == "message_delta":
                        output_tokens = event.usage.output_tokens if event.usage else 0
                        chunks = [
                            LLMChunk(
                                message=LLMMessage(role=Role.assistant, content=""),
                                usage=LLMUsage(
                                    prompt_tokens=input_tokens,
                                    completion_tokens=output_tokens,
                                ),
                            )
                        ]
                    for chunk in chunks:
                        yield chunk

        except anthropic.APIStatusError as e:
            raise BackendErrorBuilder.build_http_error(
                provider=self._provider.name,
                endpoint=self._provider.api_base,
                response=e.response,
                headers=e.response.headers,
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e
        except (anthropic.APIConnectionError, httpx.RequestError) as e:
            raise BackendErrorBuilder.build_request_error(
                provider=self._provider.name,
                endpoint=self._provider.api_base,
                error=e,  # type: ignore[arg-type]
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e

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
        system, prepared_messages, _ = self._build_kwargs(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=1,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        )

        # Use the SAME model identifier as ``_build_kwargs`` so the token
        # count corresponds to the model actually used by ``complete`` /
        # ``complete_streaming``. Mixing model identifiers between counting
        # and completion would produce misleading usage numbers.
        count_kwargs: dict = {
            "model": self._config.anthropic_model or model.name,
            "messages": prepared_messages,
        }
        if system:
            count_kwargs["system"] = system
        if tools:
            count_kwargs["tools"] = [self._mapper.prepare_tool(t) for t in tools]

        result = await self._get_client().messages.count_tokens(**count_kwargs)
        return result.input_tokens
