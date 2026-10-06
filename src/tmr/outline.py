"""The outline: every heading in the document, to jump straight to one."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from tmr.keys import key_hints
from tmr.popup import Popup

Heading = tuple[int, str, str]
"""(level, text, widget id)"""


def heading_row(level: int, text: str, top_level: int) -> Content:
    indent = "  " * max(0, level - top_level)
    style = {1: "bold", 2: "", 3: "$text-muted"}.get(level - top_level + 1, "$text-muted italic")
    return Content(indent) + Content.styled(text, style)


def matches(text: str, query: str) -> bool:
    """Does the heading contain every word typed, in any order?"""
    haystack = text.casefold()
    return all(word in haystack for word in query.casefold().split())


class Outline(Popup[str | None]):
    """A pop-up list of the document's headings. Type to narrow it; Enter jumps."""

    DEFAULT_CSS = """
    Outline > Vertical {
        width: 70%;
        max-width: 80;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("pagedown", "move(10)", show=False),
        Binding("pageup", "move(-10)", show=False),
    ]

    def __init__(self, headings: list[Heading], current: int = -1) -> None:
        super().__init__()
        self.headings = headings
        self.current = current
        self.top_level = min((level for level, _, _ in headings), default=1)
        self.typed = ""

    def compose(self) -> ComposeResult:
        box = Vertical()
        box.border_title = "Outline"
        with box:
            with Horizontal(id="outline-search", classes="popup-search"):
                yield Label("›", id="outline-prompt", classes="popup-prompt")
                yield Input(placeholder="type to narrow the headings…", id="outline-input")
            yield OptionList(id="outline-list")
            yield Label(
                key_hints([("↑↓", "move"), ("⏎", "jump"), ("esc", "close")]),
                id="outline-footer",
                classes="popup-footer",
            )

    def on_mount(self) -> None:
        self.show("")
        options = self.query_one(OptionList)
        if self.current >= 0:
            options.highlighted = self.current
        self.query_one(Input).focus()

    def show(self, query: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            Option(heading_row(level, text, self.top_level), id=block_id)
            for level, text, block_id in self.headings
            if matches(text, query)
        )
        if options.option_count:
            options.highlighted = 0

    @on(Input.Changed, "#outline-input")
    def _typed(self, event: Input.Changed) -> None:
        if event.value != self.typed:
            self.typed = event.value
            self.show(event.value)

    @on(Input.Submitted, "#outline-input")
    def _submitted(self, event: Input.Submitted) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self.dismiss(options.get_option_at_index(options.highlighted).id)

    @on(OptionList.OptionSelected)
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_move(self, step: int) -> None:
        options = self.query_one(OptionList)
        if not options.option_count:
            return
        current = options.highlighted if options.highlighted is not None else -1
        options.highlighted = max(0, min(options.option_count - 1, current + step))

    def action_cancel(self) -> None:
        self.dismiss(None)
