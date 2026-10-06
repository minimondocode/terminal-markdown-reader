"""Find a markdown file by typing part of its name, optionally inside a recently changed folder."""

from __future__ import annotations

import time
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content, Span
from textual.timer import Timer
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from tmr.keys import key_hints
from tmr.popup import Popup
from tmr.search import Entry, FileIndex, Hit, in_folder, recent_folders, search

TYPING_PAUSE = 0.06
"""Wait this long after a keystroke before searching, so fast typing only searches once."""


def highlighted(hit: Hit, age_width: int = 0) -> Content:
    """One result row. With `age_width`, how long ago it changed is shown at that column's end."""
    text = hit.entry.relative
    spans = [Span(at, at + 1, "bold $accent underline") for at in hit.positions if at < len(text)]
    # Dim the folder part so the file's own name stands out.
    if hit.entry.name_start:
        spans.insert(0, Span(0, hit.entry.name_start, "$text-muted"))
    row = Content(text, spans=spans)
    if age_width:
        age = short_age(hit.entry.modified)
        gap = max(2, age_width - row.cell_length - len(age))
        row = row + " " * gap + Content.styled(age, "$text-muted")
    return row


def short_age(modified: float) -> str:
    """How long ago, as briefly as possible: "now", "5m", "3h", "2d", "6w"."""
    seconds = max(0.0, time.time() - modified)
    for size, unit in ((60 * 60 * 24 * 7, "w"), (60 * 60 * 24, "d"), (60 * 60, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


def _entries(index: FileIndex | list[Entry] | None) -> list[Entry] | None:
    """The files to search, as a list that stays the same while searching it."""
    if isinstance(index, FileIndex):
        return index.snapshot()
    return index


class FileFinder(Popup[Path | None]):
    """A pop-up list of markdown files that narrows as you type."""

    DEFAULT_CSS = """
    FileFinder #finder-folders {
        height: 1;
        margin-top: 1;
        text-style: dim;
        text-wrap: nowrap;
        display: none;
        link-style: none;
        link-color: $foreground;
        link-background: transparent;
        link-style-hover: bold;
        link-color-hover: $foreground;
        link-background-hover: transparent;
    }
    FileFinder #finder-folders.-visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("pagedown", "move(10)", show=False),
        Binding("pageup", "move(-10)", show=False),
        Binding("tab", "pick_folder(1)", "Next folder", show=False, priority=True),
        Binding("shift+tab", "pick_folder(-1)", "Previous folder", show=False, priority=True),
    ]

    def __init__(self, index: FileIndex | list[Entry] | None) -> None:
        super().__init__()
        self.entries: list[Entry] | None = _entries(index)
        self.recent: list[tuple[str, float]] = []
        """Recently changed folders, newest first, with when they changed."""
        self.folders: list[tuple[str, float]] = []
        """The ones of those that fit on the line, and so are on offer."""
        self.folder: str | None = None
        """Only look inside this folder (None: everywhere)."""
        self.shown: list[Path] = []
        self._pending: Timer | None = None
        self._shown_query: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="finder-search", classes="popup-search"):
                yield Label("›", id="finder-prompt", classes="popup-prompt")
                yield Input(placeholder="Type part of a file name…", id="finder-input")
            yield Label("", id="finder-folders")
            yield OptionList(id="finder-results")
            with Horizontal(id="finder-footer", classes="popup-footer"):
                yield Label("", id="finder-keys", classes="popup-keys")
                yield Label("Looking for files…", id="finder-count", classes="popup-count")

    def on_mount(self) -> None:
        self.query_one(Vertical).border_title = "Find a file"
        self.query_one("#finder-results", OptionList).can_focus = False
        if self.entries is not None:
            self._list_folders()
            self.refilter()

    def index_ready(self, index: FileIndex | list[Entry]) -> None:
        """The app has (re)built the file list, or it changed."""
        self.entries = _entries(index)
        self._list_folders()
        self.refilter()

    # --- narrowing to a folder ------------------------------------------------

    def _list_folders(self) -> None:
        assert self.entries is not None
        self.recent = recent_folders(self.entries, markdown_only=True)
        if self.folder is not None and self.folder not in dict(self.recent):
            self.folder = None
        self._draw_folders()

    def _room(self) -> int:
        """How many columns wide the box's contents are."""
        return min(100, int(self.app.size.width * 0.8)) - 6  # border and padding

    def on_resize(self) -> None:
        self._draw_folders()
        if self._shown_query is not None and not self._shown_query.strip():
            self.refilter()  # to line the ages up again

    def _draw_folders(self) -> None:
        """The "Look in" line: one tab per folder, as many as fit."""
        row = self.query_one("#finder-folders", Label)
        box = self.query_one(Vertical)
        room = self._room()
        line = Content.styled("Look in  ", "dim")
        self.folders = []
        choices: list[tuple[str | None, str, str]] = [(None, "all", "")]
        choices += [(folder, f"{folder}/", short_age(modified)) for folder, modified in self.recent]
        for index, (folder, label, age) in enumerate(choices):
            chip = Content.from_markup(
                f"[@click=screen.pick_folder_at({index})] $label[/]", label=label
            )
            chip = chip + (Content.styled(f" {age}", "dim") if age else "") + " "
            if folder == self.folder:
                chip = chip.stylize("bold reverse")
            if line.cell_length + chip.cell_length > room and folder != self.folder:
                continue
            line = line + chip + " "
            if folder is not None:
                self.folders.append((folder, dict(self.recent)[folder]))
        row.update(line)
        row.set_class(bool(self.folders), "-visible")
        box.border_title = f"Find a file in {self.folder}/" if self.folder else "Find a file"
        keys = [("⏎", "open"), ("⇥", "pick a folder"), ("esc", "close")]
        if not self.folders:
            keys.pop(1)
        self.query_one("#finder-keys", Label).update(key_hints(keys))

    def action_pick_folder(self, step: int) -> None:
        if not self.folders:
            return
        order: list[str | None] = [None, *(folder for folder, _ in self.folders)]
        at = order.index(self.folder) if self.folder in order else 0
        self._narrow_to(order[(at + step) % len(order)])

    def action_pick_folder_at(self, index: int) -> None:
        order: list[str | None] = [None, *(folder for folder, _ in self.recent)]
        if 0 <= index < len(order):
            self._narrow_to(order[index])
        self.query_one("#finder-input", Input).focus()

    def _narrow_to(self, folder: str | None) -> None:
        self.folder = folder
        self._draw_folders()
        self.refilter()

    @on(Input.Changed, "#finder-input")
    def _typed(self) -> None:
        if self._pending is not None:
            self._pending.stop()
        self._pending = self.set_timer(TYPING_PAUSE, self.refilter)

    def refilter(self) -> None:
        self._pending = None
        if self.entries is None:
            return
        self._search(self.query_one("#finder-input", Input).value, self.entries)

    @work(thread=True, exclusive=True, group="finder-search")
    def _search(self, query: str, entries: list[Entry]) -> None:
        worker = get_current_worker()
        hits = search(
            query,
            entries,
            markdown_only=True,
            folder=self.folder,
            cancelled=lambda: worker.is_cancelled,
        )
        if hits is not None and not worker.is_cancelled:
            self.app.call_from_thread(self._show, query, hits, entries)

    def _show(self, query: str, hits: list[Hit], entries: list[Entry]) -> None:
        self._shown_query = query
        results = self.query_one("#finder-results", OptionList)
        hint = self.query_one("#finder-count", Label)
        self.shown = [hit.entry.path for hit in hits]
        # With nothing typed the list is the most recent changes, so say how recent.
        age_width = 0 if query.strip() else self._room() - 3  # clear of the scrollbar
        results.set_options([Option(highlighted(hit, age_width)) for hit in hits])
        if self.shown:
            results.highlighted = 0
        candidates = in_folder(entries, self.folder)
        total = sum(1 for e in candidates if e.markdown)
        if not total:
            hint.update("No markdown files here")
        elif not self.shown:
            hint.update("Nothing matches")
        else:
            hint.update(f"{total:,} markdown file{'s' if total != 1 else ''}")

    def action_move(self, step: int) -> None:
        results = self.query_one("#finder-results", OptionList)
        if not self.shown:
            return
        current = results.highlighted or 0
        results.highlighted = max(0, min(len(self.shown) - 1, current + step))

    @on(Input.Submitted, "#finder-input")
    def _submitted(self) -> None:
        # If Enter comes before the results caught up with the typing, search right now.
        query = self.query_one("#finder-input", Input).value
        if query != self._shown_query:
            if self._pending is not None:
                self._pending.stop()
                self._pending = None
            if self.entries is not None:
                hits = search(
                    query, self.entries, markdown_only=True, folder=self.folder, limit=1
                )
                if hits:
                    self.dismiss(hits[0].entry.path)
            return
        results = self.query_one("#finder-results", OptionList)
        if results.highlighted is not None and self.shown:
            self.dismiss(self.shown[results.highlighted])

    @on(OptionList.OptionSelected, "#finder-results")
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.shown[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        # Clicking outside the box closes it.
        if event.widget is self:
            self.dismiss(None)
