# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). What
the version promises is the **CLI surface** — command names, flags, and the shape
of the output. The on-disk layout `wfstat` reads belongs to Claude Code and can
change under us; when it does, the fix ships as a patch release.

## [Unreleased]

## [1.1.0] — 2026-08-02

### Added

- Output is now fitted to the terminal. Tables shed their least actionable
  columns as the window narrows (`CACHE-R` before `STATE`), status lines wrap
  onto continuation lines instead — the numbers on them are the point of the
  line, so they are never dropped.
- `watch` runs on the alternate screen buffer: your scrollback is left
  untouched, and Ctrl-C restores the terminal and prints a parting snapshot.
- `COLUMNS` pins an explicit width for any command, useful for screenshots.

### Fixed

- `watch` no longer corrupts its own display when output outgrows the window.
  It repaints in place from the cursor home position, which only works while the
  frame fits; painting past the last row scrolled the terminal, so the next
  repaint landed mid-frame and stitched frames together. Frames are now clamped
  to the window, with rows water-filled across concurrent runs.
- Under height pressure, in-flight agents keep their seats and finished ones
  collapse into a count. A stalled agent sorts last by recency and is exactly
  what you need to see. Anything hidden is always reported, so a short frame
  never reads as the whole picture.
- `live` printed an empty line in place of the agents/tokens summary when a run
  had transcripts but no parsed timestamps — a trailing conditional bound to the
  whole concatenated string rather than to the `last write:` fragment alone.

### Changed

- Redirected output stays unclamped, so `wfstat live | less` keeps every column.

## [1.0.0] — 2026-07-14

First tagged release; `wfstat` extracted into a standalone repository.

### Added

- `ls`, `show`, `agent`, `live`, and `watch`, reading only the artifacts the
  workflow engine already writes under `~/.claude/projects/`.
- Token figures summed from `message.usage` in the agent transcripts, so they
  are the real billed numbers and are available *during* a run.
- An effective run status derived from the result payload and trailing logs,
  because the engine stamps `completed` on any script that returns a value —
  including one that caught an agent death and returned a halt object.
- Liveness decided by recent write activity rather than absence of a summary,
  so a run resumed with `resumeFromRunId` is not mistaken for a finished one.
- Genuinely in-flight agents told apart from superseded ones via the journal's
  resume-cache `key`, which also recovers labels for live agents.
- CI on Linux and macOS across Python 3.9, 3.11 and 3.13.

[Unreleased]: https://github.com/keziacousins/wfstat/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/keziacousins/wfstat/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/keziacousins/wfstat/releases/tag/v1.0.0
