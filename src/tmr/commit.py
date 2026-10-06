"""Committing the open file to git, without leaving tmr.

Only that one file goes into the commit: anything else changed or staged in
the repository stays as it was. It goes onto whatever branch is checked out,
and nothing is pushed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Input, Label

from tmr.keys import key_hints
from tmr.popup import Popup

GIT_TIMEOUT = 60
"""Seconds to wait for git (commit hooks can be slow)."""


class GitError(Exception):
    """Git couldn't do it; the message says why, ready to show."""


@dataclass
class Pending:
    """A file with changes git could commit."""

    path: Path
    repo: Path
    name: str
    """Where the file is within the repository, like "docs/guide.md"."""
    branch: str
    untracked: bool
    lines: tuple[int, int] | None = None
    """How many lines the commit adds and takes away, when git can say."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise GitError("git isn't installed.") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git took longer than {GIT_TIMEOUT} seconds, so tmr stopped waiting.") from None
    except OSError as error:
        raise GitError(f"Couldn't run git: {error.strerror}") from None


def _why(result: subprocess.CompletedProcess[str]) -> str:
    """What git said went wrong, kept short."""
    lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
    return "\n".join(lines[-4:]) or f"git stopped with code {result.returncode}."


def pending(path: Path) -> Pending:
    """What committing this file would take. Raises GitError if there's nothing to commit."""
    path = path.resolve()
    top = _git(path.parent, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise GitError(f"{path.name} isn't in a git repository.")
    repo = Path(top.stdout.strip())
    name = path.relative_to(repo.resolve()).as_posix()
    status = _git(repo, "status", "--porcelain", "--", name)
    if status.returncode != 0:
        raise GitError(_why(status))
    if not status.stdout.strip():
        raise GitError(f"Nothing to commit: {name} has no changes (or git ignores it).")
    branch = _git(repo, "branch", "--show-current").stdout.strip() or "a detached HEAD"
    untracked = status.stdout.startswith("??")
    return Pending(path, repo, name, branch, untracked, _lines_changed(repo, name, path, untracked))


def _lines_changed(repo: Path, name: str, path: Path, untracked: bool) -> tuple[int, int] | None:
    """(added, taken away): every line of a new file, or what git's diff counts."""
    if untracked:
        try:
            return len(path.read_text(errors="replace").splitlines()), 0
        except OSError:
            return None
    diff = _git(repo, "diff", "HEAD", "--numstat", "--", name)
    fields = diff.stdout.split("\t")
    if diff.returncode != 0 or len(fields) < 2 or not (fields[0].isdigit() and fields[1].isdigit()):
        return None  # No commit yet, or a binary file.
    return int(fields[0]), int(fields[1])


def commit(change: Pending, message: str) -> str:
    """Commit just this file. Returns the new commit's short hash."""
    if change.untracked:
        added = _git(change.repo, "add", "--", change.name)
        if added.returncode != 0:
            raise GitError(_why(added))
    result = _git(change.repo, "commit", "--only", "-m", message, "--", change.name)
    if result.returncode != 0:
        raise GitError(_why(result))
    return _git(change.repo, "rev-parse", "--short", "HEAD").stdout.strip()


class CommitMessage(Popup[str | None]):
    """Asks for the commit message, suggesting one. Returns it, or None to not commit."""

    DEFAULT_CSS = """
    CommitMessage {
        align: center middle;
    }
    CommitMessage > Vertical {
        width: 70;
        max-width: 90%;
        margin-top: 0;
    }
    CommitMessage #commit-search {
        margin: 1 0;
    }
    /* The line above the keys would sit just under the one below the message. */
    CommitMessage #commit-keys {
        border-top: none;
        height: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Don't commit", show=False),
    ]

    def __init__(self, change: Pending) -> None:
        super().__init__()
        self.change = change
        self.suggested = f"Update {change.name}"

    def compose(self) -> ComposeResult:
        box = Vertical()
        box.border_title = f"Commit to {self.change.branch}"
        with box:
            yield Label(self._what(), id="commit-file")
            with Horizontal(id="commit-search", classes="popup-search"):
                yield Label("›", classes="popup-prompt")
                yield Input(self.suggested, id="commit-message")
            yield Label(
                key_hints([("⏎", "commit"), ("esc", "don't commit")]),
                id="commit-keys",
                classes="popup-footer",
            )

    def _what(self) -> Content:
        """The file, and how many lines it adds and takes away: "docs/plan.md  +3 −1"."""
        what = Content.styled(self.change.name, "bold")
        if self.change.lines is not None:
            added, removed = self.change.lines
            what += Content("  ") + Content.styled(f"+{added}", "$success")
            what += Content(" ") + Content.styled(f"−{removed}", "$error")
        return what

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or self.suggested)

    def action_cancel(self) -> None:
        self.dismiss(None)
