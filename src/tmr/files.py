"""Decisions about files: what counts as markdown, clutter, text, and so on."""

from __future__ import annotations

import mimetypes
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdx"}

# Folders that are machinery rather than content. Hidden by default along
# with anything whose name starts with a dot.
CLUTTER_NAMES = {
    "node_modules",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    ".cache",
    ".DS_Store",
}

# Changes inside these are never interesting to the file watcher.
IGNORED_WATCH_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

MAX_TEXT_BYTES = 2_000_000
"""Text files larger than this are cut off rather than shown in full."""

MAX_HIGHLIGHT_BYTES = 400_000
"""Code files larger than this are shown without colours, to stay fast."""


def is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_SUFFIXES


def is_markdown_name(name: str) -> bool:
    """`is_markdown` for a plain name or relative path, skipping the cost of a Path."""
    return os.path.splitext(name)[1].lower() in MARKDOWN_SUFFIXES


def is_clutter(path: Path) -> bool:
    return is_clutter_name(path.name)


def is_clutter_name(name: str) -> bool:
    """`is_clutter` for a plain name, skipping the cost of a Path."""
    return name.startswith(".") or name in CLUTTER_NAMES


def contains_markdown(folder: Path, show_hidden: bool, budget: int = 5000) -> bool:
    """Does this folder (or anything below it) contain a markdown file?

    Gives up and says yes after looking at `budget` entries, so a huge
    folder never freezes the tree.
    """
    seen = 0
    for _current, dirs, files in os.walk(folder):
        if not show_hidden:
            dirs[:] = [d for d in dirs if not is_clutter_name(d)]
        for name in files:
            if is_markdown_name(name):
                if show_hidden or not is_clutter_name(name):
                    return True
        seen += len(dirs) + len(files)
        if seen > budget:
            return True
    return False


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def describe_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    suffix = path.suffix.lower().lstrip(".")
    if mime:
        kind = mime.split("/")[0]
        label = {
            "image": "Image",
            "video": "Video",
            "audio": "Audio",
            "font": "Font",
        }.get(kind)
        if label:
            return f"{label} ({suffix.upper()})" if suffix else label
        if mime == "application/pdf":
            return "PDF document"
        if mime in ("application/zip", "application/gzip", "application/x-tar"):
            return f"Archive ({suffix.upper()})"
    if suffix:
        return f"{suffix.upper()} file"
    return "File"


@dataclass
class FileContents:
    """What we could read from a file."""

    text: str | None
    """The text, or None when the file isn't text."""
    truncated: bool = False


def read_file(path: Path) -> FileContents:
    """Read a file as text if it looks like text."""
    with path.open("rb") as handle:
        data = handle.read(MAX_TEXT_BYTES + 1)
    truncated = len(data) > MAX_TEXT_BYTES
    data = data[:MAX_TEXT_BYTES]
    if b"\0" in data[:8192]:
        return FileContents(None)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        # A multi-byte character may have been cut at the size limit.
        if truncated and error.start > len(data) - 4:
            text = data[: error.start].decode("utf-8")
        else:
            try:
                text = data.decode("latin-1")
            except UnicodeDecodeError:
                return FileContents(None)
            if sum(ch < " " and ch not in "\t\r\n\f" for ch in text[:8192]) > 30:
                return FileContents(None)
    return FileContents(text, truncated)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace a file's contents all at once, so no reader ever sees half of it.

    The bytes go to a hidden file beside it first, which then takes its place.
    The file keeps its permissions; a symlink keeps pointing where it did (the
    file it points to is the one replaced). A file that doesn't exist yet is
    made with the usual permissions for a new file.
    """
    target = Path(os.path.realpath(path))
    try:
        mode: int | None = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmr-save"
    )
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode if mode is not None else 0o666 & ~_umask())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _umask() -> int:
    """The process's file-creation mask (reading it means setting it, so put it back)."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask
