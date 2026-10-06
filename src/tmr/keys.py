"""Every key tmr answers to, in one place.

The app's key bindings, the hints along the bottom, and the key table on the
welcome page are all made from this list, and a test holds the README's table
to it too, so a key can't be added (or described) in one place and not the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from textual.binding import Binding
from textual.content import Content

Mode = Literal["reading", "editing", "searching"]


@dataclass(frozen=True)
class Key:
    shown: str
    """The key as the hints show it: "/", "[", "^S", "esc"."""
    binding: str | None = None
    """Textual's name for the key, when tmr binds it app-wide ("slash")."""
    action: str | None = None
    """The app action it runs ("find_file", "next_match(1)")."""
    name: str = ""
    """What the binding does, in a word or two ("Find file")."""
    hint: str | None = None
    """Its label in the hints along the bottom; None leaves it out of them."""
    clickable: bool = True
    """Whether clicking its hint runs the action."""
    drop: int | None = None
    """When the hints don't fit, lower numbers go first; None never goes."""
    help: str | None = None
    """What it does, for the welcome page's table and the README's; None when
    another row already covers it (`]` is on the `[` row)."""
    table_keys: tuple[str, ...] = ()
    """The keys as that table and the hints show them, when not just `shown`
    (`[ ]` for back and forward: a pair reads as one)."""
    mode: Mode = "reading"
    while_editing: bool = False
    """A reading key that still works while editing (`q` goes back to reading;
    `e` puts the cursor back in the editor). Every other reading key is off."""

    @property
    def hint_key(self) -> str:
        return " ".join(self.table_keys or (self.shown,))

    @property
    def table_cell(self) -> str:
        return " ".join(f"`{key}`" for key in self.table_keys or (self.shown,))


KEYS: list[Key] = [
    # --- reading ------------------------------------------------------------
    Key("↑", help="Move through the files, showing each one the cursor rests on", table_keys=("↑", "↓")),
    Key("Enter", help="Read the file (over to the document), or open/close a folder"),
    Key("Tab", help="Switch between the file list and the document"),
    Key(
        "/", "slash", "find_file", "Find file", hint="find file",
        help="Find a markdown file: lists the most recently changed first, or type part of "
        "a name (`Tab` narrows it to a recently changed folder)",
    ),
    Key(
        "[", "left_square_bracket", "back", "Back", hint="back/forward", drop=6,
        help="Back to the file you had open before, and forward again (like a browser)",
        table_keys=("[", "]"),
    ),
    Key("]", "right_square_bracket", "forward", "Forward"),
    Key(
        "s", "s", "search", "Search text", hint="search",
        help="Search for words in the open document (`Enter`/`↓`/`n` next, `↑`/`N` previous, "
        "`Esc` close)",
    ),
    Key("n", "n", "next_match(1)", "Next match"),
    Key("N", "N", "next_match(-1)", "Previous match"),
    Key(
        "h", "h", "outline", "Outline",
        help="Outline: every heading in the document; type to narrow it, `Enter` jumps there",
    ),
    Key(
        "e", "e", "start_editing", "Edit here", hint="edit", while_editing=True,
        help="Edit the file right here, as written (`Ctrl+S` saves, `Ctrl+G` saves and commits, "
        "`Esc` goes back to reading)",
    ),
    Key(
        "o", "o", "edit", "Open in another app", hint="open", drop=1,
        help="Open the file in your editor (`VISUAL` or `EDITOR`; `TMR_EDITOR` overrides), or failing those the app your system opens it with",
    ),
    Key(
        "c", "c", "copy_document", "Copy all", hint="copy all", drop=4,
        help="Copy the whole file, exactly as written (including the markdown symbols)",
    ),
    Key(
        "p", "p", "copy_path", "Copy path", hint="copy path", drop=3,
        help="Copy the full path of the open file (handy for pasting to an agent)",
    ),
    Key(
        "g", "g", "commit_file", "Commit to git", hint="commit", drop=5,
        help="Commit the open file to git, on the branch you're on, shown at the foot of the "
        "sidebar (only that file; asks for the message, suggesting one; doesn't push)",
    ),
    Key(
        "w", "w", "switch_worktree", "Switch worktree", hint="worktree", drop=2,
        help="Switch to another worktree (a folder with another branch checked out, such as "
        "an agent's): the same file opens there, and `g` commits to that branch",
    ),
    Key(
        "b", "b", "toggle_sidebar", "Widen/hide sidebar", hint="sidebar", drop=0,
        help="Widen the sidebar just enough to show the longest name in full (at most half "
        "the window), then hide it to give the document the full width, then back to how it was",
    ),
    Key(
        "a", "a", "toggle_all_files", "All files", hint="all files", drop=8,
        help="Show all files, not just markdown (press again to go back)",
    ),
    Key(
        ".", "full_stop", "toggle_hidden", "Hidden files", hint="hidden", drop=7,
        help="Show or hide hidden files and folders",
    ),
    Key("q", "q", "quit", "Quit", hint="quit", help="Quit", while_editing=True),
    # --- editing, when letters type themselves --------------------------------
    Key("esc", "escape", "stop_editing", "Back to reading", hint="back to reading", mode="editing"),
    Key("^S", "ctrl+s", "save_edit", "Save", hint="save", mode="editing"),
    Key("^G", "ctrl+g", "save_and_commit", "Save and commit", hint="save & commit", mode="editing"),
    Key("^Z", hint="undo", clickable=False, mode="editing"),
    Key("^Y", hint="redo", clickable=False, drop=9, mode="editing"),
    Key("^C", hint="copy", clickable=False, drop=11, mode="editing"),
    Key("^V", hint="paste", clickable=False, drop=10, mode="editing"),
    # --- searching, while the search bar is open -------------------------------
    Key("⏎", action="next_match(1)", hint="next", mode="searching"),
    Key("↑", action="previous_match", hint="previous", mode="searching"),
    Key("esc", action="close_search", hint="close search", mode="searching"),
]


