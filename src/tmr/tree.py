"""The left column: a folder tree."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Iterable

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DirectoryTree
from textual.widgets._directory_tree import DirEntry
from textual.widgets._tree import TreeNode

from tmr.files import contains_markdown, is_clutter, is_markdown
from tmr.index import FileIndex

CHANGE_FRESH_SECONDS = 20
"""How long a change dot stays bright."""

CHANGE_FADE_SECONDS = 120
"""How long before a change dot disappears."""


class FileTree(DirectoryTree):
    """A directory tree that hides clutter, fades what isn't markdown, and marks changes."""

    ICON_NODE = "▸ "
    ICON_NODE_EXPANDED = "▾ "
    ICON_FILE = "  "

    BINDINGS = [
        Binding("enter", "choose", "Open", show=False),
    ]

    class Moved(Message):
        """The reader moved the cursor onto a file (with the keys): show it."""

        def __init__(self, tree: FileTree, path: Path) -> None:
            super().__init__()
            self.tree = tree
            self.path = path

        @property
        def control(self) -> FileTree:
            return self.tree

    class Read(Message):
        """Enter on a file: show it, and go over to the document to read it."""

        def __init__(self, tree: FileTree, path: Path) -> None:
            super().__init__()
            self.tree = tree
            self.path = path

        @property
        def control(self) -> FileTree:
            return self.tree

    COMPONENT_CLASSES = DirectoryTree.COMPONENT_CLASSES | {
        "file-tree--other",
        "file-tree--changed",
        "file-tree--changed-old",
        "file-tree--open",
    }

    DEFAULT_CSS = """
    FileTree {
        overflow-x: hidden;
        background: transparent;
    }
    FileTree > .directory-tree--folder {
        text-style: none;
    }
    FileTree > .directory-tree--extension {
        text-style: none;
    }
    FileTree > .directory-tree--hidden {
        text-style: dim;
    }
    /* Markdown in the ordinary text colour, anything else (listed with `a`)
       faded; the file on show is the one in the accent colour. */
    FileTree > .file-tree--other {
        text-style: dim;
    }
    FileTree > .file-tree--open {
        color: $primary;
        text-style: bold;
    }
    FileTree > .file-tree--changed {
        color: $success;
        text-style: bold;
    }
    FileTree > .file-tree--changed-old {
        color: $success;
        text-style: dim;
    }
    """

    def __init__(self, root: Path, **kwargs) -> None:
        self.show_hidden = False
        self.markdown_only = True
        self.changed: dict[Path, float] = {}
        self.open_path: Path | None = None
        self.index: FileIndex | None = None
        """The file list, which says what's listed; until it's gathered, the
        tree looks for itself (see `_fallback_filter`)."""
        self.pinned: set[Path] = set()
        """Files listed whatever the rules say (the file tmr was started on)."""
        self._markdown_folders: dict[Path, bool] = {}
        super().__init__(root, **kwargs)
        self.show_root = False
        self.guide_depth = 3

    def on_mount(self) -> None:
        self.set_interval(5, self._fade_changes)

    # --- what gets listed -------------------------------------------------

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        index = self.index
        if index is not None and index.show_hidden != self.show_hidden:
            index = None  # Gathered for the other setting; a fresh one is on its way.
        for path in paths:
            if self._is_pinned(path):
                yield path
                continue
            relative = index.relative(path) if index is not None else None
            if index is None or relative is None or not index.knows(relative):
                if self._fallback_filter(path):
                    yield path
            elif self._safe_is_dir(path):
                if index.shows_folder(relative, self.markdown_only):
                    yield path
            elif index.shows_file(relative) and (not self.markdown_only or is_markdown(path)):
                yield path

    def _is_pinned(self, path: Path) -> bool:
        return any(path == pinned or path in pinned.parents for pinned in self.pinned)

    def _fallback_filter(self, path: Path) -> bool:
        """Whether to list a path, looking at the disk (before the file list is ready)."""
        if not self.show_hidden and is_clutter(path):
            return False
        if self.markdown_only:
            if self._safe_is_dir(path):
                return self._folder_has_markdown(path)
            return is_markdown(path)
        return True

    def _folder_has_markdown(self, folder: Path) -> bool:
        cached = self._markdown_folders.get(folder)
        if cached is None:
            cached = contains_markdown(folder, self.show_hidden)
            self._markdown_folders[folder] = cached
        return cached

    def forget_folder_contents(self) -> None:
        """Files were added or removed, so re-check which folders hold markdown
        (when the tree looks for itself)."""
        self._markdown_folders.clear()

    # --- how entries look -------------------------------------------------

    def _name_label(
        self, node: TreeNode[DirEntry], base_style: Style, style: Style
    ) -> Text:
        """The row as DirectoryTree draws it, but a markdown file without its
        ".md": nearly everything listed is markdown, so it only takes up room."""
        text = DirectoryTree.render_label(self, node, base_style, style)
        path = node.data.path if node.data is not None else None
        if path is not None and not node.allow_expand and path.suffix.lower() == ".md" and path.stem:
            text.right_crop(len(path.suffix))
        return text

    def render_label(
        self, node: TreeNode[DirEntry], base_style: Style, style: Style
    ) -> Text:
        text = self._name_label(node, base_style, style)
        path = node.data.path if node.data is not None else None
        # The cursor's row is only highlighted while the list has the focus.
        on_cursor = node.line == self.cursor_line and self.has_focus
        if path is not None and not node.allow_expand and not is_markdown(path) and not on_cursor:
            text.stylize(
                self.get_component_rich_style("file-tree--other", partial=True),
                len(self.ICON_FILE),
            )

        if path is not None and path == self.open_path and not on_cursor:
            text.stylize(
                self.get_component_rich_style("file-tree--open", partial=True),
                len(self.ICON_FILE),
            )

        dot: Text | None = None
        if path is not None and path in self.changed:
            age = time.monotonic() - self.changed[path]
            if age < CHANGE_FADE_SECONDS:
                component = (
                    "file-tree--changed"
                    if age < CHANGE_FRESH_SECONDS
                    else "file-tree--changed-old"
                )
                dot = Text(" ●", style=self.get_component_rich_style(component, partial=True))

        # Shorten long names with "…" so everything fits the column.
        depth = 0
        ancestor = node.parent
        while ancestor is not None:
            depth += 1
            ancestor = ancestor.parent
        indent = max(0, depth - (0 if self.show_root else 1)) * self.guide_depth
        room = self.scrollable_content_region.width - indent - (2 if dot else 0)
        if room > 3 and text.cell_len > room:
            text.truncate(room, overflow="ellipsis")
        if dot is not None:
            text.append_text(dot)
        return text

    # --- moving and choosing -------------------------------------------

    def _cursor_file(self) -> Path | None:
        node = self.cursor_node
        if node is None or node.allow_expand or node.data is None:
            return None
        return node.data.path

    def _moving(self, move) -> None:
        """Move the cursor, saying so if it lands on another file. Only moves
        made with the keys say so: the tree being listed again, say, doesn't
        change what's on show."""
        before = self.cursor_node
        move()
        path = self._cursor_file()
        if path is not None and self.cursor_node is not before:
            self.post_message(self.Moved(self, path))

    def action_cursor_up(self) -> None:
        self._moving(super().action_cursor_up)

    def action_cursor_down(self) -> None:
        self._moving(super().action_cursor_down)

    def action_page_up(self) -> None:
        self._moving(super().action_page_up)

    def action_page_down(self) -> None:
        self._moving(super().action_page_down)

    def action_scroll_home(self) -> None:
        self._moving(super().action_scroll_home)

    def action_scroll_end(self) -> None:
        self._moving(super().action_scroll_end)

    def action_cursor_previous_sibling(self) -> None:
        self._moving(super().action_cursor_previous_sibling)

    def action_cursor_next_sibling(self) -> None:
        self._moving(super().action_cursor_next_sibling)

    def action_cursor_parent_next_sibling(self) -> None:
        self._moving(super().action_cursor_parent_next_sibling)

    def action_choose(self) -> None:
        """Enter: read the file under the cursor, or open/close the folder."""
        path = self._cursor_file()
        if path is None:
            self.action_select_cursor()
        else:
            self.post_message(self.Read(self, path))

    # --- change marks -----------------------------------------------------

    def mark_changed(self, path: Path) -> None:
        self.changed[path] = time.monotonic()
        self._redraw_labels()

    def _fade_changes(self) -> None:
        if not self.changed:
            return
        now = time.monotonic()
        self.changed = {
            path: when
            for path, when in self.changed.items()
            if now - when < CHANGE_FADE_SECONDS
        }
        self._redraw_labels()

    def widest_row(self) -> int:
        """How many columns the rows on show need to fit without "…"."""
        plain = Style()
        return max(
            (
                line._get_guide_width(self.guide_depth, self.show_root)
                + self._name_label(line.node, plain, plain).cell_len
                for line in self._tree_lines
            ),
            default=0,
        )

    def _redraw_labels(self) -> None:
        self._clear_line_cache()
        self.refresh()

    def on_resize(self) -> None:
        self._redraw_labels()

    def on_focus(self) -> None:
        self._redraw_labels()  # The cursor's row is highlighted now, so drawn differently.

    def on_blur(self) -> None:
        self._redraw_labels()

    # --- finding nodes ----------------------------------------------------

    def find_node(self, path: Path) -> TreeNode[DirEntry] | None:
        """Find the loaded node for a path, if the tree currently shows it."""
        pending = [self.root]
        while pending:
            node = pending.pop()
            if node.data is not None and node.data.path == path:
                return node
            if node.data is None or node.data.path in path.parents or node is self.root:
                pending.extend(node.children)
        return None

    async def reveal(self, path: Path) -> None:
        """Open the folders leading to a file and put the cursor on it."""
        root = Path(self.path).resolve()
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            return
        node = self.root
        current = root
        for part in parts:
            current = current / part
            if node.allow_expand and (
                node.data is None or not node.data.loaded or not node.is_expanded
            ):
                await self.reload_node(node)
            child = self._child_for(node, current)
            if child is None:
                # The folder may still have been loading; list it again and retry.
                await self.reload_node(node)
                child = self._child_for(node, current)
            if child is None:
                return
            node = child
        self.move_cursor(node, animate=False)
        self.scroll_to_node(node, animate=False)

    @staticmethod
    def _child_for(node: TreeNode[DirEntry], path: Path) -> TreeNode[DirEntry] | None:
        for child in node.children:
            if child.data is not None and child.data.path == path:
                return child
        return None

    async def resync(self, folders: Iterable[Path] | None = None) -> None:
        """List folders again where what they'd list has changed: these (among
        those on show), or every one on show.

        Only folders whose listing differs are redrawn, so the cursor and what's
        open stay put.
        """
        wanted = None if folders is None else set(folders)
        nodes = []
        pending = [self.root]
        while pending:
            node = pending.pop()
            if node.data is None or not node.data.loaded:
                continue
            if node is not self.root and not node.is_expanded:
                continue
            if wanted is None or node.data.path in wanted:
                nodes.append(node)
            pending.extend(child for child in node.children if child.allow_expand)
        done: list[Path] = []
        for node in sorted(nodes, key=lambda node: len(node.data.path.parts)):
            path = node.data.path
            if any(path == folder or folder in path.parents for folder in done):
                continue  # Listed again along with a folder above it.
            listing = await asyncio.to_thread(self._listing, path)
            if listing != [child.data.path for child in node.children if child.data is not None]:
                await self.reload_node(node)
                done.append(path)

    def _listing(self, folder: Path) -> list[Path]:
        """What the tree would list in this folder, in its order."""
        try:
            paths = list(folder.iterdir())
        except OSError:
            return []
        return sorted(
            self.filter_paths(paths),
            key=lambda path: (not self._safe_is_dir(path), path.name.lower()),
        )
