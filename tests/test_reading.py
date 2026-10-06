"""Reading a document: how it's drawn, ticking tasks, and redrawing only when needed."""

from __future__ import annotations

from pathlib import Path

from helpers import (
    README,
    block_containing,
    settle,
    spot,
    started,
    wait_until,
)
from textual.widgets import Static

from tmr.app import TmrApp
from tmr.markdown import (
    CodeFence,
    CopyButton,
    FrontMatter,
    FrontMatterValue,
    SourceButton,
    read_front_matter,
    toggle_task,
    wrap_rows,
)
from tmr.viewer import InfoView


async def test_welcome_when_no_readme(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "a.txt").write_text("hi")
    app = TmrApp(tmp_path.resolve())
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert app.viewer.path is None
        assert "Pick a file on the left" in app.viewer.document.source


async def test_checkboxes_are_drawn_but_not_in_code(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        tasks = block_containing(app, "Write the plan").plain
        assert "[ ] Write the plan" in tasks and "•" not in tasks, "the checkbox stands in for the bullet"
        assert "[✓] Pick a name" in tasks
        code = app.viewer.document.query_one("#code-content").render()
        assert "# - [ ] not a checkbox inside code" in str(code)


async def test_picture_is_shown_as_image(folder: Path) -> None:
    from textual_image.widget import Image

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert len(app.viewer.document.query(Image)) == 1


async def test_binary_file_shows_details(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "data.bin")
        await settle(pilot)
        info = app.viewer.query_one(InfoView)
        assert "can't be shown as text" in str(info.render())


async def test_remembers_last_file(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "script.py")
        await settle(pilot)
    again = TmrApp(folder)
    async with again.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert again.viewer.path == folder / "script.py"


async def test_when_it_changed_sits_at_the_top_right(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "docs" / "guide.md")
        await settle(pilot)
        age = app.viewer.query_one("#doc-age", Static)
        assert str(age.render()) == "changed just now · ~15 tokens"
        assert age.region.right == app.viewer.document.content_region.right, "at the text's right edge"
        assert age.region.y == app.query_one("#side-title").content_region.y, "level with the folder's name"


async def test_the_text_starts_level_with_the_file_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "plain.md").write_text("Just words, no heading.\n\nMore words.\n")
    (tmp_path / "titled.md").write_text("# Title\n\nWords.\n")
    (tmp_path / "section.md").write_text("## Section\n\nWords.\n")
    app = TmrApp(tmp_path.resolve())
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        file_list = app.tree.content_region.y
        for name, first in (("plain.md", "Just words"), ("titled.md", "Title"), ("section.md", "Section")):
            await app.open_file(tmp_path / name)
            await settle(pilot)
            block = block_containing(app, first)
            assert block.content_region.y == file_list, name


