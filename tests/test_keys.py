"""The keys are listed once (tmr.keys); everything that describes them has to agree."""

from __future__ import annotations

from pathlib import Path

from tmr import keys
from tmr.app import EDITING_HINTS, HINTS, HINTS_TO_DROP_FIRST, SEARCHING_HINTS, TmrApp
from tmr.viewer import WELCOME

README = Path(__file__).resolve().parent.parent / "README.md"


def _table(markdown: str) -> list[tuple[str, str]]:
    """The rows of the first "| Key | What it does |" table in some markdown."""
    lines = markdown.split("\n")
    start = lines.index("| Key | What it does |") + 2
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        assert len(cells) == 2, f"not two cells: {line!r}"
        rows.append((cells[0], cells[1]))
    return rows


def _readme_keys_section() -> str:
    text = README.read_text()
    start = text.index("## Keys\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_readme_key_table_matches_the_keys() -> None:
    assert _table(_readme_keys_section()) == keys.help_rows()


def test_welcome_page_key_table_matches_the_keys() -> None:
    rows = _table(WELCOME)
    assert rows == keys.help_rows()
    assert ("`g`", next(key.help for key in keys.KEYS if key.shown == "g")) in rows


def test_every_bound_key_and_clickable_hint_has_an_action() -> None:
    for binding in TmrApp.BINDINGS:
        name = binding.action.split("(")[0]
        assert hasattr(TmrApp, f"action_{name}"), binding.action
    for _, _, action in HINTS + EDITING_HINTS + SEARCHING_HINTS:
        if action is not None:
            name = action.removeprefix("app.").split("(")[0]
            assert hasattr(TmrApp, f"action_{name}"), action


def test_hints_and_bindings_come_from_the_list() -> None:
    assert [key for key, _, _ in HINTS] == ["/", "[ ]", "s", "e", "o", "c", "p", "g", "w", "b", "a", ".", "q"]
    assert [key for key, _, _ in EDITING_HINTS] == ["esc", "^S", "^G", "^Z", "^Y", "^C", "^V"]
    assert [key for key, _, _ in SEARCHING_HINTS] == ["⏎", "↑", "esc"]
    assert HINTS_TO_DROP_FIRST == ["b", "o", "w", "p", "c", "g", "[ ]", ".", "a", "^Y", "^V", "^C"]
    bound = {binding.key: binding.action for binding in TmrApp.BINDINGS}
    assert bound["slash"] == "find_file"
    assert bound["right_square_bracket"] == "forward"
    assert bound["N"] == "next_match(-1)"
    assert bound["full_stop"] == "toggle_hidden"
    # Every key a hint shows while reading is bound, so the hint tells the truth.
    shown_bound = {key.hint_key for key in keys.KEYS if key.binding}
    assert {key for key, _, _ in HINTS} <= shown_bound
