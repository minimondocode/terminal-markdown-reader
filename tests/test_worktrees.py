"""The branch at the foot of the sidebar, and switching worktree (`w`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from helpers import GUIDE, messages, started, wait_until
from textual.widgets import Input

from tmr.app import TmrApp
from tmr.commit import CommitMessage
from tmr.worktrees import WorktreeLabel, WorktreePicker, place_of, worktrees


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(folder: Path) -> Path:
    git(folder, "init", "-q", "-b", "main")
    git(folder, "config", "user.name", "Test")
    git(folder, "config", "user.email", "test@example.com")
    git(folder, "config", "commit.gpgsign", "false")
    git(folder, "add", "README.md", "docs")
    git(folder, "commit", "-q", "-m", "Start")
    return folder


@pytest.fixture
def agent_tree(repo: Path) -> Path:
    """A second worktree beside the repository, on its own branch, as an agent might make."""
    tree = repo.parent / "project-agent"
    git(repo, "worktree", "add", "-q", "-b", "agent-fix", str(tree))
    return tree.resolve()


def foot(app: TmrApp) -> str:
    label = app.query_one(WorktreeLabel)
    return str(label.render()) if label.display else ""


def test_place_and_worktrees(repo: Path, agent_tree: Path) -> None:
    here = place_of(repo / "docs" / "guide.md")
    assert here is not None
    assert (here.top, here.branch, here.linked) == (repo, "main", False)
    there = place_of(agent_tree / "README.md")
    assert there is not None
    assert (there.top, there.branch, there.linked) == (agent_tree, "agent-fix", True)
    assert [(tree.path, tree.branch) for tree in worktrees(repo)] == [
        (repo, "main"),
        (agent_tree, "agent-fix"),
    ]
    git(agent_tree, "checkout", "-q", "--detach")
    detached = place_of(agent_tree)
    assert detached is not None and detached.branch is None
    assert detached.branch_label.startswith("no branch (")


def test_not_in_git(tmp_path: Path) -> None:
    assert place_of(tmp_path) is None


async def test_foot_shows_the_branch_and_follows_it(repo: Path) -> None:
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, lambda: "main" in foot(app), what="the branch")
        # The file list already names the folder, so just the branch.
        assert foot(app).strip() == "⎇ main"
        # An agent checks out another branch in the same folder.
        git(repo, "checkout", "-q", "-b", "other")
        await wait_until(pilot, lambda: "other" in foot(app), what="the new branch")
        assert any("now on other" in message for message in messages(app))


async def test_w_switches_to_the_same_file_in_another_worktree(repo: Path, agent_tree: Path) -> None:
    app = TmrApp(repo, start_file=repo / "docs" / "guide.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, lambda: "main" in foot(app), what="the branch")
        await pilot.press("w")
        await wait_until(pilot, lambda: isinstance(app.screen, WorktreePicker))
        picker = app.screen
        assert [tree.branch for tree in picker.shown] == ["main", "agent-fix"]
        # Starts on the worktree that isn't this one.
        assert picker.shown[picker.query_one("#worktree-results").highlighted].branch == "agent-fix"
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.root == agent_tree, what="the switch")
        await started(pilot)
        await wait_until(pilot, lambda: "agent-fix" in foot(app), what="the new branch")
        assert app.git_place is not None and app.git_place.linked
        assert foot(app).split("\n")[1].strip() == "worktree"
        assert app.viewer.path == agent_tree / "docs" / "guide.md"
        assert app.tree.path == agent_tree
        assert any(message.startswith("Switched to agent-fix") for message in messages(app))

        # Committing now goes to the agent's branch, not main.
        (agent_tree / "docs" / "guide.md").write_text(GUIDE + "\nFrom the worktree.\n")
        await pilot.press("g")
        await wait_until(pilot, lambda: isinstance(app.screen, CommitMessage))
        await pilot.press("enter")
        await wait_until(pilot, lambda: any(m.startswith("Committed docs/guide.md to agent-fix") for m in messages(app)))

        # And back again, typing to narrow the list.
        await pilot.press("w")
        await wait_until(pilot, lambda: isinstance(app.screen, WorktreePicker))
        app.screen.query_one(Input).value = "main"
        await wait_until(pilot, lambda: [tree.branch for tree in app.screen.shown] == ["main"])
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.root == repo, what="switching back")
        await started(pilot)
        assert app.viewer.path == repo / "docs" / "guide.md"

    assert git(agent_tree, "log", "-1", "--format=%s").strip() == "Update docs/guide.md"
    assert git(repo, "log", "-1", "--format=%s").strip() == "Start"


async def test_foot_names_the_folder_when_the_file_list_doesnt(repo: Path) -> None:
    app = TmrApp(repo / "docs")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, lambda: "main" in foot(app), what="the branch")
        assert foot(app).split("\n")[1].strip() == "project"


async def test_clicking_the_foot_opens_the_list(repo: Path) -> None:
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await wait_until(pilot, lambda: "main" in foot(app), what="the branch")
        await pilot.click(WorktreeLabel)
        await wait_until(pilot, lambda: isinstance(app.screen, WorktreePicker))
        assert "only worktree" in str(app.screen.query_one("#worktree-count").render())
        await pilot.press("enter")
        await wait_until(pilot, lambda: any(m.startswith("Already in project") for m in messages(app)))


async def test_no_foot_or_worktrees_outside_git(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "a.md").write_text("# A\n")
    app = TmrApp(loose, start_file=loose / "a.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert foot(app) == ""
        await pilot.press("w")
        await wait_until(pilot, lambda: any("isn't in a git repository" in m for m in messages(app)))
