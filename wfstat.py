#!/usr/bin/env python3
"""wfstat — visibility into Claude Code Workflow runs (historical + live).

Reads the on-disk artifacts the Workflow engine writes under
  ~/.claude/projects/<encoded-project>/
    workflows/wf_*.json                         (per-run summary, written at completion)
    subagents/workflows/wf_XXX/journal.jsonl    (started/result events, live)
    subagents/workflows/wf_XXX/agent-*.jsonl    (per-agent transcript w/ message.usage, live)
    subagents/agent-*.jsonl                     (Agent-tool subagents of the session, live)

No deps beyond the stdlib. Auto-detects the project from $PWD (override with --project / --all).

Commands:
  wfstat ls             historical + in-flight runs, newest first (default)
  wfstat show <runId>   per-model + per-agent token breakdown for one run (prefix ok)
  wfstat agent <id>     one agent's task, result, tokens, files touched (prefix ok)
  wfstat live           in flight now: workflow runs *and* Agent-tool subagents
  wfstat watch          `live` on a 2s refresh loop until Ctrl-C

Output is fitted to the terminal: tables shed their least actionable columns as
the window narrows, status lines wrap rather than lose fields, and `watch` clamps
each frame to the window (a frame that scrolls corrupts the in-place repaint).
Redirected output is left unclamped; set $COLUMNS to pin a width.
"""
import json, os, re, sys, glob, time, argparse, shutil
from pathlib import Path
from collections import defaultdict

__version__ = "1.2.1"

CLAUDE = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
PROJECTS = CLAUDE / "projects"


# ---- location -------------------------------------------------------------
def encode_project(path: Path) -> str:
    # Claude encodes the abs project path by replacing every non-alnum char with
    # '-' — not just the slashes, so /a/my.proj_x is -a-my-proj-x.
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def project_dirs(args):
    if args.all:
        return sorted(p for p in PROJECTS.iterdir() if p.is_dir())
    if args.project:
        # allow either an abs path or an already-encoded dir name
        cand = PROJECTS / encode_project(Path(args.project).resolve())
        if cand.is_dir():
            return [cand]
        cand2 = PROJECTS / args.project
        if cand2.is_dir():
            return [cand2]
        sys.exit(f"no project dir for {args.project!r}")
    # Walk up from cwd: the nearest ancestor with a Claude project dir wins,
    # so `wfstat` works from any subdirectory of the project, not just its root.
    for anc in [Path.cwd(), *Path.cwd().parents]:
        d = PROJECTS / encode_project(anc)
        if d.is_dir():
            return [d]
    sys.exit(f"no Claude project dir for cwd or any parent ({Path.cwd()}); try --all")


# ---- token accounting -----------------------------------------------------
def blank():
    return {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0, "turns": 0}


def add_usage(acc, u):
    acc["in"] += u.get("input_tokens", 0)
    acc["out"] += u.get("output_tokens", 0)
    acc["cache_read"] += u.get("cache_read_input_tokens", 0)
    acc["cache_create"] += u.get("cache_creation_input_tokens", 0)
    acc["turns"] += 1


def scan_agent_file(path):
    """Return (model->usage dict, last_ts_epoch, last_model). Skips <synthetic> lines."""
    by_model = defaultdict(blank)
    last_ts = 0.0
    last_model = None
    for d in _iter_json(path):
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        model = msg.get("model")
        usage = msg.get("usage")
        ts = d.get("timestamp")
        if ts:
            last_ts = max(last_ts, _epoch(ts))
        if not usage or not model or model == "<synthetic>":
            continue
        add_usage(by_model[model], usage)
        last_model = model
    return by_model, last_ts, last_model


def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def agent_activity(path):
    """Parse one agent transcript: task prompt, tool counts, files touched, bash."""
    task = None
    tools = defaultdict(int)
    files = defaultdict(int)
    bash = []
    for ln in _iter_json(path):
        t = ln.get("type")
        msg = ln.get("message") or {}
        if t == "user" and task is None:
            task = _msg_text(msg.get("content"))
        if t != "assistant":
            continue
        for b in msg.get("content", []):
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            n = b.get("name", "?")
            tools[n] += 1
            inp = b.get("input") or {}
            fp = inp.get("file_path")
            if n in ("Edit", "Write", "MultiEdit", "NotebookEdit") and fp:
                files[_short_path(fp)] += 1
            if n == "Bash" and inp.get("command"):
                bash.append(inp["command"].strip().splitlines()[0][:100])
    return task or "", dict(tools), dict(files), bash


def _short_path(fp, keep=3):
    """Shorten an absolute file path for display.

    Relative to $PWD when the file is under it (the common case: agents edit
    files in the project you're standing in); otherwise the trailing `keep`
    components, elided with a leading '…/'."""
    p = Path(fp)
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        pass
    parts = p.parts
    return str(fp) if len(parts) <= keep else "…/" + "/".join(parts[-keep:])


def _iter_json(path):
    """Yield each parseable JSON line of a .jsonl file.

    These files are appended to while runs are live, so a torn last line is
    routine, not corruption — it is skipped, never raised. Decoding is pinned
    to UTF-8 (what Claude Code writes) rather than the locale's, and invalid
    bytes are replaced, so a C locale can't take a command down either."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict):     # every caller expects an object
                    yield d
    except OSError:
        return


def load_summary(path):
    """A run summary (wf_*.json) as a dict, or None.

    Read whole with json.load — never line by line: nothing promises the
    engine writes it on one line, and a pretty-printed summary would otherwise
    parse as nothing at all, silently dropping every label it carries."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def journal_result(rundir, agent_id):
    """Return the agent's return value from the run journal, or None."""
    for d in _iter_json(rundir / "journal.jsonl"):
        if d.get("type") == "result" and d.get("agentId") == agent_id:
            return d.get("result")
    return None


def find_agent(pdirs, prefix):
    """Locate an agent transcript by id/prefix. Returns (agent_id, path, rundir).

    Searches workflow agents and plain Agent-tool subagents alike; the caller
    tells them apart by whether the returned dir is a wf_* run."""
    hits = []
    for p in pdirs:
        for pat in (p / "*" / "subagents" / "workflows" / "wf_*" / f"agent-{prefix}*.jsonl",
                    p / "*" / "subagents" / f"agent-{prefix}*.jsonl"):
            for f in glob.glob(str(pat)):
                if f.endswith(".meta.json"):
                    continue
                aid = Path(f).stem.replace("agent-", "")
                hits.append((aid, Path(f), Path(f).parent))
    uniq = {a: (a, f, r) for a, f, r in hits}  # dedupe by id
    if not uniq:
        sys.exit(f"no agent matching {prefix!r} (try --all across projects)")
    if len(uniq) > 1:
        sys.exit("ambiguous prefix: " + ", ".join(sorted(uniq)))
    return next(iter(uniq.values()))


