"""The file list behind the tree, find-a-file and file names in documents."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from helpers import messages, settle, started, tree_labels, wait_until
from watchfiles import Change

from tmr import index as index_module
from tmr.app import TmrApp
from tmr.index import FileIndex, build_index
from tmr.search import find_named


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git project with ignored build output, ignored notes, and clutter."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = (tmp_path / "project").resolve()
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("build/\ntemp/\nscratch.*\n")
    files = {
        "README.md": "# Hi\n",
        "docs/guide.md": "# Guide\n",
        "docs/diagram.txt": "boxes\n",
        "src/main.py": "print()\n",
        "build/out.js": "x\n",
        "build/deep/notes.md": "# Notes\n",
        "temp/draft.md": "# Draft\n",
        "temp/cache.bin": "x\n",
        "scratch.txt": "x\n",
        "scratch.md": "# Scratch\n",
        "node_modules/pkg/README.md": "# junk\n",
        ".claude/plan.md": "# hidden\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "empty").mkdir()
    return root


class GitCalls:
    """Records the git commands the file list runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.commands: list[list[str]] = []
        real = subprocess.run

        def recording(command, *args, **kwargs):
            if command and command[0] == "git":
                self.commands.append(list(command))
            return real(command, *args, **kwargs)

        monkeypatch.setattr(index_module.subprocess, "run", recording)

    def named(self, word: str) -> list[list[str]]:
        return [command for command in self.commands if word in command]


def relatives(index: FileIndex) -> list[str]:
    return [entry.relative for entry in index]


def test_one_rule_hidden_clutter_and_ignored(repo: Path) -> None:
    index = build_index(repo, show_hidden=False)
    assert relatives(index) == [
        "build/deep/notes.md",
        "docs/diagram.txt",
        "docs/guide.md",
        "README.md",
        "scratch.md",
        "src/main.py",
        "temp/draft.md",
    ]
    assert index.get("temp/draft.md").ignored and not index.get("docs/guide.md").ignored
    assert index.is_ignored("build/out.js") and index.is_ignored("scratch.txt")
    assert not index.is_ignored("docs")
    # Folders: ignored ones only for the markdown in them; ordinary ones even when empty.
    assert index.shows_folder("build", markdown_only=True)
    assert index.shows_folder("build", markdown_only=False)
    assert not index.shows_folder("src", markdown_only=True)
    assert index.shows_folder("src", markdown_only=False)
    assert index.shows_folder("empty", markdown_only=False)
    assert not index.shows_folder("node_modules", markdown_only=False)
    assert not index.shows_folder(".claude", markdown_only=True)

    everything = build_index(repo, show_hidden=True)
    names = relatives(everything)
    assert {"build/out.js", "scratch.txt", ".claude/plan.md", "node_modules/pkg/README.md"} <= set(names)
    assert everything.get(".claude/plan.md").hidden


async def expand_everything(app: TmrApp) -> None:
    tree = app.tree
    while True:
        waiting = []
        pending = [tree.root]
        while pending:
            node = pending.pop()
            if node.allow_expand and (node.data is None or not node.data.loaded):
                waiting.append(node)
            pending.extend(node.children)
        if not waiting:
            return
        for node in waiting:
            await tree.reload_node(node)


