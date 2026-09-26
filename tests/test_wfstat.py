"""End-to-end tests for wfstat against a synthetic CLAUDE_HOME fixture tree.

The CLI is exercised as a subprocess so the fixtures are read exactly the way a
real run would read them. Nothing outside the temp directory is touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "wfstat.py"
sys.path.insert(0, str(REPO))
import wfstat  # noqa: E402  (after sys.path fix-up)

PROJECT = "/tmp/demo-project"
ENCODED = "-tmp-demo-project"
SESSION = "ses-0001"


def iso(seconds_ago=0):
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.isoformat().replace("+00:00", "Z")


def assistant(model, ts, **usage):
    """One assistant transcript line carrying a usage block."""
    u = {"input_tokens": 0, "output_tokens": 0,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    u.update(usage)
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": model, "usage": u, "content": []}}


def tool_use(name, ts, **inp):
    return {"type": "assistant", "timestamp": ts, "message": {
        "model": "<synthetic>", "content": [
            {"type": "tool_use", "name": name, "input": inp}]}}


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class Fixture:
    """Builds a CLAUDE_HOME tree: one completed run and one in-flight run."""

    def __init__(self, root: Path):
        self.root = root
        self.proj = root / "projects" / ENCODED
        self.session = self.proj / SESSION
        self.build()

    def rundir(self, rid):
        return self.session / "subagents" / "workflows" / rid

    def build(self):
        self.session.mkdir(parents=True, exist_ok=True)
        self._completed()
        self._live()
        self._main_transcript()
        # The engine writes a run summary *at completion*, i.e. after the last
        # transcript write. wfstat uses exactly that ordering to tell a finished
        # run from a resumed one, so the fixture has to honour it.
        summ = self.session / "workflows" / "wf_donerun0001.json"
        later = os.path.getmtime(summ) + 30
        os.utime(summ, (later, later))

    # --- a finished run whose script returned a halt object -----------------
    def _completed(self):
        rid = "wf_donerun0001"
        jsonl(self.session / "workflows" / f"{rid}.json", [])
        with open(self.session / "workflows" / f"{rid}.json", "w") as fh:
            json.dump({
                "runId": rid,
                "workflowName": "review-changes",
                "status": "completed",          # engine lies; result says otherwise
                "startTime": 1_750_000_000_000,
                "durationMs": 615_000,
                "agentCount": 2,
                "totalTokens": 123_456,
                "totalToolCalls": 9,
                "defaultModel": "claude-sonnet-5",
                "result": {"halted": "spend limit reached after phase 2"},
                "workflowProgress": [
                    {"agentId": "aaaa1111", "label": "review:bugs",
                     "phaseTitle": "Review", "model": "claude-sonnet-5", "state": "completed"},
                    {"agentId": "bbbb2222", "label": "verify:auth.py",
                     "phaseTitle": "Verify", "model": "claude-opus-5", "state": "completed"},
                ],
            }, fh)

        rd = self.rundir(rid)
        jsonl(rd / "agent-aaaa1111.jsonl", [
            {"type": "user", "message": {"content": "Review the diff for bugs."}},
            assistant("claude-sonnet-5", iso(400), input_tokens=1000, output_tokens=200,
                      cache_read_input_tokens=5000, cache_creation_input_tokens=100),
            assistant("claude-sonnet-5", iso(390), input_tokens=500, output_tokens=300),
            tool_use("Edit", iso(395), file_path="/tmp/demo-project/src/auth.py"),
            tool_use("Bash", iso(394), command="pytest -q\nsecond line ignored"),
        ])
        jsonl(rd / "agent-bbbb2222.jsonl", [
            {"type": "user", "message": {"content": "Verify the finding."}},
            assistant("claude-opus-5", iso(380), input_tokens=2000, output_tokens=400),
        ])
        jsonl(rd / "journal.jsonl", [
            {"type": "started", "agentId": "aaaa1111", "key": "k1"},
            {"type": "result", "agentId": "aaaa1111", "key": "k1",
             "result": {"findings": 3}},
            {"type": "started", "agentId": "bbbb2222", "key": "k2"},
            {"type": "result", "agentId": "bbbb2222", "key": "k2", "result": "verified"},
        ])

    # --- an in-flight run with a superseded (orphaned) agent ----------------
    def _live(self):
        rid = "wf_liverun0002"
        rd = self.rundir(rid)
        jsonl(rd / "agent-cccc3333.jsonl", [
            {"type": "user", "message": {"content": "Old attempt, interrupted."}},
            assistant("claude-sonnet-5", iso(120), input_tokens=100, output_tokens=50),
        ])
        jsonl(rd / "agent-dddd4444.jsonl", [
            {"type": "user", "message": {"content": "Retry of the same step."}},
            assistant("claude-sonnet-5", iso(2), input_tokens=700, output_tokens=90),
        ])
        jsonl(rd / "journal.jsonl", [
            {"type": "started", "agentId": "cccc3333", "key": "kx"},
            {"type": "started", "agentId": "dddd4444", "key": "kx"},
        ])

    def _main_transcript(self):
        # Compact separators: real session transcripts carry no space after the
        # colon, and that is how the workflow name is recovered for a live run.
        with open(self.proj / f"{SESSION}.jsonl", "w") as fh:
            fh.write(json.dumps({"runId": "wf_liverun0002",
                                 "workflowName": "migrate-verbs"},
                                separators=(",", ":")) + "\n")


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fx = Fixture(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *args, cols=None):
        env = dict(os.environ, CLAUDE_HOME=self.tmp.name)
        # Output goes to a pipe, so wfstat leaves it unclamped unless COLUMNS
        # says otherwise — drop any inherited value so the width of whatever
        # terminal the suite is run from can't change what the tests see.
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        if cols:
            env["COLUMNS"] = str(cols)
        p = subprocess.run([sys.executable, str(SCRIPT), *args, f"--project={ENCODED}"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        return p.stdout


class CLITest(FixtureCase):
    # -- ls ------------------------------------------------------------------
    def test_ls_lists_both_runs(self):
        out = self.run_cli("ls")
        self.assertIn("wf_donerun0001", out)
        self.assertIn("wf_liverun0002", out)
        self.assertIn("review-changes", out)
        self.assertIn("(1 running)", out)

    def test_ls_derives_halted_from_result_despite_completed_status(self):
        out = self.run_cli("ls")
        line = next(l for l in out.splitlines() if "wf_donerun0001" in l)
        self.assertIn("halted", line)

    def test_ls_agent_count_leaves_orphans_out_like_live_does(self):
        # The live run has one orphaned agent and one in flight: `live` calls
        # that nothing done of one outstanding, so `ls` must not say 0/2.
        out = self.run_cli("ls")
        line = next(l for l in out.splitlines() if "wf_liverun0002" in l)
        self.assertIn(" 0/1 ", line)
        self.assertNotIn("0/2", line)

    def test_ls_names_live_run_from_session_transcript(self):
        out = self.run_cli("ls")
        line = next(l for l in out.splitlines() if "wf_liverun0002" in l)
        self.assertIn("migrate-verbs", line)
        self.assertIn("running", line)   # newest write is ~2s old, so not stalled

    # -- show ----------------------------------------------------------------
    def test_show_sums_usage_per_model_from_transcripts(self):
        out = self.run_cli("show", "wf_donerun0001")
        self.assertIn("claude-sonnet-5", out)
        self.assertIn("claude-opus-5", out)
        # sonnet: in 1000+500=1.5k, out 200+300=500; opus: in 2.0k, out 400
        sonnet = next(l for l in out.splitlines() if l.startswith("claude-sonnet-5"))
        self.assertIn("1.5k", sonnet)
        self.assertIn("500", sonnet)
        self.assertIn("halted", out.lower())

    def test_show_accepts_a_run_prefix_and_labels_agents(self):
        out = self.run_cli("show", "wf_done")
        self.assertIn("review:bugs", out)
        self.assertIn("verify:auth.py", out)

    def test_show_no_agents_flag_omits_breakdown(self):
        out = self.run_cli("show", "wf_done", "--no-agents")
        self.assertNotIn("review:bugs", out)

    def test_show_works_on_a_live_run_without_a_summary(self):
        out = self.run_cli("show", "wf_liverun0002")
        self.assertIn("LIVE", out)
        self.assertIn("claude-sonnet-5", out)

    # -- agent ---------------------------------------------------------------
    def test_agent_reports_task_result_and_activity(self):
        out = self.run_cli("agent", "aaaa")
        self.assertIn("Review the diff for bugs.", out)
        self.assertIn("findings", out)            # return value from the journal
        self.assertIn("review:bugs", out)         # label from the summary
        self.assertIn("auth.py", out)             # file touched
        self.assertIn("Edit", out)

    def test_agent_full_flag_prints_bash(self):
        out = self.run_cli("agent", "aaaa", "--full")
        self.assertIn("pytest -q", out)
        self.assertNotIn("second line ignored", out)  # only the first line is kept

    def test_agent_unknown_prefix_exits_nonzero(self):
        env = dict(os.environ, CLAUDE_HOME=self.tmp.name)
        p = subprocess.run([sys.executable, str(SCRIPT), "agent", "zzzz",
                            f"--project={ENCODED}"],
                           capture_output=True, text=True, env=env)
        self.assertNotEqual(p.returncode, 0)

    # -- live ----------------------------------------------------------------
    def test_live_separates_running_from_orphaned(self):
        out = self.run_cli("live")
        self.assertIn("wf_liverun0002", out)
        self.assertIn("orphaned", out)
        self.assertNotIn("wf_donerun0001", out)
        self.assertIn("1 orphaned", out)

    def test_live_excludes_runs_whose_summary_postdates_activity(self):
        # the completed run's summary was written after its transcripts
        out = self.run_cli("live")
        self.assertNotIn("review-changes", out)


def fake_run(n_running, n_done, rid="wf_fake00000001", done_first=False):
    """A live-run dict shaped like live_runs() returns, without the disk.

    `done_first` hands the finished agents the freshest timestamps, so a naive
    keep-the-top-N would bury every in-flight agent."""
    order = ["done"] * n_done + ["running"] * n_running if done_first else \
            ["running"] * n_running + ["done"] * n_done
    rows, states = [], {}
    for pos, st in enumerate(order):
        aid = f"{pos:08d}"
        states[aid] = st
        # descending ts: freshest first, exactly as live_runs() sorts
        rows.append({"id": aid, "model": "claude-sonnet-5", "usage": wfstat.blank(),
                     "ts": 2_000_000_000 - pos, "state": st, "label": f"step:{pos}"})
    return {"rid": rid, "session": "ses-0001", "states": states, "rows": rows,
            "total": wfstat.blank(), "newest": time.time()}


def big_live_run(fx, rid, n_done, n_running):
    """An in-flight run with more agents than any sane window can hold."""
    rd = fx.rundir(rid)
    journal = []
    for i in range(n_done + n_running):
        aid = f"{i:08d}"
        jsonl(rd / f"agent-{aid}.jsonl", [
            {"type": "user", "message": {"content": "task"}},
            assistant("claude-sonnet-5", iso(1 + i * 7),   # freshest run of the fixture
                      input_tokens=100 * i, output_tokens=10 * i)])
        journal.append({"type": "started", "agentId": aid, "key": f"k{i}"})
        if i < n_done:      # the freshest agents are the finished ones
            journal.append({"type": "result", "agentId": aid,
                            "key": f"k{i}", "result": "ok"})
    jsonl(rd / "journal.jsonl", journal)


class WidthTest(FixtureCase):
    """No line may exceed the terminal width, and nothing may vanish silently."""

    COMMANDS = (("ls",), ("show", "wf_donerun0001"), ("agent", "aaaa"), ("live",))

    def test_no_command_overflows_the_terminal_width(self):
        for cols in (40, 60, 80, 100, 132):
            for cmd in self.COMMANDS:
                out = self.run_cli(*cmd, cols=cols)
                over = [ln for ln in out.split("\n") if len(ln) > cols]
                self.assertEqual(over, [], f"{cmd} at {cols} cols overflowed")

    def test_piped_output_is_not_clamped(self):
        # A pipe has no width to respect: `wfstat live | less` must keep every
        # column, including the ones an 80-column terminal would drop.
        wide = self.run_cli("live")
        self.assertIn("CACHE-R", wide)
        self.assertIn("AGENT", wide)
        self.assertNotIn("…", wide.split("\n")[0])

    def test_narrow_terminal_drops_columns_rather_than_wrapping_rows(self):
        narrow, wide = self.run_cli("live", cols=80), self.run_cli("live", cols=120)
        self.assertIn("CACHE-R", wide)
        self.assertNotIn("CACHE-R", narrow)   # least actionable column goes first
        self.assertIn("STATE", narrow)        # the ones you watch for survive
        self.assertIn("AGENT", narrow)

    def test_status_line_wraps_instead_of_dropping_fields(self):
        # Every number on the run's status line survives at a width that cannot
        # hold it on one physical line.
        out = self.run_cli("live", cols=60)
        for field in ("started", "done", "in-flight", "orphaned",
                      "tokens:", "cache-r", "last write:"):
            self.assertIn(field, out)
        status = [ln for ln in out.split("\n") if "started" in ln]
        self.assertTrue(status and all(len(ln) <= 60 for ln in status))

    def test_wrap_fields_keeps_every_field(self):
        fields = ["alpha: 1", "beta: 22", "gamma: 333", "delta: 4444"]
        lines = wfstat.wrap_fields(fields, 20, indent="  ")
        self.assertTrue(len(lines) > 1)
        self.assertTrue(all(len(ln) <= 20 for ln in lines))
        joined = " ".join(lines)
        for f in fields:
            self.assertIn(f, joined)

    def test_wrap_fields_word_wraps_a_field_wider_than_the_window(self):
        lines = wfstat.wrap_fields(["agents: 25 started / 8 done / 17 in-flight"], 24)
        self.assertTrue(all(len(ln) <= 24 for ln in lines))
        self.assertNotIn("…", " ".join(lines))     # wrapped, not clipped
        self.assertIn("in-flight", " ".join(lines))

    def test_table_layout_drops_in_order_until_the_flex_column_fits(self):
        spec = [("NAME", None, "<"), ("A", 10, ">"), ("B", 10, ">"), ("C", 10, ">")]
        keep, flex = wfstat.table_layout(60, spec, 12, 24, ["C", "B", "A"])
        self.assertEqual(keep, ["NAME", "A", "B", "C"])   # 60 fits everything
        keep, flex = wfstat.table_layout(30, spec, 12, 24, ["C", "B", "A"])
        self.assertEqual(keep, ["NAME", "A"])
        self.assertGreaterEqual(flex, 12)
        self.assertLessEqual(wfstat.row_width(spec, keep, flex), 30)

    def test_fit_clips_only_when_it_must(self):
        self.assertEqual(wfstat.fit("abc", 10), "abc")
        self.assertEqual(wfstat.fit("abcdef", 4), "abc…")
        self.assertEqual(len(wfstat.fit("x" * 99, 20)), 20)


class FrameTest(FixtureCase):
    """`watch` frames must fit the window exactly — a frame that scrolls
    corrupts the in-place repaint on the next tick."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()          # its own tmp tree, so CLITest is unaffected
        big_live_run(cls.fx, "wf_bigrun00003", n_done=9, n_running=14)

    def pdirs(self):
        return [Path(self.tmp.name) / "projects" / ENCODED]

    def test_frame_never_exceeds_the_window(self):
        for cols in (40, 80, 120):
            for rows in (10, 14, 24, 40):
                lines = wfstat.live_frame(self.pdirs(), cols, rows).split("\n")
                self.assertLessEqual(len(lines), rows, f"{cols}x{rows} too tall")
                self.assertLessEqual(max(len(ln) for ln in lines), cols,
                                     f"{cols}x{rows} too wide")

    def test_short_window_still_names_the_run_and_admits_what_it_hid(self):
        lines = wfstat.live_frame(self.pdirs(), 80, 12).split("\n")
        self.assertTrue(any("wf_bigrun00003" in ln for ln in lines))
        self.assertTrue(any("hidden" in ln for ln in lines),
                        "a truncated frame must say so, not read as complete")

    def test_running_agents_outrank_finished_ones_for_scarce_rows(self):
        # The finished agents are the freshest here, so keeping the top N by
        # recency alone would show nothing that is still in flight.
        run = fake_run(n_running=3, n_done=12, done_first=True)
        lines = wfstat.render_live_run(run, 100, budget=10)
        self.assertLessEqual(len(lines), 10)
        shown = [ln for ln in lines if "step:" in ln]
        self.assertEqual(sum(1 for ln in shown if "running" in ln), 3,
                         "an in-flight agent lost its seat to a finished one")
        self.assertTrue(any("done" in ln and "hidden" in ln for ln in lines))

    def test_a_stalled_in_flight_agent_survives_elision(self):
        # The whole point of `watch`: an agent that started and went quiet
        # sorts last by recency, and is exactly what you need to see.
        run = fake_run(n_running=1, n_done=20, done_first=True)
        lines = wfstat.render_live_run(run, 100, budget=8)
        self.assertTrue(any("step:20" in ln and "running" in ln for ln in lines),
                        "the stalled agent was elided")

    def test_rows_stay_most_recent_first(self):
        run = fake_run(n_running=6, n_done=0)
        lines = wfstat.render_live_run(run, 100)
        shown = [ln for ln in lines if "step:" in ln]
        order = [ln.split("step:")[1].split()[0] for ln in shown]
        self.assertEqual(order, [str(i) for i in range(6)])

    def test_rows_are_shared_out_rather_than_split_evenly(self):
        # A small run must not sit on rows a large one could use.
        big, small = fake_run(20, 0, "wf_big"), fake_run(2, 0, "wf_small")
        alloc = wfstat._share_rows([len(wfstat.render_live_run(big, 100)) + 1,
                                    len(wfstat.render_live_run(small, 100)) + 1], 24)
        self.assertEqual(sum(alloc), 24)
        self.assertGreater(alloc[0], alloc[1])

    def test_no_live_runs_renders_a_frame_not_an_exception(self):
        empty = Path(self.tmp.name) / "projects" / "-nonexistent"
        frame = wfstat.live_frame([empty], 80, 24)
        self.assertIn("nothing in flight", frame)