def _epoch(ts):
    # ISO8601 like 2026-07-14T13:07:30.958Z
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def merge(dst, src):
    for m, u in src.items():
        for k, v in u.items():
            dst[m][k] += v


# ---- discovery ------------------------------------------------------------
def summaries(pdirs):
    """Yield (runId, path, dict) for every completed wf_*.json, newest first."""
    out = []
    for p in pdirs:
        # workflows/ lives under each session dir: <project>/<session>/workflows/
        for f in glob.glob(str(p / "*" / "workflows" / "wf_*.json")):
            d = load_summary(f)
            if d is None:
                continue
            out.append((d.get("runId", Path(f).stem), Path(f), d, Path(f).parents[1]))
    out.sort(key=lambda t: t[2].get("startTime", 0), reverse=True)
    return out


_HALT_MARKERS = ("HALT", "returned null", "failed:", "spend limit")


def effective_status(d):
    """Derive the *true* outcome of a run and a reason.

    The engine stamps status="completed" whenever the workflow script's
    function returns a value — even when the script caught an agent death
    (e.g. spend limit) and returned a halt object. So "completed" is not
    trustworthy on its own. Returns (status, reason) where status is one of
    "killed" / "halted" / "completed" / <raw>."""
    raw = d.get("status", "?")
    if raw in ("killed", "failed", "aborted", "error"):
        return raw, None
    res = d.get("result")
    if isinstance(res, dict):
        for k in ("halted", "error", "aborted"):
            if res.get(k):
                r = res[k]
                return "halted", (r if isinstance(r, str) else json.dumps(r))[:120]
    logs = d.get("logs") or []
    for line in reversed(logs[-8:]):
        s = line if isinstance(line, str) else json.dumps(line)
        if any(m in s for m in _HALT_MARKERS):
            return "halted", s.strip('"')[:120]
    return raw, None


LIVE_WINDOW = 300  # seconds of write silence before a run is no longer "live"


def _newest_activity(rundir):
    """Wall-clock mtime of the most recently written journal/agent file, or 0."""
    newest = 0.0
    for f in glob.glob(str(rundir / "journal.jsonl")) + \
             glob.glob(str(rundir / "agent-*.jsonl")):
        try:
            newest = max(newest, os.path.getmtime(f))
        except OSError:
            pass
    return newest


def _journal_mtime(rundir):
    try:
        return os.path.getmtime(rundir / "journal.jsonl")
    except OSError:
        return 0.0


def live_run_dirs(pdirs):
    """Yield (rid, rundir, session_dir) for runs writing right now.

    Absence of a summary can't decide this: a resumed run (resumeFromRunId)
    carries a stale summary from its earlier halt yet is actively appending new
    agents. So a run is live when something was written within LIVE_WINDOW and,
    if a summary exists, the *journal* has advanced since that summary.

    The journal is the load-bearing part. Only the engine writes it, and only
    to record an agent starting or returning — so it advances when a resumed
    run picks up real work, and stays put when a run is over. Comparing the
    summary against raw file activity instead loses a race: an agent that is
    still flushing its transcript as the run is killed writes *after* the
    summary lands, which read as "resumed". That happened — a death rattle
    101ms late kept a killed run on screen, labelled `⚠ stalled?`, for the
    full five minutes, and a control session watching it concluded the run was
    alive and waited on it forever."""
    summ_mtime = {}
    for rid, f, _d, _p in summaries(pdirs):
        try:
            summ_mtime[rid] = os.path.getmtime(f)
        except OSError:
            summ_mtime[rid] = 0.0
    now = time.time()
    for p in pdirs:
        for rd in glob.glob(str(p / "*" / "subagents" / "workflows" / "wf_*")):
            if not Path(rd).is_dir():
                continue
            rid = Path(rd).name
            act = _newest_activity(Path(rd))
            if not act or now - act > LIVE_WINDOW:
                continue
            smt = summ_mtime.get(rid, 0.0)
            if smt and _journal_mtime(Path(rd)) <= smt:   # summarised, no new work
                continue
            yield rid, Path(rd), Path(rd).parents[2]


def subagent_meta(path):
    """The sibling .meta.json for an agent transcript ({} if absent).

    Agent-tool subagents carry the `description` you gave the tool, which is
    the only human-readable name they ever get — workflow agents get theirs
    from the run summary instead."""
    try:
        with open(str(path)[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def subagent_state(path):
    """done / running for an Agent-tool subagent, read from its own transcript.

    These have no journal — nothing on disk records their completion, and the
    session's tool_result is not it either: an async agent's result says only
    "launched successfully" and lands immediately. What does distinguish them
    is the shape of the tail. An agent that has returned ends on an assistant
    message with stop_reason "end_turn" and no pending tool call; one still in
    its tool loop does not."""
    last = None
    for d in _iter_json(path):
        if d.get("type") == "assistant":
            last = d
    if not last:
        return "running"
    msg = last.get("message") or {}
    pending = any(isinstance(b, dict) and b.get("type") == "tool_use"
                  for b in msg.get("content") or [])
    return "done" if msg.get("stop_reason") == "end_turn" and not pending else "running"


def session_subagents(pdirs):
    """Agent-tool subagents that wrote recently, grouped by session.

    These live one level above the workflow runs, at <session>/subagents/, and
    are invisible to everything else here — a session can be running a dozen of
    them while `live` reports nothing at all, which makes "nothing in flight"
    impossible to tell from "nothing I can see"."""
    now = time.time()
    out = []
    for p in pdirs:
        for sess in sorted(glob.glob(str(p / "*" / "subagents"))):
            rows, total, newest = [], blank(), 0.0
            for af in sorted(glob.glob(str(Path(sess) / "agent-*.jsonl"))):
                try:
                    mtime = os.path.getmtime(af)
                except OSError:
                    continue
                if now - mtime > LIVE_WINDOW:
                    continue          # cheap upper bound — skip without parsing
                bm, last_ts, last_model = scan_agent_file(af)
                # Filter on the same clock the IDLE column displays. Deciding
                # liveness by mtime and then showing an age from the transcript
                # lets the two disagree, which is the whole bug above in
                # miniature: a row claiming to be live next to "9999s idle".
                ts = last_ts or mtime
                if now - ts > LIVE_WINDOW:
                    continue
                newest = max(newest, ts)
                tot = blank()
                for u in bm.values():
                    for k, v in u.items():
                        tot[k] += v
                        total[k] += v
                meta = subagent_meta(af)
                rows.append({"id": Path(af).stem.replace("agent-", ""),
                             "label": meta.get("description") or "(no description)",
                             "type": meta.get("agentType", "?"),
                             "model": meta.get("model") or last_model or "?",
                             "usage": tot, "ts": ts, "state": subagent_state(af)})
            if rows:
                rows.sort(key=lambda r: -r["ts"])
                out.append({"session": Path(sess).parent.name, "rows": rows,
                            "total": total, "newest": newest})
    return out


def live_run_name(session_dir, rid):
    """Best-effort workflow name for an in-flight run (no summary yet).

    The name lives in the parent session's main transcript, not the run dir.
    Prefer a "workflowName" that co-occurs with this runId; else the sole one."""
    mj = session_dir.parent / f"{session_dir.name}.jsonl"
    names, near = [], None
    try:
        with open(mj, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if '"workflowName"' not in ln:
                    continue
                for m in _findall_wfname(ln):
                    names.append(m)
                    if rid in ln:
                        near = m
    except OSError:
        pass
    if near:
        return near
    uniq = list(dict.fromkeys(names))
    return uniq[-1] if len(uniq) == 1 else (uniq[-1] if uniq else "?")


def _findall_wfname(line):
    out, key = [], '"workflowName":"'
    i = line.find(key)
    while i != -1:
        j = line.find('"', i + len(key))
        if j == -1:
            break
        out.append(line[i + len(key):j])
        i = line.find(key, j)
    return out


def live_run_stats(rundir):
    """Aggregate live token/agent stats for one in-flight run dir."""
    # The same classification `live` uses, so the two commands can't disagree:
    # an agent superseded by a restart is orphaned, not still outstanding.
    states, result = journal_states(rundir)
    run_total = blank()
    rows, newest = [], 0.0
    for af in sorted(glob.glob(str(rundir / "agent-*.jsonl"))):
        bm, last_ts, last_model = scan_agent_file(af)
        newest = max(newest, last_ts)
        tot = blank()
        for u in bm.values():
            for k, v in u.items():
                tot[k] += v
                run_total[k] += v
        aid = Path(af).stem.replace("agent-", "")
        rows.append((aid, last_model, tot, last_ts, aid in result))
    models = sorted({m for _a, m, _u, _t, _d in rows if m})
    return {"states": states, "result": result, "rows": rows,
            "total": run_total, "newest": newest, "models": models}


# ---- formatting -----------------------------------------------------------
def h(n):
    if n is None:
        return "-"
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return str(n)


def dur(ms):
    if not ms:
        return "-"
    s = ms / 1000
    if s < 60:
        return f"{s:.0f}s"
    m, s = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    hh, m = divmod(m, 60)
    return f"{hh}h{m:02d}m"


def ago(ts_ms):
    if not ts_ms:
        return "-"
    delta = time.time() - ts_ms / 1000
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= div:
            return f"{int(delta/div)}{unit} ago"
    return f"{int(delta)}s ago"


# ---- terminal geometry ----------------------------------------------------
UNBOUNDED = 10 ** 6   # "don't clamp" — what a pipe reports instead of a size
MIN_COLS, MIN_ROWS = 40, 10


def _env_size():
    """An explicit COLUMNS/LINES override, or None.

    Honoured even when stdout is a pipe — it's how you pin a width for a
    screenshot, a test, or `COLUMNS=100 wfstat ls | less -R`. A value of 0 (as
    some shells export) means "unset", matching shutil's own reading."""
    def val(name):
        try:
            return int(os.environ.get(name, ""))
        except ValueError:
            return 0
    cols, rows = val("COLUMNS"), val("LINES")
    return (cols, rows if rows > 0 else 30) if cols > 0 else None


def term_size():
    """(cols, rows) available for output; unbounded when stdout isn't a tty.

    Redirected output stays unclamped so `wfstat live | less` and the tests see
    full-width rows — clamping is a display concern, not a data one."""
    env = _env_size()
    if env:
        return max(env[0], MIN_COLS), max(env[1], MIN_ROWS)
    if not sys.stdout.isatty():
        return UNBOUNDED, UNBOUNDED
    sz = shutil.get_terminal_size(fallback=(100, 30))
    return max(sz.columns, MIN_COLS), max(sz.lines, MIN_ROWS)


def fit(s, cols):
    """Hard-clamp one line so it can never wrap.

    Terminal wrapping is the enemy of in-place repainting: a wrapped line
    silently costs two rows and outruns the erase-to-EOL that keeps `watch`
    from flickering."""
    s = s.rstrip()
    return s if len(s) <= cols else s[:max(0, cols - 1)] + "…"


def _pack(tokens, width, sep):
    """Greedy line-fill: join `tokens` with `sep`, breaking at `width`."""
    lines, cur = [], ""
    for t in tokens:
        cand = f"{cur}{sep}{t}" if cur else t
        if cur and len(cand) > width:
            lines.append(cur)
            cur = t
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def wrap_fields(fields, cols, indent="", sep="   "):
    """Greedily pack pre-formatted `key: value` fields, wrapping to `cols`.

    Status lines carry summary numbers you can't sensibly drop, so they wrap
    onto continuation lines under a hanging indent rather than shedding fields
    the way the tables below do."""
    width = max(cols - len(indent), 1)
    out = []
    for line in _pack(fields, width, sep):
        # One field wider than the whole window gets word-wrapped rather than
        # clipped — the numbers in it are the reason the line exists.
        out += _pack(line.split(" "), width, " ") if len(line) > width else [line]
    return [fit(indent + ln, cols) for ln in out]


def wrap_text(text, cols, indent=""):
    """Word-wrap a prose line — same greedy pack, one word per field."""
    return wrap_fields(text.split(), cols, indent=indent, sep=" ")


def table_layout(cols, spec, flex_min, flex_max, drop_order, indent=0):
    """Decide which columns survive at `cols`, and how wide the flex one gets.

    Unlike a status line a table row must not wrap: a wrapped row costs a
    second physical line and halves how many agents fit on screen, which is
    exactly what `watch`'s height budget is trying to protect. So rows shed
    their least actionable columns instead, in `drop_order`, until the flex
    column (the one with width None — LABEL / NAME / MODEL) clears `flex_min`.

    `spec` is [(key, width, align)] in display order; each fixed column costs
    width + 1 for its trailing gap. Returns (surviving keys, flex width)."""
    dropped = set()

    def slack():
        return cols - indent - sum(w + 1 for k, w, _a in spec
                                   if w is not None and k not in dropped)

    for key in drop_order:
        if slack() >= flex_min:
            break
        dropped.add(key)
    keep = [k for k, _w, _a in spec if k not in dropped]
    return keep, min(flex_max, max(flex_min, slack()))


def render_row(spec, keep, flex_w, values, indent="", cols=None):
    """One table line: surviving columns only, each clipped to its own width.

    `cols` clamps the finished row — a backstop so a mis-tuned spec or an
    exhausted drop order can still never produce a wrapping line."""
    cells = []
    for key, w, al in spec:
        if key not in keep:
            continue
        w = flex_w if w is None else w
        cells.append(f"{str(values.get(key, '')):{al}{w}.{w}}")
    line = (indent + " ".join(cells)).rstrip()
    return fit(line, cols) if cols else line


def row_width(spec, keep, flex_w, indent=0):
    """Width a full row occupies — measured from the layout, not from the
    rstripped header, so the rule under a table spans the table."""
    ws = [flex_w if w is None else w for k, w, _a in spec if k in keep]
    return indent + sum(ws) + max(len(ws) - 1, 0)


def rule(width, cols):
    return "-" * min(width, cols)


# ---- commands -------------------------------------------------------------
# Column specs: (key, width, align); width None marks the flex column that
# absorbs whatever slack the terminal leaves. drop_order runs least-actionable
# first — see table_layout.
LS_SPEC = [("RUN", 20, "<"), ("NAME", None, "<"), ("STATUS", 10, "<"),
           ("WHEN", 10, ">"), ("DUR", 7, ">"), ("AGENTS", 7, ">"),
           ("TOKENS", 8, ">"), ("MODEL", 16, "<")]
LS_DROP = ["MODEL", "DUR", "AGENTS", "WHEN", "TOKENS", "STATUS"]

SHOW_SPEC = [("MODEL", None, "<"), ("IN", 8, ">"), ("OUT", 8, ">"),
             ("CACHE-R", 9, ">"), ("CACHE-W", 9, ">"), ("TURNS", 6, ">")]
SHOW_DROP = ["CACHE-W", "CACHE-R", "TURNS", "IN"]

AGENTS_SPEC = [("LABEL", None, "<"), ("MODEL", 18, "<"), ("IN", 7, ">"),
               ("OUT", 7, ">"), ("CACHE-R", 8, ">"), ("CACHE-W", 8, ">"),
               ("TURNS", 6, ">"), ("AGENT", 8, "<")]
# AGENT is the handle you need to run `wfstat agent <id>`, so it outlives the
# cache columns rather than being the first thing dropped.
AGENTS_DROP = ["CACHE-W", "CACHE-R", "IN", "MODEL", "TURNS"]

LIVE_SPEC = [("LABEL", None, "<"), ("MODEL", 18, "<"), ("OUT", 7, ">"),
             ("IN", 7, ">"), ("CACHE-R", 8, ">"), ("TURNS", 6, ">"),
             ("IDLE", 6, ">"), ("STATE", 9, "<"), ("AGENT", 8, "<")]
LIVE_DROP = ["CACHE-R", "IN", "TURNS", "AGENT", "MODEL", "OUT"]

# Agent-tool subagents: TASK is the description you gave the tool, and it is the
# only name they have, so it gets the flex column and a generous cap.
SUBAGENT_SPEC = [("TASK", None, "<"), ("TYPE", 16, "<"), ("MODEL", 8, "<"),
                 ("OUT", 7, ">"), ("IN", 7, ">"), ("TURNS", 6, ">"),
                 ("IDLE", 6, ">"), ("STATE", 8, "<"), ("AGENT", 8, "<")]
SUBAGENT_DROP = ["IN", "TURNS", "TYPE", "AGENT", "MODEL", "OUT"]


def _usage_cells(name, u):
    """Cells shared by the token tables. The leading column is the flex one in
    each — MODEL in `show`'s per-model table, LABEL in the per-agent ones — so
    the name is filled under both keys and callers overwrite MODEL as needed."""
    return {"MODEL": name, "LABEL": name,
            "IN": h(u["in"]), "OUT": h(u["out"]),
            "CACHE-R": h(u["cache_read"]), "CACHE-W": h(u["cache_create"]),
            "TURNS": u["turns"]}


def cmd_ls(args):
    pdirs = project_dirs(args)
    rows = summaries(pdirs)
    live = [(rid, rd, p) for rid, rd, p in live_run_dirs(pdirs)
            if glob.glob(str(rd / "agent-*.jsonl"))]
    live_ids = {rid for rid, _rd, _p in live}
    prior = {rid for rid, _f, _d, _p in rows}  # runs with an (old) summary
    rows = [r for r in rows if r[0] not in live_ids]  # don't double-list resumed runs
    if not rows and not live:
        print("no workflow runs found.")
        return
    cols, _rows = term_size()
    # A legible workflow name outranks DUR/AGENTS: the name is how you pick a
    # run out of the list, and `show` has the rest.
    keep, flex = table_layout(cols, LS_SPEC, 16, 24, LS_DROP)
    print(render_row(LS_SPEC, keep, flex, {k: k for k in keep}, cols=cols))
    print(rule(row_width(LS_SPEC, keep, flex), cols))

    # in-flight first — reconstructed from transcripts (no summary yet)
    for rid, rd, p in live:
        s = live_run_stats(rd)
        idle = time.time() - s["newest"] if s["newest"] else None
        resumed = rid in prior
        if idle is not None and idle > 90:
            status = "▶ stalled?"
        else:
            status = "▶ resumed" if resumed else "▶ running"
        tok = s["total"]["in"] + s["total"]["out"]  # non-cache, comparable to summary
        model = s["models"][0] if len(s["models"]) == 1 else (
            "mixed" if s["models"] else "-")
        print(render_row(LS_SPEC, keep, flex, {
            "RUN": rid, "NAME": live_run_name(p, rid), "STATUS": status,
            "WHEN": ago(s["newest"] * 1000) if s["newest"] else "-", "DUR": "—",
            "AGENTS": _agents_cell(s["states"]),
            "TOKENS": h(tok), "MODEL": model}, cols=cols))

    for rid, _f, d, _p in rows:
        eff, _reason = effective_status(d)
        label = {"halted": "⚠ halted", "killed": "✗ killed",
                 "failed": "✗ failed"}.get(eff, eff)
        print(render_row(LS_SPEC, keep, flex, {
            "RUN": rid, "NAME": d.get("workflowName", "?"), "STATUS": label,
            "WHEN": ago(d.get("startTime")), "DUR": dur(d.get("durationMs")),
            "AGENTS": str(d.get("agentCount", "-")),
            "TOKENS": h(d.get("totalTokens")),
            "MODEL": d.get("defaultModel", "-")}, cols=cols))
    n = len(rows) + len(live)
    extra = f"  ({len(live)} running)" if live else ""
    print()
    print("\n".join(wrap_fields(
        [f"{n} run(s){extra}.",
         "`wfstat show <run>` for tokens · `wfstat live` for live agents."],
        cols, sep=" ")))


def _agents_cell(states):
    """`done/outstanding` for a live run in `ls`. Orphaned agents are left out
    of the denominator: a restart re-issued their step under a new id, which is
    already counted, so counting the corpse too reads as work still owed."""
    done = sum(1 for s in states.values() if s == "done")
    return f"{done}/{done + sum(1 for s in states.values() if s == 'running')}"


def _resolve(pdirs, prefix):
    matches = [r for r in summaries(pdirs) if r[0].startswith(prefix)]
    if not matches:
        # maybe it's live-only
        for rid, rd, p in live_run_dirs(pdirs):
            if rid.startswith(prefix):
                return ("live", rid, rd, p)
        sys.exit(f"no run matching {prefix!r}")
    if len(matches) > 1:
        sys.exit("ambiguous prefix: " + ", ".join(m[0] for m in matches))
    rid, f, d, p = matches[0]
    return ("done", rid, (f, d), p)


def cmd_show(args):
    pdirs = project_dirs(args)
    kind, rid, payload, p = _resolve(pdirs, args.run)
    # aggregate real usage from the agent transcripts
    rundir = p / "subagents" / "workflows" / rid
    by_model = defaultdict(blank)
    per_agent = []
    for af in sorted(glob.glob(str(rundir / "agent-*.jsonl"))):
        bm, last_ts, last_model = scan_agent_file(af)
        merge(by_model, bm)
        tot = blank()
        for u in bm.values():
            for k, v in u.items():
                tot[k] += v
        per_agent.append((Path(af).stem.replace("agent-", ""), last_model, tot, last_ts))

    # id → label from the run summary's workflowProgress (empty for live runs)
    labels = {}
    if kind == "done":
        for e in payload[1].get("workflowProgress", []):
            if e.get("agentId") and e.get("label"):
                labels[e["agentId"]] = e["label"]

    cols, _rows = term_size()
    print(fit(f"run:   {rid}", cols))
    if kind == "done":
        d = payload[1]
        eff, reason = effective_status(d)
        shown = eff if eff == d.get("status") else f"{eff} (raw: {d.get('status')})"
        print("\n".join(wrap_fields(
            [f"name:  {d.get('workflowName')}", f"status: {shown}",
             f"duration: {dur(d.get('durationMs'))}",
             f"agents: {d.get('agentCount')}"], cols)))
        print("\n".join(wrap_fields(
            [f"summary totalTokens: {h(d.get('totalTokens'))}",
             f"toolCalls: {d.get('totalToolCalls')}",
             f"defaultModel: {d.get('defaultModel')}"], cols)))
        if eff in ("halted", "killed", "failed") and reason:
            print()
            print("\n".join(wrap_text(f"⚠ {eff.upper()}: {reason}", cols, "  ")))
    else:
        print("\n".join(wrap_text(
            "status: LIVE (no summary yet — reconstructed from transcripts)", cols)))
    print()

    if not by_model:
        print("  (no real token usage recorded yet)")
        return

    keep, flex = table_layout(cols, SHOW_SPEC, 14, 26, SHOW_DROP)
    print(render_row(SHOW_SPEC, keep, flex, {k: k for k in keep}, cols=cols))
    print(rule(row_width(SHOW_SPEC, keep, flex), cols))
    grand = blank()
    for m in sorted(by_model):
        u = by_model[m]
        for k, v in u.items():
            grand[k] += v
        print(render_row(SHOW_SPEC, keep, flex, _usage_cells(m, u), cols=cols))
    print(rule(row_width(SHOW_SPEC, keep, flex), cols))
    print(render_row(SHOW_SPEC, keep, flex, _usage_cells("TOTAL", grand), cols=cols))
    billed = grand['in'] + grand['out'] + grand['cache_read'] + grand['cache_create']
    cache_pct = 100 * grand['cache_read'] / billed if billed else 0
    print()
    print("\n".join(wrap_fields(
        [f"wire input ≈ {h(grand['in']+grand['cache_read']+grand['cache_create'])}",
         f"{cache_pct:.0f}% of input served from cache"],
        cols, indent="  ", sep="  |  ")))

    if not args.no_agents:
        active = [a for a in per_agent if a[2]['turns']]
        cached = len(per_agent) - len(active)
        print()
        print("\n".join(wrap_text(
            f"per-agent ({len(active)} with API calls, ranked by output):", cols)))
        keep, flex = table_layout(cols, AGENTS_SPEC, 12, 26, AGENTS_DROP)
        print(render_row(AGENTS_SPEC, keep, flex, {k: k for k in keep}, cols=cols))
        print(rule(row_width(AGENTS_SPEC, keep, flex), cols))
        for aid, model, u, _ts in sorted(active, key=lambda x: -x[2]['out']):
            cells = _usage_cells(labels.get(aid, f"({aid[:8]})"), u)
            cells.update({"MODEL": str(model), "AGENT": aid[:8]})
            print(render_row(AGENTS_SPEC, keep, flex, cells, cols=cols))
        if cached:
            print(fit(f"  (+{cached} resume-cached agent(s): returned from cache, "
                      f"no API calls this run)", cols))


def _agent_meta(rundir, agent_id):
    """label / phase / model / state from the run summary's workflowProgress."""
    rid = rundir.name
    # rundir = <session>/subagents/workflows/wf_XXX  → summary at <session>/workflows/
    d = load_summary(rundir.parents[2] / "workflows" / f"{rid}.json")
    if d is None:
        return None, rid, None  # no summary (live run)
    for e in d.get("workflowProgress", []):
        if e.get("agentId") == agent_id:
            return e, rid, d.get("workflowName")
    return None, rid, d.get("workflowName")  # summary exists, no match


def _last_assistant_text(path):
    """An Agent-tool subagent's return value: its final assistant message.

    There is no journal to read it from — for these agents the last thing they
    said *is* what the parent received."""
    out = ""
    for d in _iter_json(path):
        if d.get("type") != "assistant":
            continue
        txt = _msg_text((d.get("message") or {}).get("content"))
        if txt.strip():
            out = txt
    return out or None


def cmd_agent(args):
    pdirs = project_dirs(args)
    aid, path, rundir = find_agent(pdirs, args.agent)
    workflow = rundir.name.startswith("wf_")
    by_model, _ts, _lm = scan_agent_file(path)
    tot = blank()
    for u in by_model.values():
        for k, v in u.items():
            tot[k] += v
    task, tools, files, bash = agent_activity(path)

    cols, _rows = term_size()
    print(fit(f"agent: {aid}", cols))
    if workflow:
        meta, rid, wfname = _agent_meta(rundir, aid)
        result = journal_result(rundir, aid)
        line = f"run:   {rid}"
        if wfname:
            line += f"  ({wfname})"
        print(fit(line, cols))
        if meta:
            print("\n".join(wrap_fields(
                [f"label: {meta.get('label')}", f"phase: {meta.get('phaseTitle')}",
                 f"model: {meta.get('model')}", f"state: {meta.get('state')}"], cols)))
    else:
        # A plain Agent-tool subagent: its name and type come from the sibling
        # .meta.json, and its state has to be read off its own transcript.
        meta = subagent_meta(path)
        result = _last_assistant_text(path)
        print(fit(f"run:   — (session subagent of {rundir.parent.name})", cols))
        print("\n".join(wrap_fields(
            [f"task:  {meta.get('description', '?')}",
             f"type: {meta.get('agentType', '?')}",
             f"model: {meta.get('model', '?')}",
             f"state: {subagent_state(path)}"], cols)))
    print("\n".join(wrap_fields(
        [f"tokens: in {h(tot['in'])}  out {h(tot['out'])}",
         f"cache-r {h(tot['cache_read'])}  cache-w {h(tot['cache_create'])}",
         f"turns {tot['turns']}"], cols)))

    if task:
        print("\n── task ──")
        print(_squeeze(task, args.full, 1200))
    if result is not None:
        print("\n── result ──")
        rtxt = result if isinstance(result, str) else json.dumps(result, indent=2)
        print(_squeeze(rtxt, args.full, 1600))

    if tools:
        order = sorted(tools.items(), key=lambda x: -x[1])
        print("\n── activity ──")
        print("\n".join(wrap_fields(["tools: " + f"{order[0][0]}×{order[0][1]}"]
                                    + [f"{n}×{c}" for n, c in order[1:]],
                                    cols, indent="  ", sep="  ")))
    if files:
        print("  files touched:")
        for f, c in sorted(files.items(), key=lambda x: -x[1]):
            print(fit(f"    {c:>3}  {f}", cols))
    if bash and args.full:
        print("  bash (first line of each):")
        for c in bash:
            print(fit(f"    $ {c}", cols))


def _squeeze(text, full, cap):
    text = text.strip()
    if full or len(text) <= cap:
        return text
    return text[:cap] + f"\n  … (+{len(text)-cap} chars; --full for all)"


def journal_states(rundir):
    """Classify every started agent as done / running / orphaned.

    "running" = started, no result. But a stop-then-restart leaves the
    interrupted agent started-without-result forever, while the engine
    re-issues that step as a *new* agentId sharing the same resume-cache
    `key`. So an agent is only genuinely running when no peer sharing its
    key has a result AND it is the newest-started agent for that key;
    otherwise it was superseded by a retry/restart -> "orphaned"."""
    order, agent_key, result, key_order = [], {}, set(), {}
    for d in _iter_json(rundir / "journal.jsonl"):
        t, aid, key = d.get("type"), d.get("agentId"), d.get("key")
        if t == "started" and aid:
            order.append(aid)
            agent_key[aid] = key
            key_order.setdefault(key, []).append(aid) if key else None
        elif t == "result" and aid:
            result.add(aid)
    states = {}
    for aid in order:
        if aid in result:
            states[aid] = "done"
            continue
        peers = key_order.get(agent_key.get(aid), [aid])
        superseded = any(p in result for p in peers) or peers[-1] != aid
        states[aid] = "orphaned" if superseded else "running"
    return states, result


def live_label_map(rundir):
    """Return a fn agentId -> label for an in-flight run (best-effort).

    Labels only get persisted in the run summary's workflowProgress at
    completion. For a live run we recover them two ways: (1) directly, if a
    summary already exists (e.g. a resumed run's stale summary from its first
    attempt), and (2) via the journal's resume-cache `key` — a re-run step
    keeps the same key as its earlier labelled attempt, so key->label bridges
    old agentIds to new ones. Genuinely new steps have no label anywhere yet
    and fall through to None."""
    labels = {}
    d = load_summary(rundir.parents[2] / "workflows" / f"{rundir.name}.json") or {}
    for e in d.get("workflowProgress", []):
        if e.get("agentId") and e.get("label"):
            labels[e["agentId"]] = e["label"]
    agent_key = {}
    for d in _iter_json(rundir / "journal.jsonl"):
        if d.get("agentId") and d.get("key"):
            agent_key[d["agentId"]] = d["key"]
    key_label = {agent_key[a]: lab for a, lab in labels.items() if a in agent_key}
    return lambda aid: labels.get(aid) or key_label.get(agent_key.get(aid))


NO_LIVE = ("nothing in flight: no workflow runs and no subagents have written "
           "in the last %d seconds." % LIVE_WINDOW)


def live_runs(pdirs):
    """Collect every in-flight run's agent rows, states and totals (no output).

    Split out from the printing so `watch` can budget rows across runs before
    anything is rendered."""
    out = []
    for rid, rd, p in live_run_dirs(pdirs):
        agent_files = glob.glob(str(rd / "agent-*.jsonl"))
        if not agent_files:
            continue
        states, _result = journal_states(rd)
        label_of = live_label_map(rd)
        run_total = blank()
        rows, newest = [], 0.0
        for af in sorted(agent_files):
            bm, last_ts, last_model = scan_agent_file(af)
            newest = max(newest, last_ts)
            tot = blank()
            for u in bm.values():
                for k, v in u.items():
                    tot[k] += v; run_total[k] += v
            aid = Path(af).stem.replace("agent-", "")
            rows.append({"id": aid, "model": last_model, "usage": tot,
                         "ts": last_ts, "state": states.get(aid, "running"),
                         "label": label_of(aid) or f"({aid[:8]})"})
        # Most recently active first: the freshest work is anchored to the top
        # of the window and elision eats from the bottom.
        rows.sort(key=lambda r: -r["ts"])
        out.append({"rid": rid, "session": p.name, "states": states,
                    "rows": rows, "total": run_total, "newest": newest})
    return out


def _elide(rows, lines, budget):
    """Pick which agent rows fit in `budget`, returning (shown, hidden).

    `running` agents outrank finished ones for the available seats, however
    stale. An agent that started and then went quiet sorts to the bottom by
    recency — and that stalled agent is the most interesting row on the
    screen — so done/orphaned rows collapse into a count first. Only when the
    in-flight agents alone overrun the budget do they get cut too, freshest
    kept. Display order is unchanged; rows are only removed from it."""
    if budget is None or len(lines) + len(rows) <= budget:
        return rows, []
    room = max(budget - len(lines) - 1, 1)       # -1 for the elision note
    inflight = [r for r in rows if r["state"] == "running"]
    rest = [r for r in rows if r["state"] != "running"]
    picked = {r["id"] for r in inflight[:room]}
    picked |= {r["id"] for r in rest[:max(room - len(inflight), 0)]}
    return ([r for r in rows if r["id"] in picked],
            [r for r in rows if r["id"] not in picked])


def _elision_note(hidden, cols):
    """One line accounting for elided rows, by state. Never silently empty."""
    if not hidden:
        return []
    by = defaultdict(int)
    for r in hidden:
        by[r["state"]] += 1
    note = ", ".join(f"+{c} {s}" for s, c in sorted(by.items()))
    return [fit(f"   … {note} hidden — `wfstat live` for all", cols)]


def render_live_run(run, cols, budget=None):
    """Lines for one in-flight workflow run, elided to `budget` rows."""
    states, rows = run["states"], run["rows"]
    idle = time.time() - run["newest"] if run["newest"] else None
    flag = "⚠ stalled?" if (idle and idle > 90) else "● active"
    lines = [fit(f"══ {run['rid']}  [{flag}]  {run['session']}", cols)]

    n = lambda st: sum(1 for s in states.values() if s == st)  # noqa: E731
    agents = (f"agents: {len(states)} started / {n('done')} done / "
              f"{n('running')} in-flight")
    if n("orphaned"):
        agents += f" / {n('orphaned')} orphaned"
    t = run["total"]
    fields = [agents, f"tokens: in {h(t['in'])}  out {h(t['out'])}  "
                      f"cache-r {h(t['cache_read'])}"]
    if idle is not None:
        fields.append(f"last write: {int(idle)}s ago")
    lines += wrap_fields(fields, cols, indent="   ")

    keep, flex = table_layout(cols, LIVE_SPEC, 12, 24, LIVE_DROP, indent=3)
    lines.append(render_row(LIVE_SPEC, keep, flex,
                            {k: k for k in keep}, indent="   ", cols=cols))

    shown, hidden = _elide(rows, lines, budget)
    for r in shown:
        it = f"{int(time.time()-r['ts'])}s" if r["ts"] else "-"
        u = r["usage"]
        lines.append(render_row(LIVE_SPEC, keep, flex, {
            "LABEL": r["label"], "MODEL": str(r["model"]), "OUT": h(u["out"]),
            "IN": h(u["in"]), "CACHE-R": h(u["cache_read"]),
            "TURNS": u["turns"], "IDLE": it, "STATE": r["state"],
            "AGENT": r["id"][:8]}, indent="   ", cols=cols))
    lines += _elision_note(hidden, cols)
    return lines


def render_session_agents(sess, cols, budget=None):
    """Lines for one session's in-flight Agent-tool subagents.

    Same elision contract as a workflow run: newest first, running agents hold
    their seats, whatever is dropped gets counted."""
    rows = sess["rows"]
    idle = time.time() - sess["newest"] if sess["newest"] else None
    n_run = sum(1 for r in rows if r["state"] == "running")
    lines = [fit(f"══ subagents  [{n_run} in flight]  {sess['session']}", cols)]
    t = sess["total"]
    fields = [f"agents: {len(rows)} active / {n_run} in-flight",
              f"tokens: in {h(t['in'])}  out {h(t['out'])}  cache-r {h(t['cache_read'])}"]
    if idle is not None:
        fields.append(f"last write: {int(idle)}s ago")
    lines += wrap_fields(fields, cols, indent="   ")

    keep, flex = table_layout(cols, SUBAGENT_SPEC, 14, 34, SUBAGENT_DROP, indent=3)
    lines.append(render_row(SUBAGENT_SPEC, keep, flex,
                            {k: k for k in keep}, indent="   ", cols=cols))
    shown, hidden = _elide(rows, lines, budget)
    for r in shown:
        u = r["usage"]
        lines.append(render_row(SUBAGENT_SPEC, keep, flex, {
            "TASK": r["label"], "TYPE": r["type"], "MODEL": str(r["model"]),
            "OUT": h(u["out"]), "IN": h(u["in"]), "TURNS": u["turns"],
            "IDLE": f"{int(time.time()-r['ts'])}s" if r["ts"] else "-",
            "STATE": r["state"], "AGENT": r["id"][:8]}, indent="   ", cols=cols))
    lines += _elision_note(hidden, cols)
    return lines


def live_blocks(pdirs):
    """Everything in flight — workflow runs and plain session subagents — as
    (renderer, data) pairs, most recently active first."""
    blocks = [(render_live_run, r) for r in live_runs(pdirs)] + \
             [(render_session_agents, s) for s in session_subagents(pdirs)]
    blocks.sort(key=lambda b: -b[1]["newest"])
    return blocks


def cmd_live(args):
    blocks = live_blocks(project_dirs(args))
    cols, _rows = term_size()
    if not blocks:
        print("\n".join(wrap_text(NO_LIVE, cols)))
        return
    for render, data in blocks:
        print("\n".join(render(data, cols)))
        print()


MIN_RUN_ROWS = 6   # ══ header + status + table header + a row + the elision note


def _share_rows(need, total):
    """Water-fill `total` rows across runs asking for `need` each.

    An even split wastes the window: a two-agent run can't use half the screen
    while a twenty-agent run is elided beside it. So satisfy the modest askers
    first and pour what's left over the runs still short."""
    alloc = [0] * len(need)
    left, short = total, [i for i, n in enumerate(need) if n]
    while short and left >= len(short):
        share = left // len(short)
        for i in list(short):
            take = min(need[i] - alloc[i], share)
            alloc[i] += take
            left -= take
            if alloc[i] >= need[i]:
                short.remove(i)
    for i in short:                    # hand out the remainder a row at a time
        if left <= 0:
            break
        alloc[i] += 1
        left -= 1
    return alloc


def live_frame(pdirs, cols, rows):
    """One `watch` frame, clamped to `rows` lines so it can never scroll.

    The whole in-place repaint depends on the frame fitting: paint past the
    last row and the terminal scrolls, the frame's top slides away, and the
    next cursor-home lands mid-frame — which is what shredded the display once
    output outgrew the window. Runs share the body rows by water-filling; a run
    that can't clear MIN_RUN_ROWS is dropped for a footer, not half-drawn. Whatever
    the clamp eats is always accounted for in that footer — a frame that just
    stopped short would read as "this is everything"."""
    lines = [fit(f"wfstat live — {time.strftime('%H:%M:%S')}  (Ctrl-C to stop)", cols), ""]
    runs = live_blocks(pdirs)
    if not runs:
        lines += wrap_text(NO_LIVE, cols)
        return "\n".join(lines[:rows])
    need = [len(render(d, cols)) + 1 for render, d in runs]    # +1 trailing blank
    body = rows - len(lines)
    if sum(need) <= body:
        shown, lost_runs, alloc = len(runs), 0, need
    else:
        # Show only as many runs as can each clear MIN_RUN_ROWS — a two-line
        # stub of a run tells you nothing the footer's count doesn't. The
        # newest run always gets a slot, however small the window.
        shown = max(1, min(len(runs), (body - 1) // MIN_RUN_ROWS))
        lost_runs = len(runs) - shown
        alloc = _share_rows(need[:shown], max(body - (1 if lost_runs else 0), 0))
    for (render, data), a in zip(runs[:shown], alloc):
        lines += render(data, cols, a - 1) + [""]
    while lines and not lines[-1]:
        lines.pop()
    if lost_runs or len(lines) > rows:
        lost_rows = max(len(lines) - (rows - 1), 0)
        lines = lines[:rows - 1]
        note = " and ".join(([f"+{lost_rows} row(s)"] if lost_rows else [])
                            + ([f"+{lost_runs} run(s)"] if lost_runs else []))
        lines.append(fit(f"… {note} hidden — resize or `wfstat live`", cols))
    return "\n".join(lines[:rows])


def cmd_watch(args):
    # `watch` takes over the screen. The alternate buffer means a frame can
    # never scroll and the user's scrollback is left untouched; within it we
    # repaint in place (cursor home + erase-to-EOL per line + erase-below)
    # rather than blanking, so the display doesn't flicker. Each repaint is
    # wrapped in the DEC synchronized-output markers (2026h/l) so terminals
    # that support them draw the frame atomically; others ignore the mode.
    HIDE, SHOW = "\033[?25l", "\033[?25h"
    ALT_ON, ALT_OFF = "\033[?1049h", "\033[?1049l"
    SYNC_ON, SYNC_OFF = "\033[?2026h", "\033[?2026l"
    pdirs = project_dirs(args)
    out, frame, last = sys.stdout, "", None
    out.write(ALT_ON + HIDE)   # the first frame clears (last is None => resized)
    try:
        while True:
            cols, rows = term_size()          # re-read: the window may resize
            if cols == UNBOUNDED:             # piped — no geometry to clamp to
                cols, rows = 100, 30
            frame = live_frame(pdirs, cols, rows)
            # A resize reflows the previous frame into debris the differential
            # repaint can't reach, so pay for one full clear on that frame.
            clear, last = ("\033[2J" if last != (cols, rows) else ""), (cols, rows)
            # \033[K erases stale trailing chars from a previously-longer line;
            # \033[J after the frame erases lines a shorter frame left behind.
            # No trailing newline — writing one on the last row would scroll.
            painted = "\n".join(ln + "\033[K" for ln in frame.split("\n"))
            out.write(SYNC_ON + clear + "\033[H" + painted + "\033[J" + SYNC_OFF)
            out.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        out.write(SHOW + ALT_OFF)
        out.write(frame + "\n")   # parting snapshot, on the normal screen
        out.flush()


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="project abs path or encoded dir name")
    common.add_argument("--all", action="store_true", help="scan all projects")

    ap = argparse.ArgumentParser(prog="wfstat", description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"wfstat {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("ls", parents=[common])
    sp = sub.add_parser("show", parents=[common])
    sp.add_argument("run", help="runId or unique prefix")
    sp.add_argument("--no-agents", action="store_true",
                    help="omit the per-agent breakdown (show per-model summary only)")
    ag = sub.add_parser("agent", parents=[common])
    ag.add_argument("agent", help="agent id or unique prefix")
    ag.add_argument("--full", action="store_true",
                    help="print full task/result and per-line bash (no truncation)")
    sub.add_parser("live", parents=[common])
    wp = sub.add_parser("watch", parents=[common])
    wp.add_argument("--interval", type=float, default=2.0)

    args = ap.parse_args()
    {None: cmd_ls, "ls": cmd_ls, "show": cmd_show, "agent": cmd_agent,
     "live": cmd_live, "watch": cmd_watch}[args.cmd](args)


if __name__ == "__main__":
    main()
