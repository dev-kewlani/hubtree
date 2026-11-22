"""Unit tests for tree_lib.health, why, the tree_view rollup and tell_parent. Temporary files only.

Run: python -m pytest tests/test_health.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "hubtree"))
import tree_lib as T  # noqa: E402
import tree_view  # noqa: E402


def write(path, records, age_min=0.0):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    t = time.time() - age_min * 60
    os.utime(path, (t, t))


END = [{"type": "user", "message": {"content": "hi"}},
       {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}},
       {"type": "system", "subtype": "turn_duration"}]


def tool(name, tid="t1"):
    return [{"type": "user", "message": {"content": "go"}},
            {"type": "assistant", "message": {"stop_reason": "tool_use",
                                              "content": [{"type": "tool_use", "id": tid, "name": name}]}}]


class Health(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tp = os.path.join(self.dir, "s1.jsonl")
        self.s = {"session_id": "s1", "transcript": self.tp, "open": True, "parent": "p1", "name": "hub/x/child"}
        T.LIVE_OK[0] = True
        T._TAIL_CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def live(self, **kw):
        return {"s1": dict(kw)}

    def test_busy_recent_is_working(self):
        write(self.tp, tool("Bash"), age_min=2)
        self.assertEqual(T.health(self.s, self.live(kind="background", state="working"))[0], "work")
        self.assertEqual(T.health(self.s, self.live(kind="interactive", status="busy"))[0], "tab")

    def test_busy_old_is_stuck_with_tool(self):
        write(self.tp, tool("Bash"), age_min=25)
        st, info = T.health(self.s, self.live(kind="background", state="working"))
        self.assertEqual(st, "stuck")
        self.assertEqual(info["tool"], "Bash")
        self.assertIn("Bash, 25 min with no write (limit 20)", T.why(st, info))
        st, _ = T.health(self.s, self.live(kind="interactive", status="busy"))
        self.assertEqual(st, "stuck")

    def test_limit_per_session(self):
        write(self.tp, tool("Bash"), age_min=25)
        s = dict(self.s, limit_min=60)
        self.assertEqual(T.health(s, self.live(kind="background", state="working"))[0], "work")
        s = dict(self.s, limit_min=1)
        write(self.tp, tool("Bash"), age_min=2)
        self.assertEqual(T.health(s, self.live(kind="background", state="working"))[0], "stuck")

    def test_subagent_write_is_a_heartbeat(self):
        write(self.tp, tool("Agent"), age_min=40)
        os.makedirs(os.path.join(self.dir, "s1", "subagents"))
        write(os.path.join(self.dir, "s1", "subagents", "a.jsonl"), END, age_min=1)
        self.assertEqual(T.health(self.s, self.live(kind="background", state="working"))[0], "work")

    def test_idle_is_never_stuck(self):
        write(self.tp, END, age_min=600)
        self.assertEqual(T.health(self.s, self.live(kind="interactive", status="idle"))[0], "tab")
        self.assertEqual(T.health(self.s, self.live(kind="background", state="idle"))[0], "idle")

    def test_busy_after_turn_end_is_not_stuck(self):
        # an idle compaction or a stale "busy" status: the last turn ended
        write(self.tp, END, age_min=50)
        self.assertEqual(T.health(self.s, self.live(kind="interactive", status="busy"))[0], "tab")
        self.assertEqual(T.health(self.s, self.live(kind="background", state="working"))[0], "work")

    def test_compact_summary_keeps_turn_done(self):
        recs = END + [{"type": "system", "subtype": "compact_boundary"},
                      {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}}]
        write(self.tp, recs, age_min=50)
        self.assertTrue(T.tail_state(self.tp)["done"])
        self.assertEqual(T.health(self.s, self.live(kind="interactive", status="busy"))[0], "tab")
        self.assertEqual(T.health(self.s, {})[0], "closed")  # closed later: not died

    def test_new_prompt_with_no_answer_is_stuck(self):
        write(self.tp, END + [{"type": "user", "message": {"content": "next"}}], age_min=25)
        self.assertEqual(T.health(self.s, self.live(kind="interactive", status="busy"))[0], "stuck")

    def test_waiting_status_is_you(self):
        write(self.tp, tool("Bash"), age_min=1)
        st, info = T.health(self.s, self.live(kind="interactive", status="waiting", waitingFor="permission prompt"))
        self.assertEqual(st, "you")
        self.assertEqual(T.why(st, info), "waits for you: permission prompt")

    def test_open_question_is_you(self):
        write(self.tp, tool("AskUserQuestion"), age_min=3)
        st, info = T.health(self.s, self.live(kind="interactive", status="busy"))
        self.assertEqual(st, "you")
        self.assertEqual(T.why(st, info), "waits for an answer (AskUserQuestion)")
        self.assertEqual(T.why("you", {}), "waits for you")

    def test_tool_result_closes_the_call(self):
        recs = tool("Bash") + [{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}}]
        write(self.tp, recs, age_min=25)
        st, info = T.health(self.s, self.live(kind="background", state="working"))
        self.assertEqual(st, "stuck")
        self.assertIsNone(info["tool"])  # stuck while the model works, with no open call

    def test_died_mid_turn(self):
        write(self.tp, tool("Bash"), age_min=90)
        st, info = T.health(self.s, {})
        self.assertEqual(st, "died")
        self.assertIn("stopped in the middle of a turn in Bash", T.why(st, info))

    def test_no_died_just_after_start(self):
        # The registry has the start event before the shared live list (4 s cache) has the session.
        write(self.tp, tool("Bash"), age_min=0.1)
        s = dict(self.s, last=T.now())
        self.assertEqual(T.health(s, {})[0], "closed")
        old = datetime.fromtimestamp(time.time() - T.START_GRACE_S - 5, timezone.utc).isoformat(timespec="seconds")
        self.assertEqual(T.health(dict(self.s, last=old), {})[0], "died")

    def test_clean_end_is_closed(self):
        write(self.tp, END, age_min=90)
        self.assertEqual(T.health(self.s, {})[0], "closed")
        self.assertEqual(T.health(dict(self.s, open=False), {})[0], "closed")

    def test_no_died_when_live_list_failed(self):
        write(self.tp, tool("Bash"), age_min=90)
        T.LIVE_OK[0] = False
        self.assertEqual(T.health(self.s, {})[0], "closed")

    def test_old_stop_is_closed(self):
        write(self.tp, tool("Bash"), age_min=(T.DIED_DAYS + 1) * 1440)
        self.assertEqual(T.health(self.s, {})[0], "closed")

    def test_rollup(self):
        rows = [{"depth": 0, "kind": "folder", "name": "hub"},
                {"depth": 1, "kind": "session", "state": "tab"},
                {"depth": 2, "kind": "session", "state": "stuck"},
                {"depth": 2, "kind": "session", "state": "you"},
                {"depth": 1, "kind": "session", "state": "idle"},
                {"depth": 2, "kind": "session", "state": "work"}]
        tree_view.rollup(rows)
        self.assertEqual(rows[0]["below"], "1 stuck, 1 YOU")
        self.assertEqual(rows[1]["below"], "1 stuck, 1 YOU")
        self.assertNotIn("below", rows[4])

    def test_tell_parent_once_per_episode(self):
        sent = []

        class FakeDB:
            @staticmethod
            def connect():
                return type("C", (), {"close": lambda self: None})()

            @staticmethod
            def send(con, to_sid, body, from_sid=None):
                sent.append((to_sid, body))

        old_alerts, T.ALERTS = T.ALERTS, os.path.join(self.dir, "alerts")
        sys.modules["hubdb"] = FakeDB
        try:
            write(self.tp, tool("Bash"), age_min=25)
            info = {"age_min": 25, "tool": "Bash", "limit_min": 20}
            self.assertTrue(T.tell_parent(self.s, "stuck", info))
            self.assertFalse(T.tell_parent(self.s, "stuck", info))  # same episode
            self.assertFalse(T.tell_parent(self.s, "work", {}))
            write(self.tp, tool("Bash", "t2"), age_min=30)  # a new heartbeat: a new episode
            self.assertTrue(T.tell_parent(self.s, "stuck", info))
            self.assertEqual([to for to, _ in sent], ["p1", "p1"])
            self.assertIn("child hub/x/child is stuck at ", sent[0][1])
            self.assertIn(": Bash, 25 min", sent[0][1])
        finally:
            T.ALERTS = old_alerts
            del sys.modules["hubdb"]


class Config(unittest.TestCase):
    def test_tree_table_and_bad_values(self):
        real = T._CFG

        class Fake:
            raw = {"tree": {"stuck_min": 45, "died_days": True, "alert_keep_days": -3}}

        class Broken:
            @property
            def raw(self):
                raise ValueError("broken")
        T._CFG = Fake
        try:
            self.assertEqual(T._tree_cfg("stuck_min", 20), 45)
            self.assertEqual(T._tree_cfg("died_days", 7), 7)       # a bool is not a number
            self.assertEqual(T._tree_cfg("alert_keep_days", 30), 30)  # not positive
            self.assertEqual(T._tree_cfg("missing", 5), 5)
            T._CFG = Broken()
            self.assertEqual(T._tree_cfg("stuck_min", 20), 20)       # a broken hubtree.toml never stops the tree
        finally:
            T._CFG = real

    def test_sweep_alerts(self):
        d = tempfile.mkdtemp()
        old_alerts, T.ALERTS = T.ALERTS, d
        try:
            for name, age_days in (("old.flag", 40), ("new.flag", 2), ("keep.txt", 90)):
                p = os.path.join(d, name)
                open(p, "w").close()
                t = time.time() - age_days * 86400
                os.utime(p, (t, t))
            self.assertEqual(T.sweep_alerts(), 1)
            self.assertEqual(sorted(os.listdir(d)), ["keep.txt", "new.flag"])
        finally:
            T.ALERTS = old_alerts
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
