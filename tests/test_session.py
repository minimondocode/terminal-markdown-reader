"""The open file's model (no app), saving safely, and editing as a mode."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

import tmr.session
from tmr.files import MAX_TEXT_BYTES, write_atomic
from tmr.session import NotEditable, OpenFile, describe_tokens

# --- saving safely --------------------------------------------------------------


def _leftovers(folder: Path) -> list[str]:
    return [name for name in os.listdir(folder) if name.endswith(".tmr-save")]


def test_write_atomic_replaces_the_contents_and_keeps_permissions(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("old\n")
    path.chmod(0o640)
    write_atomic(path, b"new\r\nlines\n")
    assert path.read_bytes() == b"new\r\nlines\n", "bytes exactly as given"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert _leftovers(tmp_path) == []


def test_write_atomic_writes_through_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("old\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    write_atomic(link, b"new\n")
    assert link.is_symlink(), "the link is still a link"
    assert real.read_text() == "new\n"


def test_write_atomic_makes_a_new_file_with_the_usual_permissions(tmp_path: Path) -> None:
    path = tmp_path / "fresh.md"
    mask = os.umask(0o022)
    try:
        write_atomic(path, b"hello\n")
    finally:
        os.umask(mask)
    assert path.read_text() == "hello\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_write_atomic_leaves_the_file_alone_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plan.md"
    path.write_text("old\n")

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_atomic(path, b"new\n")
    assert path.read_text() == "old\n"
    assert _leftovers(tmp_path) == []


# --- the open file ----------------------------------------------------------------


def test_disk_changes_tell_touches_from_real_changes(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n")
    file = OpenFile()
    loaded = file.load(path)
    assert loaded is not None and loaded.text == "# Plan\n"
    assert file.status() == ("changed just now · ~2 tokens", None)

    os.utime(path)  # Touched, not changed.
    assert file.disk_changed() == "same"
    path.write_text("# Plan, changed\n")
    assert file.disk_changed() == "changed"
    path.unlink()
    assert file.disk_changed() == "deleted"
    assert file.status() == ("deleted", "-deleted")


def test_token_counts_are_rounded_estimates() -> None:
    cases = [
        (0, "~0 tokens"),
        (1, "~1 token"),
        (87, "~87 tokens"),
        (846, "~850 tokens"),
        (994, "~990 tokens"),
        (995, "~1k tokens"),
        (1_234, "~1.2k tokens"),
        (9_949, "~9.9k tokens"),
        (9_950, "~10k tokens"),
        (123_456, "~123k tokens"),
        (999_499, "~999k tokens"),
        (999_500, "~1M tokens"),
        (12_345_678, "~12.3M tokens"),
    ]
    for tokens, shown in cases:
        assert describe_tokens(tokens) == shown


def test_token_count_follows_the_file_and_the_editor(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("x" * 4_000)
    file = OpenFile()
    file.load(path)
    assert file.tokens == 1_000
    path.write_text("x" * 8_000)
    assert file.disk_changed() == "changed"
    file.load(path)
    assert file.status() == ("changed just now · ~2k tokens", None)

    file.start_editing(file.editable_text(), "x" * 8_000)
    assert file.status("x" * 12_000) == ("editing · unsaved · ~3k tokens", "-editing")
    file.stop_editing()
    path.unlink()
    assert file.disk_changed() == "deleted"
    assert file.status() == ("deleted", "-deleted")

    (tmp_path / "data.bin").write_bytes(bytes(range(256)))
    file.load(tmp_path / "data.bin")
    assert file.tokens is None
    assert "token" not in file.status()[0]


def test_very_large_files_are_counted_from_their_size_not_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "huge.md"
    size = MAX_TEXT_BYTES * 5
    path.write_bytes(b"x" * size)
    file = OpenFile()
    loaded = file.load(path)
    assert loaded is not None and loaded.truncated
    assert loaded.text is not None and len(loaded.text) == MAX_TEXT_BYTES
    assert file.tokens == size // 4
    # Touched, with the part that's read unchanged but more added past it.
    with path.open("ab") as handle:
        handle.write(b"x" * 4_000_000)
    assert file.disk_changed() == "same"
    assert file.tokens == (size + 4_000_000) // 4


def test_welcome_page_has_no_file() -> None:
    file = OpenFile()
    assert file.load(None) is None
    assert file.title == "Welcome"
    assert file.disk_changed() == "none"
    with pytest.raises(NotEditable, match="Open a file first"):
        file.editable_text()


def test_only_text_files_can_be_edited(tmp_path: Path) -> None:
    file = OpenFile()
    cases = [
        ("data.bin", bytes(range(256)), "isn't text"),
        ("latin.md", "caf\xe9\n".encode("latin-1"), "isn't UTF-8"),
    ]
    for name, data, reason in cases:
        (tmp_path / name).write_bytes(data)
        file.load(tmp_path / name)
        with pytest.raises(NotEditable, match=reason):
            file.editable_text()
    (tmp_path / "gone.md").write_text("x")
    file.load(tmp_path / "gone.md")
    (tmp_path / "gone.md").unlink()
    with pytest.raises(NotEditable, match="deleted"):
        file.editable_text()


def test_ticking_touches_only_the_line_clicked(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    shown = "# Tasks\n\n- [ ] One\n- [ ] Two\n"
    path.write_text(shown)
    file = OpenFile()
    file.load(path)
    assert file.tick(2, shown) == "ticked"
    assert path.read_text() == "# Tasks\n\n- [x] One\n- [ ] Two\n"
    assert file.tick(0, shown) == "not a task"
    # Something else changed line 3 since it was drawn: leave it be.
    path.write_text("# Tasks\n\n- [x] One\n- [ ] Two, renamed\n")
    assert file.tick(3, shown) == "changed"
    assert path.read_text() == "# Tasks\n\n- [x] One\n- [ ] Two, renamed\n"


def _editing(tmp_path: Path, text: str = "one\ntwo\nthree\n") -> tuple[OpenFile, Path]:
    path = tmp_path / "plan.md"
    path.write_text(text)
    file = OpenFile()
    file.load(path)
    file.start_editing(file.editable_text(), text)
    return file, path


def test_saving_plain_edits(tmp_path: Path) -> None:
    file, path = _editing(tmp_path)
    assert file.status("one\ntwo\nthree\n") == ("editing · ~4 tokens", "-editing")
    assert file.unsaved("one\ntwo!\nthree\n")
    assert file.status("one\ntwo!\nthree\n") == ("editing · unsaved · ~4 tokens", "-editing")
    plan = file.plan_save("one\ntwo!\nthree\n")
    assert (plan.kind, plan.message) == ("write", "Saved")
    file.write(plan.text)
    file.mark_saved(plan.text)
    assert path.read_text() == "one\ntwo!\nthree\n"
    assert not file.unsaved("one\ntwo!\nthree\n")
    # Our own write coming back through the watcher isn't "changed on disk".
    assert file.disk_changed() == "editing"
    assert file.edit is not None and not file.edit.changed_on_disk


def test_saving_waits_then_merges_changes_made_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file, path = _editing(tmp_path)
    path.write_text("one\ntwo\nthree\nfour, by an agent\n")
    assert file.disk_changed() == "editing"
    assert file.edit is not None and file.edit.changed_on_disk
    assert file.status() == ("changed on disk · wait to save · ~4 tokens", "-waiting")
    mine = "ONE\ntwo\nthree\n"
    assert file.plan_save(mine).kind == "busy"
    assert path.read_text() == "one\ntwo\nthree\nfour, by an agent\n", "nothing written"

    monkeypatch.setattr(tmr.session, "QUIET_SECONDS", 0)
    assert file.status() == ("changed on disk · saving merges · ~4 tokens", "-waiting")
    plan = file.plan_save(mine)
    assert plan.kind == "write"
    assert plan.text == "ONE\ntwo\nthree\nfour, by an agent\n"
    assert plan.message == "Saved, keeping the changes made on disk too"


def test_saving_over_the_same_lines_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tmr.session, "QUIET_SECONDS", 0)
    file, path = _editing(tmp_path)
    path.write_text("one\nTWO (agent)\nthree\n")
    plan = file.plan_save("one\ntwo (me)\nthree\n")
    assert plan.kind == "conflict"
    assert plan.first_conflict_line == 1
    assert "<<<<<<< your edits\ntwo (me)\n=======\nTWO (agent)\n>>>>>>> changes on disk\n" in plan.text
    assert path.read_text() == "one\nTWO (agent)\nthree\n"
    assert file.edit is not None and file.edit.base == "one\nTWO (agent)\nthree\n"
    # The markers have to go before it saves.
    assert file.plan_save(plan.text).kind == "markers"
    assert file.plan_save("one\nTWO (both)\nthree\n").kind == "write"


def test_saving_a_deleted_file_brings_it_back(tmp_path: Path) -> None:
    file, path = _editing(tmp_path)
    path.unlink()
    plan = file.plan_save("one\n")
    assert plan.kind == "write" and "it's back" in plan.message
    file.write(plan.text)
    assert path.read_text() == "one\n"


def test_quiet_period_counts_from_the_files_own_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the watcher says so, a file that changed a moment ago is still busy.
    file, path = _editing(tmp_path)
    path.write_text("changed\n")
    assert file.disk_busy()
    old = time.time() - 60
    os.utime(path, (old, old))
    assert not file.disk_busy()


# --- editing as a mode ------------------------------------------------------------


async def test_own_writes_dont_redraw_again(tmp_path: Path, watching: threading.Event) -> None:
    """Ticking a task redraws once; the watcher then seeing the write changes nothing."""
    from helpers import settle, started, wait_until

    from tmr.app import TmrApp
    from tmr.markdown import Document

    root = tmp_path / "project"
    root.mkdir()
    readme = root / "README.md"
    readme.write_text("# Tasks\n\n- [ ] Write the plan\n")
    app = TmrApp(root.resolve())
    readme = readme.resolve()
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, watching.is_set, what="the watcher")
        opened = []
        real_open = app.viewer.open

        async def counting_open(*args, **kwargs):
            opened.append(args)
            await real_open(*args, **kwargs)

        app.viewer.open = counting_open
        app.tree.changed.pop(readme, None)
        app.viewer.document.post_message(Document.TaskToggled(2))
        await wait_until(pilot, lambda: "[x]" in readme.read_text(), what="the tick")
        assert readme.read_text() == "# Tasks\n\n- [x] Write the plan\n"
        await wait_until(pilot, lambda: readme in app.tree.changed, timeout=10, what="the watcher to see the write")
        await settle(pilot)
        assert len(opened) == 1, "drawn once for the tick, not again for the watcher"
