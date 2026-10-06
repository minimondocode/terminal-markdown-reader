"""Markdown turned into blocks of text that remember where every character came from.

The formatted view is built from this model rather than from Textual's own
markdown widget. Each character on screen carries its offset in the file, so
copying what's highlighted as markdown, ticking a checkbox, searching, and
keeping your place when the file changes are all lookups, not guesses.

Nothing here draws anything: it only turns markdown into `Block`s, which
`tmr.markdown` shows.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from string import punctuation
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from rich.cells import cell_len
from textual.content import Content, Span
from textual.style import Style

TASK = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[([ xX~])\](?=\s|$)")
"""A task line in a list: "- [ ] Write the plan" (or "[x]" done, "[~]" partly done)."""

_TASK_START = re.compile(r"^\[([ xX~])\](?=\s|$)")

TASK_BOX = {" ": ("[ ]", ".task"), "x": ("[✓]", ".task-done"), "X": ("[✓]", ".task-done"), "~": ("[~]", ".task-partial")}
"""How each task's box is shown, and the class that colours it."""

_MENTION = r"(?:\.{1,2}/)?[\w@.-]*\w(?:/[\w@.-]+)*\.(?:md|markdown)"
MENTION_WHOLE = re.compile(_MENTION, re.IGNORECASE)
MENTION_IN_TEXT = re.compile(rf"(?<![\w/.@:~-]){_MENTION}(?!\.?[\w/-])", re.IGNORECASE)
"""A markdown file named in the text, like "plan.md" or "docs/agent/lanes.md"."""

WIKI_LINK = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]*?))?\]\]")
"""A wiki link, as note apps write them: "[[plan]]" or "[[docs/plan|the plan]]",
naming plan.md. Shown as written, and a link when the file can be found."""

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]*>")

_HTML_TAG = re.compile(
    r"<!|<\?|</?(?:a|abbr|b|bdi|bdo|big|br|center|cite|code|data|del|details|dfn|div|em|font|hr|i|img"
    r"|ins|kbd|mark|p|picture|q|s|samp|small|source|span|strike|strong|sub|summary|sup|table|tbody"
    r"|td|th|thead|time|tr|tt|u|var|video|wbr)(?=[\s/>])",
    re.IGNORECASE,
)
"""HTML that's really HTML (a tag, or a comment), which isn't shown; anything
else in angle brackets, like "<slug>", is a placeholder and is shown as text."""

BULLETS = ["• ", "▪ ", "‣ ", "⭑ ", "◦ "]
"""Bullets for unordered lists, one for each level of nesting."""

_REACH = 400
"""How far ahead in the source a character of text is looked for."""


def mentioned_names(source: str) -> set[str]:
    """The markdown file names a text might mention: all those the view could
    link, and perhaps a few more (inside code blocks, say)."""
    return {match.group() for match in MENTION_IN_TEXT.finditer(source)}


def wiki_link_name(inner: str) -> str:
    """The file a wiki link names: "plan" is plan.md; "plan.md" is itself."""
    name = inner.strip()
    return name if name.lower().endswith((".md", ".markdown")) else name + ".md"


def html_text(html: str) -> str:
    """What an HTML block says once its tags and comments are taken out."""
    text = _ANY_TAG.sub(" ", _HTML_COMMENT.sub("", html))
    return re.sub(r"\s+", " ", text).strip()


def parser() -> MarkdownIt:
    md = MarkdownIt("gfm-like").use(front_matter_plugin).use(footnote_plugin)
    md.inline.ruler.before("backticks", "escaped_backticks", _escaped_backticks)
    return md


def _escaped_backticks(state, silent: bool) -> bool:
    """Code with backticks in it written as \\` (`like \\`this\\` one`): one piece of code.

    Markdown has no escapes inside code, so strictly that's three pieces with
    plain text between, but it's what's meant, and agents write it often. Taken
    this way only when the \\` come in pairs, so a lone backslash at the end of
    some code (`C:\\`) still closes it.
    """
    src, start = state.src, state.pos
    if src[start] != "`" or src[start + 1 : start + 2] == "`":
        return False
    escaped = 0
    at = start + 1
    while (at := src.find("`", at, state.posMax)) != -1:
        if src[at - 1] == "\\" and src[at + 1 : at + 2] != "`":
            escaped += 1
            at += 1
            continue
        if src[at + 1 : at + 2] == "`" or not escaped or escaped % 2:
            return False
        if not silent:
            token = state.push("code_inline", "code", 0)
            token.markup = "`"
            content = src[start + 1 : at].replace("\\`", "`").replace("\n", " ")
            if content.startswith(" ") and content.endswith(" ") and content.strip():
                content = content[1:-1]
            token.content = content
        state.pos = at + 1
        return True
    return False


