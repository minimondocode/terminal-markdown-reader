"""The open file: what's on show, and editing it in place.

No widgets here. `Viewer` shows what this says, and asks it what to do when
the file changes on disk, when a checkbox is ticked, and when an edit is saved
(which may mean merging with changes an agent made meanwhile).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from tmr.editor import MINE_MARKER, has_conflict_markers, merge
from tmr.files import MAX_TEXT_BYTES, read_file, write_atomic
from tmr.markdown import toggle_task

QUIET_SECONDS = 5.0
"""Saving waits until the file on disk has gone this long without changing."""

CHARS_PER_TOKEN = 4
"""A rough rule for English text: close enough to say whether a file is big for an agent."""


def relative_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return ""


def describe_change(modified: float) -> str:
    age = relative_age(max(0.0, time.time() - modified))
    if age:
        return f"changed {age}"
    when = datetime.fromtimestamp(modified)
    return f"changed {when.day} {when:%b %Y}"


def estimate_tokens(chars: int) -> int:
    """About how many tokens an agent reading this many characters takes in."""
    return -(-chars // CHARS_PER_TOKEN)


def describe_tokens(tokens: int) -> str:
    """Rounded, as it's only an estimate: ~850 tokens, ~1.2k, ~12k, ~1.2M."""
    if tokens >= 999_500:
        count = f"{tokens / 1_000_000:.1f}".removesuffix(".0") + "M"
    elif tokens >= 9_950:
        count = f"{round(tokens / 1000)}k"
    elif tokens >= 995:
        count = f"{tokens / 1000:.1f}".removesuffix(".0") + "k"
    elif tokens >= 100:
        count = str(round(tokens, -1))
    else:
        count = str(tokens)
    return f"~{count} token{'' if tokens == 1 else 's'}"


def _whole_file_tokens(text: str | None, truncated: bool, size: int) -> int | None:
    """Only the beginning of a very large file is read, so its size on disk says the rest."""
    if text is None:
        return None
    return estimate_tokens(size if truncated else len(text))


def read_text(path: Path) -> str:
    """The whole file, exactly as written (no newline translation)."""
    return path.read_bytes().decode("utf-8")


class NotEditable(Exception):
    """The file can't be edited here; the message says why, ready to show."""

    def __init__(self, message: str, severity: Literal["warning", "error"] = "warning") -> None:
        super().__init__(message)
        self.severity = severity


@dataclass
class EditSession:
    """A file being edited in place."""

    path: Path
    base: str
    """The file on disk when editing began, or when it was last saved."""
    saved: str
    """What the editor held at that moment; anything else is unsaved."""
    changed_on_disk: bool = False
    """Something else changed the file since then."""
    last_disk_change: float = float("-inf")
    """When we last saw it change (time.monotonic)."""


@dataclass
class Loaded:
    """A file read to be shown."""

    text: str | None
    """The text, or None when the file isn't text."""
    truncated: bool
    size: int
    modified: float


@dataclass
class SavePlan:
    """What saving should do, decided before anything is written."""

    kind: Literal["write", "conflict", "busy", "markers", "unreadable"]
    """`write`: write `text`. `conflict`: both changed the same lines; put
    `text` (with conflict markers) in the editor and write nothing. The rest
    write nothing and only say why (`message`)."""
    message: str
    text: str = ""

    @property
    def first_conflict_line(self) -> int:
        """The line the first conflict starts on (for a `conflict` plan)."""
        lines = self.text.splitlines()
        return lines.index(MINE_MARKER) if MINE_MARKER in lines else 0


Tick = Literal["ticked", "changed", "not a task"]


