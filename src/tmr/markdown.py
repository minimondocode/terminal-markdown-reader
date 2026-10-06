"""The formatted markdown view, with checkboxes, pictures, diagrams and front matter.

The document is built from `tmr.render`'s blocks: one widget per top-level
block (a heading, a paragraph, a whole list), each knowing where every
character it shows came from in the file.
"""

from __future__ import annotations

import asyncio
import re
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import count
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote

from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.content import Content
from textual.geometry import Size
from textual.highlight import highlight as highlight_code
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import Visual
from textual.widget import Widget
from textual.widgets import Label, Static

from tmr.render import (
    TASK,
    Block,
    Mention,
    Picture,
    Piece,
    Run,
    Segment,
    build,
    mentioned_names,
    slugs,
    wrap,
)

__all__ = ["CodeFence", "Document", "mentioned_names", "read_front_matter", "toggle_task"]


def toggle_task(source: str, line: int) -> str | None:
    """Tick or untick the checkbox on one line ("- [ ]" or "- [~]" to "- [x]", and back to "- [ ]").

    Returns None when that line isn't a task.
    """
    lines = source.split("\n")
    if not 0 <= line < len(lines):
        return None
    match = TASK.match(lines[line])
    if match is None:
        return None
    at = match.start(2)
    # A partly done task ("[~]") ticks to done.
    mark = " " if match.group(2) in "xX" else "x"
    lines[line] = lines[line][:at] + mark + lines[line][at + 1 :]
    return "\n".join(lines)


_URL = re.compile(r"^(https?|ftp)://\S+$|^mailto:\S+$", re.IGNORECASE)


def read_front_matter(yaml: str) -> list[tuple[str, str]] | None:
    """The top-level "key: value" pairs of simple YAML, as text to show.

    Lists become "a, b", nested keys "name: value · name: value", and folded
    text joins into one line. Returns None for YAML beyond that, so the caller
    can show it as written.
    """
    rows: list[tuple[str, str, list[str]]] = []
    for line in yaml.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[0] in " \t-":
            if not rows:
                return None
            rows[-1][2].append(stripped)
            continue
        key, colon, value = line.partition(":")
        if not colon or not key.strip() or key.strip().startswith(("[", "{", "&", "*", "?")):
            return None
        rows.append((key.strip().strip("\"'"), value.strip(), []))

    pairs = []
    for key, value, nested in rows:
        if value[:1] in ("|", ">"):
            text = " ".join(nested)
        elif value:
            text = " ".join([_scalar(value), *nested])
        elif nested and all(item.startswith("-") for item in nested):
            text = ", ".join(_scalar(item[1:].strip()) for item in nested)
        else:
            text = " · ".join(item.lstrip("- ") for item in nested)
        pairs.append((key, text))
    return pairs


def _scalar(value: str) -> str:
    """One YAML value as plain text: without quotes, comments or [brackets]."""
    if value[:1] in ("'", '"'):
        close = value.find(value[0], 1)
        return value[1:close] if close != -1 else value[1:]
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if value.startswith("[") and value.endswith("]"):
        return ", ".join(_scalar(item.strip()) for item in value[1:-1].split(",") if item.strip())
    return value


def tidy_edges(source: str, low: int, high: int) -> tuple[int, int]:
    """Widen a part-line highlight so symbols cut at its edges come in pairs.

    Highlighting "demo with a link" out of "**demo** with a [link](x.md)"
    gives the whole "**demo** with a [link](x.md)", not "demo** with a [link".
    """
    before = low
    while before > 0 and source[before - 1] in _EMPHASIS:
        before -= 1
    opening = source[before:low]
    if opening and source[low:high].count(opening) % 2 == 1:
        low = before
    after = high
    while after < len(source) and source[after] in _EMPHASIS:
        after += 1
    closing = source[high:after]
    if closing and source[low:high].count(closing) % 2 == 1:
        high = after

    # Links: take in the "[" and the "](target)" around highlighted link text.
    span = source[low:high]
    if low > 0 and source[low - 1] == "[" and "](" in span and "[" not in span[: span.index("](")]:
        low -= 1
    if source.startswith("](", high) and source.rfind("[", low, high) > source.rfind("]", low, high):
        close = source.find(")", high)
        if close != -1:
            high = close + 1
    return low, high


_EMPHASIS = "*_~`"

MATCH_STYLE = "ansi_black on ansi_yellow"
CURRENT_MATCH_STYLE = "bold ansi_black on ansi_bright_cyan"


# --- what the widgets have in common ---------------------------------------------------


class Searchable:
    """A widget whose text "search in document" looks through."""

    def search_text(self) -> str:
        """The text to search, independent of how it's wrapped on screen."""
        raise NotImplementedError

    def show_matches(self, matches: list[tuple[int, int, bool]]) -> None:
        """Highlight these (start, end, is-current) ranges of `search_text`."""
        raise NotImplementedError

    def match_row(self, index: int) -> int:
        """Which row of the widget's content this character of `search_text` is on."""
        raise NotImplementedError


class DrawnByRow:
    """A widget of laid-out rows (`lay_out`) that draws only the rows on screen.

    Textual draws a widget whole whenever any of it changes: for a table
    thousands of rows long, every search highlight, `n` or hover went over
    all of them. Here each row is drawn as it's needed, with its search
    highlights looked up rather than searched for.
    """

    _matches: list[tuple[int, int, bool]]
    _highlights: tuple[int, dict[int, list[tuple[str, int, int]]]] | None = None

    def lay_out(self, width: int) -> tuple[list[Content], object]:
        raise NotImplementedError

    def highlight_rows(self, width: int) -> dict[int, list[tuple[str, int, int]]]:
        """The search highlights on each row: (style, start, end) within the row."""
        raise NotImplementedError

    def _highlighted(self, width: int) -> dict[int, list[tuple[str, int, int]]]:
        if self._highlights is None or self._highlights[0] != width:
            self._highlights = (width, self.highlight_rows(width) if self._matches else {})
        return self._highlights[1]

    def show_matches(self, matches: list[tuple[int, int, bool]]) -> None:
        self._matches = matches
        self._highlights = None
        self.refresh()  # type: ignore[attr-defined]

    def _row_content(self, width: int, y: int) -> Content:
        line = self.lay_out(width)[0][y]
        for style, start, end in self._highlighted(width).get(y, ()):
            line = line.stylize(style, start, end)
        return line

    def render(self) -> Content:
        width = self.content_size.width  # type: ignore[attr-defined]
        lines = self.lay_out(width)[0]
        return Content("\n").join(self._row_content(width, y) for y in range(len(lines)))

    def render_line(self, y: int) -> Strip:
        widget: Widget = self  # type: ignore[assignment]
        width = widget.size.width
        lines = self.lay_out(width)[0]
        if not 0 <= y < len(lines):
            return Strip.blank(width, widget.visual_style.rich_style)
        line = self._row_content(width, y)
        selection = widget.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            start, end = span
            style = Style.from_styles(widget.screen.get_component_styles("screen--selection"))
            line = line.stylize(style, start, len(line.plain) if end == -1 else end)
        strips = Visual.to_strips(widget, line, width, 1, widget.visual_style, apply_selection=False)
        if not strips:
            return Strip.blank(width, widget.visual_style.rich_style)
        # Where each character is, for selecting with the mouse: this row, not the first.
        return strips[0].apply_offsets(0, y)