def session_subagent(fx, aid, description, seconds_ago, finished, agent_type="general-purpose"):
    """One Agent-tool subagent of the session — not a workflow agent.

    These sit at <session>/subagents/ and carry a .meta.json instead of a
    journal entry, so both their name and their state come from elsewhere."""
    d = fx.session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"content": "do the thing"}},
            assistant("claude-sonnet-5", iso(seconds_ago + 5),
                      input_tokens=400, output_tokens=120)]
    tail = {"type": "assistant", "timestamp": iso(seconds_ago), "message": {
        "model": "claude-sonnet-5", "stop_reason": "end_turn" if finished else "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 90},
        "content": ([{"type": "text", "text": "Final answer: 42."}] if finished else
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}])}}
    jsonl(d / f"agent-{aid}.jsonl", rows + [tail])
    with open(d / f"agent-{aid}.meta.json", "w") as fh:
        json.dump({"agentType": agent_type, "description": description,
                   "toolUseId": f"toolu_{aid}", "spawnDepth": 1, "model": "sonnet"}, fh)


class LivenessTest(FixtureCase):
    """A run that has been summarised is over, whatever the mtimes say."""

    def test_dying_agents_last_write_does_not_resurrect_a_killed_run(self):
        # The real failure: a run was killed and summarised, but an agent was
        # still flushing its transcript and landed 101ms *after* the summary.
        # Comparing the summary to raw file activity read that as a resume and
        # kept the corpse on screen, labelled "stalled?", for five minutes.
        rd = self.fx.rundir("wf_killedrun003")
        jsonl(rd / "journal.jsonl", [{"type": "started", "agentId": "eeee5555"}])
        jsonl(rd / "agent-eeee5555.jsonl", [
            assistant("claude-sonnet-5", iso(4), input_tokens=10, output_tokens=5)])
        summ = self.fx.session / "workflows" / "wf_killedrun003.json"
        with open(summ, "w") as fh:
            json.dump({"runId": "wf_killedrun003", "workflowName": "doomed",
                       "status": "killed", "error": "Error: Workflow aborted"}, fh)
        journal_t = os.path.getmtime(rd / "journal.jsonl")
        os.utime(summ, (journal_t + 60, journal_t + 60))                 # summary after
        os.utime(rd / "agent-eeee5555.jsonl",
                 (journal_t + 60.101, journal_t + 60.101))               # death rattle
        try:
            live = {rid for rid, _rd, _p in wfstat.live_run_dirs(
                [Path(self.tmp.name) / "projects" / ENCODED])}
            self.assertNotIn("wf_killedrun003", live)
        finally:
            for f in list(rd.iterdir()):
                f.unlink()
            rd.rmdir()
            summ.unlink()

    def test_a_resumed_run_whose_journal_advanced_is_still_live(self):
        # The case the rule must not break: a stale summary from an earlier
        # halt, but the journal has since recorded new work.
        rd = self.fx.rundir("wf_resumedrun04")
        jsonl(rd / "agent-ffff6666.jsonl", [
            assistant("claude-sonnet-5", iso(3), input_tokens=10, output_tokens=5)])
        summ = self.fx.session / "workflows" / "wf_resumedrun04.json"
        with open(summ, "w") as fh:
            json.dump({"runId": "wf_resumedrun04", "workflowName": "resumed",
                       "status": "halted"}, fh)
        base = os.path.getmtime(summ)
        jsonl(rd / "journal.jsonl", [{"type": "started", "agentId": "ffff6666"}])
        os.utime(rd / "journal.jsonl", (base + 30, base + 30))   # new work after summary
        try:
            live = {rid for rid, _rd, _p in wfstat.live_run_dirs(
                [Path(self.tmp.name) / "projects" / ENCODED])}
            self.assertIn("wf_resumedrun04", live)
        finally:
            for f in list(rd.iterdir()):
                f.unlink()
            rd.rmdir()
            summ.unlink()


