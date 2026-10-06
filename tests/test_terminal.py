"""Getting along with terminals: the mouse, colours, font size, and the hints."""

from __future__ import annotations

import pytest

from tmr.app import _hint_markup


def test_mouse_positions_stay_in_cells_not_pixels() -> None:
    """In some terminals, pixel mouse mode made every click land off-screen."""
    from tmr.driver import PIXEL_MOUSE_OFF, cell_mouse_driver

    written: list[str] = []
    driver_class = cell_mouse_driver()
    driver = driver_class.__new__(driver_class)
    driver._mouse = True
    driver.write = written.append
    driver.flush = lambda: None

    driver._query_in_band_window_resize()
    driver._enable_mouse_pixels()
    assert not any("2048" in w or "1016h" in w for w in written)

    driver._enable_mouse_support()
    assert written[0] == PIXEL_MOUSE_OFF and "\x1b[?1000h" in written
    written.clear()
    driver._disable_mouse_support()
    assert PIXEL_MOUSE_OFF in written


def test_own_palette_swaps_only_the_sixteen_colours() -> None:
    from rich.segment import Segment
    from rich.style import Style
    from textual.color import Color

    from tmr.palette import DARK, LIGHT, Palette

    segments = [
        Segment("a", Style.parse("blue on bright_black")),
        Segment("b", Style.parse("default on default")),
        Segment("c", Style.parse("#123456")),
    ]
    dark = Palette(dark=True).apply(segments, Color(0, 0, 0))
    assert dark[0].style.color.triplet.hex == DARK[4]
    assert dark[0].style.bgcolor.triplet.hex == DARK[8]
    assert dark[1].style == segments[1].style, "the terminal's own text and background stay"
    assert dark[2].style == segments[2].style
    light = Palette(dark=False).apply(segments, Color(255, 255, 255))
    assert light[0].style.color.triplet.hex == LIGHT[4]


def test_own_palette_unless_the_terminals_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    from tmr.palette import use_own_palette

    monkeypatch.delenv("TMR_COLORS", raising=False)
    assert use_own_palette()
    monkeypatch.setenv("TMR_COLORS", "tmr")
    assert use_own_palette()
    monkeypatch.setenv("TMR_COLORS", "terminal")
    assert not use_own_palette()


def test_font_size_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    from tmr.fontsize import SIZE, wanted_size

    monkeypatch.delenv("TMR_FONT_SIZE", raising=False)
    assert wanted_size() == SIZE == 15
    monkeypatch.setenv("TMR_FONT_SIZE", "18")
    assert wanted_size() == 18
    for off in ("off", "0", "no"):
        monkeypatch.setenv("TMR_FONT_SIZE", off)
        assert wanted_size() is None
    monkeypatch.setenv("TMR_FONT_SIZE", "huge")
    assert wanted_size() == SIZE


def test_font_size_set_in_terminal_app_and_put_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from tmr import fontsize

    calls = []

    class Answer:
        def communicate(self, timeout=None):
            return "11\n", ""

    def osascript(tty, size, stdout):
        calls.append((tty, str(size)))
        return Answer()

    monkeypatch.setattr(fontsize, "_osascript", osascript)
    monkeypatch.setattr(fontsize.os, "ttyname", lambda fd: "/dev/ttys009")
    monkeypatch.delenv("TMR_FONT_SIZE", raising=False)

    # Other terminals are already the right size: left alone.
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    size = fontsize.FontSize()
    size.start()
    size.restore()
    assert calls == []

    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    size = fontsize.FontSize()
    size.start()
    assert calls == [("/dev/ttys009", "15")]
    size.restore()
    assert calls == [("/dev/ttys009", "15"), ("/dev/ttys009", "11")]
    size.restore()
    assert len(calls) == 2


def test_hints_leave_out_the_obvious_keys_when_narrow() -> None:
    wide = _hint_markup(200)
    narrow = _hint_markup(100)
    assert "switch pane" not in wide, "everyone tries Tab anyway"
    assert "outline" not in wide, "h has no hint, to leave room for the ones below"
    assert "sidebar" not in narrow and "o open" not in narrow
    assert "all files" in narrow and "hidden" in narrow and "quit" in narrow
    from textual.content import Content

    assert "o open" in Content.from_markup(wide).plain
    assert "[ ] back/forward" in Content.from_markup(wide).plain


def test_hints_keep_related_keys_together() -> None:
    from textual.content import Content

    assert Content.from_markup(_hint_markup(200)).plain == (
        "/ find file · [ ] back/forward · s search · e edit · o open · c copy all · p copy path"
        " · g commit · w worktree · b sidebar · a all files · . hidden · q quit"
    )
