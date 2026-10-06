"""Starting tmr: command-line options, a #heading to open at, and piped input."""

from __future__ import annotations

from pathlib import Path

from helpers import settle, started, tree_labels

from tmr import parse_target, read_piped_input
from tmr.app import TmrApp


def test_a_heading_after_the_path(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("# Plan\n")
    (tmp_path / "odd#name.md").write_text("# Odd\n")
    assert parse_target(str(tmp_path / "plan.md#setup")) == (tmp_path / "plan.md", "setup")
    assert parse_target(str(tmp_path / "plan.md")) == (tmp_path / "plan.md", "")
    # A file with a # in its name is still that file.
    assert parse_target(str(tmp_path / "odd#name.md")) == (tmp_path / "odd#name.md", "")


def test_nothing_piped_when_stdin_is_the_terminal(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert read_piped_input() is None


async def test_opens_at_the_heading_asked_for(folder: Path) -> None:
    long = folder / "long.md"
    long.write_text("# Long\n\n" + "A paragraph.\n\n" * 30 + "## Setup\n\n" + "More.\n\n" * 30)
    app = TmrApp(folder, start_file=long, start_anchor="setup")
    async with app.run_test(size=(120, 12)) as pilot:
        await started(pilot)
        assert app.viewer.path == long
        setup = next(widget for _, widget in app.viewer.document.shown_blocks() if "Setup" in widget.plain)
        scroller = app.viewer.scroller
        assert scroller.scroll_y > 0
        assert scroller.region.y <= setup.region.y < scroller.region.y + scroller.region.height


async def test_options_list_all_or_hidden_files_and_hide_the_sidebar(folder: Path) -> None:
    app = TmrApp(folder, all_files=True, show_hidden=True, watch=False, sidebar=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert not app.query_one("#side").display
        assert not app.tree.markdown_only and app.tree.show_hidden
        assert not any(worker.group == "watch" for worker in app.workers)
        await pilot.press("b")
        await settle(pilot)
        assert app.query_one("#side").display
        labels = tree_labels(app)
        assert "notes.txt" in labels and ".secret" in labels