async def test_the_title_stays_at_the_top_once_scrolled_away(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    words = "\n\n".join(f"Paragraph {n}." for n in range(60))
    (tmp_path / "titled.md").write_text(f"# The Plan\n\n{words}\n")
    (tmp_path / "untitled.md").write_text(f"## Only a section\n\n{words}\n")
    app = TmrApp(tmp_path.resolve())
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        viewer = app.viewer
        title = viewer.query_one("#doc-title", Static)
        await app.open_file(tmp_path / "titled.md")
        await settle(pilot)
        assert str(title.render()) == "", "not while the heading itself is on show"

        viewer.scroller.scroll_to(y=20, animate=False)
        await wait_until(pilot, lambda: str(title.render()) == "The Plan", what="the title at the top")
        assert title.region.y == viewer.query_one("#doc-age").region.y, "on the line with when it changed"

        await pilot.click(title)
        await wait_until(pilot, lambda: viewer.scroller.scroll_y == 0, what="back to the top")
        await wait_until(pilot, lambda: str(title.render()) == "", what="the title gone again")

        await app.open_file(tmp_path / "untitled.md")
        await settle(pilot)
        viewer.scroller.scroll_to(y=20, animate=False)
        await settle(pilot)
        assert str(title.render()) == "", "nothing without a # title"


async def test_the_title_never_shows_while_opening_a_file_at_its_top(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    words = "\n\n".join(f"Paragraph {n}." for n in range(60))
    (tmp_path / "one.md").write_text(f"# One\n\n{words}\n")
    (tmp_path / "two.md").write_text(f"# Two\n\n{words}\n")
    app = TmrApp(tmp_path.resolve())
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        viewer = app.viewer
        title = viewer.query_one("#doc-title", Static)
        shown: list[str] = []
        real_update = title.update
        title.update = lambda text="", **kwargs: (shown.append(str(text)), real_update(text, **kwargs))[1]
        for scrolled in (0, 20):
            await app.open_file(tmp_path / "one.md")
            await settle(pilot)
            viewer.scroller.scroll_to(y=scrolled, animate=False)
            await settle(pilot)
            shown.clear()
            await app.open_file(tmp_path / "two.md")
            await settle(pilot)
            assert set(shown) <= {""}, f"from {scrolled} down, the title flashed: {shown}"


def test_toggle_task_flips_one_line_and_nothing_else() -> None:
    source = "# Plan\r\n\r\n- [ ] one\r\n1. [x] two\r\n- [ ]not a task\r\n"
    ticked = toggle_task(source, 2)
    assert ticked == source.replace("- [ ] one", "- [x] one")
    assert toggle_task(source, 3) == source.replace("1. [x] two", "1. [ ] two")
    assert toggle_task("- [~] half\n", 0) == "- [x] half\n", "a partly done task ticks to done"
    assert toggle_task(source, 0) is None
    assert toggle_task(source, 4) is None
    assert toggle_task(source, 99) is None


async def test_clicking_a_checkbox_ticks_the_task_in_the_file(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        task = block_containing(app, "Write the plan")
        task.scroll_visible(animate=False)
        await settle(pilot)
        await pilot.click(task, offset=spot(task, "[ ] Write the plan"))
        readme = folder / "README.md"
        await wait_until(pilot, lambda: "- [x] Write the plan" in readme.read_text())
        await wait_until(pilot, lambda: "[✓] Write the plan" in block_containing(app, "Write the plan").plain)
        assert "- [x] Pick a name" in readme.read_text()

        # Clicking the words (not the box) leaves the task alone.
        task = block_containing(app, "Pick a name")
        await pilot.click(task, offset=spot(task, "Pick a name", 4))
        await settle(pilot)
        assert "- [x] Pick a name" in readme.read_text()

        # And the box again unticks it.
        task = block_containing(app, "Write the plan")
        await pilot.click(task, offset=spot(task, "[✓] Write the plan"))
        await wait_until(pilot, lambda: "- [ ] Write the plan" in readme.read_text())


FRONT_MATTER = """\
---
title: "Launch plan"
status: draft  # still changing
tags: [agents, notes]
owners:
  - Alice
  - Bob
link: https://example.com/plan
summary: >
  Folded text
  on two lines.
---

# Launch plan
"""


def test_front_matter_is_read_as_pairs() -> None:
    yaml = FRONT_MATTER.split("---\n")[1]
    assert read_front_matter(yaml) == [
        ("title", "Launch plan"),
        ("status", "draft"),
        ("tags", "agents, notes"),
        ("owners", "Alice, Bob"),
        ("link", "https://example.com/plan"),
        ("summary", "Folded text on two lines."),
    ]
    assert read_front_matter("[not, a, mapping]") is None


async def test_front_matter_is_a_box_not_raw_yaml(folder: Path, monkeypatch) -> None:
    (folder / "plan.md").write_text(FRONT_MATTER)
    opened: list[str] = []
    monkeypatch.setattr(TmrApp, "open_in_browser", lambda self, url: opened.append(url))
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "plan.md")
        await settle(pilot)
        box = app.viewer.document.query_one(FrontMatter)
        assert box.border_title == "metadata"
        assert ("owners", "Alice, Bob") in box.pairs
        assert not app.viewer.document.query("Rule")
        assert [level for level, _, _ in app.viewer.headings()] == [1]

        # A web address in it can be clicked.
        link = next(v for v in box.query(FrontMatterValue) if "example.com" in str(v.render()))
        await pilot.click(link, offset=(3, 0))
        await settle(pilot)
        assert opened == ["https://example.com/plan"]


DIAGRAM = """\
# Flow

```mermaid
graph LR
  A[Idea] --> B[Ship]
```

```mermaid
this is not mermaid
```
"""


async def test_mermaid_is_drawn_and_its_source_is_a_click_away(
    folder: Path, clipboard: list[str]
) -> None:
    (folder / "flow.md").write_text(DIAGRAM)
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "flow.md")
        await settle(pilot)
        drawn, broken = app.viewer.document.query(CodeFence)
        assert drawn.diagram and not broken.diagram
        picture = drawn.query_one("#code-content").render()
        assert "Idea" in str(picture) and "┌" in str(picture)
        assert "not mermaid" in str(broken.query_one("#code-content").render())

        # Search finds words in the drawing.
        await pilot.press("s", *"ship")
        await settle(pilot)
        assert str(app.viewer.query_one("#search-count").render()) == "1 of 1"
        await pilot.press("escape")

        await pilot.click(drawn.query_one(SourceButton))
        await settle(pilot)
        assert "A[Idea] --> B[Ship]" in str(drawn.query_one("#code-content").render())
        await pilot.click(drawn.query_one(SourceButton))
        await settle(pilot)
        assert "┌" in str(drawn.query_one("#code-content").render())

        await pilot.click(drawn.query_one(CopyButton))
        await settle(pilot)
        assert clipboard[-1] == "graph LR\n  A[Idea] --> B[Ship]"


async def test_reload_only_redraws_when_the_text_changed(folder: Path) -> None:
    import os

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        readme = folder / "README.md"
        first = block_containing(app, "Final paragraph")
        os.utime(readme)  # touched, not changed
        await app.viewer.reload()
        await settle(pilot)
        assert block_containing(app, "Final paragraph") is first

        readme.write_text(README.replace("Final paragraph", "Last paragraph"))
        await app.viewer.reload()
        await settle(pilot)
        assert block_containing(app, "Last paragraph") is not first


async def test_code_without_a_language_is_shown_plain() -> None:
    code = "def loft():\n    return 1\n"
    plain = CodeFence.highlight(code, "")
    assert plain.spans == CodeFence.highlight(code, "text").spans
    assert plain.spans != CodeFence.highlight(code, "python").spans


def test_remembers_only_the_most_recent_folders(tmp_path: Path, monkeypatch) -> None:
    from tmr import state

    monkeypatch.setattr(state, "MAX_FOLDERS", 3)
    for name in ("a", "b", "c", "d", "b"):
        folder = tmp_path / name
        folder.mkdir(exist_ok=True)
        (folder / "x.md").write_text("x")
        state.remember(folder, folder / "x.md")
    assert state.last_open(tmp_path / "a") is None
    assert [Path(key).name for key in state._load()] == ["c", "d", "b"]


async def test_a_rule_before_a_heading_is_one_break_not_two(folder: Path) -> None:
    # A line of air either side of the rule, and the heading after it doesn't
    # add its own space above as well.
    (folder / "rules.md").write_text("Text.\n\n---\n\n## Section\n\nBody.\n\n---\n\nMore.\n")
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "rules.md")
        await settle(pilot)
        rows = [widget.region.y for _, widget in app.viewer.document.shown_blocks()]
        text, rule, heading, body, second_rule, more = rows
        assert rule - text == 2 and heading - rule == 2
        assert second_rule - body == 2 and more - second_rule == 2
        assert body - heading == 3, "the heading after a rule keeps the air under its own rule"

        # Take the rule away and the heading has its own space again.
        (folder / "rules.md").write_text("Text.\n\n## Section\n\nBody.\n")
        await app.viewer.document.show((folder / "rules.md").read_text(), folder)
        await settle(pilot)
        text, heading, _ = [widget.region.y for _, widget in app.viewer.document.shown_blocks()]
        assert heading - text == 3