# --- the model ------------------------------------------------------------------


@dataclass(eq=False)
class Mention:
    """A markdown file named in the text, which becomes a link if it can be found."""

    start: int
    end: int
    name: str


@dataclass(eq=False)
class Piece:
    """A stretch of formatted text, and where each of its characters is in the file.

    Offsets are counted from the start of the top-level block the text is in,
    so a block keeps them when it moves up or down the file.
    """

    text: Content
    offsets: list[int]
    mentions: list[Mention] = field(default_factory=list)
    tasks: list[tuple[int, int]] = field(default_factory=list)
    """Checkboxes: (index in the text, offset of the "[" in the source)."""

    @property
    def plain(self) -> str:
        return self.text.plain


@dataclass(eq=False)
class Run:
    """A piece laid out as a paragraph: `first` before its first line, `rest` before the others."""

    piece: Piece
    first: Content
    rest: Content
    marker: int
    """The source offset the first line's prefix stands for (a list's "-", say)."""


@dataclass(eq=False)
class Picture:
    """An image in a paragraph."""

    source: str
    alt: str
    offset: int


@dataclass(eq=False)
class Segment:
    """Something inside a list or quote that needs a widget of its own (a code block, say)."""

    indent: int
    block: Block


@dataclass(eq=False)
class Block:
    """One top-level piece of the document (a heading, a paragraph, a list...),
    or a part of one."""

    kind: str
    """heading, paragraph, text (a list or quote), rule, fence, table,
    front_matter, picture (a paragraph with images) or group (a list or quote
    with code blocks or tables inside)."""
    source: str = ""
    """The block's lines exactly as written (top-level blocks only)."""
    line: int = 0
    """Where the block starts in the file, as a line number. Updated when it moves."""
    start: int = 0
    """...and as an offset. Updated when it moves."""
    classes: str = ""
    level: int = 0
    runs: list[Run] = field(default_factory=list)
    code: str = ""
    language: str = ""
    code_offsets: list[int] = field(default_factory=list)
    header: list[Piece] = field(default_factory=list)
    rows: list[list[Piece]] = field(default_factory=list)
    yaml: str = ""
    parts: list[Piece | Picture | Segment] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """What makes two blocks the same, for keeping widgets across a reload."""
        return self.kind, self.source

    @property
    def heading_text(self) -> str:
        return self.runs[0].piece.plain if self.runs else ""

    def line_at(self, offset: int) -> int:
        """The line of the file at this offset into the block."""
        return self.line + self.source.count("\n", 0, max(0, offset))


# --- slugs, for #anchors -------------------------------------------------------------

_REMOVABLE = punctuation.replace("-", "").replace("_", "")
_NONLINGUAL = (
    r"\U000024C2-\U0001F251"
    r"\U00002702-\U000027B0"
    r"\U0001F1E0-\U0001F1FF"
    r"\U0001F300-\U0001F5FF"
    r"\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F900-\U0001F9FF"
    r"‍"
    r"♀-♂"
)
_STRIP = re.compile(f"[{re.escape(_REMOVABLE)}{_NONLINGUAL}]+")


def slug(text: str) -> str:
    """A heading's #anchor, the way GitHub makes them (and Textual did)."""
    result = _STRIP.sub("", text.strip().lower())
    return quote(re.sub(r"\s", "-", result))


def slugs(titles: list[str]) -> list[str]:
    """Anchors for headings in order, numbering repeats ("setup", "setup-1")."""
    used: defaultdict[str, int] = defaultdict(int)
    found = []
    for title in titles:
        base = slug(title)
        count = used[base]
        used[base] += 1
        found.append(f"{base}-{count}" if count else base)
    return found


# --- building ---------------------------------------------------------------------


