from __future__ import annotations

from collections.abc import MutableMapping
from enum import StrEnum, auto
import os
from pathlib import Path
import re
import shlex
import tomllib
from typing import Annotated, Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
import tomli_w

from vibe.core.paths.config_paths import CONFIG_DIR, CONFIG_FILE, PROMPTS_DIR
from vibe.core.paths.global_paths import (
    GLOBAL_ENV_FILE,
    GLOBAL_PROMPTS_DIR,
    SESSION_LOG_DIR,
)
from vibe.core.prompts import SystemPrompt
from vibe.core.tools.base import BaseToolConfig


def load_dotenv_values(
    env_path: Path = GLOBAL_ENV_FILE.path,
    environ: MutableMapping[str, str] = os.environ,
) -> None:
    if not env_path.is_file():
        return

    env_vars = dotenv_values(env_path)
    for key, value in env_vars.items():
        if not value:
            continue
        environ.update({key: value})


class MissingAPIKeyError(RuntimeError):
    """Raised when an API key is missing or the user declines interactive entry.

    Supports two construction forms to preserve backward compatibility while
    enabling the new provider-selection workflow:

    - Legacy: ``MissingAPIKeyError(env_key, provider_name)`` -- used by the
      existing ``_check_api_key`` validator and ACP/CLI call sites that report
      a missing environment variable for a configured provider.
    - New: ``MissingAPIKeyError(provider)`` -- used by
      ``vibe.core.llm.api_key_prompt.resolve_or_prompt`` when the env-var ->
      config-field -> interactive-prompt cascade is exhausted (AAP rule 10).

    Instance attributes ``env_key`` and ``provider_name`` are always set, so
    downstream handlers may inspect them uniformly. The single-argument form
    leaves ``env_key`` empty and stores the provider token in
    ``provider_name`` and ``provider``; the two-argument form derives
    ``provider`` by lowercasing ``provider_name``.

    AAP rule 2: API key values MUST NOT appear in any log line, exception
    message, or traceback. This class accepts only env-var names and provider
    identifiers -- never the secret value itself.
    """

    def __init__(self, *args: str) -> None:
        match args:
            case (provider,):
                # New signature: MissingAPIKeyError(provider) -- AAP rule 10.
                self.provider = provider
                self.env_key = ""
                self.provider_name = provider
                super().__init__(f"Missing API key for {provider}")
            case (env_key, provider_name):
                # Legacy signature: MissingAPIKeyError(env_key, provider_name).
                self.env_key = env_key
                self.provider_name = provider_name
                self.provider = provider_name.lower()
                super().__init__(
                    f"Missing {env_key} environment variable "
                    f"for {provider_name} provider"
                )
            case _:
                raise TypeError(
                    "MissingAPIKeyError requires 1 or 2 positional arguments"
                )


class MissingPromptFileError(RuntimeError):
    def __init__(
        self, system_prompt_id: str, prompt_dir: str, global_prompt_dir: str
    ) -> None:
        extra_global_prompt_dir = (
            f" or {global_prompt_dir}" if global_prompt_dir != prompt_dir else ""
        )

        super().__init__(
            f"Invalid system_prompt_id value: '{system_prompt_id}'. "
            f"Must be one of the available prompts ({', '.join(f'{p.name.lower()}' for p in SystemPrompt)}), "
            f"or correspond to a .md file in {prompt_dir}{extra_global_prompt_dir}"
        )
        self.system_prompt_id = system_prompt_id
        self.prompt_dir = prompt_dir


class WrongBackendError(RuntimeError):
    def __init__(self, backend: Backend, is_mistral_api: bool) -> None:
        super().__init__(
            f"Wrong backend '{backend}' for {'' if is_mistral_api else 'non-'}"
            f"mistral API. Use '{Backend.MISTRAL}' for mistral API and '{Backend.GENERIC}' for others."
        )
        self.backend = backend
        self.is_mistral_api = is_mistral_api


class TomlFileSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self.toml_data = self._load_toml()

    def _load_toml(self) -> dict[str, Any]:
        file = CONFIG_FILE.path
        try:
            with file.open("rb") as f:
                return tomllib.load(f)
        except FileNotFoundError:
            return {}
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(f"Invalid TOML in {file}: {e}") from e
        except OSError as e:
            raise RuntimeError(f"Cannot read {file}: {e}") from e

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        return self.toml_data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self.toml_data


class ProjectContextConfig(BaseSettings):
    max_chars: int = 40_000
    default_commit_count: int = 5
    max_doc_bytes: int = 32 * 1024
    truncation_buffer: int = 1_000
    max_depth: int = 3
    max_files: int = 1000
    max_dirs_per_level: int = 20
    timeout_seconds: float = 2.0


class SessionLoggingConfig(BaseSettings):
    save_dir: str = ""
    session_prefix: str = "session"
    enabled: bool = True

    @field_validator("save_dir", mode="before")
    @classmethod
    def set_default_save_dir(cls, v: str) -> str:
        if not v:
            return str(SESSION_LOG_DIR.path)
        return v

    @field_validator("save_dir", mode="after")
    @classmethod
    def expand_save_dir(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class Backend(StrEnum):
    # ``StrEnum`` + ``auto()`` derives each member's value from the lowercase
    # member name, so ``Backend.BLITZY.value == "blitzy"``. This lowercase
    # string is the canonical token used across argparse choices, factory map
    # keys, the session record ``provider`` field, and ``ContextLimitsConfig``
    # field names (AAP rule 13 -- no orphaned strings).
    #
    # ``BLITZY`` is listed first because it is the default provider in the
    # startup picker (matches "Blitzy (default)" in the AAP user example).
    BLITZY = auto()
    MISTRAL = auto()
    GENERIC = auto()
    ANTHROPIC = auto()
    CLAUDE_CODE = auto()


class ProviderConfig(BaseModel):
    name: str
    api_base: str
    api_key_env_var: str = ""
    api_style: str = "openai"
    backend: Backend = Backend.GENERIC
    reasoning_field_name: str = "reasoning_content"
    supports_stream_options: bool = True
    supports_tool_choice: bool = True


class _MCPBase(BaseModel):
    name: str = Field(description="Short alias used to prefix tool names")
    prompt: str | None = Field(
        default=None, description="Optional usage hint appended to tool descriptions"
    )
    startup_timeout_sec: float = Field(
        default=10.0,
        gt=0,
        description="Timeout in seconds for the server to start and initialize.",
    )
    tool_timeout_sec: float = Field(
        default=60.0, gt=0, description="Timeout in seconds for tool execution."
    )

    @field_validator("name", mode="after")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", v)
        normalized = normalized.strip("_-")
        return normalized[:256]


class _MCPHttpFields(BaseModel):
    url: str = Field(description="Base URL of the MCP HTTP server")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Additional HTTP headers when using 'http' transport (e.g., Authorization or X-API-Key)."
        ),
    )
    api_key_env: str = Field(
        default="",
        description=(
            "Environment variable name containing an API token to send for HTTP transport."
        ),
    )
    api_key_header: str = Field(
        default="Authorization",
        description=(
            "HTTP header name to carry the token when 'api_key_env' is set (e.g., 'Authorization' or 'X-API-Key')."
        ),
    )
    api_key_format: str = Field(
        default="Bearer {token}",
        description=(
            "Format string for the header value when 'api_key_env' is set. Use '{token}' placeholder."
        ),
    )

    def http_headers(self) -> dict[str, str]:
        hdrs = dict(self.headers or {})
        env_var = (self.api_key_env or "").strip()
        if env_var and (token := os.getenv(env_var)):
            target = (self.api_key_header or "").strip() or "Authorization"
            if not any(h.lower() == target.lower() for h in hdrs):
                try:
                    value = (self.api_key_format or "{token}").format(token=token)
                except Exception:
                    value = token
                hdrs[target] = value
        return hdrs