async def test_a_ruled_heading_has_a_line_of_air_under_its_rule(folder: Path) -> None:
    # Both ruled headings (# and ##) leave a blank row between their rule and
    # the text below, so the section doesn't sit on the line.
    (folder / "ruled.md").write_text("# Title\n\nIntro.\n\n## Section\n\n1. First.\n")
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "ruled.md")
        await settle(pilot)
        blocks = [widget for _, widget in app.viewer.document.shown_blocks()]
        title, intro, section, first = blocks
        assert intro.region.y - title.region.bottom == 1
        assert first.region.y - section.region.bottom == 1
        assert section.region.y - intro.region.bottom == 2, "still nearer its own section than the one before"


def test_wrapping_code_rows() -> None:
    assert wrap_rows("ab cd\nef", 0) == [(0, 5), (6, 8)]
    assert wrap_rows("aaa bbb ccc", 7) == [(0, 7), (8, 11)], "at the last space that fits, which is left out"
    assert wrap_rows("abcdefgh", 3) == [(0, 3), (3, 6), (6, 8)], "no space: cut where it's full"


async def test_code_blocks_mark_cut_off_lines(folder: Path) -> None:
    long = "x = '" + "y" * 200 + "'"
    (folder / "code.md").write_text(f"```python\nshort = 1\n{long}\n```\n\n```\nplain words\n```\n")
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "code.md")
        await settle(pilot)
        python, plain = app.viewer.document.query(CodeFence)
        assert not python.query("#code-label") and not plain.query("#code-label"), "no language label"
        box, code = python.query_one("#code-box"), python.query_one("CodeBody")
        button = python.query_one(CopyButton)
        assert button.region.y == box.region.y + 1, "the button sits in the box's top right, a line in"
        assert box.region.right - button.region.right == 2, "inset as far as the code is on the left"
        assert code.content_region.x - box.region.x == 2
        assert str(button.render()).strip() == "copy"
        assert code.content_region.y == button.region.bottom + 1, "a blank row between it and the code"
        assert box.region.bottom - code.content_region.bottom == 1, "and a blank row below the code"
        assert str(python.query_one("#code-more", Static).render()) == "\n›", "only the long line"

        # Scrolled to its end, it isn't cut off any more.
        scroll = python.query_one("CodeScroll")
        scroll.scroll_to(x=scroll.max_scroll_x, animate=False)
        await settle(pilot)
        assert str(python.query_one("#code-more", Static).render()).strip() == ""


def test_footnotes_html_and_comments() -> None:
    from tmr.render import build

    source = (
        "A claim[^1] here.\n\n<details>\n<summary>More</summary>\n\nInside.\n\n</details>\n\n"
        "<!-- a comment -->\n\n[^1]: The note,\n    continued.\n"
    )
    blocks = build(source)
    assert [(block.kind, block.classes) for block in blocks] == [
        ("paragraph", "-paragraph"),
        ("paragraph", "-paragraph -html"),
        ("paragraph", "-paragraph"),
        ("text", "-footnotes"),
    ]
    assert blocks[0].runs[0].piece.plain == "A claim[1] here."
    assert blocks[1].runs[0].piece.plain == "More"
    note = blocks[3].runs[0]
    assert (note.first.plain, note.piece.plain) == ("[1] ", "The note, continued.")
    assert blocks[3].source.startswith("[^1]:")
    assert blocks[3].line == source[: source.index("[^1]:")].count("\n")
