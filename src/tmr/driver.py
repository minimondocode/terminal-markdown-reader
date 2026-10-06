"""Talk to the terminal in whole character cells, never pixels.

Textual normally switches the terminal to report mouse positions in pixels
(mode 1016) once the terminal says it can send size reports (mode 2048), and
only starts converting pixels back to cells when the first size report
arrives. Some terminals say they support size reports but don't send one
when the mode is switched on, so they send pixel positions while Textual
reads them as cells: every click and scroll lands far off-screen and the
mouse seems dead. Cell positions are all tmr needs, so it never asks for pixels.

Textual also leaves pixel mode switched on when it exits, which breaks the
next mouse program in that tab. We switch it off on the way in and out.
"""

from __future__ import annotations

import sys

from textual.driver import Driver

PIXEL_MOUSE_OFF = "\x1b[?1016l"


def cell_mouse_driver() -> type[Driver] | None:
    """The driver tmr should use, or None for Textual's default."""
    if sys.platform == "win32":
        return None
    from textual.drivers.linux_driver import LinuxDriver

    class CellMouseDriver(LinuxDriver):
        def _query_in_band_window_resize(self) -> None:
            # Don't ask for size reports; window resizes still arrive the usual way.
            pass

        def _enable_mouse_pixels(self) -> None:
            pass

        def _enable_mouse_support(self) -> None:
            if self._mouse:
                self.write(PIXEL_MOUSE_OFF)
            super()._enable_mouse_support()

        def _disable_mouse_support(self) -> None:
            if self._mouse:
                self.write(PIXEL_MOUSE_OFF)
            super()._disable_mouse_support()

    return CellMouseDriver
