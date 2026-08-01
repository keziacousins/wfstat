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
| `wfstat agent <id>` | One agent's task prompt, return value, token usage, tools used, files touched. Prefixes work. |
| `wfstat live` | In-flight runs only: per-agent live token totals, idle time, and done/running/orphaned state. |
| `wfstat watch` | `live` on a flicker-free 2s refresh loop until Ctrl-C. `--interval` to change. |

Global flags: `--project <abs path or encoded dir name>` to target a project other than the current
directory, `--all` to scan every project. `wfstat agent --full` prints untruncated task/result text.

By default the project is auto-detected by walking up from `$PWD` to the nearest ancestor that has a
Claude project directory, so it works from any subdirectory.

## What it reads

Everything comes from files the workflow engine writes under `~/.claude/projects/<encoded-project>/`
(override the root with `$CLAUDE_HOME`):

```
<session>/workflows/wf_*.json                      per-run summary, written at completion
<session>/subagents/workflows/wf_*/journal.jsonl   started/result events, appended live
<session>/subagents/workflows/wf_*/agent-*.jsonl   per-agent transcript incl. message.usage, live
```

Token figures are summed from the `message.usage` blocks in the agent transcripts, so they are the
real billed numbers rather than the summary's rounded total — and they are available *during* a run,
before any summary exists.

## Three things it gets right that the raw files don't

These are the reasons the tool exists; each is a trap the on-disk data sets for you.

**`status: "completed"` is not trustworthy.** The engine stamps a run completed whenever the script's
function returns a value — including when the script caught an agent death (spend limit, terminal API
error) and returned a halt object. `wfstat` derives an *effective* status from the result payload and
trailing logs, and reports `⚠ halted` with the reason.

**A resumed run looks finished.** A run relaunched with `resumeFromRunId` carries the stale summary
from its earlier halt while actively appending new agents. Liveness is therefore decided by recent
write activity, not by absence of a summary — a run is live if something was written in the last five
minutes and no summary landed *after* that write.

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

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Anthropic. The on-disk layout it reads is an internal detail of Claude Code and
may change between releases.
