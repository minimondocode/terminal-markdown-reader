"""Fast matching against the file list, for "find a file" and file names in documents.

Works the way editor quick-open boxes do: the file list (`tmr.index`) is
gathered once in the background, and each keystroke only does a quick
in-memory match that can be abandoned as soon as the next key arrives.
"""

from __future__ import annotations

import heapq
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from tmr.index import Entry, FileIndex, build_index

__all__ = ["Entry", "FileIndex", "Hit", "build_index", "find_named", "in_folder", "recent_folders", "search"]


@dataclass(frozen=True)
class Hit:
    entry: Entry
    score: float
    positions: tuple[int, ...]
    """Which characters of `entry.relative` matched, for highlighting."""


def recent_folders(entries: Iterable[Entry], *, markdown_only: bool = False, limit: int = 5) -> list[tuple[str, float]]:
    """The folders with the most recent changes, newest first, with when that was."""
    latest: dict[str, float] = {}
    for entry in entries:
        if markdown_only and not entry.markdown:
            continue
        folder = entry.folder
        if folder and entry.modified > latest.get(folder, 0.0):
            latest[folder] = entry.modified
    return sorted(latest.items(), key=lambda item: -item[1])[:limit]


def in_folder(entries: Iterable[Entry], folder: str | None) -> list[Entry]:
    """Only the files inside `folder` (at any depth), or all of them for None."""
    if not folder:
        return entries if isinstance(entries, list) else list(entries)
    prefix = folder + "/"
    return [entry for entry in entries if entry.relative.startswith(prefix)]


def _in_order(query: str, text: str, start: int) -> tuple[int, ...] | None:
    """Positions of the query's letters appearing in order in `text` from `start`, if they do."""
    positions = []
    at = start
    for char in query:
        at = text.find(char, at)
        if at == -1:
            return None
        positions.append(at)
        at += 1
    return tuple(positions)


def score(query: str, entry: Entry) -> Hit | None:
    """How well one lower-cased word matches a file, or None if it doesn't."""
    text = entry.folded
    name_start = entry.name_start

    # Best: the typed text appears as-is in the file's name, even better at its start.
    at = text.find(query, name_start)
    if at != -1:
        points = 1000 + (300 if at == name_start else 0)
        positions = tuple(range(at, at + len(query)))
    else:
        at = text.find(query)
        if at != -1:
            # Appears as-is in a folder name.
            points = 600
            positions = tuple(range(at, at + len(query)))
        else:
            # The letters appear in order, e.g. "rdme" in "readme".
            found = _in_order(query, text, name_start)
            points = 400
            if found is None:
                found = _in_order(query, text, 0)
                points = 200
            if found is None:
                return None
            positions = found
            gaps = positions[-1] - positions[0] + 1 - len(positions)
            points -= min(gaps * 5, 150)
    return Hit(entry, points, positions)


def search(
    query: str,
    entries: Iterable[Entry],
    *,
    markdown_only: bool = False,
    folder: str | None = None,
    limit: int = 60,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[Hit] | None:
    """The best matches for `query`. Returns None if cancelled part-way.

    Words separated by spaces must each match, in any order ("scanner page").
    With `folder`, only files inside that folder are considered. With nothing
    typed, the most recently changed files come back, newest first.
    """
    words = [word for word in query.casefold().split() if word]
    candidates = in_folder(entries, folder)
    if markdown_only:
        candidates = [e for e in candidates if e.markdown]
    if not words:
        # Nothing typed yet: the files that changed most recently, newest first.
        recent = heapq.nlargest(limit, candidates, key=lambda entry: entry.modified)
        return [Hit(entry, 0, ()) for entry in recent]
    hits: list[Hit] = []
    for index, entry in enumerate(candidates):
        if index % 2000 == 0 and cancelled():
            return None
        total = 0.0
        positions: set[int] = set()
        for word in words:
            hit = score(word, entry)
            if hit is None:
                break
            total += hit.score
            positions.update(hit.positions)
        else:
            # Prefer short paths (files near the top) over deeply nested ones.
            total -= len(entry.folded) * 0.5
            hits.append(Hit(entry, total, tuple(sorted(positions))))
    hits.sort(key=lambda hit: (-hit.score, hit.entry.folded))
    return hits[:limit]


def find_named(name: str, near: Path, entries: FileIndex | Iterable[Entry] | None) -> Path | None:
    """The markdown file a document means when it names "plan.md" or "docs/plan.md".

    First as a path from the document's folder or any folder above it (up to
    the top of its git project), then anywhere in the file list: the file
    whose path ends that way and sits closest to the document.
    """
    for folder in (near, *near.parents):
        candidate = folder / name
        if candidate.is_file():
            return candidate.resolve()
        if (folder / ".git").exists() or folder == Path.home():
            break
    if entries is None or ".." in Path(name).parts:
        return None
    ending = "/" + name.removeprefix("./").lower()
    if isinstance(entries, FileIndex):
        candidates: Iterable[Entry] = entries.markdown_named(ending.rsplit("/", 1)[1])
    else:
        candidates = entries
    matches = [
        entry
        for entry in candidates
        if entry.markdown and ("/" + entry.relative.lower()).endswith(ending)
    ]
    if not matches:
        return None

    def shared(entry: Entry) -> int:
        return len(os.path.commonpath([entry.path.parent, near]).split(os.sep))

    return max(matches, key=lambda entry: (shared(entry), -len(entry.relative))).path
