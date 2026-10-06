"""What every pop-up has in common: the look, and the app left in view behind it.

Find a file, the outline, switching worktree and the commit message all share
one pattern: a title in the border, a `›` line to type on, a list, and a
footer with the keys (in bold) and a count on the right. They say it once, here.

Behind a pop-up the app stays on show, dimmed, so you keep your bearings.
Textual would blank it out instead: in a terminal's own colours there's no
way to blend it with the background. So all of it is drawn in grey (the
terminal's "bright black"), and faint as well, which sets it well back.
"""

from __future__ import annotations

from typing import TypeVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from textual.renderables.background_screen import BackgroundScreen
from textual.screen import ModalScreen, Screen

Result = TypeVar("Result")

DIM = Style(color="bright_black", dim=True)
"""How everything behind a pop-up is drawn; its backgrounds are left out too."""


class _Dimmed:
    """A screen drawn faint and grey, on no background, its links and clicks taken out."""

    def __init__(self, screen: Screen) -> None:
        self.screen = screen

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for text, style, control in console.render(self.screen._compositor, options):
            if control:
                yield Segment(text, style, control)
            else:
                plain = Style() if style is None else style.clear_meta_and_links()
                yield Segment(text, plain + DIM + Style(bgcolor="default"))


class Popup(ModalScreen[Result]):
    """A pop-up over the app, which stays in view, dimmed."""

    DEFAULT_CSS = """
    Popup {
        align: center top;
    }
    Popup > Vertical {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        margin-top: 2;
        border: round ansi_bright_black;
        border-title-color: $foreground;
        border-title-style: bold;
        background: $surface;
        padding: 1 2 0 2;
    }
    Popup .popup-search {
        height: 2;
        border-bottom: solid ansi_bright_black;
    }
    Popup .popup-prompt {
        width: 2;
        color: $primary;
        text-style: bold;
    }
    Popup Input {
        border: none;
        height: 1;
        padding: 0;
        background: transparent;
    }
    Popup Input:focus {
        border: none;
    }
    Popup Input > .input--selection {
        background: ansi_bright_black;
        color: $foreground;
    }
    Popup OptionList {
        height: auto;
        max-height: 30;
        margin: 1 0;
        border: none;
        padding: 0;
        background: transparent;
        scrollbar-size-vertical: 1;
    }
    Popup OptionList:focus {
        border: none;
    }
    Popup OptionList > .option-list--option-highlighted {
        background: ansi_bright_black;
        text-style: bold;
    }
    Popup .popup-footer {
        dock: bottom;
        width: 1fr;
        height: 3;
        border-top: solid ansi_bright_black;
        padding-bottom: 1;
        text-style: dim;
    }
    Popup .popup-keys {
        width: 1fr;
    }
    Popup .popup-count {
        width: auto;
    }
    """

    def render(self):
        drawn = super().render()
        if isinstance(drawn, BackgroundScreen):
            return _Dimmed(drawn.screen)
        return drawn