class _Lines:
    """Offsets of the lines of the document."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.starts = [0]
        for match in re.finditer("\n", source):
            self.starts.append(match.end())

    def start(self, line: int) -> int:
        if line >= len(self.starts):
            return len(self.source)
        return self.starts[line]

    def end(self, line: int) -> int:
        """Where this line's text ends (before its newline)."""
        if line + 1 >= len(self.starts):
            return len(self.source)
        return self.starts[line + 1] - 1


def _link(href: str) -> Style:
    return Style.from_meta({"@click": f"link({href!r})"})


class _Inline:
    """Turns one inline token into formatted text, finding each character in the source."""

    def __init__(self, lines: _Lines, base: int, token: Token, cursor: int | None = None) -> None:
        self.children = list(token.children or [])
        self.lines = lines
        self.base = base
        self.raw = token.content
        # Where each character of the raw inline text is in the file.
        self.raw_at: list[int] = []
        first = token.map[0] if token.map else 0
        position = cursor
        for index, line in enumerate(self.raw.split("\n")):
            number = first + index
            begin = lines.start(number) if position is None or index else position
            found = lines.source.find(line, begin, lines.end(number) + 1) if line else -1
            at = found if found != -1 else begin
            self.raw_at.extend(range(at, at + len(line)))
            self.raw_at.append(at + len(line))  # the newline
            position = None
        self.end_cursor = self.raw_at[-1] if self.raw_at else (cursor or 0)
        self.at = 0
        """Where we've got to in the raw text."""
        self.text: list[str] = []
        self.offsets: list[int] = []
        self.spans: list[Span] = []
        self.mentions: list[Mention] = []
        self.tasks: list[tuple[int, int]] = []
        self.pieces: list[Piece | Picture] = []
        self._length = 0

    # -- where things are ---------------------------------------------------------

    def _source_at(self, raw_index: int) -> int:
        if not self.raw_at:
            return 0
        return self.raw_at[max(0, min(raw_index, len(self.raw_at) - 1))] - self.base

    def _skip_to(self, needle: str) -> int:
        """Move past `needle` in the raw text; returns where it began (or -1)."""
        found = self.raw.find(needle, self.at)
        if found != -1:
            self.at = found + len(needle)
        return found

    def _emit(self, text: str, fixed: int | None = None) -> None:
        """Add shown text, finding each character in the raw text as we go."""
        raw = self.raw
        for char in text:
            if fixed is not None:
                self.offsets.append(fixed)
            else:
                if char.isspace():
                    found = next(
                        (i for i in range(self.at, min(len(raw), self.at + _REACH)) if raw[i].isspace()),
                        -1,
                    )
                else:
                    found = raw.find(char, self.at, self.at + _REACH)
                if found == -1:
                    self.offsets.append(self._source_at(self.at))
                else:
                    self.offsets.append(self._source_at(found))
                    self.at = found + 1
            self.text.append(char)
            self._length += 1

    # -- building ---------------------------------------------------------------

    def build(self, *, task: bool = False, pictures: bool = False) -> list[Piece | Picture]:
        children = self.children
        styles: list[tuple[Style | str, int]] = []
        in_link = 0
        if (
            task
            and children
            and children[0].type == "text"
            and _TASK_START.match(children[0].content)
            and _TASK_START.match(self.raw)
        ):
            # "[ ] Write the plan" in a list: a checkbox to click, each character standing for its own.
            box, style = TASK_BOX[self.raw[1]]
            done = style == ".task-done"
            where = self._source_at(0)
            self.tasks.append((0, where))
            for index, char in enumerate(box):
                self._emit(char, fixed=self._source_at(index))
            self.at = 3
            reach = 4 if self.raw[3:4].isspace() else 3
            self.spans.append(Span(0, 3, style))
            # Not an "@click": that would draw the box as a link, underlined and in the link colour.
            self.spans.append(Span(0, reach, Style.from_meta({"task": where})))
            children = [children[0].copy(content=children[0].content[3:]), *children[1:]]
        else:
            done = False
        index = 0
        while index < len(children):
            child = children[index]
            index += 1
            kind = child.type
            if kind == "text":
                start = self._length
                content = re.sub(r"\s+", " ", child.content)
                self._emit(content)
                if not in_link:
                    wiki = list(WIKI_LINK.finditer(content))
                    for match in wiki:
                        name = wiki_link_name(match.group(1))
                        self.mentions.append(Mention(start + match.start(), start + match.end(), name))
                    for match in MENTION_IN_TEXT.finditer(content):
                        if any(w.start() <= match.start() < w.end() for w in wiki):
                            continue  # Named inside a wiki link, which is the link.
                        self.mentions.append(Mention(start + match.start(), start + match.end(), match.group()))
            elif kind == "footnote_ref":
                # "[^note]" in the text: shown as "[1]", numbered as the footnotes are.
                label = str(child.meta.get("id", 0) + 1)
                start = self._length
                self._emit(f"[{label}]")
                self.spans.append(Span(start, self._length, "dim"))
            elif kind == "softbreak":
                at = self.raw.find("\n", self.at)
                self._emit(" ", fixed=self._source_at(at if at != -1 else self.at))
                if at != -1:
                    self.at = at + 1
            elif kind == "hardbreak":
                at = self.raw.find("\n", self.at)
                self._emit("\n", fixed=self._source_at(at if at != -1 else self.at))
                if at != -1:
                    self.at = at + 1
            elif kind == "code_inline":
                self._skip_to(child.markup)
                start = self._length
                self._emit(child.content)
                self._skip_to(child.markup)
                self.spans.append(Span(start, self._length, ".code_inline"))
                name = child.content.strip()
                if not in_link and MENTION_WHOLE.fullmatch(name):
                    self.mentions.append(Mention(start, self._length, name))
            elif kind in ("em_open", "strong_open", "s_open"):
                self._skip_to(child.markup)
                styles.append(({"em_open": ".em", "strong_open": ".strong", "s_open": ".s"}[kind], self._length))
            elif kind in ("em_close", "strong_close", "s_close"):
                self._skip_to(child.markup)
                if styles:
                    style, start = styles.pop()
                    self.spans.append(Span(start, self._length, style))
            elif kind == "link_open":
                if (
                    child.markup == "linkify"
                    and index + 1 < len(children)
                    and children[index].type == "text"
                    and children[index + 1].type == "link_close"
                    and MENTION_WHOLE.fullmatch(children[index].content)
                    and "://" not in children[index].content
                ):
                    # The web-address spotter reads "x.md" as a site in Moldova:
                    # it's a file name, which becomes a link only if it's found.
                    name = children[index].content
                    start = self._length
                    self._emit(name)
                    self.mentions.append(Mention(start, self._length, name))
                    index += 2
                    continue
                if child.markup == "autolink":
                    self._skip_to("<")
                elif child.markup != "linkify":
                    self._skip_to("[")
                styles.append((_link(child.attrs.get("href", "") or ""), self._length))
                in_link += 1
            elif kind == "link_close":
                if child.markup == "autolink":
                    self._skip_to(">")
                elif child.markup != "linkify":
                    self._skip_past_destination()
                in_link = max(0, in_link - 1)
                if styles:
                    style, start = styles.pop()
                    self.spans.append(Span(start, self._length, style))
            elif kind == "image":
                at = self.raw.find("![", self.at)
                offset = self._source_at(at if at != -1 else self.at)
                if at != -1:
                    self.at = at + 2
                source = child.attrs.get("src", "") or ""
                alt = "".join(grand.content for grand in child.children or []) or str(
                    child.attrs.get("alt", "") or ""
                )
                self._skip_past_destination()
                if pictures:
                    self._flush(styles, keep_empty=False)
                    self.pieces.append(Picture(str(source), alt, offset))
                else:
                    start = self._length
                    self._emit(f"🖼  {alt}" if alt else "🖼  ", fixed=offset)
                    self.spans.append(Span(start, self._length, _link(str(source))))
            elif kind == "html_inline":
                if _HTML_TAG.match(child.content):
                    self._skip_to(child.content)
                else:
                    # A placeholder like <slug>, not HTML: shown as written.
                    self._emit(child.content)
        if done:
            # A task that's done fades, leaving the ones still to do to stand out.
            self.spans.append(Span(0, self._length, "dim"))
        self._flush(styles, keep_empty=not pictures)
        return self.pieces

    def _skip_past_destination(self) -> None:
        """After a link's text: move past "](target)" or "][ref]"."""
        close = self.raw.find("]", self.at)
        if close == -1:
            return
        self.at = close + 1
        raw = self.raw
        if raw[self.at : self.at + 1] == "(":
            depth = 0
            for position in range(self.at, len(raw)):
                if raw[position] == "\\":
                    continue
                if raw[position] == "(":
                    depth += 1
                elif raw[position] == ")":
                    depth -= 1
                    if depth == 0:
                        self.at = position + 1
                        return
        elif raw[self.at : self.at + 1] == "[":
            end = raw.find("]", self.at)
            if end != -1:
                self.at = end + 1

    def _flush(self, styles: list[tuple[Style | str, int]], keep_empty: bool) -> None:
        """Finish the text so far as a piece (styles still open carry on into the next)."""
        length = self._length
        spans = list(self.spans)
        for style, start in styles:
            if start < length:
                spans.append(Span(start, length, style))
        text = "".join(self.text)
        if text.strip() or keep_empty:
            content = Content(text, spans=[span for span in spans if span.start < span.end])
            self.pieces.append(Piece(content, self.offsets, self.mentions, self.tasks))
        self.text = []
        self.offsets = []
        self.spans = []
        self.mentions = []
        self.tasks = []
        self._length = 0
        for position, (style, _) in enumerate(styles):
            styles[position] = (style, 0)


