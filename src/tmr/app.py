"""tmr: a two-column file explorer and markdown viewer for the terminal."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import unquote

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.filter import LineFilter
from textual.markup import escape
from textual.notifications import Notification, Notify, SeverityLevel
from textual.timer import Timer
from textual.widgets import DirectoryTree, Static
from textual.worker import get_current_worker

from tmr import clipboard, keys, state
from tmr.commit import CommitMessage, GitError, commit, pending
from tmr.divider import DEFAULT_PERCENT, WIDE_PERCENT, Divider
from tmr.driver import cell_mouse_driver
from tmr.files import is_markdown
from tmr.finder import FileFinder
from tmr.index import FileIndex, Update, wants_change
from tmr.markdown import CodeFence, Document
from tmr.outline import Outline
from tmr.palette import Palette
from tmr.search import build_index, find_named
from tmr.tree import FileTree
from tmr.viewer import Spot, Viewer
from tmr.worktrees import Place, Worktree, WorktreeLabel, WorktreePicker, place_of, read_head, worktrees

MAX_HISTORY = 100
"""Going back reaches this many files."""

GLANCE_SECONDS = 0.12
"""Moving through the file list shows the file the cursor rests on for this
long: holding ↓ doesn't draw every file it passes."""

FIT_SPARE_COLUMNS = 2
"""Blank columns after the longest name when `b` widens the sidebar to fit it."""


@dataclass
class Visit:
    """A file in the back-and-forward history, and where in it the reader was."""

    path: Path
    spot: Spot | None = None
    """Where the view was when the reader left the file (None: its top)."""
    glance: bool = False
    """Shown by moving through the file list, not opened: the next file moved
    onto takes its place, so going back skips the files only passed over."""

WATCH_RETRY_DELAYS = (2.0, 10.0, 30.0)
"""When the file watcher fails, start it again after these many seconds (then give up)."""

WATCH_SETTLED_SECONDS = 60
"""A watcher that ran this long before failing gets its full set of retries again."""

HINTS = keys.hints("reading")
"""The keys along the bottom while reading: (key, label, action when clicked)."""

EDITING_HINTS = keys.hints("editing")
"""The keys while editing a file in place, when letters type themselves."""

SEARCHING_HINTS = keys.hints("searching")
"""The keys while the search bar is open."""

HINTS_BY_MODE = {"reading": HINTS, "editing": EDITING_HINTS, "searching": SEARCHING_HINTS}

HINTS_TO_DROP_FIRST = keys.drop_order()
"""When the hints don't fit on one line, these go (in this order) until they do."""

OFF_WHILE_EDITING = keys.off_while_editing()
"""Reading keys that do nothing while a file is being edited (search, outline, …)."""

ONLY_WHILE_EDITING = keys.only_while_editing()
"""Editing keys that do nothing while reading (save, back to reading, …)."""

HEAD_CHECK_SECONDS = 2
"""How often to check whether the branch checked out has changed (an agent
switched it, say), so the foot of the sidebar keeps telling the truth."""


def editor_command() -> list[str]:
    """The editor `o` opens files in: `TMR_EDITOR`, else `VISUAL`, else `EDITOR`
    (split like a shell command line); empty when none is set."""
    for name in ("TMR_EDITOR", "VISUAL", "EDITOR"):
        value = os.environ.get(name, "").strip()
        if value:
            return shlex.split(value)
    return []


def _hint_markup(width: int = 1_000, mode: keys.Mode = "reading") -> str:
    shown = list(HINTS_BY_MODE[mode])
    dropping = iter(HINTS_TO_DROP_FIRST)
    gap = len(Content.from_markup(keys.SEPARATOR).plain)
    while sum(len(key) + len(label) + 1 + gap for key, label, _ in shown) - gap > width:
        key = next(dropping, None)
        if key is None:
            break
        shown = [hint for hint in shown if hint[0] != key]
    return keys.SEPARATOR.join(keys.hint_markup(key, label, action) for key, label, action in shown)


class Chrome(Static):
    """Fixed text (titles, hints) that shouldn't get caught up in text highlighting."""

    ALLOW_SELECT = False


class Hints(Chrome):
    """The keys along the bottom, leaving out the most obvious ones when space is short."""

    mode: keys.Mode = "reading"

    def on_resize(self, event: events.Resize) -> None:
        self.redraw()

    def show_mode(self, mode: keys.Mode) -> None:
        self.mode = mode
        self.redraw()

    def redraw(self) -> None:
        self.update(_hint_markup(self.content_size.width or 1_000, self.mode))


