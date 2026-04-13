from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.color import Color
from textual.timer import Timer
from textual.widgets import Static

from vibe import __version__
from vibe.cli.textual_ui.widgets.welcome_helpers import (
    AnimationHelper,
    LineAnimationState,
    MetadataRenderer,
    hex_to_rgb,
)
from vibe.core.config import VibeConfig


class WelcomeBanner(Static):
    FLASH_COLOR = "#FFFFFF"
    TARGET_COLORS = ("#7C5DF5", "#6B4AF0", "#5B39F3", "#4A2DD4", "#3A1FB5")
    BORDER_TARGET_COLOR = "#5B39F3"

    LINE_ANIMATION_DURATION_MS = 200
    LINE_STAGGER_MS = 280
    FLASH_RESET_DURATION_MS = 400
    ANIMATION_TICK_INTERVAL = 0.1

    COLOR_FLASH_MIDPOINT = 0.5
    COLOR_PHASE_SCALE = 2.0
    COLOR_CACHE_THRESHOLD = 0.001
    BORDER_PROGRESS_THRESHOLD = 0.01

    BLOCK = "▇▇"
    SPACE = "  "
    LOGO_TEXT_GAP = "   "

    def __init__(self, config: VibeConfig) -> None:
        super().__init__(" ")
        self.config = config
        self.animation_timer = None
        self._animation_start_time: float | None = None

        self._cached_skeleton_color: str | None = None
        self._cached_skeleton_rgb: tuple[int, int, int] | None = None
        self._flash_rgb = hex_to_rgb(self.FLASH_COLOR)
        self._target_rgbs = [hex_to_rgb(c) for c in self.TARGET_COLORS]
        self._border_target_rgb = hex_to_rgb(self.BORDER_TARGET_COLOR)

        self._line_states = [LineAnimationState() for _ in self.TARGET_COLORS]
        self.border_progress = 0.0
        self._cached_border_color: str | None = None
        self._cached_border_progress = -1.0

        self._line_duration = self.LINE_ANIMATION_DURATION_MS / 1000
        self._line_stagger = self.LINE_STAGGER_MS / 1000
        self._border_duration = self.FLASH_RESET_DURATION_MS / 1000
        self._line_start_times = [
            idx * self._line_stagger for idx in range(len(self.TARGET_COLORS))
        ]
        self._all_lines_finish_time = (
            (len(self.TARGET_COLORS) - 1) * self.LINE_STAGGER_MS
            + self.LINE_ANIMATION_DURATION_MS
        ) / 1000

        self._cached_text_lines: list[Text | None] = [None] * 7
        self._initialize_static_line_suffixes()

        self._animation_helper = AnimationHelper(self)
        self._metadata_renderer = MetadataRenderer(self)

    def _initialize_static_line_suffixes(self) -> None:
        self._static_line1_suffix = (
            f"{self.LOGO_TEXT_GAP}[b]Blitzy Agent v{__version__}[/]"
        )
        self._static_line2_suffix = (
            f"{self.LOGO_TEXT_GAP}[dim]{self.config.active_model}[/]"
        )
        mcp_count = len(self.config.mcp_servers)
        model_count = len(self.config.models)
        self._static_line3_suffix = f"{self.LOGO_TEXT_GAP}[dim]{model_count} models · {mcp_count} MCP servers[/]"
        self._static_line5_suffix = (
            f"{self.LOGO_TEXT_GAP}[dim]{self.config.displayed_workdir or Path.cwd()}[/]"
        )
        self._static_line7 = f"[dim]Type[/] [{self.BORDER_TARGET_COLOR}]/help[/] [dim]for more information • [/][{self.BORDER_TARGET_COLOR}]/terminal-setup[/][dim] for shift+enter[/]"

    @property
    def skeleton_color(self) -> str:
        return self._cached_skeleton_color or "#1e1e1e"

    @property
    def skeleton_rgb(self) -> tuple[int, int, int]:
        return self._cached_skeleton_rgb or hex_to_rgb("#1e1e1e")

    def on_mount(self) -> None:
        if not self.config.disable_welcome_banner_animation:
            self.call_after_refresh(self._init_after_styles)

    def _init_after_styles(self) -> None:
        self._cache_skeleton_color()
        self._cached_text_lines[5] = Text("")
        self._cached_text_lines[6] = Text.from_markup(self._static_line7)
        self._metadata_renderer.update_display()
        self._animation_helper.start_animation()

    def _cache_skeleton_color(self) -> None:
        try:
            border = self.styles.border
            if (
                hasattr(border, "top")
                and isinstance(edge := border.top, tuple)
                and len(edge) >= 2  # noqa: PLR2004
                and isinstance(color := edge[1], Color)
            ):
                self._cached_skeleton_color = color.hex
                self._cached_skeleton_rgb = hex_to_rgb(color.hex)
                return
        except (AttributeError, TypeError):
            pass

        self._cached_skeleton_color = "#1e1e1e"
        self._cached_skeleton_rgb = hex_to_rgb("#1e1e1e")

    def _stop_timer(self) -> None:
        if self.animation_timer:
            try:
                self.animation_timer.stop()
            except (AttributeError, RuntimeError):
                pass
            self.animation_timer: Timer | None = None

    def on_unmount(self) -> None:
        self._stop_timer()
