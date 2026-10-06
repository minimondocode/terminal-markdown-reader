"""Which branch the open file is on, and switching to another worktree.

A worktree is a folder with one of the repository's branches checked out; a
repository can have several (agents often work in one of their own). The foot
of the sidebar says which branch and folder tmr is looking at, since that's
where committing (`g`) goes, and `w` lists every worktree to switch between.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from tmr.commit import GitError, _git, _why
from tmr.keys import key_hints
from tmr.popup import Popup

BRANCH_MARK = "⎇"


@dataclass(frozen=True)
class Place:
    """Where a file is, as far as git goes."""

    top: Path
    """The worktree's folder."""
    branch: str | None
    """The branch checked out there; None when it's no branch (a "detached HEAD")."""
    head: str
    """The commit checked out, shortened ("383a55f"); empty before the first commit."""
    linked: bool
    """An extra worktree (made with `git worktree add`), not the repository's own folder."""
    head_file: Path
    """git's note of what's checked out there, which changes when the branch does."""

    @property
    def branch_label(self) -> str:
        return self.branch or f"no branch ({self.head})"


@dataclass(frozen=True)
class Worktree:
    """One of a repository's worktrees."""

    path: Path
    branch: str | None
    head: str

    @property
    def branch_label(self) -> str:
        return self.branch or f"no branch ({self.head})"


