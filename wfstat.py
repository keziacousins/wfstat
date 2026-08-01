#!/usr/bin/env python3
"""wfstat — visibility into Claude Code Workflow runs (historical + live).

Reads the on-disk artifacts the Workflow engine writes under
  ~/.claude/projects/<encoded-project>/
    workflows/wf_*.json                         (per-run summary, written at completion)
    subagents/workflows/wf_XXX/journal.jsonl    (started/result events, live)
    subagents/workflows/wf_XXX/agent-*.jsonl    (per-agent transcript w/ message.usage, live)

No deps beyond the stdlib. Auto-detects the project from $PWD (override with --project / --all).

Commands:
  wfstat ls             historical + in-flight runs, newest first (default)
  wfstat show <runId>   per-model + per-agent token breakdown for one run (prefix ok)
  wfstat agent <id>     one agent's task, result, tokens, files touched (prefix ok)
  wfstat live           in-flight runs: per-agent live token totals + elapsed
  wfstat watch          `live` on a 2s refresh loop until Ctrl-C
"""
import json, os, sys, glob, time, argparse, io, contextlib
from pathlib import Path
from collections import defaultdict

__version__ = "0.1.0"

CLAUDE = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
PROJECTS = CLAUDE / "projects"


# ---- location -------------------------------------------------------------
def encode_project(path: Path) -> str:
    # Claude encodes the abs project path by replacing every non-alnum run with '-'.
    return "-" + str(path).strip("/").replace("/", "-")


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
    try:
        with open(path) as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
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
    except FileNotFoundError:
        pass
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
    try:
        with open(path) as fh:
            for ln in fh:
                try:
                    yield json.loads(ln)
                except Exception:
                    continue
    except FileNotFoundError:
        return


def journal_result(rundir, agent_id):
    """Return the agent's return value from the run journal, or None."""
    for d in _iter_json(rundir / "journal.jsonl"):
        if d.get("type") == "result" and d.get("agentId") == agent_id:
            return d.get("result")
    return None


def find_agent(pdirs, prefix):
    """Locate an agent transcript by id/prefix. Returns (agent_id, path, rundir)."""
    hits = []
    for p in pdirs:
        for f in glob.glob(str(p / "*" / "subagents" / "workflows" / "wf_*"
                             / f"agent-{prefix}*.jsonl")):
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
            try:
                d = json.load(open(f))
            except Exception:
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


def live_run_dirs(pdirs):
    """Yield (rid, rundir, session_dir) for runs writing right now.

    Liveness is decided by *recent write activity*, not by absence of a
    summary — a resumed run (resumeFromRunId) carries a stale summary from
    its earlier halt yet is actively appending new agents. A run counts as
    live when its newest file was written within LIVE_WINDOW and no summary
    was written *after* that activity (a fresh summary => the run finished)."""
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
            if summ_mtime.get(rid, 0.0) >= act:  # summary written after last write => done
                continue
            yield rid, Path(rd), Path(rd).parents[2]


