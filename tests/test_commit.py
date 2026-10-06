"""Committing the open file to git (`g`, and `Ctrl+G` while editing)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from helpers import (
    GUIDE,
    README,
    editor_of,
    messages,
    settle,
    started,
    wait_until,
)
from textual.widgets import Input

from tmr.app import Hints, TmrApp
from tmr.commit import CommitMessage


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


async def test_g_commits_only_the_open_file(repo: Path) -> None:
    (repo / "README.md").write_text(README + "\nMore.\n")
    (repo / "docs" / "guide.md").write_text(GUIDE + "\nStaged, not mine.\n")
    git(repo, "add", "docs/guide.md")
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        assert "commit" in str(app.query_one(Hints).render())
        await pilot.press("g")
        await wait_until(pilot, lambda: isinstance(app.screen, CommitMessage))
        assert app.screen.query_one(Input).value == "Update README.md"
        await pilot.press("enter")
        await wait_until(pilot, lambda: any(m.startswith("Committed README.md to main") for m in messages(app)))

    assert git(repo, "log", "-1", "--format=%s").strip() == "Update README.md"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["README.md"]
    # What was already staged for another file is still staged, not committed.
    assert git(repo, "diff", "--cached", "--name-only").split() == ["docs/guide.md"]


async def test_ctrl_g_saves_and_commits_while_editing(repo: Path) -> None:
    app = TmrApp(repo, start_file=repo / "docs" / "guide.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("e")
        await settle(pilot)
        await pilot.press("x")
        assert "save & commit" in str(app.query_one(Hints).render())
        await pilot.press("ctrl+g")
        await wait_until(pilot, lambda: isinstance(app.screen, CommitMessage))
        assert (repo / "docs" / "guide.md").read_text() == "x" + GUIDE, "saved before asking"
        app.screen.query_one(Input).value = "Tidy the guide"
        await pilot.press("enter")
        await wait_until(pilot, lambda: any(m.startswith("Committed docs/guide.md") for m in messages(app)))
        assert app.viewer.file.edit is not None and editor_of(app).has_focus, "still editing"

    assert git(repo, "log", "-1", "--format=%s").strip() == "Tidy the guide"
    assert git(repo, "status", "--porcelain", "--", "docs/guide.md") == ""


async def test_commit_can_be_called_off_and_says_when_it_cant(repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text(README + "\nMore.\n")
    app = TmrApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("g")
        await wait_until(pilot, lambda: isinstance(app.screen, CommitMessage))
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, CommitMessage)
        assert git(repo, "log", "--format=%s").split("\n")[0] == "Start"

        # A file new to git goes in too.
        await app.open_file(repo / "notes.txt")
        await settle(pilot)
        await pilot.press("g")
        await wait_until(pilot, lambda: isinstance(app.screen, CommitMessage))
        await pilot.press("enter")
        await wait_until(pilot, lambda: any(m.startswith("Committed notes.txt") for m in messages(app)))
        await pilot.press("g")
        await wait_until(pilot, lambda: any("no changes" in m for m in messages(app)))
        assert not isinstance(app.screen, CommitMessage)
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["notes.txt"]

    outside = tmp_path / "loose"
    outside.mkdir()
    (outside / "a.md").write_text("# A\n")
    app = TmrApp(outside, start_file=outside / "a.md")
    async with app.run_test(size=(120, 40)) as pilot:
        await started(pilot)
        await pilot.press("g")
        await wait_until(pilot, lambda: any("isn't in a git repository" in m for m in messages(app)))
