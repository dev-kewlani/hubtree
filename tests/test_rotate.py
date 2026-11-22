"""Unit tests for rotate.py and the rotate event of tree_lib. Temporary files only:
no tab opens, no process ends, the real registry stays untouched.
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
import rotate as R  # noqa: E402
import session_hook  # noqa: E402


def usage(n):
    return {"input_tokens": 10, "cache_creation_input_tokens": n - 110, "cache_read_input_tokens": 100,
            "output_tokens": 999}


def rec_user(text, ts="2026-10-31T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def rec_asst(text, n=None, ts="2026-10-31T10:00:05Z", model="claude-haiku-4-5", stop="end_turn"):
    m = {"role": "assistant", "model": model, "stop_reason": stop, "content": [{"type": "text", "text": text}]}
    if n:
        m["usage"] = usage(n)
    return {"type": "assistant", "timestamp": ts, "message": m}


DONE = {"type": "system", "subtype": "turn_duration"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = dict(os.environ)
        os.environ["HUBTREE_STATE"] = self.dir
        os.environ.pop("HUBTREE_ROTATE_OFF", None)
        self.saved = (T.STATE, T.REGISTRY, T.PENDING)
        T.STATE, T.REGISTRY, T.PENDING = self.dir, os.path.join(self.dir, "sessions.jsonl"), os.path.join(self.dir, "pending")
        self.launched, self.detached = [], []
        self.orig = (R.launch, R.start_detached, R._toml)
        R._toml = lambda: {}
        R.launch = lambda *a, **k: self.launched.append(a) or ["fake"]
        R.start_detached = lambda argv: self.detached.append(argv)
        self.tp = os.path.join(self.dir, "old.jsonl")
        self.cwd = os.path.join(T.HUB, "work", "app")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        T.STATE, T.REGISTRY, T.PENDING = self.saved
        R.launch, R.start_detached, R._toml = self.orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def transcript(self, records):
        with open(self.tp, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def register(self, sid="OLD", name="work/app/task", parent="P", auto=False):
        T.append({"event": "start", "session_id": "P", "name": "work/app", "node": "work/app",
                  "level": 2, "cwd": self.cwd})
        T.append({"event": "start", "session_id": sid, "name": name, "parent": parent, "node": "work/app",
                  "level": 3, "cwd": self.cwd, "auto": auto, "tab": not auto,
                  "transcript": self.tp})


class Context(Base):
    def test_transcript_usage_sums_the_three_input_fields(self):
        self.transcript([rec_user("hi"), rec_asst("a", 5000), rec_asst("b", 7000),
                         {"type": "assistant", "isSidechain": True, "message": {"usage": usage(90000)}}])
        tok, model, _ = R.transcript_usage(self.tp)
        self.assertEqual(tok, 7000)
        self.assertEqual(model, "claude-haiku-4-5")

    def test_compact_boundary_after_usage_gives_zero(self):
        self.transcript([rec_asst("a", 50000), {"type": "system", "subtype": "compact_boundary",
                                               "timestamp": "2026-10-31T11:00:00Z"}])
        self.assertEqual(R.transcript_usage(self.tp)[0], 0)

    def test_statusline_value_wins_when_larger_and_newer_than_a_boundary(self):
        self.transcript([rec_asst("a", 5000)])
        R.write_context({"session_id": "S1", "transcript_path": self.tp, "context_window": {
            "current_usage": usage(9000), "context_window_size": 200000, "used_percentage": 4.5}})
        self.assertEqual(R.context_tokens("S1", self.tp)[:2], (9000, "statusline"))
        self.transcript([rec_asst("a", 5000), {"type": "system", "subtype": "compact_boundary",
                                              "timestamp": "2099-01-01T00:00:00Z"}])
        self.assertEqual(R.context_tokens("S1", self.tp)[0], 0)

    def test_percentage_fallback(self):
        R.write_context({"session_id": "S2", "context_window": {"context_window_size": 200000, "used_percentage": 10}})
        with open(os.path.join(self.dir, "context", "S2.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["tokens"], 20000)

    def test_config_env_and_handoff_share(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "30000"
        c = R.config()
        self.assertEqual(c["threshold"], 30000)
        self.assertEqual(c["handoff_tokens"], 9000)
        os.environ["HUBTREE_ROTATE_OFF"] = "1"
        self.assertFalse(R.config()["enabled"])


class Trigger(Base):
    def test_below_threshold_does_nothing(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "30000"
        self.register()
        self.transcript([rec_user("hi"), rec_asst("a", 29999)])
        self.assertIsNone(R.prompt_step({"session_id": "OLD", "transcript_path": self.tp, "prompt": "x"}))
        self.assertEqual(self.launched, [])

    def test_rotation_writes_handoff_pending_and_state(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "30000"
        self.register()
        self.transcript([rec_user("The code word is PELICAN-42."), rec_asst("Noted.", 31000), DONE])
        prompt = "What was the code word? ```keep``` this verbatim"
        msg = R.prompt_step({"session_id": "OLD", "transcript_path": self.tp, "prompt": prompt, "cwd": T.HUB})
        self.assertIn("A new tab named work/app/task opens now", msg)
        kind, name, cwd, new_sid, model, first = self.launched[0]
        self.assertEqual((kind, name, cwd, model), ("tab", "work/app/task", self.cwd, "claude-haiku-4-5"))
        st = R._load_state("OLD")
        self.assertEqual((st["stage"], st["new"]), ("launched", new_sid))
        text = open(st["handoff"], encoding="utf-8").read()
        self.assertIn(prompt, text)
        self.assertIn("PELICAN-42", text)
        self.assertIn(st["handoff"], first)
        pend = json.load(open(os.path.join(T.PENDING, T.slug(name, 200) + ".json"), encoding="utf-8"))
        self.assertEqual((pend["rotated_from"], pend["parent"], pend["tab"]), ("OLD", "P", True))
        self.assertEqual(len(self.detached), 1)
        again = R.prompt_step({"session_id": "OLD", "transcript_path": self.tp, "prompt": "y"})
        self.assertIn("already rotates", again)
        self.assertEqual(len(self.launched), 1)

    def test_unregistered_session_does_not_rotate(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "100"
        self.transcript([rec_asst("a", 5000)])
        self.assertIsNone(R.prompt_step({"session_id": "NOPE", "transcript_path": self.tp, "prompt": "x"}))

    def test_paused_after_a_failed_start(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "100"
        self.register()
        self.transcript([rec_asst("a", 5000)])
        open(os.path.join(R.rot_dir(), "OLD.failed"), "w").close()
        self.assertIsNone(R.prompt_step({"session_id": "OLD", "transcript_path": self.tp, "prompt": "x"}))

    def test_background_kind(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "100"
        self.register(auto=True, name="work/app/job~auto")
        self.transcript([rec_asst("a", 5000)])
        R.prompt_step({"session_id": "OLD", "transcript_path": self.tp, "prompt": "x"})
        self.assertEqual(self.launched[0][0], "background")

    def test_handoff_budget(self):
        self.transcript([rec_user("u%d " % i + "x" * 900) for i in range(50)])
        out = R.last_turns(self.tp, 5000)
        self.assertLessEqual(len(out), 5200)
        self.assertIn("u49", out)
        self.assertNotIn("u10 ", out)


class PreCompact(Base):
    def test_manual_compact_goes_on(self):
        self.assertIsNone(R.precompact_step({"trigger": "manual", "session_id": "OLD", "transcript_path": self.tp}))

    def test_idle_auto_compaction_is_blocked_without_rotation(self):
        self.register()
        self.transcript([rec_user("hi"), rec_asst("a", 900000), DONE])
        out = R.precompact_step({"trigger": "auto", "session_id": "OLD", "transcript_path": self.tp})
        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.launched, [])

    def test_auto_compaction_in_a_turn_rotates_with_the_last_user_prompt(self):
        self.register()
        self.transcript([rec_user("Build the s07 report."),
                         rec_asst("working", 900000, stop="tool_use"),
                         {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t",
                                                                   "content": "ok"}]}}])
        out = R.precompact_step({"trigger": "auto", "session_id": "OLD", "transcript_path": self.tp})
        self.assertEqual(out["decision"], "block")
        self.assertIn("opens now", out["reason"])
        text = open(R._load_state("OLD")["handoff"], encoding="utf-8").read()
        self.assertIn("Build the s07 report.", text)
        self.assertIn("finish the work", text)


class Storm(Base):
    def test_a_young_successor_does_not_rotate_and_compaction_goes_on(self):
        os.environ["HUBTREE_ROTATE_THRESHOLD"] = "100"
        self.register(sid="A")
        T.append({"event": "rotate", "session_id": "A", "to": "OLD", "name": "work/app/task"})
        self.register(sid="OLD")
        self.transcript([rec_user("go"), rec_asst("working", 900000, stop="tool_use")])
        self.assertIn("started from a rotation", R.storm("OLD"))
        self.assertIsNone(R.prompt_step({"session_id": "OLD", "transcript_path": self.tp, "prompt": "x"}))
        self.assertIsNone(R.precompact_step({"trigger": "auto", "session_id": "OLD", "transcript_path": self.tp}))
        self.assertEqual(self.launched, [])
        self.assertIsNone(R.storm("OLD", now=time.time() + R.STORM_MIN_AGE_S + 5))

    def test_name_cap_per_hour(self):
        self.register()
        for i in range(R.STORM_MAX_PER_HOUR):
            T.append({"event": "rotate", "session_id": f"X{i}", "to": f"X{i + 1}", "name": "work/app/task"})
        self.assertIn("times in the last hour", R.storm("OLD"))

    def test_unregistered_session_in_a_turn_is_not_blocked(self):
        self.transcript([rec_user("go"), rec_asst("working", 900000, stop="tool_use")])
        self.assertIsNone(R.precompact_step({"trigger": "auto", "session_id": "NOPE", "transcript_path": self.tp}))


class Registry(Base):
    def test_link_hides_old_moves_children_and_keeps_one_name(self):
        self.register()
        T.append({"event": "start", "session_id": "KID", "name": "work/app/task/kid", "parent": "OLD"})
        os.makedirs(T.PENDING, exist_ok=True)
        with open(os.path.join(T.PENDING, T.slug("work/app/task", 200) + ".json"), "w") as f:
            json.dump({"name": "work/app/task", "parent": "P", "level": 3, "rotated_from": "OLD"}, f)
        out = session_hook.start({"session_id": "NEW", "cwd": self.cwd, "source": "startup",
                                  "session_title": "work/app/task"})
        self.assertEqual(out["hookSpecificOutput"]["sessionTitle"], "work/app/task")
        s = T.sessions()
        self.assertEqual(s["OLD"]["rotated_to"], "NEW")
        self.assertTrue(s["OLD"]["hidden"])
        self.assertEqual(s["NEW"]["parent"], "P")
        self.assertEqual(s["KID"]["parent"], "NEW")
        self.assertEqual(T.by_name("work/app/task")["session_id"], "NEW")
        visible = [v for v in s.values() if v.get("name") == "work/app/task" and not v.get("hidden")]
        self.assertEqual(len(visible), 1)
        T.append({"event": "rotate", "session_id": "NEW", "to": "NEW2"})
        T.append({"event": "start", "session_id": "NEW2", "name": "work/app/task", "parent": "P"})
        self.assertEqual(T.sessions()["KID"]["parent"], "NEW2")


class Health(Base):
    def test_a_rotated_session_is_not_died(self):
        self.register()
        self.transcript([rec_user("go"), rec_asst("working", 100, stop="tool_use")])
        s = T.sessions()["OLD"]
        T.LIVE_OK[0] = True
        later = time.time() + T.START_GRACE_S + 5  # after the start grace (no "died" in the first 120 s)
        self.assertEqual(T.health(s, {}, now_s=later)[0], "died")
        R._save_state({"old": "OLD", "stage": "ended"})
        self.assertEqual(T.health(s, {}, now_s=later)[0], "closed")


class Process(Base):
    def test_claude_pid_walks_up_to_claude_exe(self):
        table = {10: (9, "python.exe"), 9: (8, "bash.exe"), 8: (7, "claude.exe"), 7: (1, "powershell.exe")}
        self.assertEqual(R.claude_pid(start=10, table=table), 8)
        self.assertIsNone(R.claude_pid(start=10, table={10: (9, "python.exe"), 9: (1, "explorer.exe")}))
        table = {10: (9, "python"), 9: (8, "bash"), 8: (7, "claude"), 7: (1, "tmux")}
        self.assertEqual(R.claude_pid(start=10, table=table), 8)

    def test_end_process_refuses_a_non_claude_pid(self):
        self.assertEqual(R.end_process(os.getpid(), None), "not claude")


class Finish(Base):
    def test_finish_waits_for_the_link_then_ends_the_old_session(self):
        self.register()
        self.transcript([rec_user("hi"), rec_asst("a", 100)])
        st = {"old": "OLD", "new": "NEW", "name": "work/app/task", "kind": "tab", "transcript": self.tp,
              "pid": None, "stage": "launched"}
        R._save_state(st)
        out = R.finish("OLD", link_wait=0.1, sleep=lambda s: None)
        self.assertEqual(out["stage"], "failed")  # the new session never started: the old one stays
        self.assertTrue(os.path.exists(self.tp))
        self.assertTrue(R._paused("OLD"))
        R._save_state(dict(st))
        T.append({"event": "rotate", "session_id": "OLD", "to": "NEW"})
        T.append({"event": "start", "session_id": "NEW", "name": "work/app/task"})
        out = R.finish("OLD", link_wait=1, sleep=lambda s: None)
        self.assertEqual(out["stage"], "done", out)
        self.assertEqual(out["end"], "no pid")
        self.assertTrue(os.path.exists(self.tp))  # the old transcript stays
        self.assertFalse(T.sessions()["OLD"]["open"])
