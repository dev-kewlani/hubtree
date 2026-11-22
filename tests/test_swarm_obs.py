"""Unit tests for swarm_obs.py. Temporary files only; no real registry, no real messages.

Run: python -m pytest tests/test_swarm_obs.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "hubtree"))
import tree_lib as T  # noqa: E402
import swarm_obs as O  # noqa: E402


def usage(i=10, cr=1000, cw=200, out=50, w1=0):
    return {"input_tokens": i, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw, "output_tokens": out,
            "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": cw - w1}}


def asst(mid, content, model="claude-haiku-5-5", u=None, stop="tool_use"):
    return {"type": "assistant", "timestamp": "2026-01-01T12:00:00Z",
            "message": {"id": mid, "model": model, "stop_reason": stop, "content": content, "usage": u or usage()}}


def call(tid, name="Bash", inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp if inp is not None else {"command": "echo hi"}}


def result(tid, err=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": err,
                                                     "content": "boom" if err else "ok"}]}}


def write(path, records, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (T.STATE, T.ALERTS, T.REGISTRY, O.DIR, O.CACHE)
        T.STATE = os.path.join(self.dir, ".hub")
        T.ALERTS = os.path.join(T.STATE, "alerts")
        T.REGISTRY = os.path.join(T.STATE, "sessions.jsonl")
        O.DIR = os.path.join(T.STATE, "swarm_obs")
        O.CACHE = os.path.join(O.DIR, "cache.json")
        O._CACHE.update({"loaded": False, "files": {}, "dirty": False})
        O._BUDGET[0] = O.MAX_BYTES_PER_CALL
        self.sent = []
        self._send = O._send
        O._send = lambda to, text: self.sent.append((to, text)) or True
        self.tp = os.path.join(self.dir, "proj", "s1.jsonl")

    def tearDown(self):
        T.STATE, T.ALERTS, T.REGISTRY, O.DIR, O.CACHE = self.saved
        O._send = self._send
        O._CACHE.update({"loaded": False, "files": {}, "dirty": False})
        shutil.rmtree(self.dir, ignore_errors=True)

    def sess(self, **kw):
        return dict({"session_id": "s1", "name": "hub/p/child", "parent": "p1", "transcript": self.tp}, **kw)


class Cost(Base):
    def test_price_lookup(self):
        self.assertEqual(O.price("claude-opus-5-5"), (4.0, 20.0, 0.2))
        self.assertEqual(O.price("claude-haiku-5-5"), (0.1, 0.5, 0.01))
        self.assertEqual(O.price("claude-sonnet-4-6"), (3.0, 15.0, 0.3))
        self.assertEqual(O.price("some-new-opus-x"), (5.0, 25.0, 0.5))  # a family word, then opus

    def test_config_overrides_default_prices(self):
        saved = O._cost_cfg, list(O._PRICES)
        O._cost_cfg = lambda: {"models": {"claude-opus-5-5": [1, 2, 3]}, "cache_write_5m": 1.5}
        O._PRICES.clear()
        try:
            self.assertEqual(O.price("claude-opus-5-5-x"), (1.0, 2.0, 3.0))
            self.assertEqual(O.price("claude-haiku-5-5"), (0.1, 0.5, 0.01))  # a default row stays
            self.assertEqual(dict(O.prices())["__w"][:2], (1.5, 2.0))
        finally:
            O._cost_cfg = saved[0]
            O._PRICES[:] = saved[1]

    def test_usd_formula(self):
        f = O.fields(usage(i=1_000_000, cr=1_000_000, cw=1_000_000, out=1_000_000, w1=400_000))
        self.assertEqual(f, [1_000_000, 1_000_000, 600_000, 400_000, 1_000_000])
        # opus 5.5: 4 in + 20 out + 0.2 read + 0.6*4*1.25 + 0.4*4*2
        self.assertAlmostEqual(O.usd_of("claude-opus-5-5", f), 4 + 20 + 0.2 + 3.0 + 3.2)

    def test_one_message_counted_once_and_incremental(self):
        # Claude Code writes one record per content block with the same usage: count once.
        write(self.tp, [asst("m1", [{"type": "thinking", "thinking": ""}]), asst("m1", [call("t1")]), result("t1")])
        st = O.file_state(self.tp)
        self.assertEqual(O.totals(st)[1], 10 + 1000 + 200 + 50)
        write(self.tp, [asst("m2", [{"type": "text", "text": "done"}], stop="end_turn", u=usage(out=70))], mode="a")
        st = O.file_state(self.tp)
        self.assertEqual(O.totals(st)[1], 1260 + 1280)
        self.assertEqual(O.direct_sum([self.tp])["tok"], 2540)
        self.assertTrue(st["end"])

    def test_partial_last_line_waits(self):
        write(self.tp, [asst("m1", [call("t1")])])
        with open(self.tp, "a", encoding="utf-8") as f:
            f.write(json.dumps(asst("m2", [call("t2")]))[:40])  # no newline yet
        self.assertEqual(O.totals(O.file_state(self.tp))[1], 1260)

    def test_session_includes_agents_and_cache_survives(self):
        write(self.tp, [asst("m1", [call("t1", "Agent", {"description": "x"})])])
        side = self.tp[:-6]
        write(os.path.join(side, "subagents", "agent-a1.jsonl"), [asst("x1", [], stop="end_turn")])
        write(os.path.join(side, "subagents", "workflows", "wf_1", "agent-a2.jsonl"), [asst("x2", [], stop="end_turn")])
        c = O.cost(self.sess())
        self.assertEqual(c["tok"], 3 * 1260)
        O.save()
        O._CACHE.update({"loaded": False, "files": {}})
        self.assertEqual(O.cost(self.sess())["tok"], 3 * 1260)  # read back from the disk cache


class Flags(Base):
    def test_loop_flag_and_one_message(self):
        recs = []
        for i in range(6):
            recs += [asst(f"m{i}", [call(f"t{i}")]), result(f"t{i}")]
        write(self.tp, recs)
        st = O.file_state(self.tp)
        fl = dict(O.flags(self.sess(), st, rot=0))
        self.assertIn("loop", fl)
        self.assertIn("Bash 6 times", fl["loop"])
        s = self.sess()
        self.assertEqual(O.notify(s, {"flags": list(fl.items()), "budget": None}), ["loop"])
        self.assertEqual(O.notify(s, {"flags": list(fl.items()), "budget": None}), [])  # same episode
        O.notify(s, {"flags": [], "budget": None})  # the loop ended
        self.assertEqual(O.notify(s, {"flags": list(fl.items()), "budget": None}), ["loop"])  # a new episode
        self.assertEqual(len(self.sent), 2)

    def test_loop_ignores_the_description(self):
        # Bash writes a new "description" per call; the command is what repeats.
        recs = []
        for i in range(5):
            recs += [asst(f"m{i}", [call(f"t{i}", inp={"command": "echo x", "description": f"try {i}"})]), result(f"t{i}")]
        write(self.tp, recs)
        self.assertIn("loop", dict(O.flags(self.sess(), O.file_state(self.tp), rot=0)))

    def test_jump_to_100_sends_one_message(self):
        b = {"level": "over", "pct": 150.0, "text": "budget 150%"}
        self.assertEqual(O.notify(self.sess(), {"flags": [], "budget": b}, live_now=False), ["budget100"])
        self.assertEqual(len(self.sent), 1)

    def test_different_inputs_no_loop(self):
        recs = []
        for i in range(8):
            recs += [asst(f"m{i}", [call(f"t{i}", inp={"command": f"echo {i}"})]), result(f"t{i}")]
        write(self.tp, recs)
        self.assertEqual(O.flags(self.sess(), O.file_state(self.tp), rot=0), [])

    def test_error_rate(self):
        recs = []
        for i in range(12):
            recs += [asst(f"m{i}", [call(f"t{i}", inp={"command": f"x{i}"})]), result(f"t{i}", err=i % 3 != 0)]
        write(self.tp, recs)
        kinds = [k for k, _ in O.flags(self.sess(), O.file_state(self.tp), rot=0)]
        self.assertEqual(kinds, ["errors"])  # 8 of 12 failed

    def test_waste_resets_on_write_or_text(self):
        big = usage(i=0, cr=3_000_000, cw=0, out=0)
        write(self.tp, [asst("m1", [call("t1", "Read", {"file_path": "a"})], u=big), result("t1"),
                        asst("m2", [call("t2", "Read", {"file_path": "b"})], u=big), result("t2")])
        self.assertIn("waste", [k for k, _ in O.flags(self.sess(), O.file_state(self.tp), rot=0)])
        write(self.tp, [asst("m3", [call("t3", "Write", {"file_path": "c"})], u=usage())], mode="a")
        self.assertEqual(O.flags(self.sess(), O.file_state(self.tp), rot=0), [])

    def test_rotations(self):
        st = O._new_state()
        st["comp"] = [time.time() - 60, time.time() - 120]
        self.assertEqual(O.flags(self.sess(), st, rot=0), [])
        self.assertEqual([k for k, _ in O.flags(self.sess(), st, rot=1)], ["rotations"])

    def test_activity_and_redaction(self):
        write(self.tp, [{"type": "user", "message": {"content": "use key sk-ant-api03-" + "A" * 40}},
                        asst("m1", [call("t1", "Read", {"file_path": "D:\\x\\notes.md"})])])
        st = O.file_state(self.tp)
        lines = O.activity(self.sess(), st)
        self.assertIn("running Read: notes.md", lines[0])
        self.assertIn("turns 1", lines[0])
        self.assertNotIn("A" * 40, "\n".join(lines))


class Agents(Base):
    def test_states_and_summary(self):
        write(self.tp, [asst("m1", [call("t1", "Workflow", {"script": "x"})]),
                        {"type": "user", "toolUseResult": {"runId": "wf_1", "workflowName": "demo"},
                         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}])
        side = self.tp[:-6]
        write(os.path.join(side, "subagents", "agent-p1.jsonl"), [asst("p", [], stop="end_turn")])
        with open(os.path.join(side, "subagents", "agent-p1.meta.json"), "w") as f:
            json.dump({"description": "plain helper"}, f)
        wd = os.path.join(side, "subagents", "workflows", "wf_1")
        write(os.path.join(wd, "agent-a.jsonl"), [asst("a", [], stop="end_turn")])
        write(os.path.join(wd, "agent-b.jsonl"), [{"type": "assistant", "apiError": "rate_limit", "isApiErrorMessage": True,
                                                   "message": {"model": "<synthetic>", "content": []}}])
        write(os.path.join(wd, "agent-c.jsonl"), [asst("c", [call("tc")])])
        write(os.path.join(wd, "journal.jsonl"), [
            {"type": "launched"},
            {"type": "started", "key": "k1", "agentId": "a", "label": "one", "phase": "Build"},
            {"type": "started", "key": "k2", "agentId": "b", "label": "two", "phase": "Build"},
            {"type": "started", "key": "k3", "agentId": "c", "label": "three", "phase": "Verify"},
            {"type": "result", "key": "k1", "agentId": "a"}, {"type": "failed", "key": "k2", "agentId": "b"}])
        O.file_state(self.tp)
        rows = O.agents(self.tp, live=True)
        st = {r["label"]: r["state"] for r in rows}
        self.assertEqual(st, {"plain helper": "done", "one": "done", "two": "failed (limit)", "three": "running"})
        self.assertEqual(O.agent_summary(rows), "+4 agents, 1 failed (1 at a usage limit), 1 running")
        self.assertTrue(any("workflow demo (phase Verify)" in x for x in O.agent_lines(rows)))
        self.assertEqual({r["label"]: r["state"] for r in O.agents(self.tp, live=False)}["three"], "failed")


class Budget(Base):
    def test_budget_marks_pause_once_and_raise(self):
        write(self.tp, [asst("m1", [call("t1")], model="claude-opus-5-5", u=usage(i=0, cr=0, cw=0, out=10_000))])
        T.append({"event": "start", "session_id": "s1", "name": "hub/p/child", "parent": "p1", "transcript": self.tp,
                  "budget_usd": 0.25})  # spent 0.20 USD: 80%
        paused = []
        self._pause = O.pause
        O.pause = lambda s, why, live: paused.append(why) or "test"
        self._live = T.live_agents
        T.live_agents = lambda max_age=None: {}
        try:
            acts = O.enforce()
            self.assertEqual(acts[0]["level"], "warn")
            self.assertEqual(acts[0]["sent"], ["budget80"])
            write(self.tp, [asst("m2", [call("t2")], model="claude-opus-5-5", u=usage(i=0, cr=0, cw=0, out=5_000))], "a")
            acts = O.enforce()
            self.assertEqual(acts[0]["level"], "over")
            self.assertEqual(acts[0]["sent"], ["budget100"])
            self.assertEqual(len(paused), 1)
            O.enforce()
            self.assertEqual(len(paused), 1)  # once per episode
            self.assertEqual(len(self.sent), 2)
            O.set_budget("s1", mult=2)
            self.assertEqual(T.sessions()["s1"]["budget_usd"], 0.5)
            self.assertEqual(O.enforce()[0]["level"], None) if O.enforce() else None
            self.assertFalse(os.path.exists(O._on("s1", "pause")))
        finally:
            O.pause = self._pause
            T.live_agents = self._live

    def test_stale_budget_sends_nothing(self):
        # An observer read the old budget, then the user raised it; no stale 100% message.
        T.append({"event": "start", "session_id": "s1", "name": "hub/p/child", "parent": "p1", "budget_tokens": 1000})
        T.append({"event": "budget", "session_id": "s1", "budget_tokens": 4000})
        stale = {"level": "over", "pct": 150.0, "text": "budget 150%"}
        self.assertEqual(O.notify(self.sess(), {"flags": [], "budget": stale, "usd": 0, "tok": 1500}, False), [])
        self.assertEqual(self.sent, [])
        self.assertFalse(os.path.exists(O._on("s1", "budget100")))

    def test_over_budget_gate(self):
        write(self.tp, [asst("m1", [call("t1")], u=usage(i=0, cr=0, cw=0, out=2000))])
        T.append({"event": "start", "session_id": "s1", "name": "hub/p/child", "transcript": self.tp,
                  "budget_tokens": 1000})
        self.assertTrue(O.over_budget("s1"))
        import autopilot
        self.assertTrue(autopilot._over_budget("s1"))
        T.append({"event": "budget", "session_id": "s1", "budget_tokens": 5000})
        self.assertFalse(O.over_budget("s1"))

    def test_no_budget(self):
        self.assertIsNone(O.budget({}, {"usd": 5, "tok": 5}))
        b = O.budget({"budget_tokens": 1000}, {"usd": 0, "tok": 1200})
        self.assertEqual(b["level"], "over")


if __name__ == "__main__":
    unittest.main(verbosity=1)