class MCPHttp(_MCPBase, _MCPHttpFields):
    transport: Literal["http"]


class MCPStreamableHttp(_MCPBase, _MCPHttpFields):
    transport: Literal["streamable-http"]


class MCPStdio(_MCPBase):
    transport: Literal["stdio"]
    command: str | list[str]
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to set for the MCP server process.",
    )

    def argv(self) -> list[str]:
        base = (
            shlex.split(self.command)
            if isinstance(self.command, str)
            else list(self.command or [])
        )
        return [*base, *self.args] if self.args else base


MCPServer = Annotated[
    MCPHttp | MCPStreamableHttp | MCPStdio, Field(discriminator="transport")
]


class ModelConfig(BaseModel):
    name: str
    provider: str
    alias: str
    temperature: float = 0.2
    input_price: float = 0.0  # Price per million input tokens
    output_price: float = 0.0  # Price per million output tokens

    @model_validator(mode="before")
    @classmethod
    def _default_alias_to_name(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "alias" not in data or data["alias"] is None:
                data["alias"] = data.get("name")
        return data


DEFAULT_PROVIDERS = [
    # AAP feature: Blitzy is the default provider. ``api_base`` is the root
    # against which ``BlitzyLLMBackend`` constructs ``/context?...`` and
    # ``/v1/api/chat`` URLs.
    ProviderConfig(
        name="blitzy",
        api_base="https://api.blitzy.com",
        api_key_env_var="BLITZY_API_KEY",
        backend=Backend.BLITZY,
    ),
    # AAP feature: Anthropic entry so the factory + ``get_provider_for_model``
    # pipeline can resolve a backend instance for ``anthropic``-backed models
    # without requiring users to populate ``[providers]`` manually. The
    # Anthropic SDK uses its own default URL internally; ``api_base`` is
    # retained for consistency with the ``ProviderConfig`` schema.
    ProviderConfig(
        name="anthropic",
        api_base="https://api.anthropic.com",
        api_key_env_var="ANTHROPIC_API_KEY",
        backend=Backend.ANTHROPIC,
    ),
    ProviderConfig(
        name="mistral",
        api_base="https://api.mistral.ai/v1",
        api_key_env_var="MISTRAL_API_KEY",
        backend=Backend.MISTRAL,
    ),
    ProviderConfig(
        name="llamacpp",
        api_base="http://127.0.0.1:8080/v1",
        api_key_env_var="",  # NOTE: if you wish to use --api-key in llama-server, change this value
        supports_stream_options=False,
        supports_tool_choice=False,
    ),
]

DEFAULT_MODELS = [
    ModelConfig(
        name="mistral-vibe-cli-latest",
        provider="mistral",
        alias="devstral-2",
        input_price=0.4,
        output_price=2.0,
    ),
    ModelConfig(
        name="devstral-small-latest",
        provider="mistral",
        alias="devstral-small",
        input_price=0.1,
        output_price=0.3,
    ),
    ModelConfig(
        name="devstral",
        provider="llamacpp",
        alias="local",
        input_price=0.0,
        output_price=0.0,
    ),
]


class ContextLimitsConfig(BaseModel):
    """Per-provider context window token limits.

    Values are read from the ``[context_limits]`` table in
    ``~/.blitzy/config.toml`` at startup via the ``TomlFileSettingsSource``
    pipeline and applied to ``AutoCompactMiddleware`` and
    ``SessionManager.compact`` (AAP rule 11: configurable, not hardcoded).

    Defaults (AAP sections 0.5.1 and 0.6.1):

    - ``blitzy=128_000`` -- Blitzy default context window.
    - ``mistral=32_000`` -- Mistral Large / Devstral default.
    - ``anthropic=200_000`` -- Claude direct API default; the 1M-token beta
      header is opt-in elsewhere and not part of the default.

    The field names match the lowercase ``Backend`` enum values exactly so
    that callers can look up a limit by enum value via
    ``getattr(context_limits, backend.value)`` (AAP rule 13).

    Validation:
        Every field carries a ``gt=0`` constraint so that ``0`` or negative
        token limits are rejected at config-load time rather than producing
        downstream silent failures (e.g.,
        ``int(-1 * 0.8) == 0`` would mask an operator typo because the
        compaction threshold would never trigger). Operators who genuinely
        want to disable compaction should set the limit to a deliberately
        large value (e.g., ``10_000_000``), not zero or negative.
    """

    blitzy: int = Field(default=128_000, gt=0)
    mistral: int = Field(default=32_000, gt=0)
    anthropic: int = Field(default=200_000, gt=0)


class VibeConfig(BaseSettings):
    active_model: str = "devstral-2"
    textual_theme: str = "terminal"
    vim_keybindings: bool = False
    disable_welcome_banner_animation: bool = False
    displayed_workdir: str = ""
    auto_compact_threshold: int = 200_000
    context_warnings: bool = False
    auto_approve: bool = False
    system_prompt_id: str = "cli"
    include_commit_signature: bool = True
    include_model_info: bool = True
    include_project_context: bool = True
    include_prompt_detail: bool = True
    enable_update_checks: bool = True
    enable_auto_update: bool = True
    api_timeout: float = 720.0

    # --- AAP feature: provider selection + session persistence -------------
    # Optional API keys stored in ``~/.blitzy/config.toml``. ``SecretStr``
    # hides values in ``repr()`` and ``model_dump()``; callers must invoke
    # ``.get_secret_value()`` to read the plaintext. The native env vars
    # ``BLITZY_API_KEY``, ``ANTHROPIC_API_KEY``, and ``MISTRAL_API_KEY`` are
    # read directly by ``vibe.core.llm.api_key_prompt.resolve_or_prompt``
    # via ``os.getenv()``, not through pydantic-settings env binding.
    blitzy_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None
    # Anthropic model identifier consumed by ``AnthropicBackend`` when
    # building request params. Verified Anthropic model snapshot per
    # AAP section 0.10.2 (Claude Sonnet 4.6 release).
    anthropic_model: str = "claude-sonnet-4-6"
    # Per-provider context-window limits used by ``AutoCompactMiddleware``
    # and ``SessionManager.compact`` (AAP rule 11). Override via the
    # ``[context_limits]`` table in ``~/.blitzy/config.toml``.
    context_limits: ContextLimitsConfig = Field(default_factory=ContextLimitsConfig)
    # ----------------------------------------------------------------------

    providers: list[ProviderConfig] = Field(
        default_factory=lambda: list(DEFAULT_PROVIDERS)
    )
    models: list[ModelConfig] = Field(default_factory=lambda: list(DEFAULT_MODELS))

    project_context: ProjectContextConfig = Field(default_factory=ProjectContextConfig)
    session_logging: SessionLoggingConfig = Field(default_factory=SessionLoggingConfig)
    tools: dict[str, BaseToolConfig] = Field(default_factory=dict)
    tool_paths: list[Path] = Field(
        default_factory=list,
        description=(
            "Additional directories or files to explore for custom tools. "
            "Paths may be absolute or relative to the current working directory. "
            "Directories are shallow-searched for tool definition files, "
            "while files are loaded directly if valid."
        ),
    )

    mcp_servers: list[MCPServer] = Field(
        default_factory=list, description="Preferred MCP server configuration entries."
    )

    enabled_tools: list[str] = Field(
        default_factory=list,
        description=(
            "An explicit list of tool names/patterns to enable. If set, only these"
            " tools will be active. Supports glob patterns (e.g., 'serena_*') and"
            " regex with 're:' prefix (e.g., 're:^serena_.*')."
        ),
    )
    disabled_tools: list[str] = Field(
        default_factory=list,
        description=(
            "A list of tool names/patterns to disable. Ignored if 'enabled_tools'"
            " is set. Supports glob patterns and regex with 're:' prefix."
        ),
    )
    agent_paths: list[Path] = Field(
        default_factory=list,
        description=(
            "Additional directories to search for custom agent profiles. "
            "Each path may be absolute or relative to the current working directory."
        ),
    )
    enabled_agents: list[str] = Field(
        default_factory=list,
        description=(
            "An explicit list of agent names/patterns to enable. If set, only these"
            " agents will be available. Supports glob patterns (e.g., 'custom-*')"
            " and regex with 're:' prefix."
        ),
    )
    disabled_agents: list[str] = Field(
        default_factory=list,
        description=(
            "A list of agent names/patterns to disable. Ignored if 'enabled_agents'"
            " is set. Supports glob patterns and regex with 're:' prefix."
        ),
    )
    skill_paths: list[Path] = Field(
        default_factory=list,
        description=(
            "Additional directories to search for skills. "
            "Each path may be absolute or relative to the current working directory."
        ),
    )
    enabled_skills: list[str] = Field(
        default_factory=list,
        description=(
            "An explicit list of skill names/patterns to enable. If set, only these"
            " skills will be active. Supports glob patterns (e.g., 'search-*') and"
            " regex with 're:' prefix."
        ),
    )
    disabled_skills: list[str] = Field(
        default_factory=list,
        description=(
            "A list of skill names/patterns to disable. Ignored if 'enabled_skills'"
            " is set. Supports glob patterns and regex with 're:' prefix."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="BLITZY_", case_sensitive=False, extra="ignore"
    )

    @property
    def system_prompt(self) -> str:
        try:
            return SystemPrompt[self.system_prompt_id.upper()].read()
        except KeyError:
            pass

        for current_prompt_dir in [PROMPTS_DIR.path, GLOBAL_PROMPTS_DIR.path]:
            custom_sp_path = (current_prompt_dir / self.system_prompt_id).with_suffix(
                ".md"
            )
            if custom_sp_path.is_file():
                return custom_sp_path.read_text()

        raise MissingPromptFileError(
            self.system_prompt_id, str(PROMPTS_DIR.path), str(GLOBAL_PROMPTS_DIR.path)
        )

    def get_active_model(self) -> ModelConfig:
        for model in self.models:
            if model.alias == self.active_model:
                return model
        raise ValueError(
            f"Active model '{self.active_model}' not found in configuration."
        )

    def get_provider_for_model(self, model: ModelConfig) -> ProviderConfig:
        for provider in self.providers:
            if provider.name == model.provider:
                return provider
        raise ValueError(
            f"Provider '{model.provider}' for model '{model.name}' not found in configuration."
        )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Define the priority of settings sources.

        Note: dotenv_settings is intentionally excluded. API keys and other
        non-config environment variables are stored in .env but loaded manually
        into os.environ for use by providers. Only BLITZY_* prefixed environment
        variables (via env_settings) and TOML config are used for Pydantic settings.
        """
        return (
            init_settings,
            env_settings,
            TomlFileSettingsSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _check_api_key(self) -> VibeConfig:
        try:
            active_model = self.get_active_model()
            provider = self.get_provider_for_model(active_model)
            api_key_env = provider.api_key_env_var
            if api_key_env and not os.getenv(api_key_env):
                raise MissingAPIKeyError(api_key_env, provider.name)
        except ValueError:
            pass
        return self

    @model_validator(mode="after")
    def _check_api_backend_compatibility(self) -> VibeConfig:
        try:
            active_model = self.get_active_model()
            provider = self.get_provider_for_model(active_model)
            MISTRAL_API_BASES = [
                "https://codestral.mistral.ai",
                "https://api.mistral.ai",
            ]
            is_mistral_api = any(
                provider.api_base.startswith(api_base) for api_base in MISTRAL_API_BASES
            )
            if provider.backend in {
                Backend.ANTHROPIC,
                Backend.CLAUDE_CODE,
                Backend.BLITZY,
            }:
                pass  # these backends don't use the Mistral/generic API base convention
            elif (is_mistral_api and provider.backend != Backend.MISTRAL) or (
                not is_mistral_api and provider.backend != Backend.GENERIC
            ):
                raise WrongBackendError(provider.backend, is_mistral_api)

        except ValueError:
            pass
        return self

    @field_validator("tool_paths", mode="before")
    @classmethod
    def _expand_tool_paths(cls, v: Any) -> list[Path]:
        if not v:
            return []
        return [Path(p).expanduser().resolve() for p in v]

    @field_validator("skill_paths", mode="before")
    @classmethod
    def _expand_skill_paths(cls, v: Any) -> list[Path]:
        if not v:
            return []
        return [Path(p).expanduser().resolve() for p in v]

    @field_validator("tools", mode="before")
    @classmethod
    def _normalize_tool_configs(cls, v: Any) -> dict[str, BaseToolConfig]:
        if not isinstance(v, dict):
            return {}

        normalized: dict[str, BaseToolConfig] = {}
        for tool_name, tool_config in v.items():
            if isinstance(tool_config, BaseToolConfig):
                normalized[tool_name] = tool_config
            elif isinstance(tool_config, dict):
                normalized[tool_name] = BaseToolConfig.model_validate(tool_config)
            else:
                normalized[tool_name] = BaseToolConfig()

        return normalized

    @model_validator(mode="after")
    def _validate_model_uniqueness(self) -> VibeConfig:
        seen_aliases: set[str] = set()
        for model in self.models:
            if model.alias in seen_aliases:
                raise ValueError(
                    f"Duplicate model alias found: '{model.alias}'. Aliases must be unique."
                )
            seen_aliases.add(model.alias)
        return self

    @model_validator(mode="after")
    def _check_system_prompt(self) -> VibeConfig:
        _ = self.system_prompt
        return self

    @classmethod
    def save_updates(cls, updates: dict[str, Any]) -> None:
        CONFIG_DIR.path.mkdir(parents=True, exist_ok=True)
        current_config = TomlFileSettingsSource(cls).toml_data

        def deep_merge(target: dict, source: dict) -> None:
            for key, value in source.items():
                if (
                    key in target
                    and isinstance(target.get(key), dict)
                    and isinstance(value, dict)
                ):
                    deep_merge(target[key], value)
                elif (
                    key in target
                    and isinstance(target.get(key), list)
                    and isinstance(value, list)
                ):
                    if key in {"providers", "models"}:
                        target[key] = value
                    else:
                        target[key] = list(set(value + target[key]))
                else:
                    target[key] = value

        deep_merge(current_config, updates)
        cls.dump_config(
            to_jsonable_python(current_config, exclude_none=True, fallback=str)
        )

    @classmethod
    def dump_config(cls, config: dict[str, Any]) -> None:
        # ``tomli_w`` cannot serialize ``None`` values. The companion
        # ``save_updates`` classmethod already filters ``None`` via
        # ``to_jsonable_python(exclude_none=True)``; we mirror that
        # behaviour here so that callers who pass ``config.model_dump()``
        # directly do not need to repeat the filter step. This is required
        # because the new optional ``*_api_key`` fields default to ``None``
        # and would otherwise break serialization.
        def _strip_none(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: _strip_none(v) for k, v in value.items() if v is not None}
            if isinstance(value, list):
                return [_strip_none(v) for v in value if v is not None]
            return value

        sanitized = _strip_none(config)
        with CONFIG_FILE.path.open("wb") as f:
            tomli_w.dump(sanitized, f)

    @classmethod
    def _migrate(cls) -> None:
        pass

    @classmethod
    def load(cls, **overrides: Any) -> VibeConfig:
        cls._migrate()
        return cls(**(overrides or {}))

    @classmethod
    def create_default(cls) -> dict[str, Any]:
        try:
            config = cls()
        except MissingAPIKeyError:
            config = cls.model_construct()

        config_dict = config.model_dump(mode="json", exclude_none=True)

        from vibe.core.tools.manager import ToolManager

        tool_defaults = ToolManager.discover_tool_defaults()
        if tool_defaults:
            config_dict["tools"] = tool_defaults

        return config_dict
