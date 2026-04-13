"""Helper classes extracted from ``WelcomeBanner`` for file-level method count reduction.

This module contains:
* Utility functions: ``hex_to_rgb``, ``rgb_to_hex``, ``interpolate_color``
* ``LineAnimationState`` — animation progress dataclass
* ``AnimationHelper`` — animation tick logic (composed into ``WelcomeBanner``)
* ``MetadataRenderer`` — rich-text line rendering (composed into ``WelcomeBanner``)

All classes are composed into ``WelcomeBanner`` via ``__init__`` injection (R2).
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from rich.align import Align
from rich.console import Group
from rich.text import Text

if TYPE_CHECKING:
    from vibe.cli.textual_ui.widgets.welcome import WelcomeBanner

__all__ = [
    "AnimationHelper",
    "LineAnimationState",
    "MetadataRenderer",
    "hex_to_rgb",
    "interpolate_color",
    "rgb_to_hex",
]


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert a hex colour string to an RGB tuple."""
    normalized = hex_color.lstrip("#")
    r, g, b = (int(normalized[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB integer values to a hex colour string."""
    return f"#{r:02x}{g:02x}{b:02x}"


def interpolate_color(
    start_rgb: tuple[int, int, int], end_rgb: tuple[int, int, int], progress: float
) -> str:
    """Linearly interpolate between two RGB colours and return as hex."""
    progress = max(0.0, min(1.0, progress))
    r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * progress)
    g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * progress)
    b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * progress)
    return rgb_to_hex(r, g, b)


@dataclass
class LineAnimationState:
    """Per-line animation progress state."""

    progress: float = 0.0
    cached_color: str | None = None
    cached_progress: float = -1.0
    rendered_color: str | None = None


class AnimationHelper:
    """Handles animation logic for WelcomeBanner."""

    def __init__(self, banner: WelcomeBanner) -> None:
        self._banner = banner

    def start_animation(self) -> None:
        self._banner._animation_start_time = monotonic()

        def tick() -> None:
            if self.is_animation_complete():
                self._banner._stop_timer()
                return
            if self._banner._animation_start_time is None:
                return

            elapsed = monotonic() - self._banner._animation_start_time
            updated_lines = self.advance_line_progress(elapsed)
            border_updated = self.advance_border_progress(elapsed)

            if border_updated:
                self.update_border_color()
            if updated_lines or border_updated:
                self._banner._metadata_renderer.update_display()

        self._banner.animation_timer = self._banner.set_interval(
            self._banner.ANIMATION_TICK_INTERVAL, tick
        )

    def advance_line_progress(self, elapsed: float) -> bool:
        any_updates = False
        for line_idx, state in enumerate(self._banner._line_states):
            if state.progress >= 1.0:
                continue
            start_time = self._banner._line_start_times[line_idx]
            if elapsed < start_time:
                continue
            progress = min(1.0, (elapsed - start_time) / self._banner._line_duration)
            if progress > state.progress:
                state.progress = progress
                any_updates = True
        return any_updates

    def advance_border_progress(self, elapsed: float) -> bool:
        if elapsed < self._banner._all_lines_finish_time:
            return False

        new_progress = min(
            1.0,
            (elapsed - self._banner._all_lines_finish_time)
            / self._banner._border_duration,
        )

        if (
            abs(new_progress - self._banner.border_progress)
            > self._banner.BORDER_PROGRESS_THRESHOLD
        ):
            self._banner.border_progress = new_progress
            return True

        return False

    def is_animation_complete(self) -> bool:
        return (
            all(state.progress >= 1.0 for state in self._banner._line_states)
            and self._banner.border_progress >= 1.0
        )

    def update_border_color(self) -> None:
        progress = self._banner.border_progress
        if (
            abs(progress - self._banner._cached_border_progress)
            < self._banner.COLOR_CACHE_THRESHOLD
        ):
            return

        border_color = self.compute_color_for_progress(
            progress, self._banner._border_target_rgb
        )
        self._banner._cached_border_color = border_color
        self._banner._cached_border_progress = progress
        self._banner.styles.border = ("round", border_color)

    def compute_color_for_progress(
        self, progress: float, target_rgb: tuple[int, int, int]
    ) -> str:
        if progress <= 0:
            return self._banner.skeleton_color

        if progress <= self._banner.COLOR_FLASH_MIDPOINT:
            phase = progress * self._banner.COLOR_PHASE_SCALE
            return interpolate_color(
                self._banner.skeleton_rgb, self._banner._flash_rgb, phase
            )

        phase = (
            progress - self._banner.COLOR_FLASH_MIDPOINT
        ) * self._banner.COLOR_PHASE_SCALE
        return interpolate_color(self._banner._flash_rgb, target_rgb, phase)


class MetadataRenderer:
    """Handles display and metadata rendering for WelcomeBanner."""

    def __init__(self, banner: WelcomeBanner) -> None:
        self._banner = banner

    def update_display(self) -> None:
        for idx in range(5):
            self.update_colored_line(idx, idx)

        lines = [line if line else Text("") for line in self._banner._cached_text_lines]
        self._banner.update(Align.center(Group(*lines)))

    def get_color(self, line_idx: int) -> str:
        state = self._banner._line_states[line_idx]
        if (
            abs(state.progress - state.cached_progress)
            < self._banner.COLOR_CACHE_THRESHOLD
            and state.cached_color
        ):
            return state.cached_color

        color = self._banner._animation_helper.compute_color_for_progress(
            state.progress, self._banner._target_rgbs[line_idx]
        )
        state.cached_color = color
        state.cached_progress = state.progress
        return color

    def update_colored_line(self, slot_idx: int, line_idx: int) -> None:
        color = self.get_color(line_idx)
        state = self._banner._line_states[line_idx]

        if color == state.rendered_color and self._banner._cached_text_lines[slot_idx]:
            return

        state.rendered_color = color
        self._banner._cached_text_lines[slot_idx] = Text.from_markup(
            self.build_line(slot_idx, color)
        )

    def build_line(self, line_idx: int, color: str) -> str:
        B = self._banner.BLOCK
        S = self._banner.SPACE
        suffix1 = self._banner._static_line1_suffix
        suffix2 = self._banner._static_line2_suffix
        suffix3 = self._banner._static_line3_suffix
        suffix5 = self._banner._static_line5_suffix

        patterns = [
            f"{S}[{color}]{B}{B}{B}{B}{B}[/]{S}{suffix1}",
            f"{S}[{color}]{B}[/]{S}{S}{S}[{color}]{B}[/]{S}{suffix2}",
            f"{S}[{color}]{B}{B}{B}{B}[/]{S}{S}{suffix3}",
            f"{S}[{color}]{B}[/]{S}{S}{S}[{color}]{B}[/]{S}",
            f"{S}[{color}]{B}{B}{B}{B}{B}[/]{S}{suffix5}",
        ]
        return patterns[line_idx]
