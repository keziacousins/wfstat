# wfstat

[![CI](https://github.com/keziacousins/wfstat/actions/workflows/ci.yml/badge.svg)](https://github.com/keziacousins/wfstat/actions/workflows/ci.yml)

Visibility into [Claude Code](https://claude.com/claude-code) **Workflow** runs — historical and live.

Claude Code's `Workflow` tool can fan out dozens of subagents across many phases. While a run is in
flight the progress tree tells you *what* is happening; it doesn't tell you what it is *costing*, which
agents are actually still working, or what any individual agent did and returned. `wfstat` reads the
artifacts the workflow engine already writes to disk and answers those questions.

Pure stdlib, single file, read-only. It never writes to `~/.claude`.

```
$ wfstat ls
RUN                  NAME                     STATUS           WHEN     DUR  AGENTS   TOKENS MODEL
-------------------------------------------------------------------------------------------------------------
wf_5ce2d095-f81      migrate-call-sites       ▶ running      12s ago       —   10/14   359.1k mixed
wf_47c9a63b-c3c      review-changes           ⚠ halted        2h ago  12m20s      15   158.8k claude-sonnet-5
wf_3c2e5583-540      research-sweep           completed       1d ago  58m44s      26   961.2k claude-sonnet-5

3 run(s)  (1 running). `wfstat show <run>` for tokens · `wfstat live` for live agents.
```

## Install

```sh
pipx install git+https://github.com/keziacousins/wfstat     # or: pip install --user ...
```

Or just drop the single file somewhere on your `PATH`:

```sh
curl -o ~/.local/bin/wfstat \
  https://raw.githubusercontent.com/keziacousins/wfstat/main/wfstat.py
chmod +x ~/.local/bin/wfstat
```

## Commands

| Command | What it gives you |
| --- | --- |
| `wfstat ls` | All runs, newest first — in-flight ones reconstructed from transcripts and listed on top. Default command. |
| `wfstat show <runId>` | Per-model and per-agent token breakdown for one run, plus cache hit rate. Prefixes work. |
| `wfstat agent <id>` | One agent's task prompt, return value, token usage, tools used, files touched. Works for workflow agents and session subagents alike. Prefixes work. |
| `wfstat live` | Everything in flight — workflow runs **and** plain Agent-tool subagents — with live token totals, idle time and state. |
| `wfstat watch` | `live` on a flicker-free 2s refresh loop until Ctrl-C, fitted to the window. `--interval` to change. |

Global flags: `--project <abs path or encoded dir name>` to target a project other than the current
directory, `--all` to scan every project, `--json` for machine-readable output (see below).
`wfstat agent --full` prints untruncated task/result text.

Every command fits its output to the terminal. Tables shed their least actionable columns as the
window narrows (`CACHE-R` goes before `STATE`); status lines wrap onto continuation lines instead,
since the numbers on them are the point. Redirected output is never clamped, so `wfstat live | less`
keeps every column — set `COLUMNS` to pin a width explicitly.

### Machine-readable output

`ls`, `show`, `agent` and `live` take `--json` and print a single JSON object instead of a table.
It is the interface to use from scripts and from other agents: nothing is dropped or clipped to fit
a window, and `agent --json` gives the full task and return value, with a structured result left as
JSON rather than flattened to text.

```sh
wfstat live --json | jq '.runs[] | select(.status == "stalled") | .run_id'
wfstat agent a1b2 --json | jq .result
```

Every document carries `"schema": 1`, which is bumped on any breaking change to the shape, so a
consumer can refuse a layout it doesn't recognise instead of misreading it. Within a schema,
fields may be added but are never removed or renamed. Timestamps are ISO 8601 in UTC
(`last_activity_at`, `started_at`), durations and ages are numbers (`duration_ms`, `idle_s`), and
a field a record can't know is `null` rather than missing: a live run has no `duration_ms`, and a
finished run has no per-state `agent_states`. Token usage is always an object with `input`,
`output`, `cache_read`, `cache_create` and `turns`.

`watch` is a display and refuses `--json`; poll `wfstat live --json` instead.

By default the project is auto-detected by walking up from `$PWD` to the nearest ancestor that has a
Claude project directory, so it works from any subdirectory.

## What it reads

Everything comes from files the workflow engine writes under `~/.claude/projects/<encoded-project>/`
(override the root with `$CLAUDE_HOME`):

```
<session>/workflows/wf_*.json                      per-run summary, written at completion
<session>/subagents/workflows/wf_*/journal.jsonl   started/result events, appended live
<session>/subagents/workflows/wf_*/agent-*.jsonl   per-agent transcript incl. message.usage, live
<session>/subagents/agent-*.jsonl                  Agent-tool subagents of the session, live
```

Token figures are summed from the `message.usage` blocks in the agent transcripts, so they are the
real billed numbers rather than the summary's rounded total — and they are available *during* a run,
before any summary exists.

## What it gets right that the raw files don't

These are the reasons the tool exists; each is a trap the on-disk data sets for you.

**`status: "completed"` is not trustworthy.** The engine stamps a run completed whenever the script's
function returns a value — including when the script caught an agent death (spend limit, terminal API
error) and returned a halt object. `wfstat` derives an *effective* status from the result payload and
trailing logs, and reports `⚠ halted` with the reason.

**A resumed run looks finished.** A run relaunched with `resumeFromRunId` carries the stale summary
from its earlier halt while actively appending new agents, so the presence of a summary cannot settle
whether a run is over — see the journal rule below for what does.

**A frame taller than the window corrupts a live display.** `wfstat watch` repaints in place from
the cursor home position, which only works while the frame fits: paint past the last row and the
terminal scrolls, so the next repaint lands mid-frame and stitches frames together. `watch` therefore
runs on the alternate screen (leaving your scrollback untouched) and clamps each frame to the window.
When agents don't all fit, in-flight ones keep their seats — a *stalled* agent sorts last by recency
and is exactly what you need to see — and finished ones collapse into a count, so a short frame never
reads as though it were the whole picture.

**A summarised run is over, whatever the mtimes say.** Liveness cannot be settled by comparing the
run summary against file activity: an agent still flushing its transcript as a run is killed writes
*after* the summary lands, which reads as a resume. `wfstat` asks instead whether the **journal** has
advanced since the summary — only the engine writes it, and only to record an agent starting or
returning, so it moves when a resumed run picks up work and stays put when a run is over. The
difference is not academic: a 101ms-late write once kept a killed run on screen as `⚠ stalled?` for
five minutes, and a session watching it concluded the run was alive and waited on it indefinitely.

**Not every agent is a workflow agent.** Agent-tool subagents live one level up, at
`<session>/subagents/`, and have no journal at all — nothing on disk records their completion, and
the session's `tool_result` is not it either (an async agent's result says only "launched
successfully" and arrives immediately). `wfstat` reads their state off the shape of their own
transcript: an agent that has returned ends on an `end_turn` assistant message with no pending tool
call. Without them, "nothing in flight" was indistinguishable from "nothing I can see".

**"Started with no result" does not mean "still running".** A stop-then-restart leaves the interrupted
agent started-forever, while the engine re-issues that step as a *new* agent id sharing the same
resume-cache `key`. `wfstat live` uses that key to tell genuinely in-flight agents from superseded
ones, which it labels `orphaned` — and to recover human-readable labels for live agents, since labels
are only persisted to the summary at completion.

## Development

```sh
python3 -m unittest discover tests -v
```

The tests build a synthetic `CLAUDE_HOME` fixture tree and run the CLI end to end as a subprocess.
No dependencies, no network, nothing touched outside a temp directory. CI runs them on Linux and
macOS against Python 3.9, 3.11 and 3.13, and checks that the packaged console script resolves.

## Releasing

Versions follow [semver](https://semver.org). What the version promises is the **CLI surface** —
command names, flags, output shape. The on-disk layout `wfstat` reads belongs to Claude Code and can
change under us; when it does, the fix ships as a patch.

There is one source of truth for the version: `__version__` in `wfstat.py`. `pyproject.toml` reads it
from there, so they cannot drift. A release is a tag — `pipx install git+…@v1.1.0` resolves to it —
and CI refuses any tag whose name disagrees with `__version__` or that has no changelog entry.

```sh
# 1. bump __version__ in wfstat.py, move Unreleased -> the new version in CHANGELOG.md
# 2. commit, then tag and push
git tag -a v1.1.0 -m "wfstat 1.1.0 — terminal-aware output"
git push origin main --follow-tags
# 3. cut the GitHub release from the changelog section
gh release create v1.1.0 --title "wfstat 1.1.0" \
  --notes-file <(awk '/^## \[1.1.0\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md)
```

Tags are annotated, never moved, and never deleted once pushed — someone may have pinned one.
A mistake gets a new patch release, not a retagged old one.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Anthropic. The on-disk layout it reads is an internal detail of Claude Code and
may change between releases.
