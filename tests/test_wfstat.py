"""End-to-end tests for wfstat against a synthetic CLAUDE_HOME fixture tree.

The CLI is exercised as a subprocess so the fixtures are read exactly the way a
real run would read them. Nothing outside the temp directory is touched.
"""
import json
import os
import subprocess
import sys
import tempfile
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


class CLITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fx = Fixture(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ, CLAUDE_HOME=self.tmp.name)
        p = subprocess.run([sys.executable, str(SCRIPT), *args, f"--project={ENCODED}"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        return p.stdout

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


class UnitTest(unittest.TestCase):
    def test_encode_project(self):
        self.assertEqual(wfstat.encode_project(Path(PROJECT)), ENCODED)

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