def _inline(
    lines: _Lines, base: int, token: Token, *, task: bool = False, cursor: int | None = None
) -> Piece:
    pieces = _Inline(lines, base, token, cursor).build(task=task)
    piece = pieces[0]
    assert isinstance(piece, Piece)
    return piece


def _inline_with_pictures(lines: _Lines, base: int, token: Token) -> list[Piece | Picture]:
    return _Inline(lines, base, token).build(pictures=True)


def _empty() -> Piece:
    return Piece(Content(""), [])


class _Prefix:
    """What goes before the lines of a list item or quote."""

    def __init__(self, first: Content, rest: Content, marker: int) -> None:
        self.first = first
        self.rest = rest
        self.marker = marker

    def take(self) -> tuple[Content, Content, int]:
        """The prefixes for the next paragraph: only the item's first line gets the bullet."""
        first = self.first
        self.first = self.rest
        return first, self.rest, self.marker


def _closing(tokens: list[Token], index: int) -> int:
    """The index of the token that closes the one at `index`."""
    depth = 0
    for position in range(index, len(tokens)):
        nesting = tokens[position].nesting
        depth += nesting
        if depth == 0:
            return position
    return len(tokens) - 1


class _Builder:
    """Builds the blocks of one document."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = _Lines(source)

    def blocks(self) -> list[Block]:
        tokens = parser().parse(self.source)
        blocks: list[Block] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            end = _closing(tokens, index) if token.nesting == 1 else index
            block = self.top_level(tokens, index, end)
            if block is not None:
                blocks.append(block)
            index = end + 1
        return blocks

    def top_level(self, tokens: list[Token], index: int, end: int) -> Block | None:
        token = tokens[index]
        if token.type == "footnote_block_open":
            return self.footnotes(tokens, index, end)
        if token.map is None:
            return None
        first, last = token.map
        start = self.lines.start(first)
        stop = self.lines.start(last)
        source = self.source[start:stop]
        block = Block(kind="", source=source, line=first, start=start)
        kind = token.type
        if kind == "heading_open":
            block.kind = "heading"
            block.level = int(token.tag[1:])
            block.classes = f"-h{block.level}"
            block.runs = [Run(_inline(self.lines, start, tokens[index + 1]), Content(), Content(), 0)]
        elif kind == "paragraph_open":
            inline = tokens[index + 1]
            if any(child.type == "image" for child in inline.children or []):
                block.kind = "picture"
                block.classes = "-paragraph"
                block.parts = list(_inline_with_pictures(self.lines, start, inline))
            else:
                block.kind = "paragraph"
                block.classes = "-paragraph"
                block.runs = [Run(_inline(self.lines, start, inline), Content(), Content(), 0)]
        elif kind in ("bullet_list_open", "ordered_list_open", "blockquote_open"):
            items: list[Run | Segment | int] = []
            if kind == "blockquote_open":
                self.flow(tokens, index + 1, end, start, _Prefix(Content(), Content(), 0), items, 0)
                block.classes = "-quote"
            else:
                self.list(tokens, index, end, start, Content(), Content(), items, 0)
                block.classes = "-list"
            self._fill(block, _tidy(items))
        elif kind == "hr":
            block.kind = "rule"
        elif kind in ("fence", "code_block"):
            self._fence(block, token, start)
        elif kind == "table_open":
            self._table(block, tokens, index, end, start)
        elif kind == "front_matter":
            block.kind = "front_matter"
            block.yaml = token.content
        elif kind == "html_block":
            # Not drawn as HTML: what it says is shown, quietly (a <details>
            # block's summary, say); a comment is nothing at all.
            text = html_text(token.content)
            if not text:
                return None
            block.kind = "paragraph"
            block.classes = "-paragraph -html"
            content = Content(text, spans=[Span(0, len(text), "dim")])
            block.runs = [Run(Piece(content, _offsets_in(source, text)), Content(), Content(), 0)]
        else:
            return None
        return block

    def footnotes(self, tokens: list[Token], index: int, end: int) -> Block | None:
        """The footnotes, gathered at the end of the document: "[1] The note…"."""
        mapped = [token.map for token in tokens[index:end] if token.map is not None]
        if not mapped:
            return None
        first, last = min(m[0] for m in mapped), max(m[1] for m in mapped)
        start, stop = self.lines.start(first), self.lines.start(last)
        block = Block(kind="", source=self.source[start:stop], line=first, start=start, classes="-footnotes")
        items: list[Run | Segment | int] = []
        position = index + 1
        while position < end:
            token = tokens[position]
            if token.type != "footnote_open":
                position += 1
                continue
            close = _closing(tokens, position)
            label = str(token.meta.get("id", 0) + 1)
            inner = [t.map for t in tokens[position:close] if t.map is not None]
            marker = (self.lines.start(inner[0][0]) if inner else start) - start
            symbol = f"[{label}] "
            prefix = _Prefix(Content.styled(symbol, ".bullet"), Content(" " * len(symbol)), marker)
            self.flow(tokens, position + 1, close, start, prefix, items, 0, item=True)
            position = close + 1
        self._fill(block, _tidy(items))
        return block

    # -- lists and quotes -----------------------------------------------------------

    def _fill(self, block: Block, items: list[Run | Segment]) -> None:
        """Lists and quotes: all text in one widget, unless something inside needs its own."""
        if not any(isinstance(item, Segment) for item in items):
            block.kind = "text"
            block.runs = [item for item in items if isinstance(item, Run)]
            return
        block.kind = "group"
        text: list[Run] = []

        def flush() -> None:
            if text:
                block.parts.append(Segment(0, Block(kind="text", runs=list(text))))
                text.clear()

        for item in items:
            if isinstance(item, Segment):
                flush()
                block.parts.append(item)
            else:
                text.append(item)
        flush()

    def list(
        self,
        tokens: list[Token],
        index: int,
        end: int,
        base: int,
        first: Content,
        rest: Content,
        items: list,
        depth: int,
    ) -> None:
        """A bullet or numbered list: each item's first line gets its bullet."""
        ordered = tokens[index].type == "ordered_list_open"
        entries = []
        position = index + 1
        while position < end:
            if tokens[position].type == "list_item_open":
                close = _closing(tokens, position)
                entries.append((position, close))
                position = close + 1
            else:
                position += 1
        if ordered:
            try:
                number = int(tokens[entries[0][0]].info) if entries else 1
            except ValueError:
                number = 1
            width = max((len(f"{n}. ") for n in range(number, number + len(entries))), default=3)
            symbols = [f"{n}. ".rjust(width + 1) for n in range(number, number + len(entries))]
        else:
            symbols = [BULLETS[depth % len(BULLETS)]] * len(entries)
        for (open_at, close_at), symbol in zip(entries, symbols):
            item = tokens[open_at]
            line = item.map[0] if item.map else 0
            marker = self.lines.start(line) - base
            text = self.source[self.lines.start(line) : self.lines.end(line) + 1]
            stripped = len(text) - len(text.lstrip())
            marker += stripped
            # A task's checkbox stands in for its bullet, and what follows lines up after the box.
            if not ordered and self._starts_with_task(tokens, open_at):
                bullet, indent = Content(""), len("[ ] ")
            else:
                bullet, indent = Content.styled(symbol, ".bullet"), cell_len(symbol)
            prefix = _Prefix(first + bullet, rest + " " * indent, marker)
            self.flow(tokens, open_at + 1, close_at, base, prefix, items, depth + (0 if ordered else 1), item=True)
            if prefix.first is not prefix.rest:
                # An empty item still gets its bullet.
                items.append(Run(_empty(), prefix.first, prefix.rest, marker))

    def flow(
        self,
        tokens: list[Token],
        index: int,
        end: int,
        base: int,
        prefix: _Prefix,
        items: list,
        depth: int,
        item: bool = False,
    ) -> None:
        """The blocks inside a list item or quote, from `index` up to `end`."""
        position = index
        first_paragraph = item
        while position < end:
            token = tokens[position]
            close = _closing(tokens, position) if token.nesting == 1 else position
            kind = token.type
            if kind == "paragraph_open" and any(
                child.type == "image" for child in tokens[position + 1].children or []
            ):
                # A picture gets a widget of its own, even in a list.
                indent = cell_len(prefix.rest.plain)
                picture = Block(kind="picture", parts=list(_inline_with_pictures(self.lines, base, tokens[position + 1])))
                prefix.take()
                items.append(Segment(indent, picture))
            elif kind in ("paragraph_open", "heading_open"):
                inline = tokens[position + 1]
                task = first_paragraph and kind == "paragraph_open" and self._is_task(inline)
                piece = _inline(self.lines, base, inline, task=task)
                if kind == "heading_open":
                    piece = Piece(piece.text.stylize("bold"), piece.offsets, piece.mentions, piece.tasks)
                first, rest, marker = prefix.take()
                if first.plain == rest.plain and inline.map:
                    # Not an item's first line: it stands for its own line, "> " and all.
                    marker = self.lines.start(inline.map[0]) - base
                items.append(Run(piece, first, rest, marker))
            elif kind in ("bullet_list_open", "ordered_list_open"):
                first, rest, _ = prefix.take()
                self.list(tokens, position, close, base, first, rest, items, depth)
                prefix.first = prefix.rest
                if not item:
                    items.append(1)  # A list in a quote has a gap after it.
            elif kind == "blockquote_open":
                bar = Content.styled("▌", ".quote-bar") + " "
                first, rest, marker = prefix.take()
                items.append(1)
                inner = _Prefix(first + bar, rest + bar, marker)
                self.flow(tokens, position + 1, close, base, inner, items, depth)
                prefix.first = prefix.rest
                items.append(1)
            else:
                indent = cell_len(prefix.rest.plain)
                segment = self.segment(tokens, position, close, base)
                if segment is not None:
                    prefix.take()
                    items.append(Segment(indent, segment))
            first_paragraph = False
            position = close + 1

    def _starts_with_task(self, tokens: list[Token], item: int) -> bool:
        """Is the list item that opens at `item` a task ("- [ ] …")?"""
        return (
            item + 2 < len(tokens)
            and tokens[item + 1].type == "paragraph_open"
            and self._is_task(tokens[item + 2])
        )

    def _is_task(self, inline: Token) -> bool:
        if inline.map is None or not _TASK_START.match(inline.content):
            return False
        line = self.source[self.lines.start(inline.map[0]) : self.lines.end(inline.map[0])]
        return TASK.match(line) is not None

    def segment(self, tokens: list[Token], index: int, end: int, base: int) -> Block | None:
        """A code block, table or rule inside a list or quote."""
        token = tokens[index]
        if token.type in ("fence", "code_block"):
            block = Block(kind="fence", line=0, start=base)
            self._fence(block, token, base)
            return block
        if token.type == "table_open":
            block = Block(kind="table", start=base)
            self._table(block, tokens, index, end, base)
            return block
        if token.type == "hr":
            return Block(kind="rule", start=base)
        return None

    # -- code and tables --------------------------------------------------------------

    def _fence(self, block: Block, token: Token, base: int) -> None:
        block.kind = "fence"
        block.language = token.info.strip()
        code = token.content.rstrip()
        block.code = code
        first = (token.map[0] + (1 if token.type == "fence" else 0)) if token.map else 0
        offsets: list[int] = []
        for index, line in enumerate(code.split("\n")):
            number = first + index
            begin = self.lines.start(number)
            found = self.source.find(line, begin, self.lines.end(number) + 1) if line else -1
            at = (found if found != -1 else begin) - base
            offsets.extend(range(at, at + len(line)))
            offsets.append(at + len(line))
        block.code_offsets = offsets[:-1] if offsets else []

    def _table(self, block: Block, tokens: list[Token], index: int, end: int, base: int) -> None:
        block.kind = "table"
        row: list[Piece] | None = None
        cursor: int | None = None
        in_head = False
        for position in range(index, end):
            token = tokens[position]
            if token.type == "thead_open":
                in_head = True
            elif token.type == "thead_close":
                in_head = False
            elif token.type == "tr_open":
                row = []
                cursor = None
                if not in_head:
                    block.rows.append(row)
            elif token.type == "inline" and row is not None:
                builder = _Inline(self.lines, base, token, cursor)
                piece = builder.build()[0]
                assert isinstance(piece, Piece)
                cursor = builder.end_cursor
                if in_head:
                    block.header.append(piece)
                else:
                    row.append(piece)
        if block.rows and not block.rows[-1]:
            block.rows.pop()


