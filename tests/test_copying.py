"""Copying: highlighted text as markdown, the whole file, a code block, the path."""

from __future__ import annotations

import subprocess
from pathlib import Path

from helpers import (
    README,
    block_containing,
    messages,
    settle,
    started,
)

from tmr.app import TmrApp
from tmr.markdown import (
    CodeFence,
    CopyButton,
)


async def test_highlighting_text_copies_it(folder: Path, monkeypatch) -> None:
    copied: list[bytes] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command == ["pbcopy"]:
            copied.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        block = block_containing(app, "Final paragraph")
        app.viewer.scroller.scroll_to_widget(block, animate=False, top=True)
        await settle(pilot)
        start = block.content_region.x - block.region.x
        await pilot.mouse_down(block, offset=(start, 0))
        await pilot.hover(block, offset=(start + 5, 0))
        await pilot.hover(block, offset=(start + 14, 0))
        await pilot.mouse_up(block, offset=(start + 14, 0))
        await settle(pilot)
        assert copied, "highlighting should copy"
        assert copied[-1].decode().startswith("Final para")


async def test_c_copies_the_whole_file(folder: Path, monkeypatch) -> None:
    copied: list[bytes] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command == ["pbcopy"]:
            copied.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("c")
        assert copied[-1].decode() == README
        assert "Copied all of README.md" in messages(app)[0]

        await app.open_file(folder / "data.bin")
        await pilot.press("c")
        assert len(copied) == 1, "binary files aren't copied"


async def highlight(pilot, start, end) -> None:
    """Drag the mouse from one character to another, `(widget, (x, y) in its content)`."""
    (first, (begin, top)), (last, (finish, bottom)) = start, end

    def at(widget, x: int, y: int) -> tuple[int, int]:
        return widget.content_region.x - widget.region.x + x, widget.content_region.y - widget.region.y + y

    await pilot.mouse_down(first, offset=at(first, begin, top))
    await pilot.hover(first, offset=at(first, begin + 1, top))
    await pilot.hover(last, offset=at(last, finish, bottom))
    await pilot.mouse_up(last, offset=at(last, finish, bottom))
    await settle(pilot)


async def test_highlighting_copies_markdown_as_written(folder: Path, clipboard: list[str]) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        heading = block_containing(app, "Tasks")
        second = block_containing(app, "Pick a name")
        x, y = second.locate("Pick a name")
        await highlight(pilot, (heading, (0, 0)), (second, (x + len("Pick a name") + 3, y)))
        assert clipboard[-1] == "## Tasks\n\n- [ ] Write the plan\n- [x] Pick a name"

        # Part of one line: just the source behind it, inline symbols included.
        intro = block_containing(app, "A demo with")
        text = intro.plain
        await highlight(pilot, (intro, (text.index("demo"), 0)), (intro, (text.index(" and") - 1, 0)))
        assert clipboard[-1] == "**demo** with a [link to the guide](docs/guide.md#setup)"
        await highlight(pilot, (intro, (text.index("with"), 0)), (intro, (text.index(" a ") + 1, 0)))
        assert clipboard[-1] == "with a"

        # Part of a code block leaves out the ``` lines.
        code = app.viewer.document.query_one("CodeBody")
        app.viewer.scroller.scroll_to_widget(code, animate=False)
        await pilot.pause()
        await highlight(pilot, (code, (0, 0)), (code, (6, 0)))
        assert clipboard[-1] == "# - [ ]"


async def test_copy_button_copies_a_code_block(folder: Path, clipboard: list[str]) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        fence = app.viewer.document.query_one(CodeFence)
        fence.scroll_visible(animate=False)
        await settle(pilot)
        await pilot.click(fence.query_one(CopyButton))
        await settle(pilot)
        assert clipboard[-1] == '# - [ ] not a checkbox inside code\nprint("loft")'
        assert messages(app)[0] == "Copied code block (2 lines)"

        # The button isn't part of the code: searching doesn't find it.
        await pilot.press("s", *"copy")
        await settle(pilot)
        assert str(app.viewer.query_one("#search-count").render()) == "no matches"


async def test_messages_replace_each_other(folder: Path, clipboard: list[str]) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("c", "c", "c")
        await settle(pilot)
        assert len(messages(app)) == 1, "no pile of boxes in the corner"
        assert messages(app)[0].startswith("Copied all of README.md")
        assert messages(app)[0].endswith("×3[/]")
        await pilot.press("a")
        await settle(pilot)
        assert messages(app) == ["Showing all files"]


async def test_p_copies_the_path(folder: Path, clipboard: list[str]) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "docs" / "guide.md")
        await pilot.press("p")
        assert clipboard[-1] == str(folder / "docs" / "guide.md")
        assert messages(app) == ["Copied path of guide.md"]


async def test_highlighting_across_a_wrapped_code_block_copies_its_source(folder: Path, clipboard: list[str]) -> None:
    # A fence with no language wraps; highlighting from one wrapped row into the
    # next still copies the words as written, the space at the break included.
    words = " ".join(f"word{n}" for n in range(40))
    (folder / "prompt.md").write_text(f"```\n{words}\n```\n")
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "prompt.md")
        await settle(pilot)
        code = app.viewer.document.query_one("CodeBody")
        first, second = code.rows[0], code.rows[1]
        assert len(code.rows) > 1 and second[0] == first[1] + 1, "wrapped at a space"
        last_word = words[first[0] : first[1]].split()[-1]
        await highlight(pilot, (code, (first[1] - len(last_word), 0)), (code, (5, 1)))
        assert clipboard[-1] == f"{last_word} {words[second[0] : second[0] + 6]}"
