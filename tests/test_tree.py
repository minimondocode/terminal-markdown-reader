"""The file list on the left: what it shows, opening from it, keeping up with changes."""

from __future__ import annotations

import threading
from pathlib import Path

from helpers import settle, started, tree_labels, wait_until

from tmr.app import TmrApp
from tmr.viewer import CodeView


async def test_opens_readme_and_hides_clutter(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert app.viewer.path == folder / "README.md"
        assert tree_labels(app) == ["docs", "README.md"], "markdown only by default"
        await pilot.press("a")
        await settle(pilot)
        labels = tree_labels(app)
        assert labels == ["docs", "data.bin", "notes.txt", "pic.png", "README.md", "script.py"]


async def test_click_file_in_tree_opens_it(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        line = tree_labels(app).index("notes.txt")
        await pilot.click("#tree", offset=(4, line))
        await wait_until(pilot, lambda: app.viewer.path == folder / "notes.txt")
        code = app.viewer.query_one(CodeView)
        assert "Plain note with loft." in code.original.plain


async def test_clicking_folder_expands_it(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.click("#tree", offset=(4, 0))
        await wait_until(pilot, lambda: "guide.md" in tree_labels(app))


async def test_enter_reads_the_file(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        app.tree.cursor_line = tree_labels(app).index("script.py")
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.viewer.path == folder / "script.py")
        assert app.viewer.scroller.has_focus, "over to the document, to read it"


async def test_moving_onto_a_file_shows_it(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        app.tree.cursor_line = tree_labels(app).index("notes.txt")
        await pilot.press("down")  # pic.png
        await pilot.press("down")  # README.md
        await settle(pilot)
        assert app.viewer.path == folder / "README.md"
        await pilot.press("down")
        await settle(pilot)
        assert app.viewer.path == folder / "script.py"
        assert app.tree.has_focus, "the cursor stays in the file list"


async def test_moving_onto_a_folder_leaves_the_file_on_show(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        app.tree.cursor_line = tree_labels(app).index("README.md")
        await pilot.press("up")
        await settle(pilot)
        assert app.viewer.path == folder / "README.md"
        assert "guide.md" not in tree_labels(app), "the folder isn't opened"


async def test_holding_a_key_shows_only_where_the_cursor_stops(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        app.tree.cursor_line = tree_labels(app).index("data.bin")
        shown: list[Path] = []
        app.viewer.open = _noting(app.viewer.open, shown)
        await pilot.press("down", "down", "down", "down")  # Quicker than the cursor rests.
        await settle(pilot)
        assert shown == [folder / "script.py"]


async def test_back_skips_files_only_passed_over(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        app.tree.cursor_line = tree_labels(app).index("data.bin")
        await pilot.press("enter")  # Read it: kept in the history.
        await settle(pilot)
        app.tree.focus()
        for _ in range(2):  # notes.txt, then pic.png
            await pilot.press("down")
            await settle(pilot)
        assert app.viewer.path == folder / "pic.png"
        await pilot.press("left_square_bracket")
        await settle(pilot)
        assert app.viewer.path == folder / "data.bin"


async def test_moving_while_editing_leaves_the_editor_alone(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("e")
        await settle(pilot)
        assert app.editing
        app.tree.focus()
        await pilot.press("up")
        await settle(pilot)
        assert app.editing
        assert app.viewer.path == folder / "README.md"


def _noting(open_, shown: list[Path]):
    async def noting(path, **kwargs):
        shown.append(path)
        await open_(path, **kwargs)

    return noting


async def test_starting_on_a_file_that_isnt_markdown_lists_all_files(folder: Path) -> None:
    app = TmrApp(folder, start_file=folder / "notes.txt")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert app.viewer.path == folder / "notes.txt"
        assert not app.tree.markdown_only
        assert app.tree.cursor_node.data.path == folder / "notes.txt"


async def test_starting_on_a_markdown_file_lists_only_markdown(folder: Path) -> None:
    app = TmrApp(folder, start_file=folder / "README.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert tree_labels(app) == ["docs", "README.md"]


async def test_toggles(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        assert "script.py" in tree_labels(app)
        await pilot.press("a")
        await settle(pilot)
        assert tree_labels(app) == ["docs", "README.md"]
        await pilot.press("a")
        await pilot.press("full_stop")
        await settle(pilot)
        labels = tree_labels(app)
        assert ".secret" in labels and "node_modules" in labels


async def test_live_updates(folder: Path, watching: threading.Event) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await app.open_file(folder / "notes.txt")
        await wait_until(pilot, watching.is_set, what="the watcher")
        (folder / "notes.txt").write_text("Changed by an agent\n")
        (folder / "fresh.md").write_text("# Fresh\n")
        await wait_until(
            pilot,
            lambda: app.viewer.query(CodeView)
            and "Changed by an agent" in app.viewer.query_one(CodeView).original.plain,
            timeout=10,
        )
        await wait_until(pilot, lambda: "fresh.md" in tree_labels(app), timeout=10)
        assert folder / "fresh.md" in app.tree.changed
        assert app.viewer.path == folder / "notes.txt", "must not jump to the new file"


async def test_changes_in_hidden_folders_leave_the_lists_alone(folder: Path) -> None:
    from watchfiles import Change

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        rebuilds: list[bool] = []
        app.rebuild_file_index = lambda: rebuilds.append(True)
        scratch = folder / ".secret" / "scratch.tmp"
        scratch.write_text("x")
        await app._apply_changes({(Change.added, str(scratch))})
        assert rebuilds == [] and scratch not in app.tree.changed

        fresh = folder / "docs" / "fresh.md"
        fresh.write_text("# Fresh\n")
        await app._apply_changes({(Change.added, str(fresh))})
        # Taken into the list as it is, not by gathering the list again.
        assert rebuilds == [] and fresh in app.tree.changed
        assert app.file_index.shows_file("docs/fresh.md")


async def test_sidebar_is_a_fifth_and_can_be_dragged(folder: Path) -> None:
    from tmr import state

    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        side = app.query_one("#side")
        assert side.size.width == 20
        # Positions below are screen columns; the divider starts at column 20.
        await pilot.mouse_down(offset=(20, 5))
        await pilot.hover(offset=(28, 5))
        await pilot.hover(offset=(35, 5))
        await pilot.mouse_up(offset=(35, 5))
        await settle(pilot)
        assert side.size.width == 35
        assert app.viewer.path == folder / "README.md", "dragging must not open or select anything"
        # Can't squeeze the document away entirely.
        await pilot.mouse_down(offset=(35, 5))
        await pilot.hover(offset=(95, 5))
        await pilot.mouse_up(offset=(95, 5))
        await settle(pilot)
        assert side.size.width == 70
    assert state.sidebar_percent() == 70.0

    again = TmrApp(folder)
    async with again.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        assert again.query_one("#side").size.width == 70


async def test_divider_thickens_under_the_mouse_with_no_tooltip(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        divider = app.query_one("#divider")
        assert divider.tooltip is None, "a tooltip would cover the text either side"
        assert "┃" not in str(divider.render())
        await pilot.hover(offset=(21, 5))
        await settle(pilot)
        assert "┃" in str(divider.render())
        await pilot.hover(offset=(50, 5))
        await settle(pilot)
        assert "┃" not in str(divider.render())


async def test_b_widens_then_hides_then_restores_the_sidebar(folder: Path) -> None:
    from tmr import state

    (folder / f"{'very-' * 12}long-name.md").write_text("# Long\n")
    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        assert app.tree.has_focus
        side = app.query_one("#side")
        await pilot.press("b")
        await settle(pilot)
        assert side.size.width == 50, "no more than half the window"
        assert app.tree.has_focus
        await pilot.press("b")
        await settle(pilot)
        assert not side.display
        assert not app.query_one("#divider").display
        assert app.viewer.size.width == 100
        assert not app.tree.has_focus, "focus moves to the document"
        await pilot.press("b")
        await settle(pilot)
        assert side.display and side.size.width == 20
    assert state.sidebar_percent() is None, "widening for now isn't remembered"


async def test_b_widens_just_enough_for_the_longest_name(folder: Path) -> None:
    name = "a-rather-long-name-for-a-file"
    (folder / f"{name}.md").write_text("# Long\n")
    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        side = app.query_one("#side")

        def shown() -> str:
            return "\n".join(app.tree.render_line(y).text for y in range(app.tree.size.height))

        assert name not in shown(), "shortened at the usual width"
        await pilot.press("b")
        await settle(pilot)
        assert 20 < side.size.width < 50
        assert name in shown() and "…" not in shown()
        assert f"{name}  " in shown(), "a little air between the name and the scrollbar"
        await pilot.press("b")
        await settle(pilot)
        assert not side.display
        await pilot.press("b")
        await settle(pilot)
        assert side.display and side.size.width == 20


async def test_changed_note_stays_over_the_document_as_b_resizes(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(120, 30)) as pilot:
        await started(pilot)
        frame = app.viewer.query_one("#doc-frame")
        for _ in range(3):
            app.action_toggle_sidebar()
            # Right in the first layout after it: nothing left to fix up later
            # (a frame drawn out of line flashes as the sidebar changes).
            app.screen._refresh_layout()
            doc = app.viewer.document.region
            assert (frame.region.x, frame.region.width) == (doc.x, doc.width)


async def test_b_hides_straight_away_when_the_names_fit(folder: Path) -> None:
    from tmr import state

    state.remember_sidebar_percent(60.0)
    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        await pilot.press("b")
        await settle(pilot)
        assert not app.query_one("#side").display
        await pilot.press("b")
        await settle(pilot)
        assert app.query_one("#side").size.width == 60


async def test_markdown_is_listed_without_its_md(folder: Path) -> None:
    (folder / "notes.txt").write_text("plain\n")
    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        await pilot.press("a")
        await settle(pilot)
        shown = [app.tree.render_line(y).text.strip() for y in range(app.tree.size.height)]
        assert "README" in shown and "README.md" not in shown
        assert "notes.txt" in shown, "other files keep their extension"
        assert "README.md" in tree_labels(app), "only the drawing leaves it off"


async def test_folder_at_top_of_sidebar(folder: Path) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(100, 30)) as pilot:
        await started(pilot)
        assert str(app.query_one("#side-title").render()) == f"📁 {folder.name}"
