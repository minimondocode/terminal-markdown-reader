"""The right side: when the file changed, the document, and a find-in-document bar."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.highlight import highlight
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Input, Label, Static

from tmr.editor import Editor, UnsavedChanges
from tmr.files import MAX_HIGHLIGHT_BYTES, describe_type, human_size, is_markdown
from tmr.keys import help_table
from tmr.markdown import (
    CURRENT_MATCH_STYLE,
    MATCH_STYLE,
    Document,
    Searchable,
    TextBlock,
)
from tmr.session import NotEditable, OpenFile, describe_change, relative_age  # noqa: F401

SEARCH_PAUSE = 0.08
"""Wait this long after a keystroke before searching, so fast typing only searches once."""

WELCOME = """\
# tmr v1

Pick a file on the left to read it here.

""" + help_table() + """
Everything can be clicked with the mouse too: files, links (web links open in your browser),
and checkboxes, which tick or untick the task in the file. Highlight text in a document to copy it,
as markdown. Click `copy` on a code block to copy everything inside it.
Drag the bar between the columns to make the file list wider or narrower.
"""


CODE_PIECE_LINES = 100
"""How many lines of a file go in each piece of a `CodeView`."""


Spot = tuple[int, int, str | None]
"""A place in a file to come back to: the line of the file at the top of the
view, how many rows past its start, and the file's text then (so the line can
be followed should the file change meanwhile)."""


class CodeView(Vertical):
    """Plain text, with colours for code.

    Drawn as a column of pieces of `CODE_PIECE_LINES` lines each, not one
    widget: Textual draws a widget's whole text again whenever anything about
    it changes (the mouse passing over, say), which for a long file (a
    package-lock.json) took a second a time and froze tmr. Now only the
    pieces on screen are drawn.
    """

    DEFAULT_CSS = """
    CodeView {
        padding: 1 4;
        width: 1fr;
        max-width: 100;
        height: auto;
    }
    """

    def __init__(self, content: Content) -> None:
        lines = content.split("\n", allow_blank=True)
        pieces = [
            CodePiece(Content("\n").join(lines[start : start + CODE_PIECE_LINES]))
            for start in range(0, len(lines), CODE_PIECE_LINES)
        ]
        super().__init__(*pieces)
        self.original = content


class CodePiece(Static, Searchable):
    """A run of lines of a `CodeView`."""

    DEFAULT_CSS = """
    CodePiece {
        width: 1fr;
    }
    """

    def __init__(self, content: Content) -> None:
        super().__init__(content, expand=True)
        self.original = content

    def search_text(self) -> str:
        return self.original.plain

    def show_matches(self, matches: list[tuple[int, int, bool]]) -> None:
        content = self.original
        for start, end, current in matches:
            content = content.stylize(CURRENT_MATCH_STYLE if current else MATCH_STYLE, start, end)
        self.update(content)

    def match_row(self, index: int) -> int:
        """The row a character is on, counting the rows long lines wrap onto."""
        width = self.content_size.width
        text = self.original.plain
        number = text.count("\n", 0, index)
        lines = text.split("\n")
        rows = sum(_rows(line, width) for line in lines[:number])
        column = index - (text.rfind("\n", 0, index) + 1)
        line = lines[number] if number < len(lines) else ""
        if not line or not width:
            return rows
        # Which of its own wrapped rows the character falls on.
        position = 0
        parts = Content(line).wrap(width)
        for row, part in enumerate(parts):
            start = line.find(part.plain, position) if part.plain else position
            end = start + len(part.plain)
            if column < end or row == len(parts) - 1:
                return rows + row
            position = end
        return rows


def _rows(line: str, width: int) -> int:
    """How many rows a line takes once wrapped to `width`."""
    return len(Content(line).wrap(width)) if line and width else 1


class InfoView(Static):
    """Details about a file that can't be shown as text."""

    DEFAULT_CSS = """
    InfoView {
        padding: 1 4;
        max-width: 100;
    }
    """


@dataclass
class Match:
    widget: Widget
    start: int
    end: int