class TmrApp(App[None]):
    TITLE = "tmr"

    CSS = """
    Screen {
        background: ansi_default;
    }
    /* Except a pop-up's, which leaves the app in view (see `tmr.popup`). */
    Popup {
        background: transparent;
    }
    #columns {
        height: 1fr;
    }
    #side {
        width: 20%;
        min-width: 12;
    }
    /* The folder's name in a box, level with when the open file changed,
       and the file list starting where the document's text does. */
    #side-title {
        height: 3;
        margin: 1 1 0 1;
        padding: 0 1;
        border: round ansi_bright_black;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        text-style: bold;
    }
    /* 100%, not 1fr: a 1fr width loses the title's side margins too,
       which left the scrollbar a column short of the sidebar's edge. */
    #tree {
        width: 100%;
        height: 1fr;
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
    }
    #viewer {
        width: 1fr;
    }
    /* The scrollbars stay grey until you reach for them: the accent colour
       is kept for what you can click and for where you are. */
    #tree, #doc-scroll {
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
    }
    Toast {
        padding: 0 1;
    }
    Toast.-information {
        border: round $success;
    }
    Toast.-warning {
        border: round $warning;
    }
    Toast.-error {
        border: round $error;
    }
    #hints {
        height: 2;
        padding: 0 1;
        border-top: solid $foreground 20%;
        text-style: dim;
        background: $boost;
        link-style: none;
        link-color: $foreground;
        link-background: transparent;
        link-style-hover: bold reverse;
    }
    """

    BINDINGS = keys.bindings()

    def __init__(
        self,
        root: Path,
        *,
        dark: bool = True,
        start_file: Path | None = None,
        start_anchor: str = "",
        own_palette: bool = False,
        all_files: bool = False,
        show_hidden: bool = False,
        watch: bool = True,
        sidebar: bool = True,
    ) -> None:
        super().__init__(driver_class=cell_mouse_driver())
        # Set before the app starts, so everything is styled once, in the right
        # colours, rather than styled and then restyled.
        self.theme = "ansi-dark" if dark else "ansi-light"
        self.root = root
        self.start_file = start_file
        self.start_anchor = start_anchor
        """The #heading to open the first file at (`tmr plan.md#setup`)."""
        self.all_files = all_files
        self.show_hidden = show_hidden
        self.watch = watch
        """Whether to follow changes on disk (`--no-watch` turns it off)."""
        self.sidebar = sidebar
        """Whether to show the file list at first (not when reading from a pipe)."""
        self.palette = Palette(dark) if own_palette else None
        """tmr's own colours, in place of the terminal's (see `tmr.palette`)."""
        self.visited: list[Visit] = []
        """The files shown, oldest first, for going back and forward."""
        self.place = -1
        """Where in `visited` the file on show is."""
        self._stepping_to: tuple[Path, int] | None = None
        """The file (and place) that going back or forward is about to show."""
        self._pending_glance: Timer | None = None
        """Showing the file the cursor moved onto, once it rests there."""
        self._glancing_at: Path | None = None
        """The file a move through the file list is about to show."""
        self._stop_watching = threading.Event()
        self._change_lock = asyncio.Lock()
        self.file_index: FileIndex | None = None
        """Every file tmr lists (see `tmr.index`), once gathered."""
        self._index_building = False
        """A fresh file list is being gathered; changes wait for it in `_changes_while_building`."""
        self._changes_while_building: list[tuple[object, str]] = []
        self.sidebar_size: Literal["usual", "fit", "half"] = "usual"
        """The sidebar's width for now: its usual one, just wide enough for the
        longest name, or half the window (`b`, see `action_toggle_sidebar`)."""
        self._last_notice: tuple[Notification, str, int] | None = None
        """The message on show, as given, and how many times in a row it was given."""
        self.git_place: Place | None = None
        """The worktree and branch the open file is in (see `tmr.worktrees`)."""
        self._head_seen: str | None = None
        """What git's HEAD note said when `git_place` was worked out."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="columns"):
            side = Vertical(id="side")
            with side:
                yield Chrome(f"📁 {self.root.name or self.root}", id="side-title", markup=False)
                tree = FileTree(self.root, id="tree")
                tree.show_hidden = self.show_hidden
                # Asked to open a file that isn't markdown: list it alongside everything else.
                if self.all_files or (self.start_file is not None and not is_markdown(self.start_file)):
                    tree.markdown_only = False
                if self.start_file is not None:
                    # Listed even if hidden or ignored, so it can be shown in the tree.
                    tree.pinned.add(self.start_file)
                yield tree
                yield WorktreeLabel(id="worktree")
            yield Divider(side, id="divider")
            yield Viewer(self.root, id="viewer")
        yield Hints(_hint_markup(), id="hints")

    @property
    def tree(self) -> FileTree:
        return self.query_one("#tree", FileTree)

    @property
    def viewer(self) -> Viewer:
        return self.query_one("#viewer", Viewer)

    @property
    def editing(self) -> bool:
        """Is a file being edited in place? Then letters type, and the reading keys are off."""
        try:
            return self.viewer.file.editing
        except NoMatches:
            return False  # Not composed yet, or closing.

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Switch keys (and clicks on their hints) on and off with the mode.

        Reading keys like `s` and `h` don't work while editing, and the
        editing keys (save, back to reading) don't work while reading.
        """
        if action in OFF_WHILE_EDITING or action in ONLY_WHILE_EDITING:
            return (action in ONLY_WHILE_EDITING) == self.editing
        return True

    def get_line_filters(self) -> Sequence[LineFilter]:
        filters = super().get_line_filters()
        return [*filters, self.palette] if self.palette is not None else filters

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """Show a message in the corner, replacing the one already there.

        Saying the same thing again (pressing `c` three times) counts up,
        "Copied ×3", rather than stacking another box on top.
        """
        if threading.get_ident() != self._thread_id:
            self.call_from_thread(
                self.notify, message, title=title, severity=severity, timeout=timeout, markup=markup
            )
            return
        # Our messages are plain text; file names may well contain [brackets].
        text = escape(message)
        last = self._last_notice
        if last is not None and not last[0].has_expired and (last[1], last[0].severity) == (text, severity):
            repeats = last[2] + 1
        else:
            repeats = 1
        shown = text + (f" [$text-muted]×{repeats}[/]" if repeats > 1 else "")
        notice = Notification(
            shown, title, severity, timeout or self.NOTIFICATION_TIMEOUT, markup=True
        )
        self._last_notice = (notice, text, repeats)
        self._notifications.clear()
        self.post_message(Notify(notice))

    async def on_mount(self) -> None:
        self.set_interval(HEAD_CHECK_SECONDS, self._check_head)
        await self._start()

    async def _start(self) -> None:
        """Show the folder: at startup, and again after switching worktree."""
        self._size_sidebar()
        self.tree.focus()
        if self.watch:
            self.watch_folder()
        self.viewer.document.find_file = lambda name, near: find_named(name, near, self.file_index)
        first = self.start_file or state.last_open(self.root) or self._readme()
        anchor, self.start_anchor = self.start_anchor, ""
        await self.viewer.open(first, anchor=anchor)
        if anchor and not self.viewer.document.goto_anchor(anchor):
            self.notify(f"Couldn't find a heading for #{anchor}.", severity="warning")
        if not self.sidebar:
            self.sidebar = True  # Only at first: b brings it back.
            self.show_sidebar(False)
        # Gathered after the first document is drawn, so it doesn't slow that down.
        # File names the document mentions that only the list can find become
        # links when it arrives (see `_file_index_ready`).
        self.call_after_refresh(self.rebuild_file_index)
        if first is not None:
            await self.tree.reveal(first)

    def _readme(self) -> Path | None:
        try:
            entries = sorted(self.root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return None
        for entry in entries:
            if entry.name.lower() in ("readme.md", "readme.markdown") and entry.is_file():
                return entry
        return None

    def on_unmount(self) -> None:
        self._stop_watching.set()

    # --- opening files ----------------------------------------------------

    @on(DirectoryTree.FileSelected, "#tree")
    async def _file_chosen(self, event: DirectoryTree.FileSelected) -> None:
        if event.path != self.viewer.path:
            await self.open_file(event.path, reveal=False)

    @on(FileTree.Moved, "#tree")
    def _cursor_moved(self, event: FileTree.Moved) -> None:
        self._stop_glance()
        if not self.editing:
            self._pending_glance = self.set_timer(GLANCE_SECONDS, lambda: self._glance(event.path))

    def _stop_glance(self) -> None:
        if self._pending_glance is not None:
            self._pending_glance.stop()
            self._pending_glance = None

    async def _glance(self, path: Path) -> None:
        """Show the file the cursor rests on."""
        self._pending_glance = None
        cursor = self.tree.cursor_node
        if self.editing or cursor is None or cursor.data is None or cursor.data.path != path:
            return
        if path != self.viewer.path:
            self._glancing_at = path
            await self.open_file(path, reveal=False)

    @on(FileTree.Read, "#tree")
    async def _read(self, event: FileTree.Read) -> None:
        self._stop_glance()
        if event.path != self.viewer.path:
            await self.open_file(event.path, reveal=False)
        if self.viewer.path != event.path:
            return  # Still editing the file before (see `open_file`).
        if 0 <= self.place < len(self.visited) and self.visited[self.place].path == event.path:
            self.visited[self.place].glance = False
        self.viewer.scroller.focus()

    async def open_file(
        self,
        path: Path,
        *,
        reveal: bool = True,
        anchor: str = "",
        back_to: Spot | None = None,
        line: int | None = None,
    ) -> None:
        """Show a file: at the `anchor`, at `back_to` (a spot it was left at),
        at a `line` of it, or at its top."""
        if self.editing:
            # Finish editing first (asking about unsaved changes), then open it.
            self.viewer.leave_editing(
                lambda: self.open_file(path, reveal=reveal, anchor=anchor, back_to=back_to, line=line)
            )
            return
        if 0 <= self.place < len(self.visited) and self.visited[self.place].path == self.viewer.path:
            # Going back to this file later comes back to this spot in it.
            self.visited[self.place].spot = self.viewer.reading_spot()
        await self.viewer.open(path, anchor=anchor, back_to=back_to, line=line)
        if reveal:
            await self.tree.reveal(path)

    @on(Viewer.Opened)
    def _opened(self, event: Viewer.Opened) -> None:
        self.tree.open_path = event.path
        self.tree._redraw_labels()
        self.find_git_place()
        if event.path is not None:
            state.remember(self.root, event.path)
            self._record_visit(event.path)

    # --- back and forward -------------------------------------------------

    def _record_visit(self, path: Path) -> None:
        stepping, self._stepping_to = self._stepping_to, None
        glance = self._glancing_at == path
        self._glancing_at = None
        if stepping is not None and stepping[0] == path:
            self.place = stepping[1]
            return
        if 0 <= self.place < len(self.visited) and self.visited[self.place].path == path:
            return  # The same file again (it changed on disk, say).
        # Like a browser: opening a file after going back drops the files ahead.
        kept = self.place + 1
        if glance and 0 <= self.place < len(self.visited) and self.visited[self.place].glance:
            kept = self.place  # Only passed over on the way here.
        self.visited = self.visited[:kept] + [Visit(path, glance=glance)]
        del self.visited[:-MAX_HISTORY]
        self.place = len(self.visited) - 1

    async def _step(self, direction: int) -> None:
        place = self.place + direction
        while 0 <= place < len(self.visited) and not self.visited[place].path.is_file():
            # Deleted or moved since: skip it.
            del self.visited[place]
            if place < self.place:
                self.place -= 1
            if direction < 0:
                place -= 1
        if not 0 <= place < len(self.visited):
            self.notify("No earlier file." if direction < 0 else "No later file.")
            return
        visit = self.visited[place]
        self._stepping_to = (visit.path, place)
        await self.open_file(visit.path, back_to=visit.spot)

    async def action_back(self) -> None:
        """Show the file that was open before this one, where it was left."""
        await self._step(-1)

    async def action_forward(self) -> None:
        """Undo going back."""
        await self._step(1)

    @on(Viewer.EditingChanged)
    def _editing_changed(self, event: Viewer.EditingChanged) -> None:
        self.query_one(Hints).show_mode("editing" if event.editing else "reading")
        self.refresh_bindings()

    @on(Viewer.SearchChanged)
    def _search_changed(self, event: Viewer.SearchChanged) -> None:
        # The hints follow the search bar: its keys while it's open.
        if not self.editing:
            self.query_one(Hints).show_mode("searching" if event.searching else "reading")

    @on(Document.LinkClicked)
    async def _link_clicked(self, event: Document.LinkClicked) -> None:
        event.stop()
        href = event.href.strip()
        if not href:
            return
        lowered = href.lower()
        if lowered.startswith(("http://", "https://", "mailto:", "ftp://")):
            self.open_in_browser(href)
            return
        if href.startswith("#"):
            if not self.viewer.document.goto_anchor(href[1:]):
                self.notify("Couldn't find that section.", severity="warning")
            return
        if lowered.startswith("file://"):
            href = href[len("file://") :]

        location, _, anchor = href.partition("#")
        location = unquote(location)
        base = self.viewer.path.parent if self.viewer.path else self.root
        candidates = []
        if location.startswith("/"):
            candidates += [Path(location), self.root / location.lstrip("/")]
        else:
            candidates.append(base / location)
        for candidate in list(candidates):
            if not candidate.suffix:
                candidates.append(candidate.with_name(candidate.name + ".md"))
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_file():
                await self.open_file(candidate, anchor=anchor)
                return
            if candidate.is_dir():
                self.show_sidebar(True)
                await self.tree.reveal(candidate)
                self.tree.focus()
                return
        self.notify(f"Couldn't find {location}", severity="warning")

    def open_in_browser(self, url: str) -> None:
        """Open a web address (or mailto:) with the system, and say so."""
        try:
            if sys.platform == "darwin":
                command = ["open", url]
            elif shutil.which("xdg-open"):
                command = ["xdg-open", url]
            else:
                command = None
            if command:
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.open_url(url)
        except OSError as error:
            self.notify(f"Couldn't open the link: {error}", severity="error")
            return
        where = url.split("://", 1)[-1].split("/", 1)[0] if "://" in url else url
        self.notify(f"Opening {where} in your browser" if "://" in url else f"Opening {where}", timeout=2)

    @on(Divider.Resized)
    def _sidebar_resized(self, event: Divider.Resized) -> None:
        # Dragging it sets the usual width, wide or not.
        self.sidebar_size = "usual"
        state.remember_sidebar_percent(event.percent)
        self.tree._redraw_labels()

    # --- the list behind "find a file" ------------------------------------

    def rebuild_file_index(self) -> None:
        """Gather the file list afresh, in the background, so finding a file is instant.

        Needed at startup and when the rules change (hidden files shown, a
        .gitignore edited); otherwise the list keeps up with changes itself.
        """
        self._index_building = True
        self._build_file_index(self.tree.show_hidden)

    @work(thread=True, exclusive=True, group="file-index", exit_on_error=False)
    def _build_file_index(self, show_hidden: bool) -> None:
        worker = get_current_worker()
        try:
            index = build_index(self.root, show_hidden, cancelled=lambda: worker.is_cancelled)
        except Exception:
            if not worker.is_cancelled:
                self.call_from_thread(self._file_index_failed)
            raise
        if not worker.is_cancelled:
            self.call_from_thread(self._file_index_built_now, index)

    async def _file_index_built_now(self, index: FileIndex) -> None:
        """A fresh file list: catch it up with what changed meanwhile, then use it."""
        if index.root != self.root:
            return  # Gathered for the worktree tmr has since switched from.
        async with self._change_lock:
            waiting, self._changes_while_building = self._changes_while_building, []
            self._index_building = False
            update = await asyncio.to_thread(index.apply, waiting) if waiting else None
            self.tree.index = index
            # (Within the lock, so no change slips in before `file_index` is set.)
            self._file_index_ready(index)
        if update is not None:
            self._mark_changes(update)
            if update.rebuild:
                self.rebuild_file_index()
        await self.tree.resync()

    def _file_index_failed(self) -> None:
        self._index_building = False
        self._changes_while_building = []

    def _file_index_ready(self, index: FileIndex) -> None:
        self.file_index = index
        # File names in the document may be found now (or no longer): restyle
        # them as links (or not) in place, without drawing it all again.
        self.viewer.document.refresh_mentions()
        if isinstance(self.screen, FileFinder):
            self.screen.index_ready(index)

    # --- keys -------------------------------------------------------------

    def action_find_file(self) -> None:
        def chosen(path: Path | None) -> None:
            if path is not None:
                self.run_worker(self.open_file(path), exclusive=True, group="open")

        self.push_screen(FileFinder(self.file_index), chosen)

    async def action_start_editing(self) -> None:
        await self.viewer.start_editing()

    async def action_save_edit(self) -> None:
        await self.viewer.save()

    async def action_save_and_commit(self) -> None:
        path = self.viewer.path
        if path is not None and await self.viewer.save():
            self.commit_file(path)

    def action_commit_file(self) -> None:
        path = self.viewer.path
        if path is None:
            self.notify("Open a file first.")
            return
        self.commit_file(path)

    @work(exclusive=True, group="commit")
    async def commit_file(self, path: Path) -> None:
        """Commit just this file to git, asking for the message first."""
        try:
            change = await asyncio.to_thread(pending, path)
        except GitError as error:
            self.notify(str(error), severity="warning")
            return
        message = await self.push_screen_wait(CommitMessage(change))
        if message is None:
            return
        try:
            short = await asyncio.to_thread(commit, change, message)
        except GitError as error:
            self.notify(f"Couldn't commit {change.name}:\n{error}", severity="error", timeout=10)
            return
        self.notify(f"Committed {change.name} to {change.branch} ({short})", timeout=3)
        self.find_git_place()

    # --- branches and worktrees ---------------------------------------------

    def find_git_place(self) -> None:
        """Work out the open file's worktree and branch, for the foot of the sidebar."""
        self._find_git_place(self.viewer.path or self.root)

    @work(thread=True, exclusive=True, group="git-place", exit_on_error=False)
    def _find_git_place(self, path: Path) -> None:
        worker = get_current_worker()
        place = place_of(path)
        head = read_head(place) if place is not None else None
        if not worker.is_cancelled:
            self.call_from_thread(self._show_git_place, place, head)

    def _show_git_place(self, place: Place | None, head: str | None) -> None:
        before = self.git_place
        self.git_place, self._head_seen = place, head
        try:
            self.query_one(WorktreeLabel).show(place, self.root)
        except NoMatches:
            return  # Switching worktree: the sidebar is being made afresh.
        if before is not None and place is not None and before.top == place.top:
            if before.branch_label != place.branch_label:
                self.notify(
                    f"{place.top.name} is now on {place.branch_label} (it was on {before.branch_label})",
                    severity="warning",
                    timeout=6,
                )

    def _check_head(self) -> None:
        """Has another branch been checked out (by an agent, say)? Then say so."""
        place = self.git_place
        if place is not None and read_head(place) != self._head_seen:
            self.find_git_place()

    def action_switch_worktree(self) -> None:
        self.pick_worktree()

    @work(exclusive=True, group="worktrees")
    async def pick_worktree(self) -> None:
        """List the repository's worktrees, and switch to the one picked."""
        place = await asyncio.to_thread(place_of, self.viewer.path or self.root)
        if place is None:
            self.notify("This folder isn't in a git repository, so there are no worktrees.")
            return
        try:
            trees = await asyncio.to_thread(worktrees, place.top)
        except GitError as error:
            self.notify(f"Couldn't list the worktrees:\n{error}", severity="error", timeout=10)
            return
        chosen = await self.push_screen_wait(WorktreePicker(trees, place.top))
        if chosen is None:
            return
        if chosen.path == place.top:
            self.notify(f"Already in {chosen.path.name}, on {chosen.branch_label}.")
            return
        await self.switch_worktree(place.top, chosen)

    async def switch_worktree(self, here: Path, there: Worktree) -> None:
        """Show the same folder, and the same file, in another worktree."""
        root, current = self.root, self.viewer.path
        if root == here or here in root.parents:
            new_root = there.path / root.relative_to(here)
            if not new_root.is_dir():
                new_root = there.path  # That folder isn't on the other branch.
        else:
            new_root = there.path  # tmr was started on a folder above the worktree.
        file = None
        if current is not None and here in current.parents:
            twin = there.path / current.relative_to(here)
            if twin.is_file() and new_root in twin.parents:
                file = twin
        if new_root == self.root:
            # The other worktree is inside the folder on show (.claude/worktrees/…).
            if file is not None:
                await self.open_file(file)
            else:
                self.show_sidebar(True)
                await self.tree.reveal(there.path)
        else:
            await self._restart_in(new_root, file)
        where = there.path.name
        self.notify(f"Switched to {there.branch_label}, in {where}", timeout=3)

    async def _restart_in(self, root: Path, file: Path | None) -> None:
        """Show another folder, as if tmr had been started there."""
        self._stop_watching.set()
        self._stop_watching = threading.Event()
        self.workers.cancel_group(self, "file-index")
        self.root = root
        self.start_file = file
        self.file_index = None
        self._index_building = False
        self._changes_while_building = []
        # Going back and forward stays within a worktree.
        self.visited, self.place, self._stepping_to = [], -1, None
        sidebar = self.query_one("#side").display
        if self.sidebar_size == "fit":
            # Fitted to names that are going; the new folder's aren't listed yet.
            self.sidebar_size = "usual"
        await self.recompose()
        if not sidebar:
            self.show_sidebar(False)
        await self._start()

    def action_stop_editing(self) -> None:
        self.viewer.leave_editing()

    async def action_quit(self) -> None:
        if self.editing:
            # Ctrl+Q while typing means "stop editing", not "close tmr".
            self.viewer.leave_editing()
            return
        self.exit()

    def action_search(self) -> None:
        self.viewer.open_search()

    def action_next_match(self, step: int) -> None:
        self.viewer.next_match(step)

    def action_previous_match(self) -> None:
        # For clicking its hint: a "-1" can't go in a click's markup.
        self.viewer.next_match(-1)

    def action_close_search(self) -> None:
        self.viewer.action_close_search()

    def action_outline(self) -> None:
        viewer = self.viewer
        headings = viewer.headings()
        if not headings:
            self.notify("This file has no headings.")
            return

        def chosen(block_id: str | None) -> None:
            if block_id is not None:
                viewer.goto_heading(block_id)

        self.push_screen(Outline(headings, viewer.current_heading()), chosen)

    def show_sidebar(self, visible: bool) -> None:
        side = self.query_one("#side")
        if side.display == visible:
            return
        if not visible and self.tree.has_focus:
            self.viewer.scroller.focus()
        side.display = visible
        self.query_one(Divider).display = visible

    def _size_sidebar(self) -> None:
        """Give the sidebar its usual width (the one last dragged to), room for
        the longest name, or half the window if that's wider still."""
        if self.sidebar_size == "fit":
            self.query_one("#side").styles.width = self._fitting_columns()
            return
        divider = self.query_one(Divider)
        percent = state.sidebar_percent() or DEFAULT_PERCENT
        if self.sidebar_size == "half":
            # Half the window, unless it's usually wider already.
            percent = max(percent, WIDE_PERCENT)
        divider.apply(percent)

    def _fitting_columns(self) -> int:
        """How wide the sidebar has to be to show every name on show in full,
        with a little air before the scrollbar."""
        tree = self.tree
        # Whatever the rows don't get now (padding, scrollbar), and the widest row.
        around = self.query_one("#side").region.width - tree.scrollable_content_region.width
        return around + tree.widest_row() + FIT_SPARE_COLUMNS

    def action_toggle_sidebar(self) -> None:
        """Usual width → wider → hidden → usual.

        Wider is just enough for the longest name on show, up to half the
        window (the document keeps the other half). If the names fit already,
        straight to hidden.
        """
        side = self.query_one("#side")
        if not side.display:
            self.show_sidebar(True)
        elif self.sidebar_size != "usual" or self._fitting_columns() <= side.size.width:
            self.sidebar_size = "usual"
            self._size_sidebar()
            self.show_sidebar(False)
        else:
            half = max(side.size.width, self.query_one("#columns").size.width * WIDE_PERCENT / 100)
            self.sidebar_size = "fit" if self._fitting_columns() < half else "half"
            self._size_sidebar()
        self.tree._redraw_labels()

    async def action_toggle_all_files(self) -> None:
        tree = self.tree
        tree.markdown_only = not tree.markdown_only
        tree.forget_folder_contents()
        await tree.reload()
        self.notify("Showing markdown files only" if tree.markdown_only else "Showing all files")

    async def action_toggle_hidden(self) -> None:
        tree = self.tree
        tree.show_hidden = not tree.show_hidden
        tree.forget_folder_contents()
        # The tree looks for itself until the list for the new setting is ready.
        self.rebuild_file_index()
        await tree.reload()
        self.notify("Showing hidden files" if tree.show_hidden else "Hiding hidden files")

    def action_edit(self) -> None:
        path = self.viewer.path
        if path is None:
            self.notify("Open a file first.")
            return
        editor = editor_command()
        try:
            if editor:
                # Hand the editor the screen until it quits (a GUI editor comes straight back).
                with self.suspend():
                    subprocess.run([*editor, str(path)])
                self.run_worker(self.viewer.reload(), exclusive=True, group="open")
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.notify("Set EDITOR (or TMR_EDITOR) to choose an editor.", severity="warning")
        except OSError as error:
            self.notify(f"Couldn't open the editor: {error}", severity="error")

    # --- copying ------------------------------------------------------------

    def on_text_selected(self, event: events.TextSelected) -> None:
        # Copy the markdown as written ("## Tasks", "- [ ] Write the plan"), not as drawn.
        text = None
        if self.screen is self.screen_stack[0] and self.viewer.document.display:
            text = self.viewer.document.selected_source(self.screen.selections)
        if text is None:
            text = self.screen.get_selected_text()
        if not text or not text.strip():
            return
        self.copy_to_clipboard(text)
        self.notify("Copied", timeout=1.5)

    def action_copy_document(self) -> None:
        """Copy the whole open file, exactly as it's written (markdown and all)."""
        path = self.viewer.path
        if path is None:
            self.notify("Open a file first.")
            return
        try:
            data = path.read_bytes()
        except OSError as error:
            self.notify(f"Couldn't read the file: {error.strerror}", severity="error")
            return
        if b"\0" in data[:8192]:
            self.notify("This file isn't text, so it can't be copied.", severity="warning")
            return
        text = data.decode("utf-8", errors="replace")
        self.copy_to_clipboard(text)
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        self.notify(f"Copied all of {path.name} ({lines:,} line{'s' if lines != 1 else ''})", timeout=2)

    @on(CodeFence.CopyRequested)
    def _copy_code_block(self, event: CodeFence.CopyRequested) -> None:
        """The copy button on a code block: everything inside it, without the ``` lines."""
        if not event.code.strip():
            self.notify("This code block is empty.")
            return
        self.copy_to_clipboard(event.code)
        lines = event.code.count("\n") + 1
        self.notify(f"Copied code block ({lines:,} line{'s' if lines != 1 else ''})", timeout=2)

    def action_copy_path(self) -> None:
        """Copy where the open file lives, ready to paste to an agent."""
        path = self.viewer.path
        if path is None:
            self.notify("Open a file first.")
            return
        self.copy_to_clipboard(str(path))
        self.notify(f"Copied path of {path.name}", timeout=2)

    def copy_to_clipboard(self, text: str) -> None:
        """Copy to the system clipboard (see `tmr.clipboard`), else ask the terminal to."""
        if clipboard.copy(text):
            self._clipboard = text
            return
        super().copy_to_clipboard(text)

    def clipboard_text(self) -> str:
        """What's on the system clipboard, else what tmr copied last."""
        text = clipboard.paste()
        return text if text is not None else self.clipboard

    # --- live updates -----------------------------------------------------

    @work(thread=True, exit_on_error=False, group="watch")
    def watch_folder(self) -> None:
        """Watch the folder for changes, starting the watcher again if it fails.

        Live updates are a nicety, so a failure never takes tmr down, but it
        isn't kept quiet either: the reader is told, since the view would
        otherwise silently go stale.
        """
        from watchfiles import watch

        # This watcher's own, since switching worktree starts another watcher.
        stop = self._stop_watching

        def interesting(_change, path: str) -> bool:
            changed = Path(path)
            return changed == self.viewer.path or wants_change(self.file_index, self.root, changed)

        failures = 0
        restarted = False
        while not stop.is_set():
            started = time.monotonic()
            try:
                for changes in watch(
                    self.root,
                    watch_filter=interesting,
                    debounce=400,
                    step=100,
                    stop_event=stop,
                    raise_interrupt=False,
                    yield_on_timeout=True,
                ):
                    if stop.is_set():
                        return
                    if restarted:
                        restarted = False
                        self._say_from_thread("Live updates are back.")
                    if changes:
                        self.call_from_thread(self._apply_changes, changes)
                reason = "the watcher stopped"
            except Exception as error:
                reason = str(error) or type(error).__name__
            if stop.is_set():
                return
            if time.monotonic() - started > WATCH_SETTLED_SECONDS:
                failures = 0
            if failures >= len(WATCH_RETRY_DELAYS):
                self._say_from_thread(
                    f"Live updates are off: {reason}. Restart tmr to turn them back on.", "error"
                )
                return
            if failures == 0:
                self._say_from_thread(f"Live updates stopped: {reason}. Trying again…")
            if stop.wait(WATCH_RETRY_DELAYS[failures]):
                return
            failures += 1
            restarted = True
            # Changes made meanwhile went unseen: catch up with them.
            self._call_from_thread_quietly(self._catch_up)

    def _say_from_thread(self, message: str, severity: SeverityLevel = "warning") -> None:
        self._call_from_thread_quietly(
            lambda: self.notify(message, severity=severity, timeout=10)
        )

    def _call_from_thread_quietly(self, callback) -> None:
        try:
            self.call_from_thread(callback)
        except Exception:
            pass  # tmr is closing.

    async def _catch_up(self) -> None:
        """Changes may have gone unseen (the watcher was down): look at everything again."""
        self.rebuild_file_index()
        if self.viewer.path is not None:
            await self.viewer.reload()

    async def _apply_changes(self, changes: set[tuple[object, str]]) -> None:
        async with self._change_lock:
            touched: set[Path] = set()
            for _change, raw in changes:
                path = Path(raw)
                try:
                    path = path.parent.resolve() / path.name
                except OSError:
                    pass
                touched.add(path)

            index = self.file_index
            if index is None or self._index_building:
                # A fresh list is on its way: it takes these in when it's ready.
                self._changes_while_building.extend(changes)
            else:
                update = await asyncio.to_thread(index.apply, changes)
                self._mark_changes(update)
                if _markdown_came_or_went(update):
                    # A file the document names may be found now, or be gone.
                    self.viewer.document.refresh_mentions()
                if update.rebuild:
                    self.rebuild_file_index()
                elif update.folders:
                    await self.tree.resync(update.folders)
                if update.changed and isinstance(self.screen, FileFinder):
                    self.screen.index_ready(index)

            current = self.viewer.path
            if current is not None and current in touched:
                await self.viewer.reload()

    def _mark_changes(self, update: Update) -> None:
        """Put a dot by files that changed; forget the dots of files that are gone."""
        tree = self.tree
        now = time.monotonic()
        for path in update.listed:
            tree.changed[path] = now
        for path in update.removed:
            tree.changed.pop(path, None)
        if update.listed or update.removed:
            tree._redraw_labels()


def _markdown_came_or_went(update: Update) -> bool:
    """Did these changes add or remove a markdown file (not just change one)?"""
    if any(is_markdown(path) for path in update.removed):
        return True
    return bool(update.folders) and any(is_markdown(path) for path in update.listed)