def _offsets_in(source: str, text: str) -> list[int]:
    """Where each character of `text` is in `source`, taking them in order
    (for text drawn from the source with parts left out)."""
    offsets = []
    at = 0
    for char in text:
        found = source.find(char, at)
        if found == -1:
            offsets.append(min(at, len(source)))
        else:
            offsets.append(found)
            at = found + 1
    return offsets


def _tidy(items: list) -> list:
    """Collapse runs of blank lines to one, and give each blank the prefix around it."""
    tidied: list = []
    for item in items:
        if isinstance(item, int):
            if tidied and isinstance(tidied[-1], int):
                continue
            tidied.append(item)
        else:
            tidied.append(item)
    # Blank lines inside a quote carry its bars: take them from the line after (or before).
    result: list = []
    for position, item in enumerate(tidied):
        if not isinstance(item, int):
            result.append(item)
            continue
        neighbour = None
        for other in tidied[position + 1 :]:
            if isinstance(other, Run):
                neighbour = other
                break
        before = next((other for other in reversed(tidied[:position]) if isinstance(other, Run)), None)
        bars = _common_bars(before, neighbour)
        result.append(Run(_empty(), bars, bars, before.marker if before else 0))
    return result


def _common_bars(before: Run | None, after: Run | None) -> Content:
    """The quote bars two lines share, for the blank line between them."""

    def bars(run: Run | None) -> str:
        if run is None:
            return ""
        text = run.rest.plain
        count = 0
        while text.startswith("▌ ", count * 2):
            count += 1
        return "▌ " * count if count else ""

    shared = min(bars(before), bars(after), key=len)
    if not shared.strip():
        return Content()
    return Content("").join(Content.styled("▌", ".quote-bar") + " " for _ in range(len(shared) // 2))


def build(source: str) -> list[Block]:
    """The blocks of a markdown document."""
    return _Builder(source).blocks()


# --- laying text out ------------------------------------------------------------------


def wrap(text: str, width: int) -> list[tuple[int, int]]:
    """Where to break text to fit `width` cells: (start, end) of each line.

    Breaks between words, dropping the spaces at a break; a word too long for
    a line is cut. "\\n" always starts a new line.
    """
    width = max(1, width)
    lines: list[tuple[int, int]] = []
    position = 0
    for hard in text.split("\n"):
        base = position
        position += len(hard) + 1
        line_start = base
        line_end = base
        line_width = 0
        started = False
        for match in _WORDS.finditer(hard):
            word_start = base + match.start()
            word_end = base + match.end()
            size = cell_len(match.group())
            if match.group().isspace():
                line_width += size
                continue
            if line_width + size <= width:
                line_width += size
                line_end = word_end
                started = True
                continue
            if started:
                lines.append((line_start, line_end))
            line_start = word_start
            while cell_len(text[line_start:word_end]) > width:
                cut = line_start
                used = 0
                while cut < word_end and used + cell_len(text[cut]) <= width:
                    used += cell_len(text[cut])
                    cut += 1
                cut = max(cut, line_start + 1)
                lines.append((line_start, cut))
                line_start = cut
            line_width = cell_len(text[line_start:word_end])
            line_end = word_end
            started = True
        lines.append((line_start, line_end if started else line_start))
    return lines


_WORDS = re.compile(r"\S+|\s+")