class SearchBar(Horizontal):
    """The find-in-document bar at the bottom of the document."""

    DEFAULT_CSS = """
    SearchBar {
        /* Boxed, so it stands apart from the document above. */
        height: 3;
        display: none;
        border: round ansi_bright_black;
    }
    SearchBar.-visible {
        display: block;
    }
    SearchBar > Label {
        width: auto;
        padding: 0 1;
        text-style: dim;
    }
    SearchBar > #search-label {
        color: $foreground;
        text-style: bold;
    }
    SearchBar > Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
    }
    SearchBar > Input:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("down", "app.next_match(1)", "Next match", show=False),
        Binding("up", "app.next_match(-1)", "Previous match", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Label("Search:", id="search-label")
        yield Input(placeholder="type words to find…", id="search-input")
        yield Label("", id="search-count")


class DocTitle(Static):
    """The document's title, at the top once it's scrolled away: click to go back up."""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.query_ancestor(Viewer).scroller.scroll_to(y=0, animate=False)


class Viewer(Vertical):
    """Shows one file at a time."""

    DEFAULT_CSS = """
    /* Centred the way the document is in its scroller, the padding on the
       right standing in for its scrollbar, so they sit over its column in the
       same layout pass: nothing jumps as the sidebar changes width. */
    Viewer #doc-head, Viewer #doc-foot {
        height: auto;
        align-horizontal: center;
    }
    Viewer #doc-head {
        /* When the file changed, level with the folder's name at the top of
           the sidebar, and a line of air under it: the text starts level
           with the file list. */
        padding: 2 1 1 0;
    }
    Viewer #doc-foot {
        /* And a line between the text (or the search bar) and the hints below. */
        padding: 0 1 1 0;
    }
    /* As wide as the document's column, so their words line up with its text. */
    Viewer #doc-frame, Viewer #search-frame {
        width: 1fr;
        max-width: 100;
        height: auto;
        padding: 0 2;
    }
    /* Just a quiet note at the top right: the document names itself. Once
       its # title has scrolled away, that title sits quietly at the left. */
    Viewer #doc-bar {
        height: 1;
        padding: 0 2;
        align-horizontal: right;
    }
    Viewer #doc-title {
        width: 1fr;
        margin-right: 2;
        text-style: dim bold;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        &:hover {
            text-style: bold;
        }
    }
    Viewer #doc-age {
        width: auto;
        text-style: dim italic;
    }
    Viewer #doc-age.-deleted, Viewer #doc-age.-waiting {
        color: $text-warning;
        text-style: italic;
    }
    Viewer #doc-age.-editing {
        color: $accent;
        text-style: bold;
    }
    Viewer #edit-column {
        height: 1fr;
        /* Room for the document's scrollbar, so it lines up with the note above. */
        padding-right: 1;
    }
    Viewer #doc-scroll {
        height: 1fr;
        align-horizontal: center;
        background: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
    }
    Viewer #doc-scroll:focus {
        background-tint: $foreground 0%;
    }
    """

    BINDINGS = [
        Binding("escape", "close_search", "Close search", show=False),
    ]

    class Opened(Message):
        """A file has been shown."""

        def __init__(self, path: Path | None) -> None:
            super().__init__()
            self.path = path

    class SearchChanged(Message):
        """The search bar opened or closed."""

        def __init__(self, searching: bool) -> None:
            super().__init__()
            self.searching = searching

    class EditingChanged(Message):
        """Editing in place started or stopped."""

        def __init__(self, editing: bool) -> None:
            super().__init__()
            self.editing = editing

    def __init__(self, root: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.root = root
        self.file = OpenFile()
        """The file on show, and any editing of it (see `tmr.session`)."""
        self._pending_search: Timer | None = None
        self.matches: list[Match] = []
        self.current_match = -1
        self._highlighted: set[Widget] = set()
        self._edit_ticker: Timer | None = None
        self._edit_start = (0, 0)
        """Where the formatted view was when editing began: see `_top_source_line`."""

    def compose(self) -> ComposeResult:
        # When the file changed, at the right of the reading column below it.
        with Vertical(id="doc-head"), Vertical(id="doc-frame"), Horizontal(id="doc-bar"):
            title = DocTitle("", id="doc-title", markup=False)
            title.ALLOW_SELECT = False
            yield title
            label = Static("", id="doc-age", markup=False)
            label.ALLOW_SELECT = False
            yield label
        with VerticalScroll(id="doc-scroll"):
            yield Document(self.root, id="document")
        with Vertical(id="doc-foot"), Vertical(id="search-frame"):
            yield SearchBar(id="search-bar")

    def on_mount(self) -> None:
        self.set_interval(30, self.update_title)
        # After the scroll is drawn, when the heading's place on screen is up to date.
        self.watch(self.scroller, "scroll_y", lambda: self.call_after_refresh(self.update_sticky_title), init=False)

    @property
    def path(self) -> Path | None:
        """The file on show (None for the welcome page)."""
        return self.file.path

    @property
    def scroller(self) -> VerticalScroll:
        return self.query_one("#doc-scroll", VerticalScroll)

    @property
    def document(self) -> Document:
        return self.query_one("#document", Document)

    # --- opening files ----------------------------------------------------

    async def open(
        self,
        path: Path | None,
        *,
        keep_scroll: bool = False,
        anchor: str = "",
        back_to: Spot | None = None,
        line: int | None = None,
    ) -> None:
        """Show a file (or the welcome page when `path` is None).

        With `keep_scroll` (the same file, changed), the view stays on the
        text it was showing, even if lines were added or removed above it.
        With `back_to`, it goes to a spot the file was left at (see `reading_spot`);
        with `line`, to that line of the file.
        """
        if self.file.editing:
            # Callers ask about unsaved changes first (see `leave_editing`).
            await self._close_editor()
        place = self._reading_place() if keep_scroll and path == self.path else None
        self.clear_matches()
        await self._show_file(path)
        if anchor:
            self.document.goto_anchor(anchor)
        elif place is not None:
            self.call_after_refresh(self._return_to, place)
        elif back_to is not None:
            left_line, offset, text = back_to
            self.call_after_refresh(self._return_to, (None, 0, left_line, offset, text))
        elif line is not None:
            self.call_after_refresh(self._show_source_line, line)
        else:
            # Straight away as well, so the new file isn't drawn even once at
            # the old one's place (its title flashing up at the top, say).
            self.scroller.scroll_to(y=0, animate=False)
            self.call_after_refresh(self.scroller.scroll_to, y=0, animate=False)
        self.update_title()
        self.call_after_refresh(self.update_sticky_title)
        if self.query_one(SearchBar).has_class("-visible"):
            self.run_search(self.query_one("#search-input", Input).value, jump=False)
        self.post_message(self.Opened(path))

    def reading_spot(self) -> Spot:
        """Where the reader is, to come back to after reading other files."""
        line, offset = self._top_source_line()
        return line, offset, self.file.text

    def _reading_place(self) -> tuple[Widget | None, int, int, int, str | None]:
        """Where the reader is: the widget at the top of the view and how far into
        it, and (should that widget go) the line of the file and the text it's in."""
        top = int(self.scroller.scroll_y)
        line, offset = self._top_source_line()
        widget: Widget | None = None
        into = 0
        if self.document.display:
            for _, shown in self.document.shown_blocks():
                y = self._y_in_document(shown)
                if y + shown.outer_size.height > top:
                    widget, into = shown, top - y
                    break
        return widget, into, line, offset, self.file.text

    def _return_to(self, place: tuple[Widget | None, int, int, int, str | None]) -> None:
        widget, into, line, offset, old_text = place
        if widget is not None and widget.is_attached and self.document.display:
            # The block at the top is still there (perhaps moved): back to it.
            self.scroller.scroll_to(y=max(0, self._y_in_document(widget) + into), animate=False)
            return
        new_line = map_line(old_text or "", self.file.text or "", line)
        self._show_source_line(new_line, offset if new_line == line else 0)

    async def reload(self, force: bool = False) -> None:
        """The open file changed on disk: show the new version, keeping our place.

        Drawing a long document again is slow, so unless `force`d this only
        does it when the text itself changed (not for a touch, or for a write
        of ours that's already on show, like ticking a task). While editing,
        the change is only noted, for the save to come.
        """
        what = self.file.disk_changed()
        if what == "none":
            return
        if what == "changed" or (force and what == "same"):
            await self.open(self.path, keep_scroll=True)
            return
        self.update_title()

    async def _show_file(self, path: Path | None) -> None:
        scroller = self.scroller
        document = self.document
        for widget in list(scroller.children):
            if widget is not document:
                await widget.remove()

        try:
            loaded = self.file.load(path)
        except OSError as error:
            document.display = False
            await document.show("", self.root)
            await scroller.mount(InfoView(Content(f"Can't open this file: {error.strerror}")))
            return

        if path is None or loaded is None:
            document.display = True
            await document.show(WELCOME, self.root)
            return

        if loaded.text is None:
            document.display = False
            await document.show("", self.root)
            info = Content.from_markup(
                "[b]$name[/b]\n\n"
                "[dim]Type[/]      $kind\n"
                "[dim]Size[/]      $size\n"
                "[dim]Changed[/]   $changed\n\n"
                "[dim italic]This file can't be shown as text.[/]",
                name=path.name,
                kind=describe_type(path),
                size=human_size(loaded.size),
                changed=_date_and_time(loaded.modified),
            )
            await scroller.mount(InfoView(info))
            return

        text = loaded.text
        if loaded.truncated:
            note = "\n\n---\n\n*This file is very large; only the beginning is shown.*\n"
        else:
            note = ""

        if is_markdown(path):
            document.display = True
            await document.show(text + note, path.parent)
            return

        document.display = False
        await document.show("", self.root)
        if len(text) <= MAX_HIGHLIGHT_BYTES:
            dark = self.app.current_theme.dark
            if dark:
                from textual.highlight import ANSIDarkHighlightTheme as theme
            else:
                from textual.highlight import ANSILightHighlightTheme as theme
            content = highlight(text, path=str(path), theme=theme)
        else:
            content = Content(text)
        if loaded.truncated:
            content = content + Content.styled(
                "\n\n… This file is very large; only the beginning is shown.", "italic dim"
            )
        await scroller.mount(CodeView(content))

    # --- ticking tasks ------------------------------------------------------

    @on(Document.TaskToggled)
    async def _task_toggled(self, event: Document.TaskToggled) -> None:
        event.stop()
        if self.path is None:
            return
        try:
            result = self.file.tick(event.line, self.document.original)
        except UnicodeDecodeError:
            self.app.notify("Couldn't read the file to tick that.", severity="error")
            return
        except OSError as error:
            self.app.notify(f"Couldn't tick that: {error.strerror}", severity="error")
            return
        if result == "changed":
            self.app.notify("The file changed. Try again.", severity="warning")
        if result != "not a task":
            await self.reload()

    # --- editing in place ---------------------------------------------------

    @property
    def editor(self) -> Editor | None:
        try:
            return self.query_one(Editor)
        except NoMatches:
            return None

    @property
    def unsaved(self) -> bool:
        editor = self.editor
        return editor is not None and self.file.unsaved(editor.text)

    async def start_editing(self) -> None:
        """Swap the formatted view for the file as written, to change it."""
        if self.file.editing:
            if self.editor is not None:
                self.editor.focus()
            return
        try:
            text = self.file.editable_text()
        except NotEditable as reason:
            self.app.notify(str(reason), severity=reason.severity)
            return

        line, offset = self._top_source_line()
        self.action_close_search()
        self._edit_start = (line, offset)
        assert self.path is not None
        editor = Editor(text, self.path)
        self.scroller.display = False
        await self.mount(Center(editor, id="edit-column"), after=self.scroller)
        self.file.start_editing(text, editor.text)
        editor.focus()
        self.call_after_refresh(editor.show_line, line)
        self._edit_ticker = self.set_interval(1, self.update_title)
        self.update_title()
        self.post_message(self.EditingChanged(True))

    @on(Editor.Changed)
    def _edited(self, event: Editor.Changed) -> None:
        self.update_title()

    async def save(self) -> bool:
        """Write the editor's text to the file. Returns whether it was saved.

        `OpenFile.plan_save` decides what that means when something else
        changed the file meanwhile (wait, merge, or show both versions).
        """
        editor = self.editor
        if not self.file.editing or editor is None:
            return False
        mine = editor.text
        plan = self.file.plan_save(mine)
        if plan.kind == "conflict":
            self._replace_editor_text(plan.text)
            editor.show_line(max(0, plan.first_conflict_line - 2))
            self.update_title()
            self.app.notify(plan.message, severity="warning", timeout=10)
            return False
        if plan.kind != "write":
            self.update_title()
            self.app.notify(plan.message, severity="error" if plan.kind == "unreadable" else "warning")
            return False
        try:
            self.file.write(plan.text)
        except OSError as error:
            self.app.notify(f"Couldn't save the file: {error.strerror}", severity="error")
            return False
        if plan.text != mine:
            self._replace_editor_text(plan.text)
        self.file.mark_saved(editor.text)
        self.update_title()
        self.app.notify(plan.message, timeout=2)
        return True

    def leave_editing(self, then: Callable[[], Awaitable[None]] | None = None) -> None:
        """Go back to reading, asking first if there are unsaved changes; then do `then`."""
        if not self.file.editing:
            if then is not None:
                self.run_worker(then(), group="leave-editing")
            return

        async def close() -> None:
            await self._close_editor(reopen=then is None)
            if then is not None:
                await then()

        async def save_and_close() -> None:
            if await self.save():
                await close()

        if not self.unsaved:
            self.run_worker(close(), group="leave-editing")
            return

        def chosen(answer: str | None) -> None:
            if answer == "save":
                self.run_worker(save_and_close(), group="leave-editing")
            elif answer == "discard":
                self.run_worker(close(), group="leave-editing")
            elif self.editor is not None:
                self.editor.focus()

        assert self.path is not None
        self.app.push_screen(UnsavedChanges(self.path.name), chosen)

    async def _close_editor(self, reopen: bool = False) -> None:
        editor = self.editor
        line = editor.top_line() if editor is not None else 0
        # Not scrolled while editing: go back to exactly the view we left, not
        # just the start of its first block (which would skip a heading's margin).
        start_line, start_offset = self._edit_start
        offset = start_offset if line == start_line else 0
        self.file.stop_editing()
        if self._edit_ticker is not None:
            self._edit_ticker.stop()
            self._edit_ticker = None
        try:
            await self.query_one("#edit-column").remove()
        except NoMatches:
            pass
        self.scroller.display = True
        self.post_message(self.EditingChanged(False))
        if reopen:
            await self.open(self.path, keep_scroll=True)
            self.call_after_refresh(self._show_source_line, line, offset)
            self.scroller.focus()
        self.update_title()

    def _replace_editor_text(self, text: str) -> None:
        """Change the whole text, keeping the cursor about where it was (and undo working)."""
        editor = self.editor
        if editor is None:
            return
        row, column = editor.cursor_location
        editor.replace(text, (0, 0), editor.document.end)
        row = min(row, editor.document.line_count - 1)
        editor.move_cursor((row, min(column, len(editor.document[row]))))

    def _top_source_line(self) -> tuple[int, int]:
        """The line of the file at the top of the formatted view, and how many
        rows the view is scrolled past where that line starts (negative in the
        margin above a heading, say)."""
        top = int(self.scroller.scroll_y)
        document = self.document
        if not document.display:
            line = max(0, top - 1)
            return line, top - (line + 1)
        for block, widget in document.shown_blocks():
            y = self._y_in_document(widget)
            if y + widget.outer_size.height <= top:
                continue
            if isinstance(widget, TextBlock) and top > y:
                row = top - self._content_y(widget)
                line = widget.source_line_at_row(row)
                first = widget.row_of_line(line)
                if first is not None and line > block.line:
                    return line, row - first
            return block.line, top - y
        return 0, 0

    def _show_source_line(self, line: int, offset: int = 0) -> None:
        """Scroll the formatted view to the part written on this line of the file."""
        document = self.document
        if not document.display:
            self.scroller.scroll_to(y=max(0, line + 1 + offset), animate=False)
            return
        chosen = None
        for block, widget in document.shown_blocks():
            if block.line > line:
                break
            chosen = block, widget
        if chosen is None:
            self.scroller.scroll_to(y=0, animate=False)
            return
        block, widget = chosen
        y = self._y_in_document(widget)
        if isinstance(widget, TextBlock) and line > block.line:
            row = widget.row_of_line(line)
            if row is not None:
                y = self._content_y(widget) + row
        self.scroller.scroll_to(y=max(0, y + offset), animate=False)

    def _content_y(self, widget: Widget) -> int:
        """Where a widget's content (inside its padding and border) starts in the document."""
        scroller = self.scroller
        return widget.content_region.y - scroller.content_region.y + int(scroller.scroll_y)

    # --- outline ------------------------------------------------------------

    def headings(self) -> list[tuple[int, str, str]]:
        """The document's headings: (level, text, widget id), top to bottom."""
        document = self.document
        if not document.display:
            return []
        return list(document.table_of_contents)

    def current_heading(self) -> int:
        """Which heading the reader is under (an index into `headings`), or -1."""
        current = -1
        top = self.scroller.scroll_y
        for index, (_, _, block_id) in enumerate(self.headings()):
            try:
                widget = self.document.query_one(f"#{block_id}")
            except NoMatches:
                continue
            if self._y_in_document(widget) <= top + 1:
                current = index
            else:
                break
        return current

    def goto_heading(self, block_id: str) -> None:
        try:
            widget = self.document.query_one(f"#{block_id}")
        except NoMatches:
            return
        self.scroller.scroll_to(y=max(0, self._y_in_document(widget) - 1), animate=False)
        self.scroller.focus()

    def _y_in_document(self, widget: Widget) -> int:
        scroller = self.scroller
        return widget.region.y - scroller.content_region.y + int(scroller.scroll_y)

    def _laid_out_y(self, widget: Widget) -> int:
        """Like `_y_in_document`, but from the layout rather than from where the
        widget was last drawn, which lags a scroll by a frame (straight after a
        file opens at its top, the old file's place)."""
        y = 0
        node: Widget | None = widget
        while node is not None and node is not self.scroller:
            y += node.virtual_region.y
            node = node.parent if isinstance(node.parent, Widget) else None
        return y

    def update_sticky_title(self) -> None:
        """At the top left: the document's # title, once it has scrolled out of
        sight (nothing for a document without one)."""
        try:
            title = self.query_one("#doc-title", Static)
        except NoMatches:
            return
        text = ""
        first = next((heading for heading in self.headings() if heading[0] == 1), None)
        if first is not None:
            try:
                widget = self.document.query_one(f"#{first[2]}")
            except NoMatches:
                widget = None
            # A heading with no height yet hasn't been laid out (the file has
            # only just opened), so where it is isn't known yet either.
            laid_out = widget is not None and widget.outer_size.height > 0
            if laid_out and self._laid_out_y(widget) + widget.outer_size.height <= self.scroller.scroll_y:
                text = first[1]
        title.update(text)

    # --- when it changed ---------------------------------------------------

    def update_title(self) -> None:
        """At the top right: when the file last changed (or how editing is
        going), and about how many tokens it is."""
        try:
            age = self.query_one("#doc-age", Static)
        except NoMatches:
            return  # tmr is closing, and a reload finished on the way out.
        editor = self.editor
        status, kind = self.file.status(editor.text if editor is not None else None)
        for name in ("-deleted", "-editing", "-waiting"):
            age.set_class(kind == name, name)
        age.update(status)
        self.update_sticky_title()

    # --- find in document -------------------------------------------------

    def open_search(self) -> None:
        bar = self.query_one(SearchBar)
        if not bar.has_class("-visible"):
            bar.add_class("-visible")
            self.post_message(self.SearchChanged(True))
        search_input = self.query_one("#search-input", Input)
        search_input.focus()
        search_input.action_select_all()

    def action_close_search(self) -> None:
        self._cancel_pending_search()
        bar = self.query_one(SearchBar)
        if not bar.has_class("-visible"):
            return
        bar.remove_class("-visible")
        self.post_message(self.SearchChanged(False))
        self.clear_matches()
        self.scroller.focus()

    @on(Input.Changed, "#search-input")
    def _search_changed(self, event: Input.Changed) -> None:
        self._cancel_pending_search()
        value = event.value
        self._pending_search = self.set_timer(SEARCH_PAUSE, lambda: self._typed_search(value))

    def _typed_search(self, value: str) -> None:
        self._pending_search = None
        self.run_search(value)

    def _cancel_pending_search(self) -> bool:
        """Stop a search that's waiting for typing to pause. Returns whether there was one."""
        if self._pending_search is None:
            return False
        self._pending_search.stop()
        self._pending_search = None
        return True

    @on(Input.Submitted, "#search-input")
    def _search_submitted(self, event: Input.Submitted) -> None:
        if self._cancel_pending_search():
            # Enter came before the search caught up with the typing: search now.
            self.run_search(event.value)
            return
        self.next_match(1)

    def _searchable(self) -> list[Widget]:
        """Everything on show whose text can be searched, in reading order."""
        found = []
        for widget in self.scroller.query("*"):
            if not isinstance(widget, Searchable):
                continue
            if not widget.display or any(
                not node.display for node in widget.ancestors if isinstance(node, Widget)
            ):
                continue
            found.append(widget)
        return found

    def run_search(self, query: str, jump: bool = True) -> None:
        self.clear_matches()
        count_label = self.query_one("#search-count", Label)
        query = query.strip()
        if not query:
            count_label.update("")
            return
        needle = query.casefold()
        self.matches = []
        for widget in self._searchable():
            assert isinstance(widget, Searchable)
            haystack = widget.search_text().casefold()
            start = haystack.find(needle)
            while start != -1:
                self.matches.append(Match(widget, start, start + len(needle)))
                start = haystack.find(needle, start + len(needle))
        if not self.matches:
            count_label.update("no matches")
            return
        # Start from the first match at or below the current scroll position
        # (matches are in reading order, so a binary search finds it).
        at = bisect_left(self.matches, self.scroller.scroll_y, key=self._match_y)
        self.current_match = at if at < len(self.matches) else 0
        self._paint_matches()
        if jump:
            self._scroll_to_current()

    def next_match(self, step: int) -> None:
        if not self.matches:
            return
        before = self.matches[self.current_match].widget
        self.current_match = (self.current_match + step) % len(self.matches)
        # Only the old and new current match change colour; leave the rest be.
        self._paint_matches({before, self.matches[self.current_match].widget})
        self._scroll_to_current()

    def clear_matches(self) -> None:
        for widget in self._highlighted:
            if widget.is_attached and isinstance(widget, Searchable):
                widget.show_matches([])
        self._highlighted = set()
        self.matches = []
        self.current_match = -1

    def _paint_matches(self, only: set[Widget] | None = None) -> None:
        by_widget: dict[Widget, list[tuple[int, int, bool]]] = {}
        for index, match in enumerate(self.matches):
            if only is None or match.widget in only:
                by_widget.setdefault(match.widget, []).append(
                    (match.start, match.end, index == self.current_match)
                )
        for widget, found in by_widget.items():
            assert isinstance(widget, Searchable)
            widget.show_matches(found)
            self._highlighted.add(widget)
        count = self.query_one("#search-count", Label)
        count.update(f"{self.current_match + 1} of {len(self.matches)}")

    def _match_y(self, match: Match) -> int:
        """Which row of the document a match is on."""
        widget = match.widget
        assert isinstance(widget, Searchable)
        return self._content_y(widget) + widget.match_row(match.start)

    def _scroll_to_current(self) -> None:
        if not 0 <= self.current_match < len(self.matches):
            return
        y = self._match_y(self.matches[self.current_match])
        height = self.scroller.scrollable_content_region.height
        if not (self.scroller.scroll_y <= y < self.scroller.scroll_y + height - 1):
            self.scroller.scroll_to(y=max(0, y - height // 3), animate=False)


def _date_and_time(timestamp: float) -> str:
    """"2 Oct 2026, 14:05" (no %-d: it isn't portable)."""
    when = datetime.fromtimestamp(timestamp)
    return f"{when.day} {when:%b %Y, %H:%M}"


def map_line(old: str, new: str, line: int) -> int:
    """Where a line of `old` is in `new`: the same line if it's still there,
    else where the lines that replaced it begin."""
    if old == new:
        return line
    old_lines, new_lines = old.split("\n"), new.split("\n")
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if i1 <= line < i2 or i1 == i2 == line:
            return j1 + (line - i1) if tag == "equal" else j1
    return max(0, len(new_lines) - 1)
