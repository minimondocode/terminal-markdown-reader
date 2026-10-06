"""Read at the same size in every terminal.

tmr reads best at about 15 points, the size most terminals start at.
macOS Terminal starts out much smaller (11 points), so there tmr sets its tab
to 15 points while it runs and puts the tab's own size back when it quits.
Terminal takes a moment to answer, so this happens alongside startup rather
than holding it up; the screen is redrawn to fit once the new size arrives.

`TMR_FONT_SIZE` picks another size, or `TMR_FONT_SIZE=off` leaves the terminal
alone.
"""

from __future__ import annotations

import os
import subprocess
import sys

SIZE = 15

# Finds the Terminal tab on the given tty, sets its font size and returns the
# size it had, or returns nothing when no tab is on that tty (inside tmux, say).
_SET_SIZE = """
on run argv
    set wanted to (item 2 of argv) as number
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is (item 1 of argv) then
                    set previous to font size of t
                    if previous is not wanted then set font size of t to wanted
                    return previous
                end if
            end repeat
        end repeat
    end tell
end run
"""


def wanted_size() -> int | None:
    """The font size to read at, or None to leave the terminal alone."""
    choice = os.environ.get("TMR_FONT_SIZE", "").strip().lower()
    if choice in ("off", "0", "no", "false"):
        return None
    if choice:
        try:
            return int(float(choice)) if float(choice) > 0 else None
        except ValueError:
            pass
    return SIZE


class FontSize:
    """Sets the terminal's font size for as long as tmr runs."""

    def __init__(self) -> None:
        self._tty: str | None = None
        self._setting: subprocess.Popen[str] | None = None

    def start(self) -> None:
        """Start setting the size, without waiting for the terminal to answer."""
        size = wanted_size()
        if size is None or os.environ.get("TERM_PROGRAM") != "Apple_Terminal":
            return
        try:
            self._tty = os.ttyname(sys.stdout.fileno())
            self._setting = _osascript(self._tty, size, stdout=subprocess.PIPE)
        except OSError:
            self._setting = None

    def restore(self) -> None:
        """Put back the size the terminal had before, once tmr is done."""
        if self._setting is None or self._tty is None:
            return
        setting, self._setting = self._setting, None
        try:
            previous, _ = setting.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            setting.kill()
            return
        previous = previous.strip()
        if previous and previous != str(wanted_size()):
            # Not waited for: the prompt can come back while Terminal resizes.
            try:
                _osascript(self._tty, previous, stdout=subprocess.DEVNULL)
            except OSError:
                pass


def _osascript(tty: str, size: int | str, stdout: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["osascript", "-e", _SET_SIZE, tty, str(size)],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
