"""Ask the terminal about itself before Textual takes over the screen."""

from __future__ import annotations

import os
import re
import select
import sys
import time

_OSC11_REPLY = re.compile(rb"\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")
_DEVICE_ATTRIBUTES_REPLY = re.compile(rb"\x1b\[\?[0-9;]*c")


def is_dark_background(timeout: float = 0.3) -> bool:
    """Ask the terminal for its background colour and judge if it is dark.

    Falls back to dark when the terminal doesn't answer. Every terminal answers
    a request for its device attributes, so asking for those too, after the
    colour, means one that doesn't know the colour question is done at once
    rather than waiting out the timeout.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    try:
        import termios
        import tty
    except ImportError:
        return True

    fd = sys.stdin.fileno()
    try:
        previous = termios.tcgetattr(fd)
    except termios.error:
        return True
    reply = b""
    try:
        tty.setraw(fd)
        os.write(sys.stdout.fileno(), b"\x1b]11;?\x1b\\\x1b[c")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.02)
            if ready:
                reply += os.read(fd, 256)
                if _DEVICE_ATTRIBUTES_REPLY.search(reply):
                    break
    except OSError:
        return True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    match = _OSC11_REPLY.search(reply)
    if match is None:
        return True
    channels = []
    for part in match.groups():
        channels.append(int(part, 16) / (16 ** len(part) - 1))
    red, green, blue = channels
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return luminance < 0.5


def probe_picture_support() -> None:
    """Have the image library ask the terminal what pictures it can show.

    The answer is kept for when a document first has a picture. Asking alone,
    rather than importing the image widgets (which does the same), leaves their
    loading until a picture needs them.
    """
    try:
        from textual_image._terminal import probe_terminal
    except ImportError:
        # Its insides moved: importing the widgets asks too.
        import textual_image.widget  # noqa: F401

        return
    try:
        probe_terminal()
    except Exception:  # A probe: whatever goes wrong, pictures are just not shown.
        # TerminalError when the terminal can't say; it has also divided by a
        # window size of zero, as on a pseudo-terminal with no size set.
        pass