class TornWriteTest(FixtureCase):
    """Files are read while the engine is still appending to them."""

    def test_a_half_written_journal_line_does_not_crash_ls(self):
        rd = self.fx.rundir("wf_tornjournal5")
        jsonl(rd / "agent-9999aaaa.jsonl", [
            assistant("claude-sonnet-5", iso(2), input_tokens=10, output_tokens=5)])
        with open(rd / "journal.jsonl", "w") as fh:
            fh.write(json.dumps({"type": "started", "agentId": "9999aaaa"}) + "\n")
            fh.write('{"type": "result", "agentId": "99')      # mid-append
        out = self.run_cli("ls")
        line = next(l for l in out.splitlines() if "wf_tornjournal5" in l)
        self.assertIn("0/1", line)
        self.assertIn("wf_tornjournal5", self.run_cli("live"))

    def test_invalid_utf8_in_a_transcript_is_not_fatal(self):
        rd = self.fx.rundir("wf_badbytes0006")
        jsonl(rd / "journal.jsonl", [{"type": "started", "agentId": "8888bbbb"}])
        jsonl(rd / "agent-8888bbbb.jsonl", [
            {"type": "user", "message": {"content": "task"}},
            assistant("claude-sonnet-5", iso(2), input_tokens=10, output_tokens=5)])
        with open(rd / "agent-8888bbbb.jsonl", "ab") as fh:
            fh.write(b'{"type": "user", "message": {"content": "\xff\xfe"}}\n')
        env = dict(os.environ, CLAUDE_HOME=self.tmp.name, LC_ALL="C", LANG="C",
                   PYTHONUTF8="0", PYTHONIOENCODING="utf-8")
        for cmd in (("ls",), ("live",), ("agent", "8888bbbb")):
            p = subprocess.run([sys.executable, str(SCRIPT), *cmd, f"--project={ENCODED}"],
                               capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, msg=f"{cmd}: {p.stderr}")


class PrettySummaryTest(FixtureCase):
    """Nothing promises a run summary is written on one line."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        summ = cls.fx.session / "workflows" / "wf_donerun0001.json"
        mtime = os.path.getmtime(summ)
        with open(summ) as fh:
            d = json.load(fh)
        with open(summ, "w") as fh:
            json.dump(d, fh, indent=2)
        os.utime(summ, (mtime, mtime))      # keep it postdating the journal

    def test_agent_finds_its_label_in_a_pretty_printed_summary(self):
        out = self.run_cli("agent", "aaaa1111")
        self.assertIn("review:bugs", out)
        self.assertIn("review-changes", out)

    def test_live_labels_bridge_through_a_pretty_printed_summary(self):
        label_of = wfstat.live_label_map(self.fx.rundir("wf_donerun0001"))
        self.assertEqual(label_of("bbbb2222"), "verify:auth.py")


class SubagentTest(FixtureCase):
    """Agent-tool subagents: the ones that are not part of any workflow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        session_subagent(cls.fx, "1111aaaa", "audit the verb catalog", 3, finished=False)
        session_subagent(cls.fx, "2222bbbb", "critique the design", 8, finished=True)
        session_subagent(cls.fx, "3333cccc", "ancient history", 9999, finished=True)

    def test_live_reports_session_subagents(self):
        out = self.run_cli("live")
        self.assertIn("audit the verb catalog", out)
        self.assertIn("critique the design", out)
        self.assertIn("subagents", out)

    def test_state_comes_from_the_transcript_tail_not_the_mtime(self):
        d = self.fx.session / "subagents"
        self.assertEqual(wfstat.subagent_state(d / "agent-1111aaaa.jsonl"), "running")
        self.assertEqual(wfstat.subagent_state(d / "agent-2222bbbb.jsonl"), "done")

    def test_stale_subagents_are_not_reported_live(self):
        out = self.run_cli("live")
        self.assertNotIn("ancient history", out)

    def test_agent_command_works_on_a_session_subagent(self):
        out = self.run_cli("agent", "2222bbbb")
        self.assertIn("critique the design", out)
        self.assertIn("general-purpose", out)
        self.assertIn("session subagent", out)
        self.assertIn("Final answer: 42.", out)   # its return value is its last message

    def test_workflow_agents_still_resolve_unambiguously(self):
        out = self.run_cli("agent", "aaaa1111")
        self.assertIn("review:bugs", out)
        self.assertNotIn("session subagent", out)

    def test_blocks_are_ordered_most_recently_active_first(self):
        blocks = wfstat.live_blocks([Path(self.tmp.name) / "projects" / ENCODED])
        stamps = [d["newest"] for _r, d in blocks]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