class Sourced:
    """A widget standing for part of the file, which can say which part is highlighted."""

    top: Block
    """The top-level block it's in (whose offsets its own are counted from)."""

    def source_span(self, widget: Widget, selection: Selection) -> tuple[int, int] | None:
        """The part of the top-level block's source behind the highlighted part of
        `widget` (itself or one of its children), or None if that can't be told."""
        return 0, len(self.top.source.rstrip("\n"))


def _document_of(widget: Widget) -> Document | None:
    for node in widget.ancestors:
        if isinstance(node, Document):
            return node
    return None


def _line_end(source: str, offset: int) -> int:
    """Where the line with this offset in it ends."""
    newline = source.find("\n", max(0, offset))
    return len(source) if newline == -1 else newline


# --- text: headings, paragraphs, lists, quotes ----------------------------------------------


@dataclass
class _Row:
    run: int
    prefix: int
    """How many characters of the row are its prefix (bullet, indent, quote bars)."""
    start: int
    end: int
    """The part of the run's text on this row."""
    first: bool


class TextBlock(DrawnByRow, Widget, Searchable, Sourced):
    """Text laid out by tmr: paragraphs with hanging indents for bullets and quotes.

    Laying it out here, rather than leaving it to Textual, means every row and
    column on screen is known to come from a particular character of the file.
    """

    COMPONENT_CLASSES = {"em", "strong", "s", "code_inline", "bullet", "quote-bar", "task", "task-done", "task-partial"}

    DEFAULT_CSS = """
    TextBlock {
        width: 1fr;
        height: auto;
        & > .em { text-style: italic; }
        & > .strong { text-style: bold; }
        & > .s { text-style: strike; }
        &:dark > .code_inline {
            background: $warning 10%;
            color: $text-warning 95%;
        }
        &:light > .code_inline {
            background: $error 5%;
            color: $text-error 95%;
        }
        &:ansi > .code_inline {
            background: ansi_default;
        }
        /* On a light background the warning colour is a red, which reads as
           an error: there, code is in the text colour on a grey patch. */
        &:ansi:light > .code_inline {
            background: ansi_white;
            color: $foreground;
        }
        & > .bullet {
            text-style: dim;
        }
        & > .quote-bar {
            color: ansi_bright_black;
        }
        /* A task's box: still to do, done, partly done. */
        & > .task {
            color: $primary;
            text-style: bold;
        }
        & > .task-done {
            color: ansi_green;
            text-style: bold;
        }
        & > .task-partial {
            color: ansi_magenta;
            text-style: bold;
        }
    }
    """

    def __init__(self, top: Block, runs: list[Run], classes: str = "", id: str | None = None) -> None:
        super().__init__(classes=classes, id=id)
        self.top = top
        self.runs = runs
        self._resolved: list[Content] | None = None
        self._layout: tuple[int, list[Content], list[_Row]] | None = None
        self._matches: list[tuple[int, int, bool]] = []
        self._starts = []
        position = 0
        for run in runs:
            self._starts.append(position)
            position += len(run.piece.plain) + 1

    # -- the text, with file names as links -------------------------------------------

    @property
    def has_mentions(self) -> bool:
        return any(run.piece.mentions for run in self.runs)

    def forget_mentions(self) -> None:
        """The file list changed: look the file names up again."""
        if self.has_mentions:
            self._resolved = None
            self._layout = None
            self._highlights = None
            self.refresh(layout=True)

    def _texts(self) -> list[Content]:
        if self._resolved is None:
            document = _document_of(self)
            texts = []
            for run in self.runs:
                text = run.piece.text
                if run.piece.mentions and document is not None:
                    text = document.link_mentions(text, run.piece.mentions)
                texts.append(text)
            self._resolved = texts
        return self._resolved

    # -- laying out ------------------------------------------------------------------

    def lay_out(self, width: int) -> tuple[list[Content], list[_Row]]:
        """The rows shown at this width, and what each one holds."""
        if self._layout is not None and self._layout[0] == width:
            return self._layout[1], self._layout[2]
        lines: list[Content] = []
        rows: list[_Row] = []
        for index, (run, text) in enumerate(zip(self.runs, self._texts())):
            prefix_width = max(cell_len(run.first.plain), cell_len(run.rest.plain))
            for number, (start, end) in enumerate(wrap(text.plain, width - prefix_width)):
                prefix = run.first if number == 0 else run.rest
                lines.append(prefix + text[start:end])
                rows.append(_Row(index, len(prefix.plain), start, end, number == 0))
        self._layout = (width, lines, rows)
        return lines, rows

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.lay_out(width)[0])

    def highlight_rows(self, width: int) -> dict[int, list[tuple[str, int, int]]]:
        _, rows = self.lay_out(width)
        starts = [self._starts[row.run] + row.start for row in rows]
        found: dict[int, list[tuple[str, int, int]]] = {}
        for start, end, current in self._matches:
            style = CURRENT_MATCH_STYLE if current else MATCH_STYLE
            number = max(0, bisect_right(starts, start) - 1)
            while number < len(rows) and starts[number] < end:
                row = rows[number]
                base = self._starts[row.run]
                low = max(start - base, row.start)
                high = min(end - base, row.end)
                if low < high:
                    found.setdefault(number, []).append(
                        (style, row.prefix + low - row.start, row.prefix + high - row.start)
                    )
                number += 1
        return found

    @property
    def plain(self) -> str:
        """The text as it's shown (rows joined with newlines)."""
        lines, _ = self.lay_out(self.content_size.width or 80)
        return "\n".join(line.plain for line in lines)

    def locate(self, text: str) -> tuple[int, int]:
        """Where `text` first appears on screen, as (x, y) within the content."""
        for y, line in enumerate(self.plain.split("\n")):
            x = line.find(text)
            if x != -1:
                return cell_len(line[:x]), y
        raise ValueError(f"{text!r} isn't shown")

    # -- search ------------------------------------------------------------------------

    def search_text(self) -> str:
        return "\n".join(run.piece.plain for run in self.runs)

    def match_row(self, index: int) -> int:
        _, rows = self.lay_out(self.content_size.width)
        for number, row in enumerate(rows):
            base = self._starts[row.run]
            if base + row.start <= index <= base + row.end:
                return number
        return 0

    # -- where things are in the file -----------------------------------------------------

    def _offset(self, row: int, column: int) -> tuple[int, bool, bool]:
        """The source offset of the character at (row, column), and whether it's
        the start of its paragraph, or past the end of it."""
        _, rows = self.lay_out(self.content_size.width)
        row = max(0, min(row, len(rows) - 1))
        info = rows[row]
        run = self.runs[info.run]
        offsets = run.piece.offsets
        index = info.start + max(0, column - info.prefix)
        if (column <= info.prefix and info.first) or index == 0:
            return run.marker, True, not offsets
        if index >= info.end:
            if index >= len(offsets) and offsets:
                return offsets[-1] + 1, False, True
            index = info.end
        if index >= len(offsets):
            return (offsets[-1] + 1 if offsets else run.marker), False, True
        return offsets[index], False, False

    def source_line_at_row(self, row: int) -> int:
        """The line of the file shown on this row."""
        _, rows = self.lay_out(self.content_size.width)
        if not rows:
            return self.top.line
        info = rows[max(0, min(row, len(rows) - 1))]
        offsets = self.runs[info.run].piece.offsets
        if info.start < len(offsets):
            return self.top.line_at(offsets[info.start])
        return self.top.line_at(self.runs[info.run].marker)

    def row_of_line(self, line: int) -> int | None:
        """The first row showing this line of the file (or a later one), if any."""
        _, rows = self.lay_out(self.content_size.width)
        for number, info in enumerate(rows):
            offsets = self.runs[info.run].piece.offsets
            at = offsets[info.start] if info.start < len(offsets) else self.runs[info.run].marker
            if at >= 0 and self.top.line_at(at) >= line:
                return number
        return None

    def source_span(self, widget: Widget, selection: Selection) -> tuple[int, int] | None:
        lines, _ = self.lay_out(self.content_size.width)
        if not lines or not self.runs:
            return None
        if selection.start is None:
            start_row, start_column = 0, 0
        else:
            start_row, start_column = selection.start.y, selection.start.x
        if selection.end is None:
            end_row, end_column = len(lines) - 1, len(lines[-1].plain) - 1
        else:
            end_row, end_column = selection.end.y, selection.end.x - 1
        low, at_start, _ = self._offset(start_row, start_column)
        high, _, past_end = self._offset(end_row, max(0, end_column))
        source = self.top.source.rstrip("\n")
        # The whole block, or (inside a list with code in it, say) just this part of it.
        whole = self.runs is self.top.runs
        begin = 0 if whole else max(0, self.runs[0].marker)
        last = self.runs[-1].piece.offsets
        finish = len(source) if whole else _line_end(source, last[-1] if last else begin)
        from_start = start_row == 0 and at_start
        to_end = self._tail_blank(end_row, end_column)
        if from_start and to_end:
            return begin, finish
        if not past_end:
            high += 1
        if to_end:
            high = finish
        elif past_end:
            # To the end of a paragraph: its closing symbols too, to the end of the line.
            high = _line_end(source, high - 1)
        if from_start:
            low = begin
        if high <= low:
            return None
        return tidy_edges(source, low, high)

    def _tail_blank(self, row: int, column: int) -> bool:
        """Is there nothing but space after (row, column) in this block?"""
        lines, _ = self.lay_out(self.content_size.width)
        if not lines:
            return True
        rest = lines[min(row, len(lines) - 1)].plain[column + 1 :]
        later = "".join(line.plain for line in lines[row + 1 :])
        return not (rest + later).strip()

    # -- clicks -------------------------------------------------------------------------

    async def action_link(self, href: str) -> None:
        self.post_message(Document.LinkClicked(href))

    def on_click(self, event: events.Click) -> None:
        # A task's box: tick or untick it.
        offset = event.style.meta.get("task")
        if offset is not None:
            event.stop()
            self.post_message(Document.TaskToggled(self.top.line_at(offset)))


