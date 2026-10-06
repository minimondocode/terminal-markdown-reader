"""Finding things: a file (`/`), words in the document (`s`), a heading (`h`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from helpers import (
    messages,
    settle,
    started,
    tree_labels,
    wait_until,
)
from textual.widgets import Static

from tmr.app import TmrApp
from tmr.finder import FileFinder
from tmr.markdown import CURRENT_MATCH_STYLE, CodeBody, TableBlock
from tmr.outline import Outline
from tmr.search import build_index
from tmr.viewer import CODE_PIECE_LINES, CodePiece, CodeView


async def test_search_highlights_and_counts(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("s", *"loft")
        await settle(pilot)
        # Title, the code block, and twice in the last paragraph.
        assert len(app.viewer.matches) == 4
        count = app.viewer.query_one("#search-count", Static)
        assert "of 4" in str(count.render())
        await pilot.press("enter")
        first = app.viewer.current_match
        await pilot.press("escape")
        assert app.viewer.matches == []
        await pilot.press("n")  # nothing to go to; must not crash
        assert first >= 0


async def test_long_code_file_is_drawn_in_pieces_and_searched(folder: Path) -> None:
    # A package-lock.json's worth of lines: drawn as one widget, every mouse
    # movement over it drew the whole file again, and tmr froze.
    lines = [f'    "package-{n}": "^1.0.{n}",' for n in range(30_000)]
    lines[25_000] = '    "the-needle": "^9.9.9",'
    (folder / "big.json").write_text("{\n" + "\n".join(lines) + "\n}\n")
    app = TmrApp(folder, start_file=folder / "big.json")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        view = app.viewer.query_one(CodeView)
        pieces = list(view.query(CodePiece))
        assert len(pieces) > 30_000 // CODE_PIECE_LINES
        assert "\n".join(str(piece.original) for piece in pieces) == view.original.plain
        await pilot.press("s", *"the-needle")
        await settle(pilot)
        [match] = app.viewer.matches
        assert "the-needle" in match.widget.search_text()
        y = app.viewer._match_y(match)
        top = int(app.viewer.scroller.scroll_y)
        assert top <= y < top + app.viewer.scroller.scrollable_content_region.height


async def test_searching_a_long_table_draws_only_what_is_on_screen(folder: Path) -> None:
    # A plan index thousands of rows long: searching for "e" lit thousands of
    # matches, and each was looked for in every cell, then the whole table drawn
    # again; tmr froze for seconds at every keystroke.
    import time

    rows = [f"| plan-{n}.md | needle {n} here | some more text in the third cell |" for n in range(1500)]
    (folder / "big.md").write_text("# Plans\n\n| File | What | Notes |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    app = TmrApp(folder, start_file=folder / "big.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await settle(pilot)
        table = app.viewer.query_one(TableBlock)
        began = time.perf_counter()
        app.viewer.run_search("e")
        await pilot.pause()
        assert time.perf_counter() - began < 1.0
        assert len(app.viewer.matches) > 10_000
        # The rows on screen are lit, the current match in its own colour.
        current = app.viewer.matches[app.viewer.current_match]
        assert current.widget is table
        row = table.match_row(current.start)
        assert CURRENT_MATCH_STYLE in [style for style, _, _ in table._highlighted(table.size.width)[row]]
        strip = table.render_line(row)
        assert "e" in strip.text


async def test_find_file_opens_and_reveals(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("slash")
        await settle(pilot)
        await pilot.press(*"deepnote")
        await settle(pilot)
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.viewer.path == folder / "docs" / "deep" / "note.md")
        await settle(pilot)
        assert "note.md" in tree_labels(app)


async def test_find_file_lists_only_markdown(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        assert not app.tree.markdown_only, "even when the file list shows everything"
        await pilot.press("slash")
        await wait_until(pilot, lambda: isinstance(app.screen, FileFinder) and app.screen.shown)
        assert app.screen.shown
        assert all(path.suffix == ".md" for path in app.screen.shown)


def test_file_search_ranks_names_first_and_is_fast(tmp_path: Path) -> None:
    import time

    from tmr.search import search

    root = tmp_path
    (root / "readme-stuff").mkdir()
    (root / "readme-stuff" / "other.txt").write_text("")
    (root / "docs").mkdir()
    (root / "docs" / "README.md").write_text("")
    (root / "README.md").write_text("")
    for i in range(3000):
        (root / f"file-{i}.txt").write_text("")
    index = build_index(root, show_hidden=False)
    started = time.perf_counter()
    hits = search("readme", index)
    assert time.perf_counter() - started < 0.2
    assert [h.entry.relative for h in hits[:3]] == ["README.md", "docs/README.md", "readme-stuff/other.txt"]
    assert [h.entry.relative for h in search("rdme", index)[:1]] == ["README.md"]
    assert [h.entry.relative for h in search("docs read", index)[:1]] == ["docs/README.md"]
    assert search("zzzz", index) == []


def test_file_search_with_nothing_typed_lists_newest_first(tmp_path: Path) -> None:
    import os

    from tmr.search import search

    for age, name in enumerate(["c.md", "a.md", "b.txt", "d.md"]):
        (tmp_path / name).write_text("")
        os.utime(tmp_path / name, (1_000_000 - age, 1_000_000 - age))
    index = build_index(tmp_path, show_hidden=False)
    assert [h.entry.relative for h in search("", index)] == ["c.md", "a.md", "b.txt", "d.md"]
    assert [h.entry.relative for h in search("", index, markdown_only=True, limit=2)] == ["c.md", "a.md"]


def test_file_search_skips_what_git_ignores_except_markdown(tmp_path: Path) -> None:

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("build/\nscratch.*\nnode_modules/\n")
    (tmp_path / "build" / "deep").mkdir(parents=True)
    (tmp_path / "build" / "out.js").write_text("")
    (tmp_path / "build" / "deep" / "notes.md").write_text("")
    (tmp_path / "build" / ".hidden").mkdir()
    (tmp_path / "build" / ".hidden" / "secret.md").write_text("")
    (tmp_path / "scratch.md").write_text("")
    (tmp_path / "scratch.txt").write_text("")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "README.md").write_text("")
    (tmp_path / "keep.md").write_text("")
    names = [e.relative for e in build_index(tmp_path, show_hidden=False)]
    assert names == ["build/deep/notes.md", "keep.md", "scratch.md"]
    everything = [e.relative for e in build_index(tmp_path, show_hidden=True)]
    assert "build/out.js" in everything


async def test_find_file_can_narrow_to_a_recently_changed_folder(folder: Path) -> None:
    import os
    import time

    from tmr.finder import FileFinder

    now = time.time()
    for path in folder.rglob("*"):
        os.utime(path, (now - 86400, now - 86400))
    os.utime(folder / "docs" / "deep" / "note.md", (now - 120, now - 120))
    os.utime(folder / "docs" / "guide.md", (now - 7200, now - 7200))

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("slash")
        await settle(pilot)
        finder = app.screen
        assert isinstance(finder, FileFinder)
        assert [name for name, _ in finder.folders] == ["docs/deep", "docs"]
        row = str(finder.query_one("#finder-folders").render())
        assert "docs/deep/ 2m" in row and "docs/ 2h" in row

        await pilot.press("tab")
        await settle(pilot)
        assert finder.folder == "docs/deep"
        assert finder.shown == [folder / "docs" / "deep" / "note.md"]

        await pilot.press("tab")
        await settle(pilot)
        assert set(finder.shown) == {folder / "docs" / "deep" / "note.md", folder / "docs" / "guide.md"}
        await pilot.press(*"guide", "enter")
        await wait_until(pilot, lambda: app.viewer.path == folder / "docs" / "guide.md")


async def test_find_file_lists_the_most_recently_changed_files_first(folder: Path) -> None:
    import os
    import time

    from tmr.finder import FileFinder

    now = time.time()
    for path in folder.rglob("*"):
        os.utime(path, (now - 86400, now - 86400))
    os.utime(folder / "docs" / "guide.md", (now - 7200, now - 7200))
    os.utime(folder / "docs" / "deep" / "note.md", (now - 120, now - 120))

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("slash")
        await settle(pilot)
        finder = app.screen
        assert isinstance(finder, FileFinder)
        assert finder.shown[:2] == [folder / "docs" / "deep" / "note.md", folder / "docs" / "guide.md"]
        results = finder.query_one("#finder-results")
        first = str(results.get_option_at_index(0).prompt)
        assert first.startswith("docs/deep/note.md") and first.rstrip().endswith("2m")

        # Typing switches back to ranking by how well the name matches, without ages.
        await pilot.press(*"readme")
        await settle(pilot)
        assert finder.shown[0] == folder / "README.md"
        assert str(results.get_option_at_index(0).prompt).rstrip() == "README.md"

        # Enter with nothing typed opens the newest file.
        await pilot.press(*["backspace"] * 6)
        await settle(pilot)
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.viewer.path == folder / "docs" / "deep" / "note.md")


def test_short_ages() -> None:
    import time

    from tmr.finder import short_age

    now = time.time()
    assert short_age(now) == "now"
    assert short_age(now - 300) == "5m"
    assert short_age(now - 3 * 3600) == "3h"
    assert short_age(now - 2 * 86400) == "2d"
    assert short_age(now - 21 * 86400) == "3w"


async def test_outline_jumps_to_a_heading(folder: Path) -> None:
    filler = "\n\n".join(f"Paragraph {n} of filler text." for n in range(40))
    (folder / "long.md").write_text(
        f"# Long\n\n{filler}\n\n## Middle part\n\n{filler}\n\n### Deep down\n\n{filler}\n\n## The end\n\nBye.\n"
    )
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "long.md")
        await settle(pilot)
        await pilot.press("h")
        await settle(pilot)
        assert isinstance(app.screen, Outline)
        rows = [str(app.screen.query_one("OptionList").get_option_at_index(i).prompt) for i in range(4)]
        assert rows == ["Long", "  Middle part", "    Deep down", "  The end"]

        await pilot.press(*"deep", "enter")
        await settle(pilot)
        assert not isinstance(app.screen, Outline)
        assert app.viewer.scroller.scroll_y > 0
        assert app.viewer.headings()[app.viewer.current_heading()][1] == "Deep down"

        # Opening it again starts on the section you're in.
        await pilot.press("h")
        await settle(pilot)
        assert app.screen.query_one("OptionList").highlighted == 2
        await pilot.press("escape")


async def test_outline_says_so_when_there_are_no_headings(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "notes.txt")
        await settle(pilot)
        await pilot.press("h")
        await settle(pilot)
        assert messages(app) == ["This file has no headings."]


async def test_search_highlights_in_a_code_block_go_when_the_search_does(folder: Path) -> None:
    # Each search drew its highlights over the last one's in a code block, so
    # typing "auto" left every "a" lit, and all of it outlived the search.
    (folder / "README.md").write_text("# Plan\n\n```\nGo-live: AUTO-SHIP, a plan and a PR.\n```\n")
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        code = app.viewer.document.query_one(CodeBody)

        def lit() -> list[str]:
            shown = code.render()
            return [shown.plain[span.start : span.end] for span in shown.spans if "yellow" in str(span.style) or "cyan" in str(span.style)]

        app.viewer.run_search("a")
        assert len(lit()) == 5
        app.viewer.run_search("auto")
        assert lit() == ["AUTO"]
        await pilot.press("s", *"auto")
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert lit() == []
        assert code.search_text() == "Go-live: AUTO-SHIP, a plan and a PR."
