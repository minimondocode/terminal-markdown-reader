"""The formatted view knows where everything it shows came from in the file."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from helpers import settle, started, wait_until

from tmr.app import TmrApp
from tmr.markdown import CodeBody, Document, TableBlock, TextBlock
from tmr.render import build, wrap
from tmr.viewer import map_line

RICH = """\
---
title: Sample
---

# Heading with **bold**

A paragraph with *emphasis*, `code`, a [link](a.md), an escaped \\*star\\*, an &amp; entity,
and a second line.

- [ ] A task
- [x] Done
  - Nested with **bold**
    1. Numbered

> A quote with *style*
>
> > Nested quote

| Name | Notes |
| --- | --- |
| Bob | Writes **plans** and [links](https://example.com) |

1. Run this:

   ```sh
   echo "in a list"
   ```

2. Then this.
"""


def _shown_pieces(blocks):
    for block in blocks:
        for run in block.runs:
            yield block, run.piece
        for part in block.parts:
            inner = getattr(part, "block", None)
            if inner is not None:
                for run in inner.runs:
                    yield block, run.piece
            elif hasattr(part, "offsets"):
                yield block, part
        for row in [block.header, *block.rows]:
            for cell in row:
                yield block, cell


def test_every_character_shown_knows_where_it_is_in_the_file() -> None:
    blocks = build(RICH)
    checked = 0
    for block, piece in _shown_pieces(blocks):
        assert len(piece.offsets) == len(piece.plain)
        for char, offset in zip(piece.plain, piece.offsets):
            if char == "✓" or char.isspace():
                continue
            assert RICH[block.start + offset] == char, (piece.plain, char)
            checked += 1
    assert checked > 150

    # Code, too: in a code block inside a list, the indentation is left out.
    fence = next(part.block for block in blocks for part in block.parts if hasattr(part, "block") and part.block.kind == "fence")
    top = next(block for block in blocks if block.kind == "group")
    at = top.start + fence.code_offsets[0]
    assert RICH[at : at + len(fence.code)] == fence.code == 'echo "in a list"'


def test_checkboxes_stand_for_the_brackets() -> None:
    blocks = build(RICH)
    tasks = next(block for block in blocks if block.kind == "text" and "A task" in block.runs[0].piece.plain)
    first = tasks.runs[0].piece
    assert first.plain == "[ ] A task"
    [(index, offset)] = first.tasks
    assert index == 0 and RICH[tasks.start + offset : tasks.start + offset + 3] == "[ ]"
    assert tasks.runs[1].piece.plain == "[✓] Done"
    # Only lists have tasks: "[ ]" elsewhere is just text.
    assert all(not piece.tasks for block, piece in _shown_pieces(build("Text [ ] here\n")))


def test_a_partly_done_task_has_its_own_box() -> None:
    [block] = build("- [~] Half there\n")
    piece = block.runs[0].piece
    assert piece.plain == "[~] Half there"
    assert piece.tasks == [(0, 2)]
    assert block.runs[0].rest.plain == "    ", "wrapped lines start under the text, after the box"


def test_backticks_escaped_inside_code_stay_in_the_code() -> None:
    source = "Header: `# Kickoff — \\`feat/<slug>\\` (prompt)` then `a` and `C:\\` and `b`.\n"
    [block] = build(source)
    piece = block.runs[0].piece
    assert piece.plain == "Header: # Kickoff — `feat/<slug>` (prompt) then a and C:\\ and b."
    code = [piece.plain[span.start : span.end] for span in piece.text.spans if span.style == ".code_inline"]
    assert code == ["# Kickoff — `feat/<slug>` (prompt)", "a", "C:\\", "b"]
    for char, offset in zip(piece.plain, piece.offsets):
        assert char.isspace() or source[block.start + offset] == char


def test_placeholders_in_angle_brackets_are_shown_but_html_is_not() -> None:
    [block] = build("Save to plan-<slug>.md, a <b>bold</b> word<br> and <!-- a note -->.\n")
    assert block.runs[0].piece.plain == "Save to plan-<slug>.md, a bold word and ."


def test_blocks_are_the_same_when_their_source_is() -> None:
    before = build("# One\n\nPara.\n\n## Two\n")
    after = build("# One\n\nNew para.\n\nPara.\n\n## Two\n")
    assert before[0].key == after[0].key
    assert before[1].key == after[2].key and before[1].line != after[2].line


def test_wrapping_breaks_between_words_and_cuts_long_ones() -> None:
    text = "hello world this is"
    assert [text[a:b] for a, b in wrap(text, 11)] == ["hello world", "this is"]
    assert [("abcdefgh")[a:b] for a, b in wrap("abcdefgh", 3)] == ["abc", "def", "gh"]
    assert wrap("a\n\nb", 5) == [(0, 1), (2, 2), (3, 4)]
    # Double-width characters take two cells.
    assert [("日本語テキスト")[a:b] for a, b in wrap("日本語テキスト", 6)] == ["日本語", "テキス", "ト"]


def test_mapping_a_line_through_an_edit() -> None:
    old = "a\nb\nc\nd\n"
    assert map_line(old, "new\nnew\na\nb\nc\nd\n", 2) == 4
    assert map_line(old, "a\nB\nc\nd\n", 1) == 1
    assert map_line(old, old, 3) == 3


# --- in the app ---------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder.resolve()


def text_block(app: TmrApp, text: str) -> TextBlock:
    for block in app.viewer.document.query(TextBlock):
        if text in block.plain:
            return block
    raise AssertionError(f"no block shows {text!r}")


async def drag(pilot, widget, start: tuple[int, int], end: tuple[int, int]) -> None:
    def at(x: int, y: int) -> tuple[int, int]:
        return widget.content_region.x - widget.region.x + x, widget.content_region.y - widget.region.y + y

    await pilot.mouse_down(widget, offset=at(*start))
    await pilot.hover(widget, offset=at(start[0] + 1, start[1]))
    await pilot.hover(widget, offset=at(*end))
    await pilot.mouse_up(widget, offset=at(*end))
    await settle(pilot)


async def test_highlighting_a_list_item_copies_it_with_its_bullet(root: Path, clipboard: list[str]) -> None:
    (root / "README.md").write_text(RICH)
    app = TmrApp(root)
    async with app.run_test(size=(120, 50)) as pilot:
        await started(pilot)
        tasks = text_block(app, "A task")
        x, y = tasks.locate("Done")
        # From the bullet of the second item to the end of its words.
        await drag(pilot, tasks, (0, y), (x + len("Done"), y))
        assert clipboard[-1] == "- [x] Done"

        # Part of a nested item, bold and all.
        x, y = tasks.locate("with bold")
        await drag(pilot, tasks, (x + len("with "), y), (x + len("with bold"), y))
        assert clipboard[-1] == "**bold**"

        # A table's cells aren't matched up bit by bit: part of one is copied as shown.
        table = app.viewer.document.query_one(TableBlock)
        table.scroll_visible(animate=False)
        await settle(pilot)
        await drag(pilot, table, (2, 1), (5, 1))
        assert clipboard[-1] == "Name"


async def test_highlighting_code_inside_a_list(root: Path, clipboard: list[str]) -> None:
    (root / "README.md").write_text(RICH)
    app = TmrApp(root)
    async with app.run_test(size=(120, 50)) as pilot:
        await started(pilot)
        code = app.viewer.document.query_one(CodeBody)
        code.scroll_visible(animate=False)
        await settle(pilot)
        await drag(pilot, code, (0, 0), (4, 0))
        assert clipboard[-1] == "echo"


async def test_reloading_keeps_the_text_you_were_reading_in_view(root: Path) -> None:
    sections = "\n".join(f"## Part {n}\n\nWords about part {n}.\n" for n in range(60))
    doc = root / "README.md"
    doc.write_text(sections)
    app = TmrApp(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        viewer = app.viewer
        target = text_block(app, "Words about part 30.")
        viewer.scroller.scroll_to(y=viewer._y_in_document(target), animate=False)
        await settle(pilot)
        top_before = viewer._top_source_line()
        others = {id(widget) for widget in viewer.document.children}

        # An agent adds a dozen lines near the top.
        extra = "\n".join(f"New line {n}.\n" for n in range(12))
        doc.write_text(sections.replace("## Part 1\n", extra + "\n## Part 1\n"))
        await viewer.reload()
        await settle(pilot)

        # Still reading part 30, although it's further down the file now.
        assert viewer._y_in_document(target) == int(viewer.scroller.scroll_y)
        assert viewer._top_source_line()[0] == top_before[0] + 24
        # Only the new blocks were drawn: everything else kept its widget.
        kept = {id(widget) for widget in viewer.document.children}
        assert len(others - kept) == 0
        assert len(kept - others) == 12


async def test_reloading_when_the_text_on_screen_changed(root: Path) -> None:
    sections = "\n".join(f"## Part {n}\n\nWords about part {n}.\n" for n in range(60))
    doc = root / "README.md"
    doc.write_text(sections)
    app = TmrApp(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        viewer = app.viewer
        target = text_block(app, "Words about part 30.")
        viewer.scroller.scroll_to(y=viewer._y_in_document(target), animate=False)
        await settle(pilot)
        extra = "\n".join(f"New line {n}.\n" for n in range(5))
        doc.write_text(
            sections.replace("## Part 1\n", extra + "\n## Part 1\n").replace(
                "Words about part 30.", "Words about part thirty."
            )
        )
        await viewer.reload()
        await settle(pilot)
        replaced = text_block(app, "Words about part thirty.")
        assert viewer._y_in_document(replaced) == int(viewer.scroller.scroll_y)


async def test_named_files_become_links_when_the_file_list_arrives(root: Path, monkeypatch) -> None:
    from tmr import app as app_module

    (root / "notes" / "old").mkdir(parents=True)
    (root / "notes" / "old" / "far.md").write_text("# Far\n")
    (root / "README.md").write_text("# Hi\n\nSee far.md and ghost.md for more.\n")
    (root / ".git").mkdir()  # so looking for far.md next to the README stops here
    shown: list[str] = []
    real_show = Document.show

    def counting_show(self, source, base_dir):
        shown.append(source)
        return real_show(self, source, base_dir)

    monkeypatch.setattr(Document, "show", counting_show)
    # Hold the file list back until the document has been looked at without it.
    ready = threading.Event()
    real_build_index = app_module.build_index

    def held_build_index(*args, **kwargs):
        ready.wait(10)
        return real_build_index(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_index", held_build_index)
    app = TmrApp(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(pilot, lambda: app.viewer.document.query(TextBlock), what="the document")
        block = text_block(app, "See far.md")

        def linked() -> list[str]:
            return [
                str(block.render()[span.start : span.end])
                for span in block.render().spans
                if "@click" in getattr(span.style, "meta", {})
            ]

        assert app.file_index is None and linked() == [], "far.md is only found through the file list"
        ready.set()
        await wait_until(pilot, lambda: app.file_index is not None and linked(), what="the link")
        await settle(pilot)
        assert linked() == ["far.md"], "found through the file list; ghost.md is nowhere"
        assert len(shown) == 1, "linked in place, not drawn again"
        assert text_block(app, "See far.md") is block


async def test_first_document_is_drawn_without_waiting_for_the_file_list(root: Path, monkeypatch) -> None:
    from tmr import app as app_module

    (root / "README.md").write_text("# Hi\n\nSee docs/guide.md for more.\n")
    # The file list doesn't arrive until the test is over.
    done = threading.Event()
    real_build_index = app_module.build_index

    def held_build_index(*args, **kwargs):
        done.wait(10)
        return real_build_index(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_index", held_build_index)
    app = TmrApp(root)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_until(pilot, lambda: app.viewer.document.query(TextBlock), what="the document")
            assert "See docs/guide.md" in text_block(app, "See docs").plain
            assert app.file_index is None
    finally:
        done.set()


async def test_search_scrolls_to_the_exact_row(root: Path) -> None:
    long_paragraph = " ".join(f"word{n}" for n in range(400)) + " needle at the end."
    filler = "\n\n".join(f"Filler paragraph {n}." for n in range(40))
    (root / "README.md").write_text(f"# Search\n\n{filler}\n\n{long_paragraph}\n\n{filler}\n")
    app = TmrApp(root)
    async with app.run_test(size=(80, 24)) as pilot:
        await started(pilot)
        await pilot.press("s", *"needle")
        await settle(pilot)
        viewer = app.viewer
        [match] = viewer.matches
        block = match.widget
        assert isinstance(block, TextBlock)
        row = block.match_row(match.start)
        assert row > 3, "the match is several rows into the paragraph"
        assert "needle" in block.plain.split("\n")[row]
        y = viewer._content_y(block) + row
        top = int(viewer.scroller.scroll_y)
        assert top <= y < top + viewer.scroller.scrollable_content_region.height


async def test_search_finds_words_in_tables(root: Path) -> None:
    (root / "README.md").write_text(RICH)
    app = TmrApp(root)
    async with app.run_test(size=(120, 50)) as pilot:
        await started(pilot)
        await pilot.press("s", *"plans")
        await settle(pilot)
        [match] = app.viewer.matches
        assert isinstance(match.widget, TableBlock)
        assert "plans" in str(match.widget.render()).split("\n")[match.widget.match_row(match.start)]


async def test_ticking_a_task_in_a_list_with_code(root: Path) -> None:
    doc = root / "README.md"
    doc.write_text("# Steps\n\n- [ ] Run it:\n\n  ```sh\n  make\n  ```\n\n- [ ] Ship it\n")
    app = TmrApp(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        block = text_block(app, "Ship it")
        x, y = block.locate("[ ] Ship it")
        await pilot.click(block, offset=(block.content_region.x - block.region.x + x, block.content_region.y - block.region.y + y))
        await wait_until(pilot, lambda: "- [x] Ship it" in doc.read_text(), what="the tick")
        assert doc.read_text() == "# Steps\n\n- [ ] Run it:\n\n  ```sh\n  make\n  ```\n\n- [x] Ship it\n"


async def test_named_files_follow_files_coming_and_going(root: Path, watching: threading.Event) -> None:
    (root / ".git").mkdir()
    (root / "notes").mkdir()
    (root / "README.md").write_text("# Hi\n\nThe plan is in later.md, once it's written.\n")
    app = TmrApp(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        block = text_block(app, "later.md")

        def linked() -> bool:
            content = block.render()
            return any(
                "@click" in getattr(span.style, "meta", {}) and content.plain[span.start : span.end] == "later.md"
                for span in content.spans
            )

        assert not linked()
        await wait_until(pilot, watching.is_set, what="the watcher")

        # An agent writes it, in another folder: the name becomes a link.
        (root / "notes" / "later.md").write_text("# Later\n")
        await wait_until(pilot, linked, timeout=10, what="the link")

        # ...and stops being one when the file goes.
        (root / "notes" / "later.md").unlink()
        await wait_until(pilot, lambda: not linked(), timeout=10, what="the link to go")


async def test_pictures_in_a_list_are_shown_as_images(root: Path) -> None:
    from PIL import Image as PILImage
    from textual_image.widget import Image

    PILImage.new("RGB", (40, 20), "orange").save(root / "pic.png")
    (root / "README.md").write_text("# Pics\n\n- An item\n\n  ![A picture](pic.png)\n\n- Another\n")
    app = TmrApp(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        assert len(app.viewer.document.query(Image)) == 1
        assert "• Another" in text_block(app, "Another").plain
