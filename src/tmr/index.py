"""Every file under the folder that tmr lists, kept up to date as files change.

One list behind the file tree, "find a file" and the file names documents
mention, with one rule for what's in it:

- Hidden and clutter files and folders (`.git`, `node_modules`, anything
  starting with a dot) are left out, until hidden files are shown (`.`).
- So is whatever git ignores, like build output, except markdown: notes often
  live in ignored scratch folders (`temp/`, `drafts/`), and they're what tmr
  is for. Showing hidden files shows ignored ones too.

The list is gathered once, in the background, and then changed a file at a
time as the watcher reports changes, rather than gathered again.
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from tmr.files import IGNORED_WATCH_PARTS, is_clutter_name, is_markdown_name

MAX_FILES = 200_000
"""The list stops growing at this many files (and is then known to be incomplete)."""

IGNORED_FOLDER_BUDGET = 20_000
"""Look through at most this many entries of each folder git ignores for markdown,
so a huge one (Rust's `target/`) can't hold the list up."""

IGNORED_TOTAL_BUDGET = 200_000
"""...and at most this many across all of them."""


@dataclass(frozen=True)
class Entry:
    path: Path
    relative: str
    folded: str
    """`relative`, lower-cased, for matching."""
    name_start: int
    """Where the file's own name begins inside `relative`."""
    markdown: bool
    modified: float = 0.0
    """When the file last changed (Unix time)."""
    ignored: bool = False
    """Git ignores it (it's listed because it's markdown, or hidden files are shown)."""
    hidden: bool = False
    """It's hidden or clutter, or inside such a folder (listed only when hidden files are shown)."""

    @property
    def folder(self) -> str:
        """The folder it's in, relative to the root ("" for the root itself)."""
        return self.relative[: max(0, self.name_start - 1)]


@dataclass
class Update:
    """What a batch of changes did to the list."""

    folders: set[Path] = field(default_factory=set)
    """Folders whose listing may have changed (files came or went below them)."""
    listed: set[Path] = field(default_factory=set)
    """Files the lists show that were added or changed."""
    removed: set[Path] = field(default_factory=set)
    """Files the lists showed that are gone."""
    rebuild: bool = False
    """What git ignores changed: the list has to be gathered again."""

    @property
    def changed(self) -> bool:
        return bool(self.folders or self.listed or self.removed)


def is_hidden(relative: str) -> bool:
    """Is this path (relative to the root) hidden or clutter, or inside such a folder?"""
    return any(is_clutter_name(part) for part in relative.split("/"))


def _ancestors(relative: str) -> Iterator[str]:
    """The folders a relative path is inside, nearest first, ending with the root ("")."""
    while relative:
        cut = relative.rfind("/")
        relative = relative[:cut] if cut != -1 else ""
        yield relative


def _modified(path: str) -> float | None:
    """When a file last changed, or None if it isn't a file (anymore)."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime if stat.S_ISREG(info.st_mode) else None


def _git(root: Path, args: list[str], paths: Iterable[str] = (), stdin: bytes | None = None) -> list[str] | None:
    """The NUL-separated names git prints, or None if git can't say (not a repository, say)."""
    command = ["git", *args, "-z"]
    paths = list(paths)
    if paths:
        command += ["--", *paths]
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, input=stdin, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # check-ignore says 1 when nothing is ignored.
    if result.returncode not in (0, 1) or (result.returncode == 1 and args[0] != "check-ignore"):
        return None
    return [name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def _walk(
    top: Path, show_hidden: bool, markdown_only: bool, budget: int, cancelled: Callable[[], bool]
) -> tuple[list[str], bool, int]:
    """Files below `top` (relative to it), whether all of it was looked through,
    and how many entries were looked at."""
    found: list[str] = []
    seen = 0
    for current, dirs, files in os.walk(top):
        if cancelled():
            return found, False, seen
        if not show_hidden:
            dirs[:] = [d for d in dirs if not is_clutter_name(d)]
        dirs.sort(key=str.casefold)
        base = os.path.relpath(current, top)
        for name in sorted(files, key=str.casefold):
            if not show_hidden and is_clutter_name(name):
                continue
            if markdown_only and not is_markdown_name(name):
                continue
            found.append(name if base == "." else f"{base}/{name}")
        seen += len(dirs) + len(files)
        if seen > budget:
            return found, False, seen
    return found, True, seen


@dataclass
class _Scan:
    """What looking through (part of) the folder found."""

    files: list[str] = field(default_factory=list)
    ignored_markdown: set[str] = field(default_factory=set)
    ignored_dirs: set[str] = field(default_factory=set)
    ignored_files: set[str] = field(default_factory=set)
    partial_dirs: set[str] = field(default_factory=set)
    in_git: bool = False
    complete: bool = True


def _scan(root: Path, show_hidden: bool, cancelled: Callable[[], bool], below: str = "") -> _Scan:
    """Look through the folder (or just the part `below`), following the rule above."""
    scan = _Scan()
    scope = [below] if below else []
    listed = None if show_hidden else _git(root, ["ls-files", "--cached", "--others", "--exclude-standard"], scope)
    if listed is None:
        names, whole, _ = _walk(root / below, show_hidden, False, MAX_FILES, cancelled)
        prefix = f"{below}/" if below else ""
        scan.files = [prefix + name for name in names]
        scan.complete = whole
        return scan

    scan.in_git = True
    scan.files = [name for name in listed if not is_hidden(name)]
    # --directory folds each ignored folder into one line, so git doesn't have to
    # wade through node_modules and the like; the folders worth a look are walked
    # below, for markdown only, within a budget.
    ignored = _git(root, ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], scope) or []
    budget = IGNORED_TOTAL_BUDGET
    for name in ignored:
        if cancelled():
            scan.complete = False
            break
        if is_hidden(name.rstrip("/")):
            continue
        if not name.endswith("/"):
            if is_markdown_name(name):
                scan.files.append(name)
                scan.ignored_markdown.add(name)
            else:
                scan.ignored_files.add(name)
            continue
        folder = name.rstrip("/")
        if below and not (folder == below or folder.startswith(below + "/")):
            folder = below  # git named a folder above: all of `below` is ignored.
        scan.ignored_dirs.add(folder)
        found, whole, seen = _walk(root / folder, False, True, min(IGNORED_FOLDER_BUDGET, budget), cancelled)
        budget = max(0, budget - seen)
        if not whole:
            scan.partial_dirs.add(folder)
        for markdown in found:
            scan.files.append(f"{folder}/{markdown}")
            scan.ignored_markdown.add(f"{folder}/{markdown}")
    return scan


class FileIndex:
    """The files tmr lists, with how many (and how many markdown) are below each folder.

    Changed on the app's thread, read from others (the tree lists folders in a
    thread, and documents look up file names in one), hence the lock.
    """

    def __init__(self, root: Path, show_hidden: bool) -> None:
        self.root = root
        self.show_hidden = show_hidden
        self.in_git = False
        self.complete = True
        """False when the list stopped short (too many files): only trust what's in it."""
        self.ignored_dirs: set[str] = set()
        """The outermost folders git ignores (relative)."""
        self.ignored_files: set[str] = set()
        """Files git ignores that aren't in the list (not markdown), outside those folders."""
        self.partial_dirs: set[str] = set()
        """Ignored folders only partly looked through (too big)."""
        self._entries: dict[str, Entry] = {}
        self._files_below: Counter[str] = Counter()
        self._markdown_below: Counter[str] = Counter()
        self._by_name: dict[str, set[str]] = {}
        """Markdown files by their lower-cased name, for finding the one a document mentions."""
        self._snapshot: list[Entry] | None = None
        self._lock = threading.RLock()

    # --- gathering ------------------------------------------------------------

    @classmethod
    def build(
        cls, root: Path, show_hidden: bool, cancelled: Callable[[], bool] = lambda: False
    ) -> FileIndex:
        """Look through the whole folder."""
        index = cls(root, show_hidden)
        scan = _scan(root, show_hidden, cancelled)
        index.in_git = scan.in_git
        index._take(scan, cancelled)
        return index

    def _take(self, scan: _Scan, cancelled: Callable[[], bool] = lambda: False) -> list[Entry]:
        """Add what a scan found. Returns the entries added."""
        with self._lock:
            self.ignored_dirs |= scan.ignored_dirs
            self.ignored_files |= scan.ignored_files
            self.partial_dirs |= scan.partial_dirs
            if not scan.complete:
                self.complete = False
        base = str(self.root)
        added = []
        for count, name in enumerate(sorted(set(scan.files), key=str.casefold)):
            if len(self._entries) >= MAX_FILES:
                self.complete = False
                break
            if count % 2000 == 0 and cancelled():
                self.complete = False
                break
            # Also drops files git remembers but that are gone from disk.
            modified = _modified(os.path.join(base, name))
            if modified is not None:
                entry = self._entry(name, modified, ignored=name in scan.ignored_markdown)
                self._add(entry)
                added.append(entry)
        return added

    def _entry(self, relative: str, modified: float, ignored: bool = False) -> Entry:
        return Entry(
            path=self.root / relative,
            relative=relative,
            folded=relative.casefold(),
            name_start=relative.rfind("/") + 1,
            markdown=is_markdown_name(relative),
            modified=modified,
            ignored=ignored,
            hidden=is_hidden(relative),
        )

    def _add(self, entry: Entry) -> None:
        with self._lock:
            old = self._entries.get(entry.relative)
            self._entries[entry.relative] = entry
            self._snapshot = None
            if old is not None:
                return
            for folder in _ancestors(entry.relative):
                self._files_below[folder] += 1
                if entry.markdown:
                    self._markdown_below[folder] += 1
            if entry.markdown:
                key = entry.relative[entry.name_start :].lower()
                self._by_name.setdefault(key, set()).add(entry.relative)

    def _remove(self, relative: str) -> list[Entry]:
        """Take a file, or everything below a folder, out of the list. Returns what went."""
        with self._lock:
            if relative in self._entries:
                gone = [self._entries[relative]]
            elif self._files_below.get(relative):
                prefix = relative + "/"
                gone = [entry for name, entry in self._entries.items() if name.startswith(prefix)]
            else:
                gone = []
            for entry in gone:
                del self._entries[entry.relative]
                for folder in _ancestors(entry.relative):
                    self._files_below[folder] -= 1
                    if not self._files_below[folder]:
                        del self._files_below[folder]
                    if entry.markdown:
                        self._markdown_below[folder] -= 1
                        if not self._markdown_below[folder]:
                            del self._markdown_below[folder]
                if entry.markdown:
                    names = self._by_name.get(entry.relative[entry.name_start :].lower())
                    if names is not None:
                        names.discard(entry.relative)
            if gone:
                self._snapshot = None
            prefix = relative + "/"
            for known in (self.ignored_dirs, self.ignored_files, self.partial_dirs):
                for name in [n for n in known if n == relative or n.startswith(prefix)]:
                    known.discard(name)
            return gone

    # --- reading --------------------------------------------------------------

    def snapshot(self) -> list[Entry]:
        """Every file in the list, in name order. Not changed afterwards: safe to keep."""
        with self._lock:
            if self._snapshot is None:
                self._snapshot = sorted(self._entries.values(), key=lambda entry: entry.folded)
            return self._snapshot

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.snapshot())

    def __len__(self) -> int:
        return len(self._entries)

    def relative(self, path: Path) -> str | None:
        """`path` relative to the root, as the list names it ("" for the root), or None if outside."""
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            return None
        return "" if relative == "." else relative

    def get(self, relative: str) -> Entry | None:
        return self._entries.get(relative)

    def knows(self, relative: str) -> bool:
        """Can the list answer for this path, or was it too big to look through?"""
        if not self.complete:
            return False
        with self._lock:
            return not any(
                relative == folder or relative.startswith(folder + "/") for folder in self.partial_dirs
            )

    def is_ignored(self, relative: str) -> bool:
        """Does git ignore this path (or a folder it's in), as far as the list knows?"""
        if not self.in_git or self.show_hidden:
            return False
        with self._lock:
            if relative in self.ignored_files or relative in self.ignored_dirs:
                return True
            return any(folder in self.ignored_dirs for folder in _ancestors(relative) if folder)

    def shows_file(self, relative: str) -> bool:
        return relative in self._entries

    def shows_folder(self, relative: str, markdown_only: bool) -> bool:
        """Should the tree list this folder?

        With markdown only, when there's markdown somewhere below it. Otherwise
        when anything listed is below it, or it's an ordinary (perhaps empty) folder.
        """
        if not self.show_hidden and is_hidden(relative):
            return False
        with self._lock:
            if markdown_only:
                return self._markdown_below.get(relative, 0) > 0
            return self._files_below.get(relative, 0) > 0 or not self.is_ignored(relative)

    def markdown_named(self, name: str) -> list[Entry]:
        """The markdown files with this name (any case), wherever they are."""
        with self._lock:
            return [self._entries[relative] for relative in self._by_name.get(name.lower(), ())]

    def wants_change(self, path: Path) -> bool:
        """Is a change to this path worth reading? Not inside machinery like `.git` or
        `node_modules`, and not to files git ignores that the list leaves out."""
        relative = self.relative(path)
        if relative is None:
            return False
        if IGNORED_WATCH_PARTS.intersection(relative.split("/")):
            return False
        if path.name == ".gitignore":
            return True
        return is_markdown_name(relative) or not self.is_ignored(relative)

    # --- keeping up with changes ------------------------------------------------

    def apply(self, changes: Iterable[tuple[object, str]]) -> Update:
        """Take in what the watcher saw (`watchfiles` changes), a file at a time.

        Only the paths named are looked at: a new file is looked up, a new folder
        is looked through. Git is asked only whether new paths are ignored.
        """
        from watchfiles import Change

        update = Update()
        new_files: list[str] = []
        new_dirs: list[str] = []
        for change, raw in changes:
            path = Path(raw)
            try:
                path = path.parent.resolve() / path.name
            except OSError:
                pass
            relative = self.relative(path)
            if not relative:
                continue
            if path.name == ".gitignore" and not is_hidden(relative.rpartition("/")[0] or "_"):
                update.rebuild = True
            if not self.show_hidden and is_hidden(relative):
                continue
            try:
                info = os.stat(path)
            except OSError:
                info = None
            if change == Change.deleted or info is None:
                gone = self._remove(relative)
                update.removed |= {entry.path for entry in gone}
                self._touch_folders(relative, update)
                continue
            if stat.S_ISDIR(info.st_mode):
                if change == Change.added and not self._files_below.get(relative):
                    new_dirs.append(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            entry = self._entries.get(relative)
            if entry is not None:
                if entry.modified != info.st_mtime:
                    self._add(replace(entry, modified=info.st_mtime))
                update.listed.add(entry.path)
                continue
            new_files.append(relative)

        if not (new_files or new_dirs):
            return update
        # New folders are looked through whole, so skip files already inside one.
        new_dirs = [d for d in sorted(set(new_dirs), key=len) if not any(d.startswith(o + "/") for o in new_dirs if o != d)]
        new_files = [f for f in set(new_files) if not any(f.startswith(d + "/") for d in new_dirs)]
        ignored = self._learn_ignored(new_files + new_dirs)

        for relative in new_dirs:
            if relative in ignored or self.is_ignored(relative):
                # (`_learn_ignored` has noted the folder as ignored.)
                found, whole, _ = _walk(self.root / relative, False, True, IGNORED_FOLDER_BUDGET, lambda: False)
                names = [f"{relative}/{name}" for name in found]
                scan = _Scan(files=names, ignored_markdown=set(names))
                if not whole:
                    scan.partial_dirs.add(relative)
            else:
                scan = _scan(self.root, self.show_hidden, lambda: False, below=relative)
            for entry in self._take(scan):
                update.listed.add(entry.path)
            self._touch_folders(relative, update)

        for relative in new_files:
            is_ignored = relative in ignored or self.is_ignored(relative)
            markdown = is_markdown_name(relative)
            if is_ignored and not markdown:
                with self._lock:
                    self.ignored_files.add(relative)
                continue
            modified = _modified(str(self.root / relative))
            if modified is None:
                continue
            entry = self._entry(relative, modified, ignored=is_ignored)
            self._add(entry)
            update.listed.add(entry.path)
            self._touch_folders(relative, update)
        return update

    def _touch_folders(self, relative: str, update: Update) -> None:
        """A path came or went: the folders it's in may list differently now."""
        for folder in _ancestors(relative):
            update.folders.add(self.root / folder if folder else self.root)

    def _learn_ignored(self, relatives: list[str]) -> set[str]:
        """Which of these new paths git ignores, asking git once for all of them.

        Folders above them that are new too are asked about as well, so that a
        new ignored folder (`target/`) is known from then on and the files that
        follow into it needn't be asked about.
        """
        if not self.in_git or self.show_hidden:
            return set()
        asking: set[str] = set()
        for relative in relatives:
            if self.is_ignored(relative):
                continue
            asking.add(relative)
            for folder in _ancestors(relative):
                if not folder or self._files_below.get(folder):
                    break
                asking.add(folder)
        if not asking:
            return set()
        names = sorted(asking)
        answer = _git(self.root, ["check-ignore", "--stdin"], stdin=("\0".join(names) + "\0").encode("utf-8", "surrogateescape"))
        ignored = set(answer or ())
        with self._lock:
            for name in sorted(ignored, key=lambda n: n.count("/")):
                inside_known = any(folder in self.ignored_dirs for folder in _ancestors(name) if folder)
                if not inside_known and (self.root / name).is_dir():
                    self.ignored_dirs.add(name)
        return ignored


def build_index(root: Path, show_hidden: bool, cancelled: Callable[[], bool] = lambda: False) -> FileIndex:
    """List every file under `root` that tmr should offer."""
    return FileIndex.build(root, show_hidden, cancelled)


def wants_change(index: FileIndex | None, root: Path, path: Path) -> bool:
    """Is a change to `path` worth reading? (See `FileIndex.wants_change`.)"""
    if index is not None:
        return index.wants_change(path)
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return not IGNORED_WATCH_PARTS.intersection(parts)
