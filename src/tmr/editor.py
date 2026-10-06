"""Editing a file in place: the text box, its colours, and merging with changes made on disk."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from rich.style import Style
from textual import on
from textual._text_area_theme import TextAreaTheme
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Offset
from textual.widgets import Button, Label, TextArea

from tmr.files import is_markdown
from tmr.keys import editor_bindings
from tmr.popup import Popup

MINE_MARKER = "<<<<<<< your edits"
SPLIT_MARKER = "======="
THEIRS_MARKER = ">>>>>>> changes on disk"

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".json": "json",
    ".css": "css",
    ".tcss": "css",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
    ".svg": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".sql": "sql",
}


def language_for(path: Path) -> str | None:
    if is_markdown(path):
        return "markdown"
    return LANGUAGES.get(path.suffix.lower())


# --- merging ----------------------------------------------------------------

Hunk = tuple[int, int, list[str]]
"""Lines [start, end) of the original replaced by these lines."""


def _hunks(base: list[str], other: list[str]) -> list[Hunk]:
    matcher = SequenceMatcher(None, base, other, autojunk=False)
    return [
        (i1, i2, other[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _apply(base: list[str], start: int, end: int, hunks: list[Hunk]) -> list[str]:
    """Lines [start, end) of the original, with these changes made to them."""
    result: list[str] = []
    position = start
    for hunk_start, hunk_end, lines in hunks:
        result += base[position:hunk_start] + lines
        position = hunk_end
    return result + base[position:end]


def _ending_in_newline(lines: list[str]) -> list[str]:
    if lines and not lines[-1].endswith(("\n", "\r")):
        return lines[:-1] + [lines[-1] + "\n"]
    return lines


def merge(base: str, mine: str, theirs: str) -> tuple[str, bool]:
    """Combine two sets of changes to the same original, line by line.

    Returns the combined text, and whether it went cleanly. Where both
    changed the same lines differently, both versions are kept between
    conflict markers (and it didn't go cleanly).
    """
    original = base.splitlines(keepends=True)
    changes = [(*hunk, 0) for hunk in _hunks(original, mine.splitlines(keepends=True))]
    changes += [(*hunk, 1) for hunk in _hunks(original, theirs.splitlines(keepends=True))]
    changes.sort(key=lambda change: (change[0], change[1], change[3]))

    merged: list[str] = []
    clean = True
    position = 0
    index = 0
    while index < len(changes):
        start, end = changes[index][0], changes[index][1]
        group = [changes[index]]
        index += 1
        # Changes that overlap, or that both add lines at the same spot, have to be settled together.
        while index < len(changes):
            next_start, next_end = changes[index][0], changes[index][1]
            inserting = next_start == next_end or start == end
            if next_start < end or (next_start == end and inserting):
                group.append(changes[index])
                end = max(end, next_end)
                index += 1
            else:
                break
        merged += original[position:start]
        position = end
        ours = [change[:3] for change in group if change[3] == 0]
        others = [change[:3] for change in group if change[3] == 1]
        if not others:
            merged += _apply(original, start, end, ours)
            continue
        if not ours:
            merged += _apply(original, start, end, others)
            continue
        mine_lines = _apply(original, start, end, ours)
        their_lines = _apply(original, start, end, others)
        if mine_lines == their_lines:
            merged += mine_lines
            continue
        clean = False
        merged = _ending_in_newline(merged)
        merged += [MINE_MARKER + "\n", *_ending_in_newline(mine_lines)]
        merged += [SPLIT_MARKER + "\n", *_ending_in_newline(their_lines)]
        merged += [THEIRS_MARKER + "\n"]
    merged += original[position:]
    return "".join(merged), clean


def has_conflict_markers(text: str) -> bool:
    lines = set(text.splitlines())
    return MINE_MARKER in lines and THEIRS_MARKER in lines


# --- the text box -----------------------------------------------------------

EMPHASIS = [
    # (pattern, style for the words, style for the symbols around them)
    (re.compile(r"(`+)(.+?)(\1)"), "inline_code", "markup.symbol"),
    (re.compile(r"(!?\[)([^\]]*)(\]\()([^)\s]*)(?:\s+\"[^\"]*\")?(\))"), "link", "markup.symbol"),
    (re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)(\1)"), "bold", "markup.symbol"),
    (re.compile(r"(~~)(?=\S)(.+?)(?<=\S)(\1)"), "strikethrough", "markup.symbol"),
    (re.compile(r"(?<![*\w])(\*|_)(?=[^\s*_])(.+?)(?<=[^\s*_])(\1)(?![*\w])"), "italic", "markup.symbol"),
]

TASK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(\[[ xX~]\])")


def _byte_offset(line: str, index: int) -> int:
    return len(line[:index].encode("utf-8"))


def inline_highlights(line: str) -> list[tuple[int, int, str]]:
    """Bold, italic, code, links and task boxes on one line of markdown (as byte ranges)."""
    found: list[tuple[int, int, str]] = []
    taken: list[tuple[int, int]] = []

    def add(start: int, end: int, name: str) -> None:
        if start < end:
            found.append((_byte_offset(line, start), _byte_offset(line, end), name))

    task = TASK.match(line)
    if task:
        add(task.start(1), task.end(1), "list.marker")
    for pattern, words, symbols in EMPHASIS:
        for match in pattern.finditer(line):
            # Nothing inside `code` is emphasis, and a link's parts aren't either.
            if any(start < match.end() and match.start() < end for start, end in taken):
                continue
            if words == "link":
                add(match.start(1), match.end(1), symbols)
                add(match.start(2), match.end(2), "link.label")
                add(match.start(3), match.end(3), symbols)
                add(match.start(4), match.end(4), "link.uri")
                add(match.end(4), match.end(5), symbols)
            else:
                add(match.start(1), match.end(1), symbols)
                add(match.start(2), match.end(2), words)
                add(match.start(3), match.end(3), symbols)
            if words in ("inline_code", "link"):
                taken.append((match.start(), match.end()))
    return found


def editor_theme() -> TextAreaTheme:
    """Colours from the terminal's own palette, like the rest of tmr."""
    symbol = Style(color="bright_black")
    return TextAreaTheme(
        name="tmr",
        syntax_styles={
            # Markdown
            "heading": Style(color="blue", bold=True),
            "heading.marker": symbol,
            "list.marker": Style(color="cyan", bold=True),
            "text.literal": Style(color="green"),
            "punctuation.delimiter": symbol,
            "punctuation.special": symbol,
            "link.uri": Style(color="blue", underline=True),
            "link.label": Style(color="magenta"),
            "bold": Style(bold=True),
            "italic": Style(italic=True),
            "strikethrough": Style(strike=True),
            "inline_code": Style(color="green"),
            "markup.symbol": symbol,
            "string.escape": symbol,
            # Code
            "comment": Style(color="bright_black", italic=True),
            "string": Style(color="green"),
            "string.documentation": Style(color="green"),
            "keyword": Style(color="magenta"),
            "keyword.function": Style(color="magenta"),
            "keyword.return": Style(color="magenta"),
            "keyword.operator": Style(color="magenta"),
            "conditional": Style(color="magenta"),
            "repeat": Style(color="magenta"),
            "exception": Style(color="magenta"),
            "include": Style(color="magenta"),
            "number": Style(color="yellow"),
            "float": Style(color="yellow"),
            "boolean": Style(color="yellow"),
            "constant.builtin": Style(color="yellow"),
            "json.null": Style(color="yellow"),
            "function": Style(color="blue"),
            "function.call": Style(color="blue"),
            "method": Style(color="blue"),
            "method.call": Style(color="blue"),
            "class": Style(color="cyan"),
            "type": Style(color="cyan"),
            "type.class": Style(color="cyan"),
            "type.builtin": Style(color="cyan"),
            "constructor": Style(color="cyan"),
            "tag": Style(color="blue"),
            "yaml.field": Style(color="blue", bold=True),
            "json.label": Style(color="blue", bold=True),
            "toml.type": Style(color="blue"),
            "css.property": Style(color="blue"),
        },
    )


class Editor(TextArea):
    """The file as written, to change it. Ctrl+S saves; Esc goes back to reading."""

    DEFAULT_CSS = """
    Editor {
        width: 1fr;
        /* The document's column (100) less this margin, so lines wrap as wide as when reading. */
        max-width: 96;
        height: 1fr;
        /* Text lines up with the title above, like the formatted view's. */
        margin: 0 2;
        border: none;
        padding: 1 2 0 2;
        background: transparent;
        scrollbar-size-vertical: 1;
    }
    Editor:focus {
        border: none;
        background-tint: $foreground 0%;
    }
    Editor .text-area--cursor-line {
        background: $foreground 5%;
    }
    """

    BINDINGS = editor_bindings()
    """Ctrl+S, Ctrl+G and Esc (see `tmr.keys`)."""

    def __init__(self, text: str, path: Path, **kwargs) -> None:
        language = language_for(path)
        super().__init__(
            text,
            language=language,
            soft_wrap=True,
            tab_behavior="indent",
            show_line_numbers=language not in (None, "markdown"),
            **kwargs,
        )
        self.register_theme(editor_theme())
        self.theme = "tmr"

    def action_paste(self) -> None:
        # Ctrl+V pastes what's on the system clipboard, not only what was copied in here.
        if self.read_only:
            return
        clipboard_text = getattr(self.app, "clipboard_text", None)
        text = clipboard_text() if clipboard_text else self.app.clipboard
        if text and (result := self._replace_via_keyboard(text, *self.selection)):
            self.move_cursor(result.end_location)

    def _build_highlight_map(self) -> None:
        # tree-sitter only colours markdown's blocks (headings, lists, code);
        # add bold, italic, `code` and links within lines, outside code blocks.
        super()._build_highlight_map()
        if self.language != "markdown":
            return
        # Every keystroke colours the whole file again, so remember each line's
        # colours: only lines that changed are worked out afresh.
        highlights = self._highlights
        known: dict[str, list[tuple[int, int, str]]] = getattr(self, "_inline_known", {})
        kept: dict[str, list[tuple[int, int, str]]] = {}
        for row in range(self.document.line_count):
            if any(name == "text.literal" for _, _, name in highlights.get(row, ())):
                continue
            line = self.document[row]
            if any(symbol in line for symbol in "*_`[~"):
                extra = known.get(line)
                if extra is None:
                    extra = inline_highlights(line)
                kept[line] = extra
                if extra:
                    highlights[row].extend(extra)
        self._inline_known = kept

    def top_line(self) -> int:
        """The line of the file at the top of the box."""
        row, _ = self.wrapped_document.offset_to_location(Offset(0, int(self.scroll_y)))
        return row

    def show_line(self, line: int) -> None:
        """Put the cursor at the start of this line, with the line at the top."""
        line = max(0, min(line, self.document.line_count - 1))
        self.move_cursor((line, 0))
        y = self.wrapped_document.location_to_offset((line, 0)).y
        self.scroll_to(y=y, animate=False)


# --- asking about unsaved changes ---------------------------------------------


class UnsavedChanges(Popup[str]):
    """Save, throw away, or keep editing? Returns "save", "discard" or "keep"."""

    DEFAULT_CSS = """
    UnsavedChanges {
        align: center middle;
    }
    UnsavedChanges > Vertical {
        width: 60;
        max-width: 90%;
        margin-top: 0;
        border: round $warning;
        padding: 1 2;
    }
    UnsavedChanges #unsaved-question {
        width: 1fr;
        margin-bottom: 1;
    }
    UnsavedChanges Horizontal {
        height: auto;
        align-horizontal: right;
    }
    UnsavedChanges Button {
        margin-left: 1;
        min-width: 10;
    }
    """

    BINDINGS = [
        Binding("s", "choose('save')", "Save", show=False),
        Binding("d", "choose('discard')", "Discard", show=False),
        Binding("escape", "choose('keep')", "Keep editing", show=False),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.file_name = name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"You have changes to {self.file_name} that aren't saved.",
                id="unsaved-question",
                markup=False,
            )
            with Horizontal():
                yield Button("Save (s)", id="save", variant="primary", compact=True)
                yield Button("Discard (d)", id="discard", variant="error", compact=True)
                yield Button("Keep editing (esc)", id="keep", compact=True)

    def on_mount(self) -> None:
        self.query_one("#save", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "keep")

    def action_choose(self, answer: str) -> None:
        self.dismiss(answer)
