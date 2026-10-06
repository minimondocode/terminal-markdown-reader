# Contributing to tmr

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Before you start

- For a bug, open an issue with what you did, what you expected and what
  happened, plus `tmr --version`, your OS and your terminal app. Terminal
  quirks matter here, so the terminal app helps most.
- For a new feature or a larger change, open an issue first so we can agree on
  it before you spend time on code. tmr aims to stay small: a reader first,
  with light editing.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and Python 3.13 or later.

```sh
git clone https://github.com/minimondocode/terminal-markdown-reader
cd terminal-markdown-reader
uv sync                       # install everything, including the dev tools
uv run tmr                    # run it from the checkout
uv run pytest                 # run the tests (in parallel; -n0 for one process)
uv run ruff check src tests   # lint
```

## Pull requests

- Keep each pull request to one change, with tests for what it fixes or adds.
  The tests drive the real app through Textual's test pilot; `tests/helpers.py`
  has the shared waits.
- Make sure `uv run pytest` and `uv run ruff check src tests` pass. There's no
  CI: the maintainer runs both on every pull request before merging.
- The code is formatted by hand, so please don't run `ruff format` over it.
  Match the style around your change.
- If a key or a behaviour changes, update `README.md` too, and add a line to
  `CHANGELOG.md`.
- Textual is pinned to one minor version (see the README). If your change needs
  a newer Textual, say so in the pull request.

By contributing, you agree that your work is released under the project's
[MIT licence](LICENSE).
