"""The vertical bar between the columns; drag it to resize the file list."""

from __future__ import annotations

from textual import events
from textual.content import Content
from textual.message import Message
from textual.widget import Widget

DEFAULT_PERCENT = 20.0
WIDE_PERCENT = 50.0
"""How wide `b` makes the sidebar for now: half the window."""
MIN_COLUMNS = 12
"""The file list never gets narrower than this."""
MIN_DOCUMENT_COLUMNS = 30
"""...and always leaves at least this much room for the document."""


class Divider(Widget):
    """Drag to resize the column on its left. Double-click to reset."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    Divider {
        width: 2;
        height: 1fr;
        color: $foreground 20%;
    }
    Divider:hover, Divider.-dragging {
        color: $primary;
        text-style: bold;
    }
    """

    class Resized(Message):
        """The user finished dragging; `percent` is the new width of the left column."""

        def __init__(self, percent: float) -> None:
            super().__init__()
            self.percent = percent

    def __init__(self, target: Widget, **kwargs) -> None:
        super().__init__(**kwargs)
        self.target = target
        self._dragging = False

    def render(self) -> Content:
        # A plain thin line, set off from the file list's scrollbar by a blank column
        # (which can be grabbed too), so it doesn't read as one. Under the mouse it
        # thickens and lights up, the way a resize handle does in a GUI: that says
        # "drag me" without a tooltip covering the text on both sides.
        line = " ┃" if self.mouse_hover or self._dragging else " │"
        return Content("\n").join(Content(line) for _ in range(self.size.height))

    def watch_mouse_hover(self, _hover: bool) -> None:
        self.refresh()

    def _columns(self) -> int:
        parent = self.parent
        return parent.size.width if isinstance(parent, Widget) else self.screen.size.width

    def percent_for(self, columns_from_left: int) -> float:
        total = max(1, self._columns())
        widest = max(MIN_COLUMNS, total - MIN_DOCUMENT_COLUMNS)
        columns = max(MIN_COLUMNS, min(widest, columns_from_left))
        return round(columns / total * 100, 1)

    def apply(self, percent: float) -> None:
        self.target.styles.width = f"{percent}%"

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self._dragging = True
        self.add_class("-dragging")
        self.refresh()
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        event.stop()
        left = self.target.region.x
        self.apply(self.percent_for(event.screen_x - left))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        self.remove_class("-dragging")
        self.refresh()
        self.release_mouse()
        left = self.target.region.x
        percent = self.percent_for(event.screen_x - left)
        self.apply(percent)
        self.post_message(self.Resized(percent))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if event.chain == 2:
            self.apply(DEFAULT_PERCENT)
            self.post_message(self.Resized(DEFAULT_PERCENT))