def live_run_name(session_dir, rid):
    """Best-effort workflow name for an in-flight run (no summary yet).

    The name lives in the parent session's main transcript, not the run dir.
    Prefer a "workflowName" that co-occurs with this runId; else the sole one."""
    mj = session_dir.parent / f"{session_dir.name}.jsonl"
    names, near = [], None
    try:
        for ln in open(mj):
            if '"workflowName"' not in ln:
                continue
            for m in _findall_wfname(ln):
                names.append(m)
                if rid in ln:
                    near = m
    except FileNotFoundError:
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
    started, result = _journal_counts(rundir)
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
    return {"started": started, "result": result, "rows": rows,
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


# ---- commands -------------------------------------------------------------
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
    hdr = (f"{'RUN':<20} {'NAME':<24} {'STATUS':<10} {'WHEN':>10} {'DUR':>7} "
           f"{'AGENTS':>7} {'TOKENS':>8} {'MODEL':<16}")
    print(hdr)
    print("-" * len(hdr))

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
        agents = f"{len(s['result'])}/{len(s['started'])}"
        model = s["models"][0] if len(s["models"]) == 1 else (
            "mixed" if s["models"] else "-")
        print(f"{rid:<20.20} {live_run_name(p, rid):<24.24} {status:<10.10} "
              f"{ago(s['newest']*1000) if s['newest'] else '-':>10} "
              f"{'—':>7} {agents:>7} {h(tok):>8} {model:<16.16}")

    for rid, _f, d, _p in rows:
        eff, _reason = effective_status(d)
        label = {"halted": "⚠ halted", "killed": "✗ killed",
                 "failed": "✗ failed"}.get(eff, eff)
        print(f"{rid:<20.20} {d.get('workflowName','?'):<24.24} "
              f"{label:<10.10} {ago(d.get('startTime')):>10} "
              f"{dur(d.get('durationMs')):>7} {str(d.get('agentCount','-')):>7} "
              f"{h(d.get('totalTokens')):>8} {d.get('defaultModel','-'):<16.16}")
    n = len(rows) + len(live)
    extra = f"  ({len(live)} running)" if live else ""
    print(f"\n{n} run(s){extra}. `wfstat show <run>` for tokens · `wfstat live` for live agents.")


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

    print(f"run:   {rid}")
    if kind == "done":
        d = payload[1]
        eff, reason = effective_status(d)
        shown = eff if eff == d.get("status") else f"{eff} (raw: {d.get('status')})"
        print(f"name:  {d.get('workflowName')}   status: {shown}   "
              f"duration: {dur(d.get('durationMs'))}   agents: {d.get('agentCount')}")
        print(f"summary totalTokens: {h(d.get('totalTokens'))}   "
              f"toolCalls: {d.get('totalToolCalls')}   defaultModel: {d.get('defaultModel')}")
        if eff in ("halted", "killed", "failed") and reason:
            print(f"\n  ⚠ {eff.upper()}: {reason}")
    else:
        print("status: LIVE (no summary yet — reconstructed from transcripts)")
    print()

    if not by_model:
        print("  (no real token usage recorded yet)")
        return

    print(f"{'MODEL':<26} {'IN':>8} {'OUT':>8} {'CACHE-R':>9} {'CACHE-W':>9} {'TURNS':>6}")
    print("-" * 72)
    grand = blank()
    for m in sorted(by_model):
        u = by_model[m]
        for k, v in u.items():
            grand[k] += v
        print(f"{m:<26.26} {h(u['in']):>8} {h(u['out']):>8} {h(u['cache_read']):>9} "
              f"{h(u['cache_create']):>9} {u['turns']:>6}")
    print("-" * 72)
    print(f"{'TOTAL':<26} {h(grand['in']):>8} {h(grand['out']):>8} "
          f"{h(grand['cache_read']):>9} {h(grand['cache_create']):>9} {grand['turns']:>6}")
    billed = grand['in'] + grand['out'] + grand['cache_read'] + grand['cache_create']
    cache_pct = 100 * grand['cache_read'] / billed if billed else 0
    print(f"\n  wire input ≈ {h(grand['in']+grand['cache_read']+grand['cache_create'])}"
          f"  |  {cache_pct:.0f}% of input served from cache")

    if not args.no_agents:
        active = [a for a in per_agent if a[2]['turns']]
        cached = len(per_agent) - len(active)
        print(f"\nper-agent ({len(active)} with API calls, ranked by output):")
        print(f"{'LABEL':<26} {'MODEL':<18} {'IN':>7} {'OUT':>7} {'CACHE-R':>8} "
              f"{'CACHE-W':>8} {'TURNS':>6}  {'AGENT':<8}")
        print("-" * 92)
        for aid, model, u, _ts in sorted(active, key=lambda x: -x[2]['out']):
            lbl = labels.get(aid, f"({aid[:8]})")
            print(f"{lbl:<26.26} {str(model):<18.18} {h(u['in']):>7} {h(u['out']):>7} "
                  f"{h(u['cache_read']):>8} {h(u['cache_create']):>8} {u['turns']:>6}  "
                  f"{aid[:8]:<8}")
        if cached:
            print(f"  (+{cached} resume-cached agent(s): returned from cache, no API calls this run)")


def _agent_meta(rundir, agent_id):
    """label / phase / model / state from the run summary's workflowProgress."""
    rid = rundir.name
    # rundir = <session>/subagents/workflows/wf_XXX  → summary at <session>/workflows/
    summ = rundir.parents[2] / "workflows" / f"{rid}.json"
    for d in _iter_json(summ):
        for e in d.get("workflowProgress", []):
            if e.get("agentId") == agent_id:
                return e, rid, d.get("workflowName")
        return None, rid, d.get("workflowName")  # summary exists, no match
    return None, rid, None  # no summary (live run)


def cmd_agent(args):
    pdirs = project_dirs(args)
    aid, path, rundir = find_agent(pdirs, args.agent)
    meta, rid, wfname = _agent_meta(rundir, aid)
    by_model, _ts, _lm = scan_agent_file(path)
    tot = blank()
    for u in by_model.values():
        for k, v in u.items():
            tot[k] += v
    task, tools, files, bash = agent_activity(path)
    result = journal_result(rundir, aid)

    print(f"agent: {aid}")
    line = f"run:   {rid}"
    if wfname:
        line += f"  ({wfname})"
    print(line)
    if meta:
        print(f"label: {meta.get('label')}   phase: {meta.get('phaseTitle')}   "
              f"model: {meta.get('model')}   state: {meta.get('state')}")
    print(f"tokens: in {h(tot['in'])}  out {h(tot['out'])}  "
          f"cache-r {h(tot['cache_read'])}  cache-w {h(tot['cache_create'])}  "
          f"turns {tot['turns']}")

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
        print("  tools: " + "  ".join(f"{n}×{c}" for n, c in order))
    if files:
        print("  files touched:")
        for f, c in sorted(files.items(), key=lambda x: -x[1]):
            print(f"    {c:>3}  {f}")
    if bash and args.full:
        print("  bash (first line of each):")
        for c in bash:
            print(f"    $ {c}")


def _squeeze(text, full, cap):
    text = text.strip()
    if full or len(text) <= cap:
        return text
    return text[:cap] + f"\n  … (+{len(text)-cap} chars; --full for all)"


def _journal_counts(rundir):
    started = set(); result = set()
    jf = rundir / "journal.jsonl"
    try:
        for ln in open(jf):
            d = json.loads(ln)
            aid = d.get("agentId")
            if d.get("type") == "started":
                started.add(aid)
            elif d.get("type") == "result":
                result.add(aid)
    except FileNotFoundError:
        pass
    return started, result


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
    summ = rundir.parents[2] / "workflows" / f"{rundir.name}.json"
    for d in _iter_json(summ):
        for e in d.get("workflowProgress", []):
            if e.get("agentId") and e.get("label"):
                labels[e["agentId"]] = e["label"]
    agent_key = {}
    for d in _iter_json(rundir / "journal.jsonl"):
        if d.get("agentId") and d.get("key"):
            agent_key[d["agentId"]] = d["key"]
    key_label = {agent_key[a]: lab for a, lab in labels.items() if a in agent_key}
    return lambda aid: labels.get(aid) or key_label.get(agent_key.get(aid))


def cmd_live(args):
    pdirs = project_dirs(args)
    found = False
    for rid, rd, p in live_run_dirs(pdirs):
        agent_files = glob.glob(str(rd / "agent-*.jsonl"))
        if not agent_files:
            continue
        found = True
        states, result = journal_states(rd)
        label_of = live_label_map(rd)
        run_total = blank()
        rows = []
        newest = 0.0
        for af in sorted(agent_files):
            bm, last_ts, last_model = scan_agent_file(af)
            newest = max(newest, last_ts)
            tot = blank()
            for u in bm.values():
                for k, v in u.items():
                    tot[k] += v; run_total[k] += v
            aid = Path(af).stem.replace("agent-", "")
            rows.append((aid, last_model, tot, last_ts, states.get(aid, "running")))

        n_done = sum(1 for s in states.values() if s == "done")
        n_run = sum(1 for s in states.values() if s == "running")
        n_orph = sum(1 for s in states.values() if s == "orphaned")
        idle = time.time() - newest if newest else None
        flag = "⚠ stalled?" if (idle and idle > 90) else "● active"
        print(f"══ {rid}  [{flag}]  {p.name}")
        orph = f" / {n_orph} orphaned" if n_orph else ""
        print(f"   agents: {len(states)} started / {n_done} done / "
              f"{n_run} in-flight{orph}   "
              f"tokens: in {h(run_total['in'])}  out {h(run_total['out'])}  "
              f"cache-r {h(run_total['cache_read'])}   "
              f"last write: {int(idle)}s ago" if idle else "")
        print(f"   {'LABEL':<24} {'MODEL':<18} {'OUT':>7} {'IN':>7} {'CACHE-R':>8} "
              f"{'TURNS':>6}  {'IDLE':>6}  {'STATE':<9} {'AGENT':<8}")
        for aid, model, u, ts, st in sorted(rows, key=lambda x: -x[3]):
            it = f"{int(time.time()-ts)}s" if ts else "-"
            lbl = label_of(aid) or f"({aid[:8]})"
            print(f"   {lbl:<24.24} {str(model):<18.18} {h(u['out']):>7} {h(u['in']):>7} "
                  f"{h(u['cache_read']):>8} {u['turns']:>6}  {it:>6}  {st:<9} {aid[:8]:<8}")
        print()
    if not found:
        print("no in-flight workflow runs. (all runs have completed summaries)")


def cmd_watch(args):
    # Flicker-free: render the whole frame into a buffer *before* touching the
    # screen, then repaint in place (cursor home + erase-to-EOL per line +
    # erase-below) instead of a blanking clear. Wrap each repaint in the DEC
    # synchronized-output markers (2026h/l) so terminals that support them draw
    # the frame atomically; others ignore the unknown private mode.
    HIDE, SHOW = "\033[?25l", "\033[?25h"
    SYNC_ON, SYNC_OFF = "\033[?2026h", "\033[?2026l"
    sys.stdout.write("\033[2J\033[H" + HIDE)  # one clean clear at startup
    try:
        while True:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print(f"wfstat live — {time.strftime('%H:%M:%S')}  (Ctrl-C to stop)\n")
                cmd_live(args)
            # \033[K erases stale trailing chars from a previously-longer line;
            # \033[J after the frame erases any lines a shorter frame left behind.
            painted = "".join(line + "\033[K\n" for line in buf.getvalue().split("\n"))
            sys.stdout.write(SYNC_ON + "\033[H" + painted + "\033[J" + SYNC_OFF)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()


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
