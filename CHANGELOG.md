# Changelog

What changed in each version of tmr. Versions follow [semantic versioning](https://semver.org/).

## 1.0.0 (2026-10-06)

The first public release.

- A two-column file explorer: the folder's files on the left, the open markdown
  file formatted on the right. Starts on the file last open in that folder, or
  `README.md`, or a welcome page.
- Follows changes on disk and keeps your place; new or changed files get a dot
  in the list for a couple of minutes.
- Find a file (`/`), most recently changed first; search in the document (`s`);
  jump to a heading (`h`); back and forward through files (`[` `]`).
- Links to other markdown files, including file names written in plain text and
  wiki links (`[[plan]]`).
- Copy the file as written (`c`) or its path (`p`); highlighting text copies it
  as markdown; every code block has a `copy` button.
- Tick task checkboxes in place, edit the file right there (`e`), or open it in
  your editor (`o`). Edits made on disk while you type are merged when you save.
- Commit the open file to git (`g`) and switch between worktrees (`w`).
- Mermaid diagrams, pictures, tables, front matter and footnotes.
- Colours made for light and dark terminals; `TMR_COLORS`, `TMR_FONT_SIZE` and
  `TMR_EDITOR` settings.