def place_of(path: Path) -> Place | None:
    """The worktree and branch a file or folder is in; None if it isn't in git."""
    folder = path if path.is_dir() else path.parent
    try:
        dirs = _git(
            folder, "rev-parse", "--path-format=absolute",
            "--show-toplevel", "--git-dir", "--git-common-dir",
        )
    except GitError:
        return None
    lines = dirs.stdout.split("\n")
    if dirs.returncode != 0 or len(lines) < 3 or not lines[0]:
        return None  # Not in git, or inside the .git folder itself.
    top, git_dir, common_dir = (Path(line).resolve() for line in lines[:3])
    branch = _git(folder, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = _git(folder, "rev-parse", "--short", "HEAD")
    return Place(
        top=top,
        branch=branch.stdout.strip() or None if branch.returncode == 0 else None,
        head=head.stdout.strip() if head.returncode == 0 else "",
        linked=git_dir != common_dir,
        head_file=git_dir / "HEAD",
    )


def read_head(place: Place) -> str | None:
    """What git's HEAD note says now (cheap enough to check every few seconds)."""
    try:
        return place.head_file.read_text()
    except OSError:
        return None


def worktrees(top: Path) -> list[Worktree]:
    """The repository's worktrees that are there to switch to, its own folder first."""
    listed = _git(top, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        raise GitError(_why(listed))
    found = []
    for block in listed.stdout.strip().split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.split("\n") if line)
        if "worktree" not in fields or "bare" in fields or "prunable" in fields:
            continue
        path = Path(fields["worktree"])
        if not path.is_dir():
            continue
        branch = fields.get("branch", "").removeprefix("refs/heads/") or None
        found.append(Worktree(path.resolve(), branch, fields.get("HEAD", "")[:7]))
    return found


def home_short(path: Path) -> str:
    """A folder as people write it: "~/src/tmr" rather than "/Users/me/src/tmr"."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def short_folders(paths: list[Path]) -> dict[Path, str]:
    """Each folder from the one they all share onwards ("tmr", "tmr-fix", or
    "tmr/.claude/worktrees/fix"): the part that tells them apart, not the way there."""
    if not paths:
        return {}
    base = Path(os.path.commonpath(paths)).parent
    return {path: path.relative_to(base).as_posix() for path in paths}


def _fit(line: Content, width: int) -> Content:
    return line.truncate(max(0, width), ellipsis=True)


class WorktreeLabel(Static):
    """The foot of the sidebar: the branch the open file is on, and in which folder."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    WorktreeLabel {
        height: auto;
        padding: 0 2 1 2;
        /* A line at the top of its row. */
        border-top: hkey ansi_bright_black;
        display: none;
    }
    WorktreeLabel.-in-git {
        display: block;
    }
    WorktreeLabel:hover {
        background: $boost;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.place: Place | None = None
        self.root: Path | None = None
        """The folder the file list shows, whose name is already at its top."""

    def show(self, place: Place | None, root: Path) -> None:
        self.place, self.root = place, root
        self.set_class(place is not None, "-in-git")
        if place is not None:
            self.tooltip = (
                f"Commits (g) go to {place.branch_label}, in {home_short(place.top)}\n"
                "Click or press w to switch worktree"
            )
        self.refresh(layout=True)

    def on_resize(self, event: events.Resize) -> None:
        self.refresh()

    def render(self) -> Content:
        place = self.place
        if place is None:
            return Content()
        width = self.content_size.width
        branch = Content.styled(f"{BRANCH_MARK} ", "$text-muted") + Content.styled(
            place.branch_label, "bold $accent"
        )
        lines = [_fit(branch, width)]
        if place.top == self.root:
            # The file list's own folder, named at its top: only say if it's a worktree.
            if place.linked:
                lines.append(Content.styled("  worktree", "$text-muted"))
        else:
            # Somewhere the file list doesn't name (tmr was started in a subfolder,
            # or the file is in another repository): say which folder, and
            # "worktree" too when there's room for both.
            folder = Content.styled(f"  {place.top.name}", "$text-muted")
            if place.linked:
                both = Content.styled(f"  worktree · {place.top.name}", "$text-muted")
                folder = both if both.cell_length <= width else folder
            lines.append(_fit(folder, width))
        return Content("\n").join(lines)

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        await self.run_action("app.switch_worktree")


class WorktreePicker(Popup[Worktree | None]):
    """A pop-up list of the repository's worktrees; pick one to switch to it."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
    ]

    def __init__(self, trees: list[Worktree], here: Path) -> None:
        super().__init__()
        self.trees = trees
        self.here = here
        self.shown: list[Worktree] = []
        self.folders = short_folders([tree.path for tree in trees])

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="worktree-search", classes="popup-search"):
                yield Label("›", id="worktree-prompt", classes="popup-prompt")
                yield Input(placeholder="Type part of a branch or folder name…", id="worktree-input")
            yield OptionList(id="worktree-results")
            with Horizontal(id="worktree-footer", classes="popup-footer"):
                yield Label(key_hints([("⏎", "switch"), ("esc", "close")]), id="worktree-keys", classes="popup-keys")
                yield Label(self._count(), id="worktree-count", classes="popup-count")

    def _count(self) -> str:
        if len(self.trees) <= 1:
            return "This is the only worktree"
        return f"{len(self.trees)} worktrees"

    def on_mount(self) -> None:
        self.query_one(Vertical).border_title = "Switch worktree"
        self.query_one("#worktree-results", OptionList).can_focus = False
        self.refilter()

    def _room(self) -> int:
        """How many columns a row has, clear of the box's border, padding and scrollbar."""
        return min(100, int(self.app.size.width * 0.8)) - 7

    def _row(self, tree: Worktree, branch_width: int) -> Content:
        here = tree.path == self.here
        mark = Content.styled("● ", "$success") if here else Content("  ")
        branch = _fit(Content.styled(tree.branch_label, "bold"), branch_width)
        row = mark + branch + " " * (branch_width - branch.cell_length + 2)
        tail = Content.styled("  you're here", "$success") if here else Content()
        # A long folder loses its start, since its end says the most.
        folder = self.folders[tree.path]
        room = self._room() - row.cell_length - tail.cell_length
        if len(folder) > room:
            folder = "…" + folder[len(folder) - max(0, room - 1):]
        return row + Content.styled(folder, "$text-muted") + tail

    def on_resize(self) -> None:
        self.refilter()

    @on(Input.Changed, "#worktree-input")
    def refilter(self) -> None:
        words = self.query_one("#worktree-input", Input).value.lower().split()
        self.shown = [
            tree
            for tree in self.trees
            if all(word in f"{tree.branch_label} {self.folders[tree.path]}".lower() for word in words)
        ]
        branch_width = min(30, max((len(tree.branch_label) for tree in self.shown), default=0))
        results = self.query_one("#worktree-results", OptionList)
        results.set_options([Option(self._row(tree, branch_width)) for tree in self.shown])
        if self.shown:
            # Start on the first worktree that isn't this one: switching is the point.
            elsewhere = [at for at, tree in enumerate(self.shown) if tree.path != self.here]
            results.highlighted = elsewhere[0] if elsewhere and not words else 0
        elif self.trees:
            self.query_one("#worktree-count", Label).update("Nothing matches")
            return
        self.query_one("#worktree-count", Label).update(self._count())

    def action_move(self, step: int) -> None:
        results = self.query_one("#worktree-results", OptionList)
        if self.shown:
            current = results.highlighted or 0
            results.highlighted = max(0, min(len(self.shown) - 1, current + step))

    @on(Input.Submitted, "#worktree-input")
    def _submitted(self) -> None:
        results = self.query_one("#worktree-results", OptionList)
        if results.highlighted is not None and self.shown:
            self.dismiss(self.shown[results.highlighted])

    @on(OptionList.OptionSelected, "#worktree-results")
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.shown[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_click(self, event: events.Click) -> None:
        # Clicking outside the box closes it.
        if event.widget is self:
            self.dismiss(None)
