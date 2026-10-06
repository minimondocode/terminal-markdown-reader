"""Remember which file was open in each folder, and how wide the file list is."""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_FOLDERS = 200
"""Remember the last file in this many folders, forgetting the longest unused."""


def _state_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "tmr" / "last-open.json"


def _load() -> dict[str, str]:
    try:
        data = json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def last_open(root: Path) -> Path | None:
    relative = _load().get(str(root))
    if not relative:
        return None
    path = root / relative
    return path if path.is_file() else None


def remember(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    data = _load()
    key, value = str(root), str(relative)
    if list(data.items())[-1:] == [(key, value)]:
        return  # Already the latest (reloading a file that changed, say).
    data.pop(key, None)
    data[key] = value
    for old in list(data)[:-MAX_FOLDERS]:
        del data[old]
    _write(_state_file(), data)


def _write(target: Path, data: dict) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1))
        temporary.replace(target)
    except OSError:
        pass


def _settings_file() -> Path:
    return _state_file().with_name("settings.json")


def _load_settings() -> dict:
    try:
        data = json.loads(_settings_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sidebar_percent() -> float | None:
    value = _load_settings().get("sidebar_percent")
    if isinstance(value, (int, float)) and 5 <= value <= 90:
        return float(value)
    return None


def remember_sidebar_percent(percent: float) -> None:
    data = _load_settings()
    data["sidebar_percent"] = percent
    _write(_settings_file(), data)