class JSONTest(FixtureCase):
    """--json: one parseable document per command, carrying the same facts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        session_subagent(cls.fx, "4444dddd", "summarise the logs", 4, finished=True)

    def run_json(self, *args, cols=None):
        doc = json.loads(self.run_cli(*args, "--json", cols=cols))
        self.assertEqual(doc["schema"], 1)
        return doc

    def test_ls_lists_live_and_finished_runs_in_one_shape(self):
        runs = self.run_json("ls")["runs"]
        by_id = {r["run_id"]: r for r in runs}
        self.assertEqual(runs[0]["run_id"], "wf_liverun0002")     # in-flight first
        live, done = by_id["wf_liverun0002"], by_id["wf_donerun0001"]
        self.assertEqual(set(live), set(done))                    # uniform records
        self.assertTrue(live["live"])
        self.assertEqual(live["name"], "migrate-verbs")
        self.assertEqual(live["agent_states"], {"done": 0, "running": 1, "orphaned": 1})
        self.assertEqual(live["total_tokens"], 800 + 140)
        self.assertIsNone(live["duration_ms"])
        self.assertFalse(done["live"])
        self.assertEqual(done["status"], "halted")
        self.assertEqual(done["raw_status"], "completed")
        self.assertEqual(done["reason"], "spend limit reached after phase 2")
        self.assertEqual(done["started_at"], "2025-06-15T15:06:40.000Z")
        self.assertEqual(done["duration_ms"], 615_000)

    def test_show_carries_exact_usage_and_labels(self):
        d = self.run_json("show", "wf_done")
        sonnet = d["usage"]["by_model"]["claude-sonnet-5"]
        self.assertEqual(sonnet, {"input": 1500, "output": 500, "cache_read": 5000,
                                  "cache_create": 100, "turns": 2})
        self.assertEqual(d["usage"]["total"]["output"], 900)
        self.assertEqual(d["usage"]["wire_input"], 3500 + 5000 + 100)
        labels = {a["agent_id"]: a["label"] for a in d["agents"]}
        self.assertEqual(labels, {"aaaa1111": "review:bugs", "bbbb2222": "verify:auth.py"})
        self.assertEqual(d["agents"][0]["agent_id"], "aaaa1111")  # 500 out beats 400

    def test_show_no_agents_omits_the_list(self):
        self.assertNotIn("agents", self.run_json("show", "wf_done", "--no-agents"))

    def test_show_on_a_live_run(self):
        d = self.run_json("show", "wf_liverun0002")
        self.assertTrue(d["live"])
        self.assertEqual(d["name"], "migrate-verbs")
        self.assertIsNone(d["agents"][0]["label"])     # unknown is null, not "(id)"

    def test_agent_result_stays_structured_and_paths_stay_whole(self):
        d = self.run_json("agent", "aaaa")
        self.assertEqual(d["kind"], "workflow")
        self.assertEqual(d["result"], {"findings": 3})     # JSON, not a string
        self.assertEqual(d["files"], {"/tmp/demo-project/src/auth.py": 1})
        self.assertEqual(d["bash"], ["pytest -q"])
        self.assertEqual(d["label"], "review:bugs")
        self.assertEqual(d["usage"]["turns"], 2)

    def test_agent_on_a_session_subagent(self):
        d = self.run_json("agent", "4444dddd")
        self.assertEqual(d["kind"], "subagent")
        self.assertIsNone(d["run_id"])
        self.assertEqual(d["label"], "summarise the logs")
        self.assertEqual(d["state"], "done")
        self.assertEqual(d["result"], "Final answer: 42.")

    def test_live_reports_states_and_subagents(self):
        d = self.run_json("live")
        self.assertEqual(d["window_s"], wfstat.LIVE_WINDOW)
        run = next(r for r in d["runs"] if r["run_id"] == "wf_liverun0002")
        states = {a["agent_id"]: a["state"] for a in run["agents"]}
        self.assertEqual(states, {"cccc3333": "orphaned", "dddd4444": "running"})
        self.assertTrue(all(a["label"] is None for a in run["agents"]))
        self.assertNotIn("wf_donerun0001", {r["run_id"] for r in d["runs"]})
        subs = [a for s in d["subagents"] for a in s["agents"]]
        self.assertIn("summarise the logs", {a["description"] for a in subs})

    def test_json_is_never_clamped_to_the_terminal(self):
        # A long label in a 40-column window would be clipped in a table.
        self.assertEqual(self.run_json("agent", "bbbb", cols=40)["label"], "verify:auth.py")
        self.assertEqual(self.run_json("show", "wf_done", cols=40)["name"], "review-changes")

    def test_global_flags_work_before_the_subcommand_too(self):
        env = dict(os.environ, CLAUDE_HOME=self.tmp.name)
        for argv in (["--json", f"--project={ENCODED}", "ls"],
                     [f"--project={ENCODED}", "ls", "--json"],
                     ["--json", "ls", f"--project={ENCODED}"]):
            p = subprocess.run([sys.executable, str(SCRIPT), *argv],
                               capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, msg=f"{argv}: {p.stderr}")
            self.assertEqual(json.loads(p.stdout)["schema"], 1)

    def test_watch_refuses_json(self):
        env = dict(os.environ, CLAUDE_HOME=self.tmp.name)
        p = subprocess.run([sys.executable, str(SCRIPT), "watch", "--json",
                            f"--project={ENCODED}"], capture_output=True, text=True, env=env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("live --json", p.stderr)
        self.assertEqual(p.stdout, "")


class UnitTest(unittest.TestCase):
    def test_encode_project(self):
        self.assertEqual(wfstat.encode_project(Path(PROJECT)), ENCODED)

    def test_encode_project_replaces_every_non_alnum_char(self):
        # Not just the slashes: dots, underscores and spaces all become '-',
        # one for one, so a dotfile directory yields a double dash.
        self.assertEqual(wfstat.encode_project(Path("/home/me/my.proj_x")),
                         "-home-me-my-proj-x")
        self.assertEqual(wfstat.encode_project(Path("/Users/me/.config/a b")),
                         "-Users-me--config-a-b")

    def test_project_is_autodetected_for_a_dotted_path(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory(suffix=".dotted_dir") as proj:
            proj = os.path.realpath(proj)
            # Spelled out here rather than via encode_project, so the test
            # can't agree with a wrong encoder by construction.
            enc = "".join(c if c.isascii() and c.isalnum() else "-" for c in proj)
            (Path(home) / "projects" / enc).mkdir(parents=True)
            env = dict(os.environ, CLAUDE_HOME=home)
            p = subprocess.run([sys.executable, str(SCRIPT), "ls"], cwd=proj,
                               capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, msg=p.stderr)
            self.assertIn("no workflow runs found", p.stdout)

    def test_effective_status_trusts_result_over_status(self):
        st, reason = wfstat.effective_status(
            {"status": "completed", "result": {"halted": "spend limit"}})
        self.assertEqual(st, "halted")
        self.assertEqual(reason, "spend limit")

    def test_effective_status_scans_trailing_logs(self):
        st, reason = wfstat.effective_status(
            {"status": "completed", "logs": ["fine", "agent returned null"]})
        self.assertEqual(st, "halted")
        self.assertIn("returned null", reason)

    def test_effective_status_passes_clean_runs_through(self):
        self.assertEqual(wfstat.effective_status({"status": "completed"}), ("completed", None))

    def test_short_path_relativizes_to_cwd(self):
        target = Path.cwd() / "pkg" / "mod.py"
        self.assertEqual(wfstat._short_path(str(target)), "pkg/mod.py")

    def test_short_path_elides_unrelated_absolute_paths(self):
        self.assertEqual(wfstat._short_path("/a/b/c/d/e.py"), "…/c/d/e.py")

    def test_human_numbers(self):
        self.assertEqual(wfstat.h(999), "999")
        self.assertEqual(wfstat.h(1500), "1.5k")
        self.assertEqual(wfstat.h(2_400_000), "2.4M")
        self.assertEqual(wfstat.h(None), "-")

    def test_duration_formatting(self):
        self.assertEqual(wfstat.dur(0), "-")
        self.assertEqual(wfstat.dur(45_000), "45s")
        self.assertEqual(wfstat.dur(615_000), "10m15s")
        self.assertEqual(wfstat.dur(7_260_000), "2h01m")


if __name__ == "__main__":
    unittest.main()