def listed_files(app: TmrApp) -> set[str]:
    tree = app.tree
    found = set()
    pending = [tree.root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        if node.data is not None and not node.allow_expand:
            found.add(node.data.path.relative_to(app.root).as_posix())
    return found


@pytest.mark.parametrize("markdown_only", [True, False])
async def test_tree_and_find_a_file_agree(repo: Path, markdown_only: bool) -> None:
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        if not markdown_only:
            await app.action_toggle_all_files()
        await expand_everything(app)
        index = app.file_index
        expected = {e.relative for e in index if e.markdown or not markdown_only}
        assert listed_files(app) == expected
        # Hidden files: both show them.
        await app.action_toggle_hidden()
        await wait_until(pilot, lambda: app.tree.index is not None and app.tree.index.show_hidden)
        await settle(pilot)
        await expand_everything(app)
        assert ".claude/plan.md" in listed_files(app)
        assert ".claude/plan.md" in relatives(app.file_index)


def test_changes_are_taken_in_without_gathering_again(repo: Path, monkeypatch) -> None:
    index = build_index(repo, show_hidden=False)
    calls = GitCalls(monkeypatch)

    # A file changes: its time is freshened, no git at all.
    guide = repo / "docs" / "guide.md"
    guide.write_text("# Guide, again\n")
    update = index.apply({(Change.modified, str(guide))})
    assert update.listed == {guide} and not update.rebuild
    assert index.get("docs/guide.md").modified == guide.stat().st_mtime
    assert calls.commands == []

    # A file goes: no git either, and its folder lists differently.
    guide.unlink()
    update = index.apply({(Change.deleted, str(guide))})
    assert update.removed == {guide} and repo / "docs" in update.folders
    assert index.get("docs/guide.md") is None
    assert calls.commands == []

    # A new file: git is only asked whether it's ignored.
    fresh = repo / "plans" / "fresh.md"
    fresh.parent.mkdir()
    fresh.write_text("# Fresh\n")
    update = index.apply({(Change.added, str(fresh))})
    assert index.shows_file("plans/fresh.md") and index.shows_folder("plans", markdown_only=True)
    assert fresh in update.listed and repo in update.folders
    assert calls.named("ls-files") == []

    # A deleted folder takes its files with it.
    (repo / "docs" / "diagram.txt").unlink()
    (repo / "docs").rmdir()
    index.apply({(Change.deleted, str(repo / "docs"))})
    assert not any(name.startswith("docs/") for name in relatives(index))
    assert not index.shows_folder("docs", markdown_only=True)


def test_new_ignored_folder_is_learned_once(repo: Path, monkeypatch) -> None:
    index = build_index(repo, show_hidden=False)
    (repo / ".gitignore").write_text("build/\ntemp/\nscratch.*\ntarget/\n")
    index = build_index(repo, show_hidden=False)
    calls = GitCalls(monkeypatch)
    target = repo / "target" / "debug"
    target.mkdir(parents=True)
    (target / "a.o").write_text("x")
    (target / "NOTES.md").write_text("# Build notes\n")
    changes = {(Change.added, str(repo / "target")), (Change.added, str(target / "a.o"))}
    index.apply(changes)
    assert index.shows_file("target/debug/NOTES.md") and not index.shows_file("target/debug/a.o")
    assert "target" in index.ignored_dirs
    assert index.get("target/debug/NOTES.md").ignored
    asked = len(calls.commands)

    # Files that follow into it are known to be ignored without asking git.
    (target / "b.o").write_text("x")
    index.apply({(Change.added, str(target / "b.o"))})
    assert len(calls.commands) == asked and not index.shows_file("target/debug/b.o")
    assert not index.wants_change(target / "b.o")
    assert index.wants_change(target / "MORE.md")
    assert calls.named("ls-files") == []


def test_new_ordinary_folder_skips_what_git_ignores_inside_it(repo: Path) -> None:
    index = build_index(repo, show_hidden=False)
    project = repo / "tools"
    (project / "build").mkdir(parents=True)
    (project / "README.md").write_text("# Tools\n")
    (project / "run.sh").write_text("x\n")
    (project / "build" / "out.js").write_text("x\n")
    index.apply({(Change.added, str(project))})
    assert {"tools/README.md", "tools/run.sh"} <= set(relatives(index))
    assert not index.shows_file("tools/build/out.js")


def test_gitignore_change_asks_for_a_fresh_list(repo: Path) -> None:
    index = build_index(repo, show_hidden=False)
    (repo / ".gitignore").write_text("build/\n")
    assert index.apply({(Change.modified, str(repo / ".gitignore"))}).rebuild


def test_huge_ignored_folder_is_looked_through_within_a_budget(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(index_module, "IGNORED_FOLDER_BUDGET", 50)
    for number in range(300):
        (repo / "build" / f"chunk{number:03}.o").write_text("")
    index = build_index(repo, show_hidden=False)
    assert "build" in index.partial_dirs
    assert not index.knows("build/deep") and index.knows("docs")
    assert index.shows_file("docs/guide.md")


def test_hidden_files_off_until_shown(repo: Path) -> None:
    index = build_index(repo, show_hidden=False)
    secret = repo / ".claude" / "other.md"
    secret.write_text("# x\n")
    update = index.apply({(Change.added, str(secret))})
    assert not update.changed and not index.shows_file(".claude/other.md")


def test_named_files_are_found_by_name(repo: Path) -> None:
    index = build_index(repo, show_hidden=False)
    near = repo / "src"
    assert find_named("notes.md", near, index) == repo / "build" / "deep" / "notes.md"
    assert find_named("deep/notes.md", near, index) == repo / "build" / "deep" / "notes.md"
    assert find_named("NOTES.md", near, index) == repo / "build" / "deep" / "notes.md"
    assert find_named("elsewhere/notes.md", near, index) is None


async def test_new_folders_appear_without_listing_everything_again(
    repo: Path, watching: threading.Event
) -> None:
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, watching.is_set, what="the watcher")
        assert "plans" not in tree_labels(app)

        async def no_full_reload():
            raise AssertionError("the whole tree was listed again")

        app.tree.reload = no_full_reload
        builds: list[bool] = []
        app.rebuild_file_index = lambda: builds.append(True)
        (repo / "plans" / "q3").mkdir(parents=True)
        (repo / "plans" / "q3" / "roadmap.md").write_text("# Roadmap\n")
        await wait_until(pilot, lambda: "plans" in tree_labels(app), timeout=10)
        assert app.file_index.shows_file("plans/q3/roadmap.md")
        (repo / "README.md").unlink()
        await wait_until(pilot, lambda: "README.md" not in tree_labels(app), timeout=10)
        assert builds == []


async def test_watcher_failure_is_told_and_restarted(repo: Path, monkeypatch) -> None:
    import watchfiles

    from tmr import app as app_module

    monkeypatch.setattr(app_module, "WATCH_RETRY_DELAYS", (0.1, 0.1, 0.1))
    starts: list[int] = []

    def flaky_watch(*paths, stop_event: threading.Event, **kwargs):
        starts.append(1)
        if len(starts) == 1:
            raise OSError("too many open files")
        yield set()
        stop_event.wait()

    monkeypatch.setattr(watchfiles, "watch", flaky_watch)
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: any("Live updates stopped" in n for n in messages(app)))
        assert "too many open files" in " ".join(messages(app))
        await wait_until(pilot, lambda: any("Live updates are back" in n for n in messages(app)))
        assert len(starts) == 2


async def test_watcher_gives_up_after_a_few_tries(repo: Path, monkeypatch) -> None:
    import watchfiles

    from tmr import app as app_module

    monkeypatch.setattr(app_module, "WATCH_RETRY_DELAYS", (0.05, 0.05))
    starts: list[int] = []

    def broken_watch(*paths, **kwargs):
        starts.append(1)
        raise OSError("no watching here")
        yield  # pragma: no cover

    monkeypatch.setattr(watchfiles, "watch", broken_watch)
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: any("Live updates are off" in n for n in messages(app)))
        assert len(starts) == 3


async def test_tree_lists_before_the_file_list_is_ready(repo: Path, monkeypatch) -> None:

    from tmr import app as app_module

    real = app_module.build_index
    ready = threading.Event()

    def held(*args, **kwargs):
        ready.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_index", held)
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        # The tree looks for itself meanwhile (an empty folder mustn't trip it up).
        await wait_until(pilot, lambda: "README.md" in tree_labels(app))
        assert app.file_index is None
        before = tree_labels(app)
        ready.set()
        await started(pilot)
        # The ignored build/ folder (which has notes in it) is listed either way.
        assert tree_labels(app) == before