def bindings() -> list[Binding]:
    """The app-wide key bindings, for both modes: the app switches off the
    ones that don't apply (see `off_while_editing` and `only_while_editing`)."""
    return [
        Binding(key.binding, key.action, key.name, show=False)
        for key in KEYS
        if key.binding is not None and key.action is not None
    ]


def editor_bindings() -> list[Binding]:
    """The editor's own keys, which run the app's editing actions.

    They come before the text box's own handling of keys (priority), since
    it would otherwise take Esc to mean "move to the next widget".
    """
    return [
        Binding(key.binding, f"app.{key.action}", key.name, show=False, priority=True)
        for key in KEYS
        if key.mode == "editing" and key.binding is not None and key.action is not None
    ]


def _action_name(action: str) -> str:
    return action.split("(", 1)[0]


def off_while_editing() -> frozenset[str]:
    """The app actions that do nothing while a file is being edited."""
    return frozenset(
        _action_name(key.action)
        for key in KEYS
        if key.mode == "reading" and key.action is not None and not key.while_editing
    )


def only_while_editing() -> frozenset[str]:
    """The app actions that do nothing unless a file is being edited."""
    return frozenset(
        _action_name(key.action) for key in KEYS if key.mode == "editing" and key.action is not None
    )


def hints(mode: Mode) -> list[tuple[str, str, str | None]]:
    """The hints along the bottom: (key, label, action to run when clicked)."""
    return [
        (key.hint_key, key.hint, f"app.{key.action}" if key.clickable and key.action else None)
        for key in KEYS
        if key.mode == mode and key.hint is not None
    ]


SEPARATOR = " [dim]·[/] "
"""Between one key's hint and the next, along the bottom and in the pop-ups."""


def hint_markup(key: str, label: str, action: str | None = None) -> str:
    """One key's hint, as markup: the key in bold, then what it does."""
    text = f"[b not dim]{key.replace('[', '\\[')}[/] {label}"
    return f"[@click={action}]{text}[/]" if action else text


def key_hints(pairs: list[tuple[str, str]]) -> Content:
    """A pop-up's keys, as its footer shows them: "⏎ open · esc close"."""
    return Content.from_markup(SEPARATOR.join(hint_markup(key, label) for key, label in pairs))


def drop_order() -> list[str]:
    """The keys whose hints go first when they don't all fit, first to go first."""
    dropping = sorted((key for key in KEYS if key.drop is not None), key=lambda key: key.drop)
    return [key.hint_key for key in dropping]


def help_rows() -> list[tuple[str, str]]:
    """The key table's rows, as markdown: (keys, what they do)."""
    return [(key.table_cell, key.help) for key in KEYS if key.mode == "reading" and key.help]


def help_table() -> str:
    """The key table, in markdown, as on the welcome page and in the README."""
    lines = ["| Key | What it does |", "| --- | --- |"]
    lines += [f"| {keys} | {text} |" for keys, text in help_rows()]
    return "\n".join(lines) + "\n"
