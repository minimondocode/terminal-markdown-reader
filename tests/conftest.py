import subprocess
import threading
from pathlib import Path

import pytest
from helpers import GUIDE, README
from PIL import Image as PILImage


@pytest.fixture(autouse=True)
def _private_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's remembered files and sidebar width out of the real ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A small project: markdown, other files, a picture, clutter and a hidden folder."""
    root = tmp_path / "project"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".secret").mkdir()
    (root / "README.md").write_text(README)
    (root / "docs" / "guide.md").write_text(GUIDE)
    (root / "docs" / "deep" / "note.md").write_text("# Deep\n")
    (root / "notes.txt").write_text("Plain note with loft.\n")
    (root / "script.py").write_text("print('hi')\n")
    (root / "data.bin").write_bytes(bytes(range(256)) * 4)
    (root / "node_modules" / "pkg" / "x.md").write_text("# junk\n")
    (root / ".secret" / "hidden.md").write_text("# hidden\n")
    PILImage.new("RGB", (40, 20), "orange").save(root / "pic.png")
    return root.resolve()


@pytest.fixture
def watching(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set once the file watcher is watching, so changes made from then on are seen.

    The watcher starts in its own thread a moment after the app does; a test
    that writes a file before then would wait for a change that never comes.
    """
    import watchfiles.main

    ready = threading.Event()
    real = watchfiles.main.RustNotify

    def noting(*args, **kwargs):
        notify = real(*args, **kwargs)
        ready.set()
        return notify

    monkeypatch.setattr(watchfiles.main, "RustNotify", noting)
    return ready


@pytest.fixture
def clipboard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What tmr copies to the system clipboard (through pbcopy), in order."""
    copied: list[str] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command == ["pbcopy"]:
            copied.append(kwargs["input"].decode())
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    return copied
