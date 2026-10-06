"""Editing in place (`e`): typing, saving, and merging with changes made on disk."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from helpers import (
    GUIDE,
    README,
    block_containing,
    editor_of,
    messages,
    settle,
    started,
    wait_until,
)
from textual.widgets import Static

from tmr.app import Hints, TmrApp
from tmr.editor import (
    UnsavedChanges,
    has_conflict_markers,
    inline_highlights,
    merge,
)
from tmr.finder import FileFinder
from tmr.outline import Outline


def test_merge_keeps_changes_to_different_lines() -> None:
    base = "one\ntwo\nthree\nfour\nfive\n"
    mine = "one\nTWO\nthree\nfour\nfive\n"
    theirs = "one\ntwo\nthree\nfour\nFIVE\nsix\n"
    assert merge(base, mine, theirs) == ("one\nTWO\nthree\nfour\nFIVE\nsix\n", True)
    # Neighbouring lines, changed by one side each, are fine too.
    assert merge(base, mine, "one\ntwo\nTHREE\nfour\nfive\n") == ("one\nTWO\nTHREE\nfour\nfive\n", True)
    # The same change made by both is just that change.
    assert merge(base, mine, mine) == (mine, True)
    # Nothing changed on one side: the other side wins.
    assert merge(base, base, theirs) == (theirs, True)
    assert merge(base, mine, base) == (mine, True)


def test_merge_marks_lines_both_changed() -> None:
    base = "one\ntwo\nthree"
    merged, clean = merge(base, "one\nmine\nthree", "one\ntheirs\nthree")
    assert not clean
    assert merged == (
        "one\n<<<<<<< your edits\nmine\n=======\ntheirs\n>>>>>>> changes on disk\nthree"
    )
    assert has_conflict_markers(merged)
    # Both adding different lines at the end is a conflict too: which goes first?
    merged, clean = merge("a\n", "a\nb\n", "a\nc\n")
    assert not clean and "b\n=======\nc\n" in merged


def test_markdown_symbols_are_coloured_within_lines() -> None:
    line = "Some **bold**, *it*, `**not bold**` and [a link](http://x.y)"
    names = {(line.encode()[start:end].decode(), name) for start, end, name in inline_highlights(line)}
    assert ("**", "markup.symbol") in names and ("bold", "bold") in names
    assert ("it", "italic") in names
    assert ("**not bold**", "inline_code") in names
    assert ("not bold", "bold") not in names
    assert ("a link", "link.label") in names and ("http://x.y", "link.uri") in names
    task = {name for _, _, name in inline_highlights("- [ ] todo")}
    assert task == {"list.marker"}


async def test_i_edits_the_markdown_as_written_and_saves_it(folder: Path, watching: threading.Event) -> None:
    readme = folder / "README.md"
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, watching.is_set, what="the watcher")
        await pilot.press("e")
        await settle(pilot)
        editor = editor_of(app)
        assert editor.has_focus
        assert editor.text == README, "the markdown as written, symbols and all"
        assert "**demo**" in editor.text
        assert not app.viewer.scroller.display
        assert "save" in str(app.query_one(Hints).render())
        assert str(app.query_one("#doc-age", Static).render()) == "editing · ~77 tokens"

        # Letters type, rather than working as keys (q would quit, s would search).
        editor.move_cursor((0, len("# Project Loft")))
        await pilot.press("space", "q", "s")
        await settle(pilot)
        assert editor.document[0] == "# Project Loft qs"
        assert app.is_running
        assert str(app.query_one("#doc-age", Static).render()) == "editing · unsaved · ~78 tokens"
        assert readme.read_text() == README, "nothing is saved until Ctrl+S"

        app.tree.changed.pop(readme, None)
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert readme.read_text() == README.replace("# Project Loft", "# Project Loft qs")
        assert "Saved" in messages(app)
        assert str(app.query_one("#doc-age", Static).render()) == "editing · ~78 tokens"

        # Our own save coming back through the file watcher isn't "changed on disk".
        await wait_until(pilot, lambda: readme in app.tree.changed, timeout=10, what="the watcher to see the save")
        await settle(pilot)
        assert app.viewer.file.edit is not None and not app.viewer.file.edit.changed_on_disk

        await pilot.press("escape")
        await wait_until(pilot, lambda: app.viewer.file.edit is None and app.viewer.editor is None)
        await wait_until(pilot, lambda: "Project Loft qs" in block_containing(app, "Project Loft").plain)
        assert app.viewer.scroller.display
        assert "search" in str(app.query_one(Hints).render())


@pytest.mark.parametrize("width", [120, 220])
async def test_editing_keeps_the_text_as_wide_as_reading(folder: Path, width: int) -> None:
    app = TmrApp(folder)
    async with app.run_test(size=(width, 40)) as pilot:
        await started(pilot)
        reading = block_containing(app, "Final paragraph").content_region
        await pilot.press("e")
        await settle(pilot)
        editing = editor_of(app).content_region
        assert (editing.x, editing.width) == (reading.x, reading.width)


async def test_editing_and_going_back_keeps_the_view_where_it_was(folder: Path) -> None:
    long = folder / "long.md"
    long.write_text("\n".join(f"## Part {n}\n\nSome words about part {n}.\n" for n in range(40)))
    notes = folder / "notes.txt"
    notes.write_text("".join(f"line {n}\n" for n in range(100)))
    app = TmrApp(folder)
    async with app.run_test(size=(120, 30)) as pilot:
        await started(pilot)
        for path in (long, notes):
            await app.open_file(path)
            await settle(pilot)
            # The very top, the blank line above a heading, the heading itself,
            # and partway into a block: each comes back exactly as it was.
            for y in (0, 12, 13, 14, 15):
                app.viewer.scroller.scroll_to(y=y, animate=False)
                await settle(pilot)
                await pilot.press("e")
                await settle(pilot)
                await pilot.press("escape")
                await wait_until(pilot, lambda: app.viewer.file.edit is None)
                await settle(pilot)
                assert app.viewer.scroller.scroll_y == y, (path.name, y)


async def test_leaving_with_unsaved_changes_asks_first(folder: Path) -> None:
    readme = folder / "README.md"
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("e")
        await settle(pilot)
        await pilot.press("x")
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, UnsavedChanges)

        # Esc again: keep editing.
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, UnsavedChanges)
        assert editor_of(app).text.startswith("x# Project")

        # Opening another file asks too; discarding leaves the file alone and opens it.
        await app.open_file(folder / "docs" / "guide.md")
        await settle(pilot)
        assert isinstance(app.screen, UnsavedChanges)
        await pilot.press("d")
        await wait_until(pilot, lambda: app.viewer.path == folder / "docs" / "guide.md")
        assert app.viewer.file.edit is None
        assert readme.read_text() == README

        # Ctrl+Q while editing stops editing (asking first), rather than closing tmr.
        await pilot.press("e")
        await settle(pilot)
        await pilot.press("y")
        await pilot.press("ctrl+q")
        await settle(pilot)
        assert isinstance(app.screen, UnsavedChanges)
        await pilot.press("s")
        await wait_until(pilot, lambda: app.viewer.file.edit is None)
        assert app.is_running
        assert (folder / "docs" / "guide.md").read_text() == "y" + GUIDE
        # And with nothing unsaved, it goes straight back to reading.
        await pilot.press("e")
        await settle(pilot)
        await pilot.press("ctrl+q")
        await wait_until(pilot, lambda: app.viewer.file.edit is None)
        assert app.is_running
        # Out of editing, q quits as always.
        await pilot.press("q")
        await wait_until(pilot, lambda: not app.is_running)


async def test_editor_copies_and_pastes_with_the_system_clipboard(
    folder: Path, clipboard: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command == ["pbpaste"]:
            return subprocess.CompletedProcess(command, 0, stdout=b"from elsewhere ")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name.startswith("pb") else None)
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("e")
        await settle(pilot)
        editor = editor_of(app)
        editor.selection = ((0, 2), (0, 9))
        await pilot.press("ctrl+c")
        assert clipboard == ["Project"]
        editor.move_cursor((0, 2))
        await pilot.press("ctrl+v")
        assert editor.document[0] == "# from elsewhere Project Loft"


async def test_reading_keys_are_off_while_editing(folder: Path) -> None:
    from tmr.viewer import SearchBar

    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("e")
        await settle(pilot)
        assert app.editing
        # Even away from the editor, reading keys do nothing (not even complain).
        app.tree.focus()
        await pilot.press("s", "h", "b", "slash")
        await settle(pilot)
        assert not app.query_one(SearchBar).has_class("-visible")
        assert not isinstance(app.screen, (Outline, FileFinder))
        assert app.query_one("#side").display
        assert messages(app) == []
        # Nor do clicks on their hints (as the hints bar would run them).
        assert not await app.run_action("search")
        assert not await app.run_action("copy_path")
        assert not app.query_one(SearchBar).has_class("-visible")
        # Ctrl+S works wherever the focus is, while editing.
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert messages(app) == ["Saved"]
        # Esc goes back to reading from anywhere; nothing was changed, so it doesn't ask.
        await pilot.press("escape")
        await wait_until(pilot, lambda: app.viewer.file.edit is None)
        assert not app.editing
        # Back to reading: the editing keys are off, the reading keys on again.
        assert not await app.run_action("save_edit")
        await pilot.press("s")
        await settle(pilot)
        assert app.query_one(SearchBar).has_class("-visible")


async def test_saving_waits_while_the_file_is_being_changed(
    folder: Path, monkeypatch: pytest.MonkeyPatch, watching: threading.Event
) -> None:
    import tmr.session

    readme = folder / "README.md"
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, watching.is_set, what="the watcher")
        await pilot.press("e")
        await settle(pilot)
        editor = editor_of(app)
        editor.move_cursor((0, len("# Project Loft")))
        await pilot.press("!")

        # An agent adds a line at the end while we type.
        by_agent = README + "\nAdded by an agent.\n"
        readme.write_text(by_agent)
        await wait_until(pilot, lambda: app.viewer.file.edit.changed_on_disk, timeout=10)
        assert editor.document[0] == "# Project Loft!", "what we typed is untouched"
        assert "wait to save" in str(app.query_one("#doc-age", Static).render())

        await pilot.press("ctrl+s")
        await settle(pilot)
        assert readme.read_text() == by_agent, "saving waits until the file is quiet"
        assert "still being changed" in messages(app)[0]

        # Once it has been quiet for a while, saving keeps both sets of changes.
        monkeypatch.setattr(tmr.session, "QUIET_SECONDS", 0)
        await pilot.press("ctrl+s")
        await settle(pilot)
        both = by_agent.replace("# Project Loft", "# Project Loft!")
        assert readme.read_text() == both
        assert editor.text == both
        assert messages(app) == ["Saved, keeping the changes made on disk too"]


async def test_saving_over_the_same_lines_shows_both_versions(
    folder: Path, monkeypatch: pytest.MonkeyPatch, watching: threading.Event
) -> None:
    import tmr.session

    monkeypatch.setattr(tmr.session, "QUIET_SECONDS", 0)
    readme = folder / "README.md"
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, watching.is_set, what="the watcher")
        await pilot.press("e")
        await settle(pilot)
        editor = editor_of(app)
        editor.move_cursor((0, len("# Project Loft")))
        await pilot.press("!")
        by_agent = README.replace("# Project Loft", "# Project Attic")
        readme.write_text(by_agent)
        await wait_until(pilot, lambda: app.viewer.file.edit.changed_on_disk, timeout=10)

        await pilot.press("ctrl+s")
        await settle(pilot)
        assert readme.read_text() == by_agent, "nothing saved"
        assert editor.text.startswith(
            "<<<<<<< your edits\n# Project Loft!\n=======\n# Project Attic\n>>>>>>> changes on disk\n"
        )

        # Saving with the markers still in is refused.
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert readme.read_text() == by_agent
        assert "Pick between" in messages(app)[0]

        # Pick one; now it saves.
        editor.replace("# Project Attic!\n", (0, 0), (5, 0))
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert readme.read_text() == by_agent.replace("# Project Attic", "# Project Attic!")
        assert messages(app) == ["Saved"]


async def test_files_that_cant_be_edited_here(folder: Path) -> None:
    (folder / "latin.md").write_bytes("caf\xe9\n".encode("latin-1"))
    app = TmrApp(folder)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        for name, reason in [
            ("data.bin", "isn't text"),
            ("latin.md", "isn't UTF-8"),
        ]:
            await app.open_file(folder / name)
            await settle(pilot)
            await pilot.press("e")
            await settle(pilot)
            assert app.viewer.file.edit is None
            assert reason in messages(app)[0]

        # Other text files can be edited, with colours for their language.
        await app.open_file(folder / "script.py")
        await settle(pilot)
        await pilot.press("e")
        await settle(pilot)
        assert editor_of(app).language == "python"


def test_o_prefers_tmr_editor_then_visual_then_editor(monkeypatch) -> None:
    from tmr.app import editor_command

    for name in ("TMR_EDITOR", "VISUAL", "EDITOR"):
        monkeypatch.delenv(name, raising=False)
    assert editor_command() == []
    monkeypatch.setenv("EDITOR", "nano")
    assert editor_command() == ["nano"]
    monkeypatch.setenv("VISUAL", "code --wait")
    assert editor_command() == ["code", "--wait"]
    monkeypatch.setenv("TMR_EDITOR", "vim")
    assert editor_command() == ["vim"]
    monkeypatch.setenv("TMR_EDITOR", "  ")
    assert editor_command() == ["code", "--wait"]
