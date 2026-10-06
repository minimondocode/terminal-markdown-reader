"""tmr's own 16 colours, for terminals whose palette we can't count on.

Everything tmr draws is coloured with the terminal's 16 ANSI colours ("blue",
"bright black"), so with `TMR_COLORS=terminal` it takes on the look of a
terminal with a well-chosen palette. Many terminals' palettes are harsh,
though (macOS Terminal's blue is a deep navy that's hard to read on black or
white), so by default tmr swaps each of the 16 colours for one of its own as
the screen is drawn.

The terminal's own text and background colours are left alone, so tmr still
sits on the background you chose, with text in the colour you chose.
"""

from __future__ import annotations

import os
from functools import lru_cache

from rich.color import Color as RichColor
from rich.color import ColorType
from rich.segment import Segment
from rich.style import Style
from textual.color import Color
from textual.filter import LineFilter

DARK = [
    "#22272e",  # black
    "#f47067",  # red
    "#57ab5a",  # green
    "#c69026",  # yellow
    "#539bf5",  # blue
    "#b083f0",  # magenta
    "#39c5cf",  # cyan
    "#adbac7",  # white
    "#636e7b",  # bright black
    "#ff938a",  # bright red
    "#6bc46d",  # bright green
    "#daaa3f",  # bright yellow
    "#6cb6ff",  # bright blue
    "#dcbdfb",  # bright magenta
    "#56d4dd",  # bright cyan
    "#cdd9e5",  # bright white
]
"""For dark backgrounds."""

LIGHT = [
    "#24292f",  # black
    "#cf222e",  # red
    "#2a8f45",  # green
    "#b08800",  # yellow
    "#0969da",  # blue
    "#8250df",  # magenta
    "#1b7c83",  # cyan
    "#e8ecf0",  # white: only ever a background (scrollbars, code blocks), so light
    "#a0a8b0",  # bright black
    "#a40e26",  # bright red
    "#2da44e",  # bright green
    "#bf8700",  # bright yellow
    "#218bff",  # bright blue
    "#a475f9",  # bright magenta
    "#3192aa",  # bright cyan
    "#ffffff",  # bright white
]
"""For light backgrounds."""


def use_own_palette() -> bool:
    """Should tmr use its own colours rather than the terminal's?

    tmr's own unless `TMR_COLORS=terminal` asks for the terminal's (a terminal
    that knows its palette suits tmr can set that itself).
    """
    return os.environ.get("TMR_COLORS", "").strip().lower() != "terminal"


class Palette(LineFilter):
    """Swaps the terminal's 16 colours for tmr's own as each line is drawn."""

    def __init__(self, dark: bool) -> None:
        super().__init__()
        self.colors = [RichColor.parse(color) for color in (DARK if dark else LIGHT)]

    def _swap(self, color: RichColor | None) -> RichColor | None:
        if color is None or color.number is None:
            return color
        if color.type in (ColorType.STANDARD, ColorType.EIGHT_BIT) and color.number < 16:
            return self.colors[color.number]
        return color

    @lru_cache(1024)
    def restyle(self, style: Style) -> Style:
        color, bgcolor = self._swap(style.color), self._swap(style.bgcolor)
        if color is style.color and bgcolor is style.bgcolor:
            return style
        return style + Style.from_color(color, bgcolor)

    def apply(self, segments: list[Segment], background: Color) -> list[Segment]:
        restyle = self.restyle
        return [
            Segment(text, restyle(style) if style is not None else None, control)
            for text, style, control in segments
        ]
