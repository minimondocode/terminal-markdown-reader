"""tmr: browse a folder and read its markdown files, beautifully, in the terminal."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from functools import cache
from pathlib import Path


@cache
def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("terminal_markdown_reader")
    except PackageNotFoundError:
        return "unknown"


class _ShowVersion(argparse.Action):
    """--version, looking the version up only when asked: that takes a moment."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        print(f"{parser.prog} {_version()}")
        parser.exit()


def parse_target(argument: str) -> tuple[Path, str]:
    """The path given on the command line, and the #heading after it (if any).

    "docs/plan.md#setup" opens plan.md at its "Setup" heading. A "#" is only
    read that way when nothing of that full name exists, so a file or folder
    with a "#" in its name still opens.
    """
    path = Path(argument).expanduser()
    if path.exists() or "#" not in argument:
        return path, ""
    before, _, anchor = argument.rpartition("#")
    return Path(before).expanduser(), anchor


def read_piped_input() -> Path | None:
    """When markdown is piped in (`cat plan.md | tmr`), keep it in a file to show,
    and take the keyboard back from the terminal. None when nothing is piped."""
    if sys.stdin.isatty():
        return None
    data = sys.stdin.buffer.read()
    folder = Path(tempfile.mkdtemp(prefix="tmr-"))
    file = folder / "stdin.md"
    file.write_bytes(data)
    try:
        tty = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return file
    os.dup2(tty, 0)
    os.close(tty)
    sys.stdin = sys.__stdin__ = os.fdopen(0, "r", closefd=False)
    return file


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tmr",
        description="Browse a folder and read its markdown files in the terminal.",
        epilog="Pipe markdown in to read it: cat notes.md | tmr",
    )
    parser.add_argument(
        "--version", action=_ShowVersion, nargs=0, default=argparse.SUPPRESS,
        help="show the version and exit",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="folder to browse, or a file to open, with an optional #heading (default: the current folder)",
    )
    parser.add_argument("-a", "--all", action="store_true", help="list all files, not just markdown (as the a key does)")
    parser.add_argument("--hidden", action="store_true", help="list hidden and git-ignored files too (as the . key does)")
    parser.add_argument("--no-watch", action="store_true", help="don't follow changes made to files on disk")
    parser.add_argument(
        "--colors", choices=("tmr", "terminal"), help="tmr's own 16 colours (default) or the terminal's (TMR_COLORS)",
    )
    parser.add_argument(
        "--font-size", metavar="POINTS",
        help="the size to set macOS Terminal's font to while tmr runs, or 'off' to leave it (TMR_FONT_SIZE)",
    )
    args = parser.parse_args()

    if args.colors:
        os.environ["TMR_COLORS"] = args.colors
    if args.font_size:
        os.environ["TMR_FONT_SIZE"] = args.font_size

    piped = read_piped_input() if args.path is None else None
    if piped is not None:
        target, anchor = piped, ""
    else:
        target, anchor = parse_target(args.path or ".")
        if not target.exists():
            parser.error(f"{args.path} doesn't exist")
    target = target.resolve()
    start_file = None
    if target.is_file():
        start_file = target
        target = target.parent

    # Bring the text to the size tmr reads best at. Terminal answers while
    # the rest starts up.
    from tmr.fontsize import FontSize

    font_size = FontSize()
    font_size.start()
    try:
        # Ask the terminal about itself before the full-screen app starts, when
        # its replies can still be read: its colours, and what pictures it can show.
        from tmr.terminal import is_dark_background, probe_picture_support

        dark = is_dark_background()
        probe_picture_support()

        from tmr.app import TmrApp
        from tmr.palette import use_own_palette

        TmrApp(
            target,
            dark=dark,
            start_file=start_file,
            start_anchor=anchor,
            own_palette=use_own_palette(),
            all_files=args.all,
            show_hidden=args.hidden,
            watch=not args.no_watch,
            sidebar=piped is None,
        ).run()
    finally:
        font_size.restore()
