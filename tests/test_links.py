"""Following links and file names, and going back and forward between files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from helpers import (
    block_containing,
    messages,
    settle,
    spot,
    started,
    tree_labels,
    wait_until,
)

from tmr.app import TmrApp
from tmr.search import build_index, find_named


async def test_clicking_link_opens_other_document(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        block = block_containing(app, "link to the guide")
        await pilot.click(block, offset=spot(block, "link to the guide", 2))
        await wait_until(pilot, lambda: app.viewer.path == folder / "docs" / "guide.md")
        assert "guide.md" in tree_labels(app), "tree should reveal the opened file"


async def test_missing_link_warns(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        block = block_containing(app, "missing link")
        await pilot.click(block, offset=spot(block, "missing link", 2))
        await settle(pilot)
        assert app.viewer.path == folder / "README.md"
        assert "Couldn't find" in messages(app)[0]


async def test_web_links_open_in_the_browser(folder: Path, monkeypatch) -> None:
    (folder / "web.md").write_text("# Web\n\nRead [the docs](https://example.com/docs) or https://example.org.\n")
    launched: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, *args, **kwargs) -> None:
            if command[0] == "open":
                launched.append(command)

    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, *a, **k: FakePopen(command) if command[0] == "open" else real_popen(command, *a, **k),
    )
    monkeypatch.setattr("tmr.app.sys.platform", "darwin")
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "web.md")
        await settle(pilot)
        block = block_containing(app, "the docs")
        await pilot.click(block, offset=spot(block, "the docs", 2))
        await settle(pilot)
        await pilot.click(block, offset=spot(block, "example.org", 2))
        await settle(pilot)
        assert launched == [["open", "https://example.com/docs"], ["open", "https://example.org"]]
        assert messages(app) == ["Opening example.org in your browser"]


def _mention_links(source: str, known: dict[str, str]) -> list[tuple[str, str]]:
    """(text, href) for each link the view shows, given which names can be found."""
    from tmr.render import build

    found = []
    for block in build(source):
        for run in block.runs:
            piece = run.piece
            for span in piece.text.spans:
                action = getattr(span.style, "meta", {}).get("@click", "")
                if action.startswith("link("):
                    found.append((span.start, piece.plain[span.start : span.end], action[6:-2]))
            for mention in piece.mentions:
                if mention.name in known:
                    found.append((mention.start, piece.plain[mention.start : mention.end], known[mention.name]))
    return [(text, href) for _, text, href in sorted(found)]


def test_named_markdown_files_become_links() -> None:
    known = {"plan.md": "/p/plan.md", "docs/lanes.md": "/p/docs/lanes.md", "a.md": "/p/a.md"}
    source = (
        "See `plan.md`, docs/lanes.md and plan.md. Not `gone.md`, gone.md,\n"
        "`plan.md later`, notplan.md.bak or https://x.org/plan.md. Real: [a](a.md)."
    )
    assert _mention_links(source, known) == [
        ("plan.md", "/p/plan.md"),
        ("docs/lanes.md", "/p/docs/lanes.md"),
        ("plan.md", "/p/plan.md"),
        ("https://x.org/plan.md", "https://x.org/plan.md"),
        ("a", "a.md"),
    ]


def test_unfound_names_are_not_web_links() -> None:
    # The web-address spotter would send "gone.md" to a site in Moldova.
    assert _mention_links("Shipped gone.md today.", {}) == []


def test_finding_a_named_file(folder: Path) -> None:
    (folder / "docs" / "archive").mkdir()
    (folder / "docs" / "archive" / "old-plan.md").write_text("# Old\n")
    (folder / ".git").mkdir()
    entries = build_index(folder, False)
    near = folder / "docs" / "deep"
    assert find_named("note.md", near, entries) == folder / "docs" / "deep" / "note.md"
    assert find_named("docs/guide.md", near, None) == folder / "docs" / "guide.md"
    assert find_named("old-plan.md", near, entries) == folder / "docs" / "archive" / "old-plan.md"
    assert find_named("old-plan.md", near, None) is None
    assert find_named("nowhere.md", near, entries) is None


async def test_clicking_a_named_file_opens_it(folder: Path) -> None:
    (folder / "docs" / "archive").mkdir()
    (folder / "docs" / "archive" / "old-plan.md").write_text("# Old\n")
    (folder / "docs" / "ship.md").write_text("# Ship\n\nShipped `old-plan.md` last week.\n")
    app = TmrApp(folder, start_file=folder / "docs" / "ship.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, lambda: app.file_index is not None)
        await settle(pilot)
        block = block_containing(app, "Shipped old-plan.md")
        await pilot.click(block, offset=spot(block, "old-plan.md", 2))
        await wait_until(pilot, lambda: app.viewer.path == folder / "docs" / "archive" / "old-plan.md")


async def test_back_and_forward_between_files(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        readme, guide, note = folder / "README.md", folder / "docs" / "guide.md", folder / "notes.txt"
        await app.open_file(guide)
        await app.open_file(note)
        await settle(pilot)

        await pilot.press("left_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == guide)
        await pilot.press("left_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == readme)
        await pilot.press("left_square_bracket")
        await settle(pilot)
        assert app.viewer.path == readme
        assert messages(app) == ["No earlier file."]

        await pilot.press("right_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == guide)
        assert "guide.md" in tree_labels(app), "tree should reveal the file gone back to"

        # Opening another file from here drops the ones ahead, like a browser.
        await app.open_file(folder / "script.py")
        await settle(pilot)
        assert [visit.path for visit in app.visited] == [readme, guide, folder / "script.py"]
        await pilot.press("right_square_bracket")
        await settle(pilot)
        assert app.viewer.path == folder / "script.py"


async def test_back_and_forward_come_back_to_where_you_were(folder: Path) -> None:
    long = folder / "long.md"
    long.write_text("\n".join(f"## Part {n}\n\nSome words about part {n}.\n" for n in range(40)))
    notes = folder / "notes.txt"
    notes.write_text("".join(f"line {n}\n" for n in range(100)))
    app = TmrApp(folder)
    async with app.run_test(size=(120, 30)) as pilot:
        await started(pilot)
        await app.open_file(long)
        await settle(pilot)
        app.viewer.scroller.scroll_to(y=37, animate=False)
        await settle(pilot)
        await app.open_file(notes)
        await settle(pilot)
        app.viewer.scroller.scroll_to(y=50, animate=False)
        await settle(pilot)

        await pilot.press("left_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == long)
        await settle(pilot)
        assert app.viewer.scroller.scroll_y == 37
        await pilot.press("right_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == notes)
        await settle(pilot)
        assert app.viewer.scroller.scroll_y == 50

        # Lines added above it since: still the same line of the file at the top.
        await pilot.press("left_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == long)
        await settle(pilot)
        line = app.viewer.reading_spot()[0]
        assert line > 0
        await app.open_file(notes)
        await settle(pilot)
        long.write_text("# Added\n\nA new start.\n\n" + long.read_text())
        await settle(pilot)
        await pilot.press("left_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == long)
        await settle(pilot)
        assert app.viewer.reading_spot()[0] == line + 4

        # Opening a file afresh (not going back) starts at its top.
        await app.open_file(notes)
        await settle(pilot)
        assert app.viewer.scroller.scroll_y == 0


async def test_back_skips_deleted_files(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "notes.txt")
        await app.open_file(folder / "script.py")
        await settle(pilot)
        (folder / "notes.txt").unlink()
        await pilot.press("left_square_bracket")
        await wait_until(pilot, lambda: app.viewer.path == folder / "README.md")


def test_wiki_links_name_files() -> None:
    source = "# Wiki\n\nSee [[docs/guide]] and [[docs/guide|the guide]] and [[nowhere]].\n"
    assert _mention_links(source, {"docs/guide.md": "docs/guide.md"}) == [
        ("[[docs/guide]]", "docs/guide.md"),
        ("[[docs/guide|the guide]]", "docs/guide.md"),
    ]
