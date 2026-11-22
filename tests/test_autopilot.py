"""Unit tests for autopilot.py, spawn.py (task file) and owner_update.py. Temporary files only: a temp state
folder and stub transcripts. No claude process starts (RUN and the live list are stubs).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "hubtree")
sys.path.insert(0, HERE)
import tree_lib as T  # noqa: E402
import autopilot as A  # noqa: E402

NOW = 1795100000.0  # a fixed "now"; resets are relative to it


def limit_rec(reset, kind="five_hour", text=None, with_epoch=True):
    r = {"type": "assistant", "timestamp": "2026-11-19T09:00:00Z",
         "message": {"model": "<synthetic>", "role": "assistant", "stop_reason": "stop_sequence",
                     "content": [{"type": "text", "text": text or "You've hit your 5-hour limit · resets 3:30am "
                                                                  "(America/New_York)"}]},
         "apiError": "usage_limit_reached", "error": "rate_limit", "isApiErrorMessage": True}
    if with_epoch:
        r["apiErrorParams"] = {"rate_limit_info": {"status": "rejected", "resetsAt": reset, "rateLimitType": kind}}
    else:
        r.pop("apiError")
        r.pop("error")
    return r


def user(text):
    return {"type": "user", "timestamp": "2026-11-19T08:59:00Z", "message": {"role": "user", "content": text}}


def answer(text="done"):
    return {"type": "assistant", "timestamp": "2026-11-19T08:59:30Z",
            "message": {"model": "claude-haiku-5-5", "role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]}}


TOOL_RESULT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}
DONE = {"type": "system", "subtype": "turn_duration"}
NOTICE = user("<task-notification>\n<status>failed</status>\nAgent failed: You've hit your limit</task-notification>")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = dict(os.environ)
        os.environ["HUBTREE_STATE"] = self.dir
        os.environ.pop("HUBTREE_AUTOPILOT_OFF", None)
        self.saved = (T.STATE, T.REGISTRY, T.PENDING, T.live_agents, A.RUN[0], A.STOP_WAIT_S)
        T.STATE, T.REGISTRY, T.PENDING = self.dir, os.path.join(self.dir, "sessions.jsonl"), os.path.join(self.dir, "p")
        self.live = {}
        T.live_agents = lambda max_age=None: self.live
        self.calls = []
        A.RUN[0] = self.fake_run
        A.STOP_WAIT_S = 2
        self.reset = NOW + 3600

    def tearDown(self):
        T.STATE, T.REGISTRY, T.PENDING, T.live_agents, A.RUN[0], A.STOP_WAIT_S = self.saved
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_run(self, argv, cwd=None, timeout=60):
        self.calls.append(list(argv))
        if argv[1] == "stop":
            self.live.pop(self.stop_sid, None)
            return 0, f"stopped {argv[2]}"
        return 0, f"backgrounded · {argv[3][:8]}"

    def transcript(self, recs, name="t.jsonl"):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return p


class TestCheck(Base):
    def test_limit_at_end_is_planned(self):
        tp = self.transcript([user("build it"), answer(), TOOL_RESULT, limit_rec(self.reset), DONE])
        c = A.check("S1", tp, NOW)
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["due"], self.reset + 120)
        self.assertEqual(c["key"], str(int(self.reset // 60)))
        self.assertEqual(c["label"], time.strftime("%H:%M", time.localtime(self.reset + 120)))

    def test_task_notices_after_the_limit_keep_the_plan(self):
        tp = self.transcript([user("go"), limit_rec(self.reset), DONE, NOTICE, limit_rec(self.reset), DONE])
        self.assertTrue(A.check("S1", tp, NOW)["ok"])

    def test_interrupted_turn_gets_none(self):
        tp = self.transcript([user("go"), limit_rec(self.reset), user("[Request interrupted by user]"), DONE])
        c = A.check("S1", tp, NOW)
        self.assertFalse(c["ok"])
        self.assertIn("interrupted", c["why"])

    def test_user_wrote_after_the_limit(self):
        tp = self.transcript([user("go"), limit_rec(self.reset), user("what now"), answer("here"), DONE])
        self.assertFalse(A.check("S1", tp, NOW)["ok"])

    def test_stop_word_before_the_limit(self):
        tp = self.transcript([user("go"), answer(), user("stop now please"), limit_rec(self.reset), DONE])
        c = A.check("S1", tp, NOW)
        self.assertFalse(c["ok"])
        self.assertIn("stop", c["why"])

    def test_no_limit(self):
        tp = self.transcript([user("go"), answer(), DONE])
        self.assertFalse(A.check("S1", tp, NOW)["ok"])

    def test_text_only_limit_uses_the_text_parser(self):
        tp = self.transcript([user("go"), limit_rec(0, text="You've hit your session limit · resets 3:30am",
                                                    with_epoch=False), DONE])
        c = A.check("S1", tp, NOW)
        self.assertTrue(c["ok"], c)
        self.assertEqual(time.strftime("%H:%M", time.localtime(c["reset"])), "03:30")

    def test_parse_reset_forms(self):
        ref = NOW
        r = A.parse_reset("You've hit your limit · resets 3pm", ref)
        self.assertEqual(time.strftime("%H:%M", time.localtime(r)), "15:00")
        self.assertGreater(r, ref)
        self.assertLessEqual(r - ref, 86400)
        r = A.parse_reset("resets 9:15am (America/New_York)", ref)
        self.assertGreater(r, ref)
        r = A.parse_reset("resets Dec 3, 4:05am", ref)
        self.assertEqual(time.strftime("%m-%d %H:%M", time.localtime(r)), "12-03 04:05")
        self.assertIsNone(A.parse_reset("no reset time here", ref))

    def test_weekly_limit_shows_the_date(self):
        reset = NOW + 3 * 86400 + 600
        tp = self.transcript([user("go"), limit_rec(reset, kind="seven_day"), DONE])
        c = A.check("S1", tp, NOW)
        self.assertTrue(c["ok"])
        self.assertEqual(c["kind"], "seven_day")
        self.assertEqual(c["label"], time.strftime("%a %d %b %H:%M", time.localtime(reset + 120)))


class TestFire(Base):
    def setUp(self):
        super().setUp()
        self.tp = self.transcript([user("go"), limit_rec(self.reset), DONE])
        self.key = A.check("S1", self.tp, NOW)["key"]

    def test_exactly_once_at_the_due_time(self):
        self.assertFalse(A.fire("S1", self.key, tp=self.tp, now=self.reset + 60)["ok"])  # before reset + 2 min
        r = A.fire("S1", self.key, tp=self.tp, now=self.reset + 121)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["prompt"], A.PROMPT)
        again = A.fire("S1", self.key, tp=self.tp, now=self.reset + 200)
        self.assertFalse(again["ok"])
        self.assertIn("once", again["why"])
        with open(os.path.join(self.dir, "autocontinue", "log.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_kill_blocks(self):
        open(os.path.join(self.dir, "KILL"), "w").close()
        r = A.fire("S1", self.key, tp=self.tp, now=self.reset + 121)
        self.assertFalse(r["ok"])
        self.assertIn("KILL", r["why"])

    def test_off_switch(self):
        A.set_off("S1", True)
        self.assertFalse(A.fire("S1", self.key, tp=self.tp, now=self.reset + 121)["ok"])
        A.set_off("S1", False)
        self.assertTrue(A.fire("S1", self.key, tp=self.tp, now=self.reset + 121)["ok"])

    def test_config_switch(self):
        os.environ["HUBTREE_AUTOPILOT_OFF"] = "1"
        self.assertFalse(A.fire("S1", self.key, tp=self.tp, now=self.reset + 121)["ok"])

    def test_wrong_key_refused(self):
        self.assertFalse(A.fire("S1", "123", tp=self.tp, now=self.reset + 121)["ok"])

    def test_cli_exit_codes(self):
        self.assertEqual(A.main(["fire", "--sid", "S1", "--key", self.key, "--transcript", self.tp,
                                 "--now", str(self.reset + 121)]), 0)
        self.assertEqual(A.main(["fire", "--sid", "S1", "--key", self.key, "--transcript", self.tp,
                                 "--now", str(self.reset + 122)]), 3)


class TestScan(Base):
    def register(self, sid, auto=True, name="hub/x~auto"):
        tp = self.transcript([user("go"), limit_rec(self.reset), DONE], name=sid + ".jsonl")
        T.append({"event": "start", "session_id": sid, "name": name, "auto": auto, "cwd": self.dir,
                  "transcript": tp, "node": "hub", "level": 1})
        return tp

    def test_stopped_background_session_continues_once(self):
        self.register("B1")
        self.assertEqual(A.scan(now=self.reset + 60), [])  # not due
        acts = A.scan(now=self.reset + 130)
        self.assertEqual([a["act"] for a in acts], ["continued"], acts)
        self.assertEqual(self.calls, [["claude", "--bg", "--resume", "B1", A.PROMPT]])
        self.assertEqual(A.scan(now=self.reset + 500), [])
        self.assertEqual(len(self.calls), 1)

    def test_live_background_session_waits_for_its_mod_then_stop_and_wake(self):
        self.register("B2")
        self.stop_sid = "B2"
        self.live["B2"] = {"kind": "background", "pid": 123, "id": "b2short", "state": "done"}
        acts = A.scan(now=self.reset + 130)
        self.assertEqual([a["act"] for a in acts], ["wait"])
        self.assertEqual(self.calls, [])
        acts = A.scan(now=self.reset + 120 + A.GRACE_S + 1)
        self.assertEqual([a["act"] for a in acts], ["continued"], acts)
        self.assertEqual(self.calls, [["claude", "stop", "b2short"], ["claude", "--bg", "--resume", "B2", A.PROMPT]])

    def test_tab_and_kill_are_skipped(self):
        self.register("I1", auto=False, name="hub/tab")
        self.live["I1"] = {"kind": "interactive", "pid": 9, "status": "idle"}
        self.assertEqual(A.scan(now=self.reset + 999), [])
        self.register("B3")
        open(os.path.join(self.dir, "KILL"), "w").close()
        self.assertEqual(A.scan(now=self.reset + 999), [{"why": "KILL file set"}])
        self.assertEqual(self.calls, [])

    def test_mod_fired_first_so_the_daemon_skips(self):
        tp = self.register("B4")
        key = A.check("B4", tp, NOW)["key"]
        self.assertTrue(A.fire("B4", key, by="mod", tp=tp, now=self.reset + 121)["ok"])
        self.assertEqual(A.scan(now=self.reset + 2000), [])
        self.assertEqual(self.calls, [])


class TestSpawnAndOwnerUpdate(Base):
    """The task file (autopilot block, --interactive, template override) and owner_update.py."""

    def spawn(self, *extra):
        import io
        import contextlib
        import spawn
        import owner_update  # noqa: F401
        cwd = os.path.join(T.HUB, "work", "app")
        os.makedirs(cwd, exist_ok=True)
        T.append({"event": "start", "session_id": "P1", "name": "work/app", "node": "work/app", "cwd": cwd, "level": 2})
        started = []

        class FakeRun:
            returncode, stdout, stderr = 0, "backgrounded", ""

        saved = (spawn.subprocess.run, sys.argv)
        spawn.subprocess.run = lambda *a, **k: started.append(a[0]) or FakeRun()
        sys.argv = ["spawn.py", "--parent", "P1", "--name", "child-x", "--task", "Do a small thing.", *extra]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                try:
                    spawn.main()
                except SystemExit:
                    pass
        finally:
            spawn.subprocess.run, sys.argv = saved
        res = json.loads(out.getvalue().strip().splitlines()[-1])
        with open(res["task_file"], encoding="utf-8") as f:
            return res, f.read(), started

    def test_autopilot_is_the_default(self):
        res, text, started = self.spawn("--auto")
        self.assertTrue(res["autopilot"])
        self.assertEqual(res["name"], "work/app/child-x~auto")
        self.assertIn("## Autopilot: this session works without the user", text)
        self.assertIn("## Owner updates", text)
        self.assertLess(text.index("## Owner updates"), text.index("## Rules for this session"))
        self.assertIn("result --name", text)
        with open(res["task_file"][:-3] + ".task.json", encoding="utf-8") as f:
            self.assertTrue(json.load(f)["autopilot"])
        self.assertEqual(started[0][:2], ["claude", "--bg"])
        self.assertNotIn("--dangerously-skip-permissions", started[0])
        self.assertNotIn("--remote-control", started[0])

    def test_interactive_has_no_autopilot_block(self):
        res, text, _ = self.spawn("--auto", "--interactive")
        self.assertFalse(res["autopilot"])
        self.assertNotIn("## Autopilot", text)
        self.assertIn("## Owner updates", text)

    def test_tab_starts_through_the_terminal_layer(self):
        log = os.path.join(self.dir, "term.log")
        os.environ["HUBTREE_TERM_LOG"] = log
        res, _, started = self.spawn()
        self.assertEqual(res["mode"], "tab")
        self.assertEqual(started, [])  # no direct subprocess call
        with open(log, encoding="utf-8") as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual(row["argv"][0], "claude")
        self.assertIn("--name", row["argv"])
        self.assertEqual(row["title"], res["name"])

    def test_user_template_replaces_the_default(self):
        import htconf
        with open(os.path.join(htconf.home(), "task_template.md"), "w", encoding="utf-8") as f:
            f.write("MY RULES for ${name}\n${task}\n${updates}")
        try:
            res, text, _ = self.spawn("--auto")
        finally:
            os.remove(os.path.join(htconf.home(), "task_template.md"))
        self.assertTrue(text.startswith("MY RULES for work/app/child-x~auto"))
        self.assertIn("Do a small thing.", text)
        self.assertNotIn("## Rules for this session", text)

    def test_owner_update_goes_into_the_section(self):
        import owner_update as O
        res, _, _ = self.spawn("--auto")
        name = res["name"]
        sid = "C1"
        T.append({"event": "start", "session_id": sid, "name": name + "-live", "task": res["task_file"],
                  "node": "work/app"})
        self.assertEqual(O.main(["--child", sid, "--words", "Use blue, not red.", "--by", "work/app"]), 0)
        O.main(["--child", sid, "--words", "Actually green.", "--by", "work/app"])
        with open(res["task_file"], encoding="utf-8") as f:
            text = f.read()
        sec = text[text.index("## Owner updates"):text.index("## Autopilot")]
        self.assertNotIn("(no updates yet)", sec)
        self.assertIn('"Use blue, not red."', sec)
        self.assertLess(sec.index("Use blue"), sec.index("Actually green"))

    def test_owner_update_adds_the_section_to_an_old_task_file(self):
        import owner_update as O
        p = os.path.join(self.dir, "old.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# Task\n\nDo it.\n\n## When the task is done\n\n1. Report.\n")
        O.add_update(p, "Stop after step 2.", "parent")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.rstrip().endswith('"Stop after step 2."'))
        self.assertIn("## When the task is done", text)