class Rule(Widget):
    """A horizontal rule ("---"), with a line of air either side."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    Rule {
        width: 1fr;
        border-bottom: solid ansi_bright_black;
        height: 1;
        margin: 1 0;
    }
    """

    def __init__(self, top: Block, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.top = top

    def render(self) -> Content:
        return Content("")


# --- tables -------------------------------------------------------------------------------


class TableBlock(DrawnByRow, Widget, Searchable, Sourced):
    """A table, drawn with lines between the cells, which wrap to fit."""

    COMPONENT_CLASSES = {"em", "strong", "s", "code_inline", "keyline", "table-header"}

    DEFAULT_CSS = """
    TableBlock {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        & > .em { text-style: italic; }
        & > .strong { text-style: bold; }
        & > .s { text-style: strike; }
        &:dark > .code_inline {
            background: $warning 10%;
            color: $text-warning 95%;
        }
        &:light > .code_inline {
            background: $error 5%;
            color: $text-error 95%;
        }
        &:ansi > .code_inline {
            background: ansi_default;
        }
        &:ansi:light > .code_inline {
            background: ansi_white;
            color: $foreground;
        }
        & > .keyline {
            color: ansi_bright_black;
        }
        & > .table-header {
            text-style: bold;
        }
    }
    """

    def __init__(self, top: Block, table: Block, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.top = top
        self.table = table
        self.cells: list[list[Piece]] = [table.header, *table.rows] if table.header else list(table.rows)
        self.columns = max((len(row) for row in self.cells), default=0)
        self._resolved: list[list[Content]] | None = None
        self._layout: tuple[int, list[Content], dict[tuple[int, int], list[tuple[int, int, int, int]]]] | None = None
        self._matches: list[tuple[int, int, bool]] = []
        self._order: list[tuple[int, int, int]] = []
        """(row, column, start in the search text) of each cell."""
        position = 0
        for row_index, row in enumerate(self.cells):
            for column, cell in enumerate(row):
                self._order.append((row_index, column, position))
                position += len(cell.plain) + 1

    @property
    def has_mentions(self) -> bool:
        return any(cell.mentions for row in self.cells for cell in row)

    def forget_mentions(self) -> None:
        if self.has_mentions:
            self._resolved = None
            self._layout = None
            self._highlights = None
            self.refresh(layout=True)

    def _texts(self) -> list[list[Content]]:
        if self._resolved is None:
            document = _document_of(self)
            self._resolved = [
                [
                    document.link_mentions(cell.text, cell.mentions)
                    if cell.mentions and document is not None
                    else cell.text
                    for cell in row
                ]
                for row in self.cells
            ]
        return self._resolved

    def _widths(self, width: int) -> list[int]:
        """Each column's width inside its borders (including a space either side)."""
        columns = self.columns
        if not columns:
            return []
        natural = [2] * columns
        for row in self.cells:
            for column, cell in enumerate(row):
                natural[column] = max(natural[column], cell_len(cell.plain) + 2)
        room = max(columns * 3, width - (columns + 1))
        total = sum(natural)
        if total <= room:
            # Spread the spare room over the columns, in proportion to their widths.
            spare = room - total
            widths = [size + spare * size // total for size in natural]
        else:
            # Narrow columns keep their width; the wide ones share what's left.
            widths = list(natural)
            wide = set(range(columns))
            left = room
            while wide:
                fair = max(3, left // len(wide))
                fits = {column for column in wide if natural[column] <= fair}
                if not fits:
                    for column in wide:
                        widths[column] = fair
                    break
                wide -= fits
                left -= sum(natural[column] for column in fits)
        widths[-1] += room - sum(widths)
        return widths

    def lay_out(self, width: int):
        if self._layout is not None and self._layout[0] == width:
            return self._layout[1], self._layout[2]
        widths = self._widths(width)
        texts = self._texts()
        line_style = ".keyline"

        def border(left: str, middle: str, right: str) -> Content:
            return Content.styled(left + middle.join("─" * size for size in widths) + right, line_style)

        lines: list[Content] = [border("┌", "┬", "┐")]
        places: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
        header = bool(self.table.header)
        for row_index, row in enumerate(texts):
            wrapped = []
            for column in range(self.columns):
                text = row[column] if column < len(row) else Content("")
                if header and row_index == 0:
                    text = text.stylize_before(".table-header")
                wrapped.append((text, wrap(text.plain, widths[column] - 2)))
            height = max(len(parts) for _, parts in wrapped)
            first = len(lines)
            for line_number in range(height):
                line = Content.styled("│", line_style)
                for column, (text, parts) in enumerate(wrapped):
                    # Headers sit at the bottom of their row, cells at the top.
                    offset = height - len(parts) if header and row_index == 0 else 0
                    part_index = line_number - offset
                    column_start = len(line.plain) + 1
                    if 0 <= part_index < len(parts):
                        start, end = parts[part_index]
                        piece = text[start:end]
                        places.setdefault((row_index, column), []).append(
                            (first + line_number, column_start, start, end)
                        )
                    else:
                        piece = Content("")
                    padding = widths[column] - 2 - piece.cell_length
                    line = line + " " + piece + " " * (padding + 1) + Content.styled("│", line_style)
                lines.append(line)
            if row_index < len(texts) - 1:
                lines.append(border("├", "┼", "┤"))
        lines.append(border("└", "┴", "┘"))
        self._layout = (width, lines, places)
        return lines, places

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.lay_out(width)[0]) if self.columns else 0

    def render(self) -> Content:
        if not self.columns:
            return Content("")
        return super().render()

    def highlight_rows(self, width: int) -> dict[int, list[tuple[str, int, int]]]:
        _, places = self.lay_out(width)
        bases = [base for _, _, base in self._order]
        found: dict[int, list[tuple[str, int, int]]] = {}
        for start, end, current in self._matches:
            style = CURRENT_MATCH_STYLE if current else MATCH_STYLE
            index = max(0, bisect_right(bases, start) - 1)
            while index < len(self._order) and bases[index] < end:
                row, column, base = self._order[index]
                for number, at, part_start, part_end in places.get((row, column), []):
                    low = max(start - base, part_start)
                    high = min(end - base, part_end)
                    if low < high:
                        found.setdefault(number, []).append(
                            (style, at + low - part_start, at + high - part_start)
                        )
                index += 1
        return found

    def search_text(self) -> str:
        return "\n".join(cell.plain for row in self.cells for cell in row)

    def match_row(self, index: int) -> int:
        _, places = self.lay_out(self.content_size.width)
        for row, column, base in reversed(self._order):
            if index >= base:
                for number, _, start, end in places.get((row, column), []):
                    if start <= index - base <= end:
                        return number
                return places.get((row, column), [(0,)])[0][0]
        return 0

    def source_span(self, widget: Widget, selection: Selection) -> tuple[int, int] | None:
        lines, _ = self.lay_out(self.content_size.width)
        whole = (selection.start is None or (selection.start.y, selection.start.x) <= (0, 0)) and (
            selection.end is None or selection.end.y >= len(lines) - 1
        )
        if whole:
            return 0, len(self.top.source.rstrip("\n"))
        return None

    async def action_link(self, href: str) -> None:
        self.post_message(Document.LinkClicked(href))


# --- front matter -----------------------------------------------------------------------------


class FrontMatterValue(Static):
    """One value in the front matter box; web addresses in it can be clicked."""

    async def action_link(self, href: str) -> None:
        self.post_message(Document.LinkClicked(href))


class FrontMatter(Widget, Sourced):
    """The YAML block at the top of a file ("---"), shown as a small table."""

    DEFAULT_CSS = """
    FrontMatter {
        /* Quiet, in faded rows without a box: it's about the document, not in it. */
        width: 1fr;
        height: auto;
        layout: vertical;
        margin: 1 0 0 0;
        & > Horizontal {
            height: auto;
        }
        & .front-key {
            text-style: dim;
            padding-right: 2;
        }
        & .front-value {
            width: 1fr;
            text-style: dim;
        }
        & .front-raw {
            text-style: dim;
        }
    }
    """

    def __init__(self, top: Block) -> None:
        super().__init__()
        self.top = top
        self.pairs = read_front_matter(top.yaml)
        self.border_title = "metadata"

    def compose(self) -> ComposeResult:
        if not self.pairs:
            yield Static(self.top.yaml, classes="front-raw", markup=False)
            return
        width = min(24, max(cell_len(key) for key, _ in self.pairs)) + 2
        for key, value in self.pairs:
            content = Content(value)
            if _URL.match(value):
                content = content.stylize(Style.from_meta({"@click": f"link({value!r})"}))
            with Horizontal():
                label = Label(key, classes="front-key", markup=False)
                label.styles.width = width
                yield label
                yield FrontMatterValue(content, classes="front-value")

    def source_span(self, widget: Widget, selection: Selection) -> tuple[int, int] | None:
        return None


# --- pictures -------------------------------------------------------------------------------


class ImageNote(Static):
    """Shown in place of a picture that can't be displayed."""

    DEFAULT_CSS = """
    ImageNote {
        text-style: dim italic;
        margin: 0 0 1 0;
    }
    """


def _picture(base_dir: Path, picture: Picture) -> Widget:
    """Make a widget for an image in the document."""
    source = picture.source
    alt = picture.alt
    label = f"🖼  {alt}" if alt else "🖼  picture"
    if re.match(r"^[a-z]+://", source, re.IGNORECASE) or source.startswith("data:"):
        return ImageNote(f"{label}  (web picture, not shown)", markup=False)

    path = Path(unquote(source.split("#")[0].split("?")[0]))
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        return ImageNote(f"{label}  (picture not found: {source})", markup=False)
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as image:
            image.verify()
        from textual_image.widget import Image
    except Exception:
        return ImageNote(f"{label}  (can't show this kind of picture)", markup=False)
    widget = Image(str(path), classes="picture")
    widget.tooltip = alt or path.name
    return widget


class PictureParagraph(Vertical, Sourced):
    """A paragraph with pictures in it: its text, with real images between."""

    DEFAULT_CSS = """
    PictureParagraph {
        width: 1fr;
        height: auto;
        & > TextBlock {
            margin: 0 0 1 0;
        }
        & Image {
            width: auto;
            height: auto;
            max-width: 100%;
            max-height: 20;
        }
        & ImageNote {
            margin: 0;
        }
    }
    """

    def __init__(self, top: Block, block: Block, base_dir: Path, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.top = top
        self.block = block
        self.base_dir = base_dir

    def compose(self) -> ComposeResult:
        for part in self.block.parts:
            if isinstance(part, Picture):
                yield _picture(self.base_dir, part)
            elif isinstance(part, Piece):
                yield TextBlock(self.top, [Run(part, Content(), Content(), part.offsets[0] if part.offsets else 0)])


# --- code blocks ------------------------------------------------------------------------------


class FenceButton(Static):
    """A small button in the top right of a code block's box: a shade lighter
    than the box, so it reads as one, but quiet beside the code."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    FenceButton {
        width: auto;
        padding: 0 1;
        margin-left: 1;
        /* Solid terminal colours, as the box's are: a see-through tint
           can't be mixed over those, and came out darker than the box. */
        &:dark {
            color: ansi_bright_white;
            background: ansi_bright_black;
        }
        &:light {
            color: ansi_black;
            background: ansi_bright_white;
        }
        &:hover {
            color: $accent;
        }
        &.-copied {
            color: $text-success;
        }
    }
    """


class CopyButton(FenceButton):
    """The "copy" button: copies everything in the code block."""

    def __init__(self, label: str = "copy") -> None:
        super().__init__(label, markup=False)
        self.label = label

    def on_click(self, event: events.Click) -> None:
        event.stop()
        fence = self.query_ancestor(CodeFence)
        self.post_message(CodeFence.CopyRequested(fence.code))
        self.update("✓ copied")
        self.add_class("-copied")
        self.set_timer(1.5, self._reset)

    def _reset(self) -> None:
        self.update(self.label)
        self.remove_class("-copied")


class SourceButton(FenceButton):
    """Switches a diagram between the drawing and the mermaid it's drawn from."""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        fence = self.query_ancestor(CodeFence)
        fence.show_source(not fence.showing_source)
        self.update("show diagram" if fence.showing_source else "show source")


def _is_mermaid(language: str | None) -> bool:
    return bool(language) and language.split()[0].lower() == "mermaid"


DIAGRAM_WIDTH = 86
"""Diagrams wider than this are drawn again, more tightly, to fit the reading column."""


@lru_cache(maxsize=64)
def draw_mermaid(source: str) -> str | None:
    """A mermaid diagram drawn with box-drawing characters, or None if it can't be."""
    try:
        from termaid import render

        drawing = render(source, padding_x=2, padding_y=0)
        if max((cell_len(line) for line in drawing.splitlines()), default=0) > DIAGRAM_WIDTH:
            drawing = render(source, padding_x=1, padding_y=0, gap=2)
    except Exception:
        return None
    drawing = drawing.rstrip()
    return drawing if drawing.strip() else None


class CodeScroll(ScrollableContainer):
    """The box around the code, which scrolls sideways for long lines."""

    DEFAULT_CSS = """
    CodeScroll {
        height: auto;
        overflow: scroll hidden;
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 0;
        background: transparent;
    }
    """

    @property
    def allow_horizontal_scroll(self) -> bool:
        return True


def wrap_rows(text: str, width: int) -> list[tuple[int, int]]:
    """Where each shown row of `text` starts and ends in it: one per line, or
    (given a width) wrapped at spaces to fit, the space at a break left out."""
    rows = []
    start = 0
    for line in text.split("\n"):
        end = start + len(line)
        at = start
        while width > 0 and cell_len(text[at:end]) > width:
            cut, cells = at, 0
            while cells + cell_len(text[cut]) <= width:
                cells += cell_len(text[cut])
                cut += 1
            cut = max(cut, at + 1)
            space = text.rfind(" ", at + 1, cut + 1)
            if space != -1:
                rows.append((at, space))
                at = space + 1
            else:
                rows.append((at, cut))
                at = cut
        rows.append((at, end))
        start = end + 1
    return rows


class CodeBody(Static, Searchable):
    """The code itself (or the diagram drawn from it), wrapped to fit if it's
    plain text (a fence with no language: a prompt, say) rather than code."""

    DEFAULT_CSS = """
    CodeBody {
        /* A blank line below (the button row and its air are above). */
        width: auto;
        padding: 0 0 1 0;
        &.-wrap {
            width: 1fr;
        }
    }
    """

    def __init__(self, content: Content, wrap: bool = False) -> None:
        super().__init__(content, id="code-content", markup=False, classes="-wrap" if wrap else "")
        self.original = content
        """The code as coloured, without search highlights or wrapping: `update`
        replaces Static's own `content` with what's on screen."""
        self.wrap = wrap
        self.rows = wrap_rows(content.plain, 0)
        """Where each row on screen starts and ends in the code's text."""
        self._matches: list[tuple[int, int, bool]] = []

    def set_code(self, content: Content) -> None:
        self.original = content
        self._matches = []
        self.rows = wrap_rows(content.plain, self.content_size.width if self.wrap else 0)
        self._show()

    def on_resize(self) -> None:
        if self.wrap:
            rows = wrap_rows(self.original.plain, self.content_size.width)
            if rows != self.rows:
                self.rows = rows
                self._show()

    def _show(self) -> None:
        content = self.original
        for start, end, current in self._matches:
            content = content.stylize(CURRENT_MATCH_STYLE if current else MATCH_STYLE, start, end)
        if self.wrap:
            content = Content("\n").join(content[start:end] for start, end in self.rows)
        self.update(content)

    def index_at(self, x: int, y: int) -> int:
        """Where in the code's text the cell at (x, y) on screen is."""
        start, end = self.rows[max(0, min(y, len(self.rows) - 1))]
        text = self.original.plain
        at, cells = start, 0
        while at < end and cells + cell_len(text[at]) <= x:
            cells += cell_len(text[at])
            at += 1
        return at

    def search_text(self) -> str:
        return self.original.plain

    def show_matches(self, matches: list[tuple[int, int, bool]]) -> None:
        self._matches = matches
        self._show()

    def match_row(self, index: int) -> int:
        return max(0, bisect_right([start for start, _ in self.rows], index) - 1)


class CodeFence(Widget, Sourced):
    """A code block with a button that copies everything inside it (a prompt, say).

    Mermaid blocks are drawn as diagrams instead, with the source a click away.
    """

    DEFAULT_CSS = """
    CodeFence {
        /* The code is on a patch of its own, set apart from quotes (which have
           a bar down their side), with two columns of air either side: on the
           right, one of them is where a cut-off line's › goes. Its buttons sit
           in the patch's top right corner, so they plainly belong to it, with
           a line of air above and below them (about as much as the two
           columns at the side: a cell is twice as tall as it's wide). */
        width: 1fr;
        height: auto;
        layout: vertical;
        overflow: hidden hidden;
        color: $foreground;
        margin: 1 0;
        & #code-bar {
            /* Its right edge in as far as the code's left one is. */
            height: 1;
            margin-bottom: 1;
            padding-right: 1;
            align-horizontal: right;
        }
        & > #code-box {
            height: auto;
            padding: 1 1 0 2;
        }
        &:dark > #code-box {
            background: ansi_black;
        }
        &:light > #code-box {
            background: ansi_white;
        }
        & #code-label {
            width: 1fr;
            text-style: dim italic;
        }
        & #code-row {
            height: auto;
        }
        & CodeScroll {
            width: 1fr;
        }
        & #code-more {
            width: 1;
            padding: 0 0 1 0;
            text-style: dim;
        }
    }
    """

    class CopyRequested(Message):
        def __init__(self, code: str) -> None:
            super().__init__()
            self.code = code

    def __init__(self, top: Block, fence: Block, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.top = top
        self.fence = fence
        self.code = fence.code
        self.lexer = fence.language
        self.diagram = _is_mermaid(fence.language) and draw_mermaid(fence.code) is not None
        """Whether this block is drawn as a diagram."""
        self.showing_source = False

    @classmethod
    def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
        if _is_mermaid(language):
            drawing = draw_mermaid(code)
            if drawing is not None:
                return Content(drawing)
        return cls._colour(code, language or "text", ansi, dark)

    @staticmethod
    def _colour(code: str, language: str, ansi: bool, dark: bool) -> Content:
        # No language given: show it plain, as GitHub does. Guessing means
        # Pygments loading every lexer it has, a noticeable pause the first time.
        if ansi:
            if dark:
                from textual.highlight import ANSIDarkHighlightTheme as theme
            else:
                from textual.highlight import ANSILightHighlightTheme as theme
        else:
            from textual.highlight import HighlightTheme as theme
        return highlight_code(code, language=language or None, theme=theme)

    def compose(self) -> ComposeResult:
        # The buttons sit in the box's top row, at the right.
        with Vertical(id="code-box"):
            with Horizontal(id="code-bar"):
                if self.diagram:
                    yield Label("mermaid diagram", id="code-label")
                    yield SourceButton("show source")
                    yield CopyButton("copy source")
                else:
                    yield CopyButton()
            with Horizontal(id="code-row"):
                with CodeScroll():
                    yield CodeBody(self._content(), wrap=not self.lexer)
                yield Static("", id="code-more")

    def on_mount(self) -> None:
        self.watch(self.query_one(CodeScroll), "scroll_x", self._mark_cut_lines, init=False)

    def on_resize(self) -> None:
        self.call_after_refresh(self._mark_cut_lines)

    def _mark_cut_lines(self) -> None:
        """A › beside each line that runs on past the right edge (scroll to see it)."""
        body, scroll = self.query_one(CodeBody), self.query_one(CodeScroll)
        text = body.original.plain
        edge = scroll.scroll_x + scroll.size.width
        marks = ["›" if cell_len(text[start:end]) > edge else "" for start, end in body.rows]
        self.query_one("#code-more", Static).update("\n".join(marks))

    def _content(self) -> Content:
        ansi, dark = self.app.native_ansi_color, self.app.current_theme.dark
        if self.showing_source:
            return self._colour(self.code, "text", ansi, dark)
        return self.highlight(self.code, self.lexer, ansi, dark)

    def show_source(self, source: bool) -> None:
        """Show the mermaid as written (True) or as a drawing (False)."""
        if not self.diagram:
            return
        self.showing_source = source
        self.query_one(CodeBody).set_code(self._content())
        self.call_after_refresh(self._mark_cut_lines)

    @property
    def shows_source_text(self) -> bool:
        """Is what's on screen the code as written (not a drawing)?"""
        return not self.diagram or self.showing_source

    def source_span(self, widget: Widget, selection: Selection) -> tuple[int, int] | None:
        body = self.query_one(CodeBody)
        text = body.original.plain
        if widget is not body:
            return None

        def index(offset, default: int) -> int:
            return default if offset is None else body.index_at(offset.x, offset.y)

        begin = index(selection.start, 0)
        finish = index(selection.end, len(text))
        whole = self.top.source.rstrip("\n")
        if (not text[:begin].strip() and not text[finish:].strip()) or not self.shows_source_text:
            # All of it (or a drawing, which can't be matched up bit by bit):
            # the whole block, ``` lines and all.
            if self.fence is self.top:
                return 0, len(whole)
            return self._own_span()
        if finish <= begin or not self.fence.code_offsets:
            return None
        offsets = self.fence.code_offsets
        low = offsets[min(begin, len(offsets) - 1)]
        high = offsets[min(finish - 1, len(offsets) - 1)] + 1
        return low, high

    def _own_span(self) -> tuple[int, int]:
        """A code block inside a list: from its first line to its last."""
        offsets = self.fence.code_offsets
        if not offsets:
            return 0, 0
        source = self.top.source
        start = source.rfind("\n", 0, offsets[0])
        start = source.rfind("\n", 0, max(0, start)) + 1
        end = source.find("\n", offsets[-1])
        end = source.find("\n", end + 1) if end != -1 else -1
        return start, len(source.rstrip("\n")) if end == -1 else end


# --- lists and quotes with code in them ------------------------------------------------------


class BlockGroup(Vertical, Sourced):
    """A list or quote with a code block or table inside: its text and those, one after another."""

    DEFAULT_CSS = """
    BlockGroup {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, top: Block, base_dir: Path, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.top = top
        self.base_dir = base_dir

    def compose(self) -> ComposeResult:
        for part in self.top.parts:
            if not isinstance(part, Segment):
                continue
            widget = _segment_widget(self.top, part.block, self.base_dir)
            if widget is None:
                continue
            if part.indent:
                widget.styles.margin = (0, 0, 0, part.indent)
            yield widget


def _segment_widget(top: Block, block: Block, base_dir: Path) -> Widget | None:
    if block.kind == "text":
        return TextBlock(top, block.runs, classes="-segment")
    if block.kind == "fence":
        return CodeFence(top, block)
    if block.kind == "table":
        return TableBlock(top, block)
    if block.kind == "rule":
        return Rule(top)
    if block.kind == "picture":
        return PictureParagraph(top, block, base_dir)
    return None


_heading_ids = count(1)


def block_widget(block: Block, base_dir: Path) -> Widget | None:
    """The widget that shows a top-level block."""
    kind = block.kind
    if kind == "heading":
        widget: Widget = TextBlock(block, block.runs, classes=block.classes, id=f"heading-{next(_heading_ids)}")
    elif kind in ("paragraph", "text"):
        widget = TextBlock(block, block.runs, classes=block.classes)
    elif kind == "picture":
        widget = PictureParagraph(block, block, base_dir, classes=block.classes)
    elif kind == "group":
        widget = BlockGroup(block, base_dir, classes=block.classes)
    elif kind == "rule":
        widget = Rule(block)
    elif kind == "fence":
        widget = CodeFence(block, block)
    elif kind == "table":
        widget = TableBlock(block, block)
    elif kind == "front_matter":
        widget = FrontMatter(block)
    else:
        return None
    widget.top_block = block  # type: ignore[attr-defined]
    return widget


# --- the document -------------------------------------------------------------------------------


class Document(Widget):
    """Markdown with clickable checkboxes, real pictures, diagrams, and copyable code blocks."""

    DEFAULT_CSS = """
    /* A reading column: about 100 characters a line (plus the padding),
       centred when the pane is wider, like a page on a desk. */
    Document {
        height: auto;
        layout: vertical;
        max-width: 100;
        padding: 0 4 2 4;
        background: transparent;
        color: $foreground;
        overflow-y: hidden;

        /* Headings in the text colour, told apart by their weight and rules
           (the accent colour is for links, and where you are). */
        & > .-h1 {
            color: $foreground;
            text-style: bold;
            border-bottom: double ansi_bright_black;
            margin: 1 0 1 0;
        }
        & > .-h2 {
            color: $foreground;
            text-style: bold;
            border-bottom: solid ansi_bright_black;
            margin: 2 0 1 0;
        }
        & > .-h3 {
            color: $foreground;
            text-style: bold;
            margin: 1 0 0 0;
        }
        & > .-h4 {
            color: $foreground;
            text-style: bold italic;
            margin: 1 0;
        }
        & > .-h5, & > .-h6 {
            text-style: dim bold;
            margin: 1 0;
        }
        & > .-paragraph {
            margin: 0 0 1 0;
        }
        & > .-list {
            margin: 0 0 1 0;
        }
        & > .-footnotes {
            margin: 1 0;
        }

        & > .-quote {
            border-left: outer ansi_bright_black;
            margin: 1 0;
            padding: 0 1;
        }
        /* A heading straight after a rule: the rule already makes the break,
           so the heading doesn't add its own space above as well. Nor at the
           very top: the first line is level with the file list's. Whole margins,
           since a lone margin-top here would zero the other sides too. */
        & > .-h1.-after-rule, & > .-h1:first-child,
        & > .-h2.-after-rule, & > .-h2:first-child,
        & > .-h4.-after-rule, & > .-h4:first-child,
        & > .-h5.-after-rule, & > .-h5:first-child,
        & > .-h6.-after-rule, & > .-h6:first-child,
        & > .-quote:first-child {
            margin: 0 0 1 0;
        }
        & > .-h3.-after-rule, & > .-h3:first-child {
            margin: 0;
        }
    }
    """

    class LinkClicked(Message):
        """A link in the document was clicked."""

        def __init__(self, href: str) -> None:
            super().__init__()
            self.href = unquote(href)

    class TaskToggled(Message):
        """A checkbox was clicked: tick or untick the task on this line of the file."""

        def __init__(self, line: int) -> None:
            super().__init__()
            self.line = line

    def __init__(self, base_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_dir = base_dir
        self.find_file: Callable[[str, Path], Path | None] | None = None
        """Where a markdown file named in this document is (given the name and
        this document's folder), for turning the name into a link."""
        self.original = ""
        """The markdown as written."""
        self.blocks: list[Block] = []
        self._widgets: list[Widget | None] = []
        """The widget showing each block (None for blocks that aren't shown)."""
        self._found: dict[str, str | None] = {}
        self._lock = asyncio.Lock()

    @property
    def source(self) -> str:
        return self.original

    # -- showing a document ------------------------------------------------------------

    async def show(self, source: str, base_dir: Path) -> None:
        """Show this markdown. Blocks that haven't changed since the last time keep
        their widgets; only the ones that did are drawn again."""
        async with self._lock:
            blocks = await asyncio.to_thread(build, source) if len(source) > 20_000 else build(source)
            same_place = base_dir == self.base_dir
            self.base_dir = base_dir
            self.original = source
            if not same_place:
                self._found = {}
            old_blocks, old_widgets = self.blocks, self._widgets
            if not same_place or not old_blocks:
                await self._replace_all(blocks)
                return
            matcher = SequenceMatcher(None, [b.key for b in old_blocks], [b.key for b in blocks], autojunk=False)
            widgets: list[Widget | None] = []
            final: list[Block] = []
            removing: list[Widget] = []
            mounting: list[tuple[list[Widget], Widget | None]] = []
            anchor: Widget | None = None
            with self.app.batch_update():
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag == "equal":
                        for old, new, widget in zip(old_blocks[i1:i2], blocks[j1:j2], old_widgets[i1:i2]):
                            # The same block, perhaps moved: keep its widget, update where it is.
                            old.line, old.start = new.line, new.start
                            final.append(old)
                            widgets.append(widget)
                            if widget is not None:
                                anchor = widget
                        continue
                    removing.extend(widget for widget in old_widgets[i1:i2] if widget is not None)
                    fresh = []
                    for block in blocks[j1:j2]:
                        widget = block_widget(block, base_dir)
                        final.append(block)
                        widgets.append(widget)
                        if widget is not None:
                            fresh.append(widget)
                    if fresh:
                        mounting.append((fresh, anchor))
                        anchor = fresh[-1]
                if removing:
                    await self.remove_children(removing)
                for fresh, after in mounting:
                    if after is None:
                        if self.children:
                            await self.mount_all(fresh, before=0)
                        else:
                            await self.mount_all(fresh)
                    else:
                        await self.mount_all(fresh, after=after)
            self.blocks = final
            self._widgets = widgets
            self._mark_headings_after_rules()

    async def _replace_all(self, blocks: list[Block]) -> None:
        widgets = [block_widget(block, self.base_dir) for block in blocks]
        with self.app.batch_update():
            await self.remove_children()
            shown = [widget for widget in widgets if widget is not None]
            if shown:
                await self.mount_all(shown)
        self.blocks = blocks
        self._widgets = widgets
        self._mark_headings_after_rules()

    def _mark_headings_after_rules(self) -> None:
        previous: Block | None = None
        for block, widget in self.shown_blocks():
            if block.kind == "heading":
                widget.set_class(previous is not None and previous.kind == "rule", "-after-rule")
            previous = block

    def shown_blocks(self) -> list[tuple[Block, Widget]]:
        """Each top-level block on show, with its widget, top to bottom."""
        return [(block, widget) for block, widget in zip(self.blocks, self._widgets) if widget is not None]

    # -- file names as links ----------------------------------------------------------------

    def link_mentions(self, text: Content, mentions: list[Mention]) -> Content:
        """Make the file names in this text links, where the files can be found."""
        for mention in mentions:
            target = self._find(mention.name)
            if target is not None:
                text = text.stylize(Style.from_meta({"@click": f"link({target!r})"}), mention.start, mention.end)
        return text

    def _find(self, name: str) -> str | None:
        if name not in self._found:
            found = self.find_file(name, self.base_dir) if self.find_file else None
            self._found[name] = quote(str(found)) if found is not None else None
        return self._found[name]

    def refresh_mentions(self) -> None:
        """The file list has changed: file names may now be found (or not)."""
        self._found = {}
        for widget in self.query("TextBlock, TableBlock"):
            if isinstance(widget, (TextBlock, TableBlock)):
                widget.forget_mentions()

    # -- headings ------------------------------------------------------------------------

    @property
    def table_of_contents(self) -> list[tuple[int, str, str]]:
        """(level, text, widget id) for each heading, top to bottom."""
        headings = []
        for block, widget in self.shown_blocks():
            if block.kind == "heading" and widget.id is not None:
                headings.append((block.level, block.heading_text, widget.id))
        return headings

    def goto_anchor(self, anchor: str) -> bool:
        """Scroll to the heading this #anchor names. Returns whether there is one."""
        headings = self.table_of_contents
        for (_, _, widget_id), name in zip(headings, slugs([title for _, title, _ in headings])):
            if name == anchor:
                self.query_one(f"#{widget_id}").scroll_visible(top=True, animate=False)
                return True
        return False

    # -- copying what's highlighted -------------------------------------------------------------

    def selected_source(self, selections: dict[Widget, Selection]) -> str | None:
        """The markdown as written behind the highlighted text, symbols and all.

        Returns None when the highlight isn't (only) in this document, so the
        caller can fall back to the text as shown.
        """
        start: int | None = None
        end: int | None = None
        for widget, selection in selections.items():
            if not widget.is_attached:
                continue
            if self not in widget.ancestors:
                if widget.get_selection(selection):
                    return None
                continue
            owner = next((node for node in (widget, *widget.ancestors) if isinstance(node, Sourced)), None)
            if owner is None:
                continue
            top = owner.top
            span = owner.source_span(widget, selection)
            if span is None:
                if len(selections) == 1:
                    return None
                span = 0, len(top.source.rstrip("\n"))
            low, high = top.start + span[0], top.start + span[1]
            start = low if start is None else min(start, low)
            end = high if end is None else max(end, high)
        if start is None or end is None:
            return None
        return self.original[start:end].rstrip()
