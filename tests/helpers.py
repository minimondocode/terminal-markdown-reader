"""Helpers shared by the tests that drive the app: waiting for it, and looking at it.

The tests wait for what they need to have happened (a file shown, a message
given, the file list gathered, the app gone quiet) rather than sleeping for
a guessed length of time: faster when things are quick, and still right
when they're slow.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from textual.pilot import Pilot

from tmr.app import TmrApp
from tmr.editor import Editor
from tmr.finder import FileFinder
from tmr.markdown import TextBlock
from tmr.tree import FileTree
from tmr.viewer import Viewer

LONG_RUNNING = {"watch", "_loader"}
"""Worker groups that run as long as the app does: the file watcher, and the
tree's queue of folders to list. Everything else finishes, and is waited for."""


async def wait_until(
    pilot: Pilot, condition: Callable[[], object], timeout: float = 5.0, what: str = ""
) -> None:
    """Wait for `condition()` to come true, checking whenever the app goes idle."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting{f' for {what}' if what else ''}")
        await pilot.pause()


def busy(app: TmrApp) -> str | None:
    """What the app is still doing, or None when it has nothing left to do."""
    for worker in app.workers:
        if worker.group not in LONG_RUNNING and not worker.is_finished:
            return f"worker {worker.name or worker.group!r}"
    if app._index_building:
        return "gathering the file list"
    if app._pending_glance is not None:
        return "showing the file the cursor moved onto"
    if app._change_lock.locked():
        return "taking in changes"
    # The main screen, even with a pop-up (find-a-file, say) on top of it.
    base = app.screen_stack[0] if app.screen_stack else None
    if base is not None:
        for tree in base.query(FileTree):
            if tree._load_queue.qsize() or tree.lock.is_locked:
                return "listing folders"
        for viewer in base.query(Viewer):
            if viewer._pending_search is not None:
                return "search waiting for the typing to pause"
    if isinstance(app.screen, FileFinder) and app.screen._pending is not None:
        return "find-a-file waiting for the typing to pause"
    return None


async def settle(pilot: Pilot, timeout: float = 5.0) -> None:
    """Wait until the app has finished what it's doing: workers done, folders
    listed, changes taken in, and nothing waiting for typing to pause.

    It has to look quiet twice in a row, so work that one step schedules
    for the next (after a refresh, say) is caught too.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    quiet = 0
    while quiet < 2:
        await pilot.pause()
        doing = busy(pilot.app)
        if doing is None:
            quiet += 1
            continue
        quiet = 0
        if loop.time() > deadline:
            raise AssertionError(f"the app is still busy: {doing}")


async def started(pilot: Pilot) -> None:
    """Wait until startup is over: the first file shown, and the file list gathered."""
    app = pilot.app
    await wait_until(pilot, lambda: app.file_index is not None, what="the file list")
    await settle(pilot)


def tree_labels(app: TmrApp) -> list[str]:
    tree = app.tree
    return [
        str(node.label)
        for line in range(tree.last_line + 1)
        if (node := tree.get_node_at_line(line)) is not None
    ]


def messages(app: TmrApp) -> list[str]:
    """The messages showing in the corner."""
    return [str(notification.message) for notification in app._notifications]


def block_containing(app: TmrApp, text: str) -> TextBlock:
    for block in app.viewer.document.query(TextBlock):
        if text in block.plain:
            return block
    raise AssertionError(f"no block contains {text!r}")


def editor_of(app: TmrApp) -> Editor:
    editor = app.viewer.editor
    assert editor is not None, "not editing"
    return editor


def spot(block: TextBlock, text: str, extra: int = 0) -> tuple[int, int]:
    """Where to click to hit `text` in a block (`extra` characters into it)."""
    x, y = block.locate(text)
    return block.content_region.x - block.region.x + x + extra, block.content_region.y - block.region.y + y


README = """\
# Project Loft

A **demo** with a [link to the guide](docs/guide.md#setup) and a [missing link](nope.md).

![A test picture](pic.png)

## Tasks

- [ ] Write the plan
- [x] Pick a name

```python
# - [ ] not a checkbox inside code
print("loft")
```

Final paragraph mentioning loft again, and Loft once more.
"""
"""The `folder` fixture's README.md."""

GUIDE = """\
# Guide

## Setup

Go [back to the readme](../README.md).
"""
"""The `folder` fixture's docs/guide.md."""
