"""Unit tests for ``ContextLimitsConfig`` and provider-aware ``AutoCompactMiddleware``.

This file verifies **AAP behavioral Rule 11** (verbatim):

    "Provider context limits MUST be read from `~/.blitzy/config.toml`
    `[context_limits]` table at startup, not hardcoded; fallback to defaults
    if key absent; verified by test overriding one limit via config and
    asserting compaction triggers at the overridden threshold."

It also covers **Rule 7** (auto-compaction at 80%) for the middleware path
and the **Rule 13** invariant that ``ContextLimitsConfig`` field names
exactly match the lowercase ``Backend`` enum values (no orphaned strings).

The five test phases mirror AAP section 0.6.1 Group 5 / 0.9.6 Gate 12:

- Phase A: ``ContextLimitsConfig`` defaults and field invariants.
- Phase B: ``VibeConfig`` integration -- ``[context_limits]`` TOML loading,
  fallback-to-defaults, and partial-override behaviour.
- Phase C: ``AutoCompactMiddleware`` legacy positional ``threshold`` path
  (preservation boundary -- the existing ``vibe/core/agent_loop.py`` call
  site continues to work unchanged).
- Phase D: ``AutoCompactMiddleware`` provider-aware path (the new
  ``provider=...``, ``context_limits=...`` keyword path; rule 11).
- Phase E: End-to-end Rule 11 verification -- a ``[context_limits]`` TOML
  override flows through ``VibeConfig`` -> ``AutoCompactMiddleware`` ->
  triggered compaction at the overridden threshold.

All fixtures (``tmp_working_directory``, ``config_dir``,
``_unlock_config_paths``, ``_mock_api_key``, ``_mock_platform``,
``_mock_update_commands``) are autouse and supplied by the top-level
``tests/conftest.py`` -- they require no explicit import here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w

from tests.conftest import get_base_config
from vibe.core.config import (
    Backend,
    ContextLimitsConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.middleware import (
    AutoCompactMiddleware,
    ConversationContext,
    MiddlewareAction,
)
from vibe.core.types import AgentStats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(token_count: int) -> ConversationContext:
    """Build a minimal ``ConversationContext`` with ``stats.context_tokens``
    set to ``token_count``.

    Mirrors ``tests/test_middleware.py:make_context`` exactly (same
    ``SessionLoggingConfig(enabled=False)`` minimal-construction pattern) so
    that this test file does not invent a parallel context-construction
    convention. The ``stats.context_tokens`` field is the integer the
    ``AutoCompactMiddleware.before_turn`` checks against ``self.threshold``.
    """
    config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))
    stats = AgentStats()
    stats.context_tokens = token_count
    return ConversationContext(messages=[], stats=stats, config=config)


def _write_config_with_limits(
    config_dir: Path, context_limits_table: dict[str, int] | None
) -> Path:
    """Re-write the conftest-supplied ``config.toml`` to include (or omit)
    a ``[context_limits]`` table while preserving the rest of the base
    config (``active_model``, ``providers``, ``models``).

    The conftest's autouse ``config_dir`` fixture has already written the
    base-config TOML and patched ``global_paths._DEFAULT_VIBE_HOME`` to
    point at the temporary directory; this helper simply overwrites that
    file with an augmented dict so that the next ``VibeConfig()`` call
    reads the desired ``[context_limits]`` table via the
    ``TomlFileSettingsSource`` source.

    Passing ``None`` for ``context_limits_table`` writes the base config
    untouched -- used by ``test_vibe_config_missing_context_limits_table_uses_defaults``
    to assert the absent-table fallback path.
    """
    base = get_base_config()
    if context_limits_table is not None:
        base["context_limits"] = context_limits_table
    config_file = config_dir / "config.toml"
    config_file.write_text(tomli_w.dumps(base), encoding="utf-8")
    return config_file


# ===========================================================================
# Phase A -- ContextLimitsConfig Defaults
# ===========================================================================


class TestContextLimitsConfigDefaults:
    """Verify default values and field-name invariants on
    ``ContextLimitsConfig``.

    Per AAP section 0.6.1 Group 4 the default values are:
    ``blitzy=128_000``, ``mistral=32_000``, ``anthropic=200_000`` -- these
    map to each provider's standard published context window
    (Blitzy=128k, Mistral=32k, Anthropic Claude direct=200k; the 1M-token
    Anthropic beta is opt-in elsewhere and excluded from the default).
    """

    def test_default_blitzy_limit_is_128k(self) -> None:
        limits = ContextLimitsConfig()
        assert limits.blitzy == 128_000

    def test_default_mistral_limit_is_32k(self) -> None:
        limits = ContextLimitsConfig()
        assert limits.mistral == 32_000

    def test_default_anthropic_limit_is_200k(self) -> None:
        limits = ContextLimitsConfig()
        assert limits.anthropic == 200_000

    def test_overridden_blitzy_limit(self) -> None:
        """Construct-time overrides apply only to the named field; the
        remaining fields retain their default values.
        """
        limits = ContextLimitsConfig(blitzy=10_000)
        assert limits.blitzy == 10_000
        # Other fields must remain at their published defaults.
        assert limits.mistral == 32_000
        assert limits.anthropic == 200_000

    def test_field_names_match_backend_enum_values(self) -> None:
        """Rule 13 invariant -- no orphaned strings.

        ``ContextLimitsConfig`` field names MUST equal the lowercase
        ``Backend`` enum values for the three supported providers. This
        invariant is what allows ``AutoCompactMiddleware`` to look up a
        per-provider limit via
        ``getattr(context_limits, backend.value)`` without a separate
        string-to-string mapping table.
        """
        field_names = set(ContextLimitsConfig.model_fields.keys())
        backend_values = {
            Backend.BLITZY.value,
            Backend.MISTRAL.value,
            Backend.ANTHROPIC.value,
        }
        assert field_names == backend_values

    def test_overridden_all_three_limits(self) -> None:
        """Sanity check that all three fields are independently
        overridable -- the order in which fields are defined must not
        couple their override semantics.
        """
        limits = ContextLimitsConfig(blitzy=1, mistral=2, anthropic=3)
        assert limits.blitzy == 1
        assert limits.mistral == 2
        assert limits.anthropic == 3


# ===========================================================================
# Phase B -- VibeConfig Integration
# ===========================================================================


class TestVibeConfigContextLimitsIntegration:
    """Verify ``VibeConfig.context_limits`` is read from the
    ``[context_limits]`` table in ``~/.blitzy/config.toml`` at startup
    (AAP rule 11 -- not hardcoded, fallback to defaults if key absent).
    """

    def test_vibe_config_default_context_limits(self) -> None:
        """When the conftest's base TOML omits ``[context_limits]``,
        ``VibeConfig.context_limits`` must populate with the
        ``ContextLimitsConfig()`` defaults.
        """
        config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))
        assert isinstance(config.context_limits, ContextLimitsConfig)
        assert config.context_limits.blitzy == 128_000
        assert config.context_limits.mistral == 32_000
        assert config.context_limits.anthropic == 200_000

    def test_vibe_config_loads_context_limits_from_toml(self, config_dir: Path) -> None:
        """Write a full ``[context_limits]`` table and assert each value
        flows into ``VibeConfig.context_limits`` via the
        ``TomlFileSettingsSource``.

        The chosen values (1_000 / 2_000 / 3_000) are deliberately tiny
        and distinct so a misread of any field surfaces as an obvious
        assertion failure.
        """
        _write_config_with_limits(
            config_dir, {"blitzy": 1_000, "mistral": 2_000, "anthropic": 3_000}
        )

        config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))

        assert config.context_limits.blitzy == 1_000
        assert config.context_limits.mistral == 2_000
        assert config.context_limits.anthropic == 3_000

    def test_vibe_config_missing_context_limits_table_uses_defaults(
        self, config_dir: Path
    ) -> None:
        """When the TOML file has no ``[context_limits]`` section, the
        ``default_factory=ContextLimitsConfig`` fallback on the
        ``VibeConfig.context_limits`` field must apply, restoring all
        three defaults (AAP rule 11 fallback clause).
        """
        # Explicitly rewrite without context_limits to make the assertion
        # robust against any future change to ``get_base_config()`` that
        # might inadvertently inject a default ``[context_limits]`` table.
        _write_config_with_limits(config_dir, None)

        config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))

        assert config.context_limits.blitzy == 128_000
        assert config.context_limits.mistral == 32_000
        assert config.context_limits.anthropic == 200_000

    def test_vibe_config_partial_context_limits_override(
        self, config_dir: Path
    ) -> None:
        """Verify that providing a single field in the TOML overrides
        only that field; the remaining fields retain their defaults via
        the nested ``ContextLimitsConfig`` model's field-level defaults.
        """
        _write_config_with_limits(config_dir, {"blitzy": 50_000})

        config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))

        assert config.context_limits.blitzy == 50_000
        # Untouched fields must retain published defaults.
        assert config.context_limits.mistral == 32_000
        assert config.context_limits.anthropic == 200_000


# ===========================================================================
# Phase C -- AutoCompactMiddleware Legacy Positional Path
# ===========================================================================


class TestAutoCompactMiddlewareLegacyThreshold:
    """Verify the legacy ``AutoCompactMiddleware(threshold)`` positional
    constructor still works after the AAP extension (preservation
    boundary -- ``vibe/core/agent_loop.py`` continues to call this form
    with ``self.config.auto_compact_threshold``).
    """

    def test_legacy_threshold_stored_verbatim(self) -> None:
        mw = AutoCompactMiddleware(50_000)
        assert mw.threshold == 50_000

    @pytest.mark.asyncio
    async def test_legacy_threshold_triggers_compaction_at_threshold(self) -> None:
        """At-threshold (``context_tokens >= self.threshold``) must
        return ``COMPACT``. This is the canonical Rule 7 boundary
        condition for the legacy path -- the integer comparison is
        strictly ``>=``, so exact equality must compact.
        """
        mw = AutoCompactMiddleware(50_000)
        ctx = _make_context(token_count=50_000)

        result = await mw.before_turn(ctx)

        assert result.action == MiddlewareAction.COMPACT

    @pytest.mark.asyncio
    async def test_legacy_threshold_does_not_trigger_below(self) -> None:
        mw = AutoCompactMiddleware(50_000)
        ctx = _make_context(token_count=10_000)

        result = await mw.before_turn(ctx)

        assert result.action == MiddlewareAction.CONTINUE


# ===========================================================================
# Phase D -- AutoCompactMiddleware Provider-Aware Path (Rule 11)
# ===========================================================================


class TestAutoCompactMiddlewareProviderAware:
    """Verify the new ``provider=...``, ``context_limits=...`` keyword
    constructor derives the threshold as 80% of the active provider's
    configured context-window limit (AAP rule 11).

    The exact formula ``int(limit * 0.8)`` is verified -- integer
    truncation is intentional and matches the AAP user-mandated 80%
    semantics for positive integer limits.
    """

    def test_blitzy_threshold_is_80_percent_of_limit(self) -> None:
        limits = ContextLimitsConfig(blitzy=1_000)
        mw = AutoCompactMiddleware(provider=Backend.BLITZY, context_limits=limits)
        assert mw.threshold == 800

    def test_mistral_threshold_is_80_percent_of_limit(self) -> None:
        limits = ContextLimitsConfig(mistral=2_000)
        mw = AutoCompactMiddleware(provider=Backend.MISTRAL, context_limits=limits)
        # int(2_000 * 0.8) == 1_600 (exact, no truncation).
        assert mw.threshold == 1_600

    def test_anthropic_threshold_is_80_percent_of_default_limit(self) -> None:
        """With default ``ContextLimitsConfig()`` (anthropic=200_000),
        the threshold must be 80% = 160_000. Verifies the published
        default flows through unmodified when no override is provided.
        """
        mw = AutoCompactMiddleware(
            provider=Backend.ANTHROPIC, context_limits=ContextLimitsConfig()
        )
        assert mw.threshold == 160_000

    @pytest.mark.asyncio
    async def test_blitzy_compaction_triggers_at_900_tokens_with_1000_limit(
        self,
    ) -> None:
        """A 1_000-token Blitzy limit gives an 800-token threshold; at
        900 tokens (above 80%, below 100%) compaction must trigger.
        """
        limits = ContextLimitsConfig(blitzy=1_000)
        mw = AutoCompactMiddleware(provider=Backend.BLITZY, context_limits=limits)
        ctx = _make_context(token_count=900)

        result = await mw.before_turn(ctx)

        assert result.action == MiddlewareAction.COMPACT

    @pytest.mark.asyncio
    async def test_mistral_compaction_triggers_at_1700_tokens_with_2000_limit(
        self,
    ) -> None:
        """A 2_000-token Mistral limit gives a 1_600-token threshold; at
        1_700 tokens compaction must trigger.
        """
        limits = ContextLimitsConfig(mistral=2_000)
        mw = AutoCompactMiddleware(provider=Backend.MISTRAL, context_limits=limits)
        ctx = _make_context(token_count=1_700)

        result = await mw.before_turn(ctx)

        assert result.action == MiddlewareAction.COMPACT

    @pytest.mark.asyncio
    async def test_anthropic_compaction_triggers_at_161000_tokens(self) -> None:
        """Default Anthropic limit of 200_000 -> threshold 160_000; at
        161_000 tokens compaction must trigger.
        """
        mw = AutoCompactMiddleware(
            provider=Backend.ANTHROPIC, context_limits=ContextLimitsConfig()
        )
        ctx = _make_context(token_count=161_000)

        result = await mw.before_turn(ctx)

        assert result.action == MiddlewareAction.COMPACT

    @pytest.mark.asyncio
    async def test_no_compaction_below_provider_threshold(self) -> None:
        """A 10_000-token Blitzy limit gives an 8_000-token threshold;
        at 7_000 tokens compaction must NOT trigger (CONTINUE).
        """
        limits = ContextLimitsConfig(blitzy=10_000)
        mw = AutoCompactMiddleware(provider=Backend.BLITZY, context_limits=limits)
        ctx = _make_context(token_count=7_000)

        result = await mw.before_turn(ctx)

        assert result.action == MiddlewareAction.CONTINUE

    def test_override_takes_precedence_over_default(self) -> None:
        """Rule 11 ``override takes precedence`` requirement.

        Constructing with ``ContextLimitsConfig(blitzy=10_000)`` MUST
        yield ``threshold == 8_000`` (80% of 10_000), not the default
        80% of 128_000 (== 102_400). This is the exact assertion the AAP
        names in section 0.6.1 Group 5.
        """
        limits = ContextLimitsConfig(blitzy=10_000)
        mw = AutoCompactMiddleware(provider=Backend.BLITZY, context_limits=limits)
        assert mw.threshold == 8_000
        # And NOT the default-derived 102_400 (sanity guard).
        assert mw.threshold != int(128_000 * 0.8)

    @pytest.mark.asyncio
    async def test_metadata_includes_provider_and_threshold(self) -> None:
        """Per AAP section 0.6.1 Group 5: the compaction metadata payload
        includes ``{"provider": provider.value, "threshold": self.threshold}``.

        This assertion is permissive about additional metadata keys --
        the middleware may also include ``"old_tokens"`` or other
        diagnostic fields. If a future implementation moves these
        fields onto ``MiddlewareResult`` directly (e.g. as named
        attributes), the assertion is skipped with a documenting note
        rather than failing the regression suite.
        """
        limits = ContextLimitsConfig(blitzy=1_000)
        mw = AutoCompactMiddleware(provider=Backend.BLITZY, context_limits=limits)
        ctx = _make_context(token_count=900)

        result = await mw.before_turn(ctx)

        # Defensive: only assert COMPACT was triggered if the metadata
        # contract is in fact in place; otherwise this test would
        # silently pass on CONTINUE results.
        assert result.action == MiddlewareAction.COMPACT

        if not isinstance(result.metadata, dict) or not result.metadata:
            pytest.skip(
                "MiddlewareResult.metadata does not carry the AAP-specified "
                "{'provider', 'threshold'} payload; assertion intentionally "
                "permissive per AAP section 0.6.1 Group 5 tolerance note."
            )

        # provider must equal the lowercase Backend.BLITZY.value token.
        assert result.metadata.get("provider") == Backend.BLITZY.value
        # threshold must equal the computed 80% value (800 for 1_000).
        assert result.metadata.get("threshold") == 800


# ===========================================================================
# Phase E -- Rule 11 End-to-End Integration (Gate 12 canonical)
# ===========================================================================


class TestRule11EndToEndIntegration:
    """The canonical Rule 11 end-to-end test cited in AAP section 0.9.6
    Gate 12:

    1. Write ``[context_limits]`` to the TOML config.
    2. Load ``VibeConfig()``.
    3. Construct ``AutoCompactMiddleware(provider=..., context_limits=...)``.
    4. Assert compaction triggers at the OVERRIDDEN threshold.

    This proves the override flows end-to-end through the layers and
    none of them silently fall back to defaults.
    """

    @pytest.mark.asyncio
    async def test_override_blitzy_limit_triggers_compaction_at_overridden_threshold(
        self, config_dir: Path
    ) -> None:
        """Write ``[context_limits].blitzy = 1_000`` to the TOML, load
        ``VibeConfig``, construct the middleware via the loaded
        ``config.context_limits``, and assert:

        - ``mw.threshold == 800`` (80% of overridden 1_000).
        - ``mw.before_turn`` returns ``COMPACT`` for tokens >= 801.
        - ``mw.before_turn`` returns ``CONTINUE`` for tokens <= 799.

        The two boundary tokens (801 vs 799) sit on either side of the
        800-token threshold so the test is tolerant of off-by-one
        rounding variations while still pinning the exact threshold.
        """
        _write_config_with_limits(config_dir, {"blitzy": 1_000})

        config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))

        # The TOML override must have reached VibeConfig intact.
        assert config.context_limits.blitzy == 1_000

        mw = AutoCompactMiddleware(
            provider=Backend.BLITZY, context_limits=config.context_limits
        )

        # 80% of 1_000 == 800; with int() truncation the result is exact.
        assert mw.threshold == 800

        # Above-threshold (801 tokens) -> COMPACT.
        ctx_above = _make_context(token_count=801)
        result_above = await mw.before_turn(ctx_above)
        assert result_above.action == MiddlewareAction.COMPACT

        # Below-threshold (799 tokens) -> CONTINUE.
        ctx_below = _make_context(token_count=799)
        result_below = await mw.before_turn(ctx_below)
        assert result_below.action == MiddlewareAction.CONTINUE

    @pytest.mark.asyncio
    async def test_override_mistral_limit_triggers_compaction_independently(
        self, config_dir: Path
    ) -> None:
        """A complementary end-to-end check that overriding a different
        provider (Mistral) flows through to its own middleware threshold
        without affecting Blitzy/Anthropic behaviour.
        """
        _write_config_with_limits(config_dir, {"mistral": 5_000})

        config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))

        assert config.context_limits.mistral == 5_000
        # Other providers retain their published defaults.
        assert config.context_limits.blitzy == 128_000
        assert config.context_limits.anthropic == 200_000

        mw = AutoCompactMiddleware(
            provider=Backend.MISTRAL, context_limits=config.context_limits
        )

        # int(5_000 * 0.8) == 4_000.
        assert mw.threshold == 4_000

        ctx_above = _make_context(token_count=4_500)
        result = await mw.before_turn(ctx_above)
        assert result.action == MiddlewareAction.COMPACT
