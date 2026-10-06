"""The system clipboard, through whichever tool the system has.

macOS has `pbcopy`/`pbpaste`; Wayland desktops `wl-copy`/`wl-paste`; X11 ones
`xclip` or `xsel`. The first one found is used. Without any (over ssh, say),
copying is left to the terminal (Textual asks it to, with OSC 52), and
pasting gives back what tmr itself copied last.
"""

from __future__ import annotations

import shutil
import subprocess

TIMEOUT = 2
"""Seconds to wait for a clipboard tool: it should answer at once."""

COPY_COMMANDS: list[list[str]] = [
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
]

PASTE_COMMANDS: list[list[str]] = [
    ["pbpaste"],
    ["wl-paste", "--no-newline"],
    ["xclip", "-selection", "clipboard", "-o"],
    ["xsel", "--clipboard", "--output"],
]


def _available(commands: list[list[str]]) -> list[str] | None:
    for command in commands:
        if shutil.which(command[0]):
            return command
    return None


def copy(text: str) -> bool:
    """Put `text` on the system clipboard. Returns whether a tool did it."""
    command = _available(COPY_COMMANDS)
    if command is None:
        return False
    try:
        subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def paste() -> str | None:
    """What's on the system clipboard, or None if no tool can say."""
    command = _available(PASTE_COMMANDS)
    if command is None:
        return None
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", errors="replace")
