# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). What
the version promises is the **CLI surface** — command names, flags, and the shape
of the output. The on-disk layout `wfstat` reads belongs to Claude Code and can
change under us; when it does, the fix ships as a patch release.

## [Unreleased]

## [1.2.1] — 2026-09-26

### Fixed

- `ls` crashed with a `JSONDecodeError` when it caught a run journal mid-append.
  Journals are written to while runs are live, so a torn last line is routine;
  it is now skipped, as it already was everywhere else.
- Projects whose path contains anything but letters, digits and slashes — a
  `.`, `_` or space — were never found, by auto-detection or by `--project`.
  Claude Code replaces *every* non-alphanumeric character with `-`, and
  `wfstat` only replaced the slashes.
- `ls` counted orphaned agents as outstanding, so after a stop-and-restart it
  reported `0/2` for a run that `live` correctly showed as one agent in flight.
  Both commands now share one classification, and `ls` leaves orphans out.
- Agent labels and workflow names were read from the run summary line by line,
  so a pretty-printed summary would have silently yielded none. The summary is
  now always parsed whole.
- Transcripts are decoded as UTF-8 regardless of locale, and invalid bytes no
  longer take a command down. File handles are no longer leaked.

## [1.2.0] — 2026-08-02

### Added

- `live` and `watch` now report **Agent-tool subagents**, not just workflow
  agents. They live at `<session>/subagents/` and were previously invisible: a
  session could be running a dozen of them while `wfstat` reported nothing,
  making "nothing in flight" impossible to tell from "nothing I can see".
- `agent <id>` resolves those subagents too, showing the description you gave
  the Agent tool, its type and model, and its return value — which for these is
  its final assistant message, since no journal records one.
- In-flight blocks are ordered most-recently-active first, runs and subagents
  interleaved.

### Fixed

- A killed run could keep showing as live, labelled `⚠ stalled?`, for the full
  five-minute window. Liveness compared the run summary against raw file
  activity, and an agent still flushing its transcript as the run was killed
  wrote **101ms after** the summary landed — which read as a resume. Liveness
  now asks whether the **journal** has advanced since the summary: only the
  engine writes it, and only to record an agent starting or returning, so it
  moves when a resumed run picks up real work and stays put when a run is over.
  This misreport was load-bearing — a control session watching a dead run
  concluded it was alive and waited on it indefinitely.

### Changed

- The empty-state message now names both things it looked for and the window it
  looked in, rather than claiming "all runs have completed summaries".

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

[Unreleased]: https://github.com/keziacousins/wfstat/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/keziacousins/wfstat/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/keziacousins/wfstat/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/keziacousins/wfstat/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/keziacousins/wfstat/releases/tag/v1.0.0