class OpenFile:
    """The file on show: its path, the text on show, and any editing of it."""

    def __init__(self) -> None:
        self.path: Path | None = None
        """None for the welcome page."""
        self.text: str | None = None
        """The text on show, as read, to tell real changes from touches (None if not text)."""
        self.modified: float | None = None
        """When the file last changed on disk (Unix time)."""
        self.tokens: int | None = None
        """About how many tokens the whole file is (None if not text)."""
        self.deleted = False
        self.edit: EditSession | None = None

    @property
    def editing(self) -> bool:
        return self.edit is not None

    # --- showing ------------------------------------------------------------

    def load(self, path: Path | None) -> Loaded | None:
        """Read a file to show it (None: the welcome page). Raises OSError."""
        self.path = path
        self.deleted = False
        self.text = None
        self.modified = None
        self.tokens = None
        if path is None:
            return None
        info = path.stat()
        contents = read_file(path)
        self.modified = info.st_mtime
        self.text = contents.text
        self.tokens = _whole_file_tokens(contents.text, contents.truncated, info.st_size)
        return Loaded(contents.text, contents.truncated, info.st_size, info.st_mtime)

    def disk_changed(self) -> Literal["none", "editing", "deleted", "same", "changed"]:
        """The watcher saw the file change. What does that mean for what's on show?

        `editing`: noted for the save to come; the editor is left alone.
        `deleted`: it's gone. `same`: only touched (or our own write coming
        back round); the time was freshened. `changed`: show it again.
        """
        if self.path is None:
            return "none"
        if self.edit is not None:
            self._changed_while_editing()
            return "editing"
        if not self.path.exists():
            self.deleted = True
            return "deleted"
        if self.text is not None:
            try:
                info = self.path.stat()
                contents = read_file(self.path)
            except OSError:
                return "changed"
            if contents.text == self.text:
                self.modified = info.st_mtime
                # Past the part that's read, a very large file may still have changed.
                self.tokens = _whole_file_tokens(contents.text, contents.truncated, info.st_size)
                self.deleted = False
                return "same"
        return "changed"

    # --- ticking tasks ------------------------------------------------------

    def tick(self, line: int, shown: str) -> Tick:
        """Tick or untick the task on this line of the file, as drawn from `shown`.

        The file may have changed since it was drawn, so only ever the line
        that was clicked, and only if it still reads the same. Raises OSError
        or UnicodeDecodeError when the file can't be read or written.
        """
        assert self.path is not None
        before = read_text(self.path)
        lines, shown_lines = before.split("\n"), shown.split("\n")
        if line >= len(lines) or line >= len(shown_lines) or lines[line] != shown_lines[line]:
            return "changed"
        after = toggle_task(before, line)
        if after is None:
            return "not a task"
        write_atomic(self.path, after.encode("utf-8"))
        return "ticked"

    # --- editing in place ---------------------------------------------------

    def editable_text(self) -> str:
        """The file as written, to edit it. Raises NotEditable saying why not."""
        if self.path is None:
            raise NotEditable("Open a file first.")
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            raise NotEditable("This file has been deleted.") from None
        except OSError as error:
            raise NotEditable(f"Couldn't open the file: {error.strerror}", "error") from None
        if b"\0" in data[:8192]:
            raise NotEditable("This file isn't text, so it can't be edited.")
        if len(data) > MAX_TEXT_BYTES:
            raise NotEditable("This file is too big to edit here. Press o to use your editor.")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise NotEditable(
                "This file isn't UTF-8 text, so editing it here could garble it. "
                "Press o to use your editor."
            ) from None

    def start_editing(self, text: str, in_editor: str) -> EditSession:
        """Begin editing: `text` as read from disk, `in_editor` as the editor holds it."""
        assert self.path is not None
        self.edit = EditSession(self.path, base=text, saved=in_editor)
        self.deleted = False
        return self.edit

    def stop_editing(self) -> None:
        self.edit = None

    def unsaved(self, in_editor: str) -> bool:
        return self.edit is not None and in_editor != self.edit.saved

    def _changed_while_editing(self) -> None:
        session = self.edit
        assert session is not None
        try:
            disk = read_text(session.path)
        except (OSError, UnicodeDecodeError):
            disk = None
        if disk == session.base:
            # Our own save coming back round, or a change that was undone.
            session.changed_on_disk = False
        else:
            session.changed_on_disk = True
            session.last_disk_change = time.monotonic()

    def disk_busy(self) -> bool:
        """Has the file changed on disk in the last few seconds?"""
        session = self.edit
        if session is None:
            return False
        if time.monotonic() - session.last_disk_change < QUIET_SECONDS:
            return True
        try:
            return time.time() - session.path.stat().st_mtime < QUIET_SECONDS
        except OSError:
            return False

    def plan_save(self, mine: str) -> SavePlan:
        """What saving the editor's text (`mine`) should do.

        If something else (an agent) changed the file meanwhile, this waits
        until the file has been quiet for a few seconds, then keeps both sets
        of changes; where both changed the same lines, nothing is written and
        the two versions are put side by side (from then on, the version on
        disk is what the editor's text is measured against).
        """
        session = self.edit
        assert session is not None
        if has_conflict_markers(mine):
            return SavePlan(
                "markers",
                "Pick between your edits and the changes on disk (the <<<<<<< … >>>>>>> part) first.",
            )
        try:
            disk: str | None = read_text(session.path)
        except FileNotFoundError:
            disk = None
        except (OSError, UnicodeDecodeError) as error:
            reason = getattr(error, "strerror", None) or "it isn't UTF-8 text any more"
            return SavePlan("unreadable", f"Couldn't read the file to save it: {reason}")

        if disk is None:
            return SavePlan("write", "Saved (the file had been deleted, so it's back)", mine)
        if disk == session.base:
            return SavePlan("write", "Saved", mine)
        # (If the watcher hasn't told us yet, the file's own time says how recent it was.)
        session.changed_on_disk = True
        if self.disk_busy():
            return SavePlan(
                "busy",
                "The file is still being changed (by an agent?). "
                "Keep editing, and save once it has been quiet for a few seconds.",
            )
        text, clean = merge(session.base, mine, disk)
        if not clean:
            session.base = disk
            session.changed_on_disk = False
            return SavePlan(
                "conflict",
                "Your edits and the changes on disk touch the same lines, so nothing was saved. "
                "Keep the version you want (between <<<<<<< and >>>>>>>) and save again.",
                text,
            )
        return SavePlan("write", "Saved, keeping the changes made on disk too", text)

    def write(self, text: str) -> None:
        """Write a `write` plan's text to the file, all at once. Raises OSError."""
        session = self.edit
        assert session is not None
        write_atomic(session.path, text.encode("utf-8"))
        session.base = text
        session.changed_on_disk = False
        self.deleted = False

    def mark_saved(self, in_editor: str) -> None:
        """The editor's text, as it is now, is what's saved."""
        if self.edit is not None:
            self.edit.saved = in_editor

    # --- the top right ------------------------------------------------------

    @property
    def title(self) -> str:
        return self.path.name if self.path is not None else "Welcome"

    def status(self, in_editor: str | None = None) -> tuple[str, str | None]:
        """What the top right of the document says, and the class to colour it by.

        While editing, `in_editor` is the editor's text: whether it's saved,
        and its size as you type.
        """
        status, kind = self._status(in_editor)
        tokens = self.tokens
        if self.edit is not None and in_editor is not None:
            tokens = estimate_tokens(len(in_editor))
        if status and tokens is not None and kind != "-deleted":
            status += " · " + describe_tokens(tokens)
        return status, kind

    def _status(self, in_editor: str | None) -> tuple[str, str | None]:
        session = self.edit
        if session is not None:
            if session.changed_on_disk:
                if self.disk_busy():
                    return "changed on disk · wait to save", "-waiting"
                return "changed on disk · saving merges", "-waiting"
            unsaved = in_editor is not None and self.unsaved(in_editor)
            return ("editing · unsaved" if unsaved else "editing"), "-editing"
        if self.path is None:
            return "", None
        if self.deleted:
            return "deleted", "-deleted"
        if self.modified is not None:
            return describe_change(self.modified), None
        return "", None

