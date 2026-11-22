"""Unit tests for swarm.py and the swarm parts of spawn.py and tree_lib.py. Temporary files
only: a temp state folder and a temp hubtree.db. No claude process starts (RUN and the live list are stubs).

Run: python -m pytest tests/test_swarm.py -q
"""
import contextlib
import io
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
import swarm as S  # noqa: E402
from owner_update import HEAD  # noqa: E402


def ts(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t or time.time()))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = dict(os.environ)
        os.environ["HUBTREE_STATE"] = self.dir
        os.environ["HUBTREE_DB"] = os.path.join(self.dir, "hubtree.db")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.saved = (T.STATE, T.REGISTRY, T.PENDING, S.RUN[0], S.LIVE[0], dict(S.CFG_OVERRIDE), S.TEAMS)
        T.STATE, T.REGISTRY, T.PENDING = self.dir, os.path.join(self.dir, "sessions.jsonl"), os.path.join(self.dir, "p")
        self.calls, self.live = [], {}
        S.RUN[0] = self.run_stub
        S.LIVE[0] = lambda: dict(self.live)
        S.CFG_OVERRIDE.clear()
        self.settle, S.STOP_SETTLE_S = S.STOP_SETTLE_S, 0
        S._LEDGER.clear()
        S._TOP.clear()
        self.git = {}  # argv tail -> (code, out)

    def tearDown(self):
        T.STATE, T.REGISTRY, T.PENDING, S.RUN[0], S.LIVE[0], cfg, S.TEAMS = self.saved
        S.STOP_SETTLE_S = self.settle
        S.CFG_OVERRIDE.clear()
        S.CFG_OVERRIDE.update(cfg)
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_stub(self, argv, cwd=None, timeout=90):
        self.calls.append(list(argv))
        if argv[0] == "git":
            for k, v in self.git.items():
                if k in " ".join(argv):
                    return v
            return 128, "fatal: not a git repository"
        if any(str(x).endswith("spawn.py") for x in argv) and "--from-queue" in argv:
            qid = argv[argv.index("--from-queue") + 1]
            s = T.sessions()[qid]
            T.append({"event": "dequeued", "session_id": qid, "name": s["name"]})
            return 0, json.dumps({"ok": True, "name": s["name"], "mode": "background"})
        if any(str(x).endswith("spawn.py") for x in argv):
            nm = argv[argv.index("--name") + 1]
            return 0, json.dumps({"ok": True, "name": "p/" + nm, "queued": "--after" in argv})
        if argv[:3] == ["claude", "--bg", "--resume"] and getattr(self, "copies", 0) > 0:
            self.copies -= 1
            return 0, "note: session x is already running in the background, so this started a copy as cp1. done"
        if argv[:2] == ["claude", "stop"]:
            for k, a in list(self.live.items()):
                if a.get("id") == argv[2]:
                    self.live.pop(k)
            return 0, "stopped"
        return 0, "ok"

    def start(self, sid, name, parent=None, node="work/api", auto=True, task=None):
        T.append({"event": "start", "session_id": sid, "name": name, "parent": parent, "node": node, "auto": auto,
                  "cwd": self.dir, "task": task})

    def queue(self, qid, name, parent, after=(), when="released", auto=True, node="work/api"):
        T.append({"event": "queued", "session_id": qid, "name": name, "parent": parent, "node": node, "auto": auto,
                  "after": list(after), "when": when, "task": os.path.join(self.dir, "t.md"), "opts": {"auto": auto}})

    def ledger(self, sid, path, t=None):
        d = os.path.join(self.dir, "ledger")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts(t), "sid": sid, "tool": "Write", "path": path}) + "\n")


class TestQueue(Base):
    def test_fold_and_health(self):
        self.start("P", "hub/p")
        self.queue("q-1", "hub/p/kid~auto", "P", after=["hub/p/dep~auto"])
        s = T.sessions()["q-1"]
        st, info = T.health(s, {})
        self.assertEqual(st, "queued")
        self.assertEqual(T.why(st, info), "waits for dep~auto (released)")
        self.assertEqual(T.by_name("hub/p/kid~auto")["session_id"], "q-1")
        T.append({"event": "dequeued", "session_id": "q-1"})
        self.start("real", "hub/p/kid~auto", "P")
        s = T.sessions()
        self.assertTrue(s["q-1"]["gone"] and s["q-1"]["hidden"])
        self.assertEqual(T.by_name("hub/p/kid~auto")["session_id"], "real")

    def test_after_waits_until_released(self):
        self.start("P", "hub/p")
        self.start("D", "hub/p/dep~auto", "P")
        self.queue("q-1", "hub/p/kid~auto", "P", after=["hub/p/dep~auto"])
        self.assertEqual(S.tick(), [])
        self.assertTrue(T.sessions()["q-1"]["queued"])
        r = S.post_result("D", {"done": "built it"}, kick=False)
        self.assertTrue(r["ok"])
        acts = S.tick()
        self.assertEqual([a["act"] for a in acts], ["started"])
        self.assertTrue(T.sessions()["q-1"]["gone"])

    def test_when_done_needs_end(self):
        self.start("P", "hub/p")
        self.start("D", "hub/p/dep~auto", "P")
        self.queue("q-1", "hub/p/kid~auto", "P", after=["D"], when="done")
        S.post_result("D", {"done": "x"}, kick=False)
        self.assertEqual(S.tick(), [])
        T.append({"event": "end", "session_id": "D"})
        self.assertEqual([a["act"] for a in S.tick()], ["started"])

    def test_rejected_result_does_not_release(self):
        self.start("P", "hub/p")
        self.start("D", "hub/p/dep~auto", "P")
        self.queue("q-1", "hub/p/kid~auto", "P", after=["D"])
        rid = S.post_result("D", {"done": "x"}, kick=False)["id"]
        S.reject(rid, "wrong")
        self.assertEqual(S.tick(), [])

    def test_limit_per_node_and_order(self):
        S.CFG_OVERRIDE.update(max_working_per_node=2, max_working_total=8)
        self.start("P", "hub/p", auto=False)
        for i in range(2):
            self.start(f"W{i}", f"hub/p/w{i}~auto", "P")
            self.live[f"W{i}"] = {"kind": "background", "state": "working", "id": f"w{i}", "name": f"hub/p/w{i}~auto"}
        self.queue("q-a", "hub/p/a~auto", "P")
        self.queue("q-b", "hub/p/b~auto", "P")
        self.assertEqual([a["act"] for a in S.tick()], ["wait", "wait"])
        self.live.pop("W0")
        T.append({"event": "end", "session_id": "W0"})  # it finished
        acts = S.tick()
        self.assertEqual([(a["act"], a["name"]) for a in acts], [("started", "hub/p/a~auto"), ("wait", "hub/p/b~auto")])
        acts = S.tick()  # the started child counts in its grace time, though it is not live yet
        self.assertEqual([a["act"] for a in acts], ["wait"])

    def test_starting_children_count(self):
        S.CFG_OVERRIDE.update(max_working_per_node=2, max_working_total=8)
        self.start("P", "hub/p", auto=False)
        self.start("N1", "hub/p/n1~auto", "P")  # in the registry, not in the live list yet
        os.makedirs(T.PENDING, exist_ok=True)
        with open(os.path.join(T.PENDING, "n2.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "hub/p/n2~auto", "parent": "P", "auto": True, "node": "work/api"}, f)
        # a queue row that started, whose real session ended already: it does not count
        self.queue("q-old", "hub/p/old~auto", "P")
        T.append({"event": "dequeued", "session_id": "q-old"})
        self.start("OLD", "hub/p/old~auto", "P")
        T.append({"event": "end", "session_id": "OLD"})
        per, total = S.working_counts(T.sessions(), {})
        self.assertEqual((per.get("work/api"), total), (2, 2))
        self.queue("q-a", "hub/p/a~auto", "P")
        self.assertEqual([a["act"] for a in S.tick()], ["wait"])

    def test_limit_total(self):
        S.CFG_OVERRIDE.update(max_working_per_node=5, max_working_total=1)
        self.start("P", "hub/p")
        self.live["X"] = {"kind": "background", "state": "working", "id": "x", "cwd": os.path.join(self.dir, "other")}
        self.queue("q-a", "hub/p/a~auto", "P")
        self.assertEqual([a["act"] for a in S.tick()], ["wait"])

    def test_paused_queue_row_is_held_and_cancel(self):
        self.start("P", "hub/p")
        self.queue("q-a", "hub/p/a~auto", "P")
        T.append({"event": "paused", "session_id": "q-a", "by": "user"})
        self.assertEqual(S.tick(), [])
        self.assertTrue(S.cancel("hub/p/a~auto")["ok"])
        self.assertFalse(S.queued())

    def test_start_now_ignores_deps(self):
        self.start("P", "hub/p")
        self.queue("q-a", "hub/p/a~auto", "P", after=["nobody"])
        self.assertTrue(S.start_now("hub/p/a~auto")["ok"])


class TestClaims(Base):
    def test_conflict_and_one_message(self):
        import hubdb
        self.start("P1", "hub/p1")
        self.start("P2", "hub/p2")
        self.start("A", "hub/p1/a~auto", "P1")
        self.start("B", "hub/p2/b~auto", "P2")
        f = os.path.join(self.dir, "shared.txt")
        self.ledger("A", f)
        self.ledger("B", f.upper())  # the same file in another case
        self.ledger("A", os.path.join(self.dir, "only_a.txt"))
        self.live = {"A": {"kind": "background", "state": "working"}, "B": {"kind": "background", "state": "idle"}}
        cf = S.conflicts(["A", "B"])
        self.assertEqual(set(cf), {"A", "B"})
        self.assertEqual(len(cf["A"]), 1)
        ex = S.row_extras(T.sessions(), self.live)
        self.assertIn("shared.txt", ex["A"]["conflict"])
        acts = S.alert_conflicts(T.sessions(), self.live)
        self.assertEqual(acts[0]["parents_told"], 2)
        self.assertEqual(S.alert_conflicts(T.sessions(), self.live), [])  # once only
        con = hubdb.connect()
        self.assertEqual(len(hubdb.inbox(con, "P1")), 1)
        self.assertEqual(len(hubdb.inbox(con, "P2")), 1)
        con.close()
        lines = S.detail_lines("A", T.sessions(), self.live)
        self.assertTrue(any(x.startswith("CONFLICT") for x in lines))
        self.assertTrue(any("only_a.txt" in x for x in lines))

    def test_old_write_is_no_conflict(self):
        self.start("A", "hub/a~auto")
        self.start("B", "hub/b~auto")
        f = os.path.join(self.dir, "shared.txt")
        self.ledger("A", f, time.time() - 3 * 3600)
        self.ledger("B", f)
        self.assertEqual(S.conflicts(["A", "B"]), {})

    def test_dead_session_is_no_conflict(self):
        self.start("A", "hub/a~auto")
        self.start("B", "hub/b~auto")
        f = os.path.join(self.dir, "shared.txt")
        self.ledger("A", f)
        self.ledger("B", f)
        self.live = {"A": {"kind": "background", "state": "working"}}
        self.assertEqual(S.row_extras(T.sessions(), self.live).get("A", {}).get("conflict"), None)


class TestResults(Base):
    def task_file(self, name):
        p = os.path.join(self.dir, "tasks", name + ".md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# Task\n\ndo it\n\n" + HEAD + "\n\n(no updates yet)\n\n## Rules\n\nx\n")
        return p

    def test_post_count_accept_closes(self):
        self.start("P", "hub/p")
        self.start("C", "hub/p/c~auto", "P", task=self.task_file("c"))
        self.live["C"] = {"kind": "background", "state": "idle", "id": "c1", "pid": 999999}
        rid = S.post_result("C", {"done": "made x", "proof": "test ok", "commits": "abc,def"}, kick=False)["id"]
        ex = S.row_extras(T.sessions(), self.live)
        self.assertEqual(ex["P"]["results"], 1)
        self.assertEqual(ex["C"]["result"], "open")
        self.assertTrue(any("results from children (1 open" in x for x in S.detail_lines("P", T.sessions(), {})))
        self.git = {"cat-file": (0, "")}  # the named commits exist in the hub repo
        self.git["rev-parse"] = (0, self.dir)
        r = S.accept(rid)
        self.assertTrue(r["ok"], r)
        self.assertIn(["claude", "stop", "c1"], self.calls)
        s = T.sessions()["C"]
        self.assertTrue(s["accepted"])
        self.assertFalse(s["open"])
        self.assertEqual(S.results(parent="P")[0]["status"], "accepted")
        self.assertFalse(S.accept(rid)["ok"])  # twice: refused

    def test_accept_refused_when_not_committed(self):
        self.start("P", "hub/p")
        self.start("C", "hub/p/c~auto", "P")
        f = os.path.join(self.dir, "work.py")
        open(f, "w").close()
        self.ledger("C", f)
        rid = S.post_result("C", {"done": "x", "commits": ["zzz"]}, kick=False)["id"]
        self.git = {"rev-parse": (0, self.dir), "status": (0, "?? work.py\n"), "cat-file": (1, "")}
        r = S.accept(rid)
        self.assertFalse(r["ok"])
        self.assertIn("not committed: work.py", r["error"])
        self.assertIn("commit zzz is in no repo", r["error"])
        self.assertTrue(T.sessions()["C"]["open"])
        self.assertTrue(S.accept(rid, force=True)["ok"])

    def test_change_writes_task_file_and_wakes(self):
        self.start("P", "hub/p")
        tf = self.task_file("c")
        self.start("C", "hub/p/c~auto", "P", task=tf)
        rid = S.post_result("C", {"done": "x"}, kick=False)["id"]
        r = S.change(rid, "Add a test for the empty case.")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["delivered"], "resumed")
        text = open(tf, encoding="utf-8").read()
        self.assertIn("Add a test for the empty case.", text)
        self.assertNotIn("(no updates yet)", text)
        resume = [c for c in self.calls if c[:3] == ["claude", "--bg", "--resume"]]
        self.assertEqual(resume[0][3], "C")
        self.assertEqual(S.get_result(rid)["status"], "change")

    def test_deliver_working_waits_for_idle(self):
        self.start("C", "hub/c~auto")
        self.live["C"] = {"kind": "background", "state": "working", "id": "c1", "pid": 999999}
        self.assertEqual(S.deliver(T.sessions()["C"], "hello", self.live), "later")
        self.assertEqual(S.deliver_pending(T.sessions(), self.live), [])
        self.live["C"]["state"] = "idle"
        acts = S.deliver_pending(T.sessions(), self.live)
        self.assertEqual(acts[0]["act"], "delivered")

    def test_resume_retries_when_a_copy_started(self):
        self.start("C", "hub/c~auto")
        self.copies = 1
        ok, note = S.resume(T.sessions()["C"], "hello", {})
        self.assertTrue(ok, note)
        self.assertIn(["claude", "stop", "cp1"], self.calls)
        self.assertEqual(len([c for c in self.calls if c[:3] == ["claude", "--bg", "--resume"]]), 2)
        self.copies = 2
        ok, note = S.resume(T.sessions()["C"], "hello", {})
        self.assertFalse(ok)
        self.assertIn("a copy twice", note)

    def test_deliver_tab_uses_inbox(self):
        import hubdb
        self.start("C", "hub/c", auto=False)
        self.live["C"] = {"kind": "interactive", "status": "idle"}
        self.assertEqual(S.deliver(T.sessions()["C"], "hello", self.live), "inbox")
        con = hubdb.connect()
        self.assertEqual(hubdb.inbox(con, "C")[0]["body"], "hello")
        con.close()


class TestGroup(Base):
    def tree3(self):
        self.start("R", "hub/r~auto")
        self.start("C1", "hub/r/c1~auto", "R")
        self.start("G1", "hub/r/c1/g1~auto", "C1")
        self.start("X", "hub/x~auto")  # outside the sub-tree
        for k in ("R", "C1", "G1", "X"):
            self.live[k] = {"kind": "background", "state": "working", "id": k.lower(), "pid": 999999}

    def test_pause_resume_with_preview(self):
        import autopilot
        self.tree3()
        tg = S.targets("hub/r~auto")
        self.assertEqual({s["session_id"] for s in tg}, {"R", "C1", "G1"})
        pv = S.preview("pause", tg, self.live)
        self.assertEqual(pv["text"], "pause 3 sessions?")
        r = S.run_group("pause", tg, live=self.live)
        self.assertEqual(len(r["done"]), 3)
        self.assertEqual(sorted(c[2] for c in self.calls if c[:2] == ["claude", "stop"]), ["c1", "g1", "r"])
        for k in ("R", "C1", "G1"):  # claude stop makes the SessionEnd hook write an end event
            T.append({"event": "end", "session_id": k})
        s = T.sessions()
        for k in ("R", "C1", "G1"):
            self.assertTrue(s[k]["paused"])
            self.assertTrue(autopilot.is_off(k))
            self.assertEqual(T.health(s[k], self.live)[0], "paused")
        self.assertFalse(s["X"].get("paused"))
        self.assertEqual(S.preview("pause", S.targets("hub/r~auto"), self.live)["text"], "pause 0 sessions? (3 skipped)")
        tg = S.targets("hub/r~auto")
        self.assertEqual(S.preview("resume", tg, self.live)["text"], "resume 3 sessions?")
        self.calls.clear()
        r = S.run_group("resume", tg, live=self.live)
        self.assertEqual(len([c for c in self.calls if c[:3] == ["claude", "--bg", "--resume"]]), 3)
        s = T.sessions()
        for k in ("R", "C1", "G1"):
            self.assertFalse(s[k]["paused"])
            self.assertFalse(autopilot.is_off(k))
        log = [json.loads(x) for x in open(os.path.join(self.dir, "swarm", "actions.jsonl"), encoding="utf-8")]
        self.assertEqual([x["action"] for x in log].count("pause"), 3)
        self.assertEqual([x["action"] for x in log].count("resume"), 3)

    def test_stop_skips_tabs_and_me(self):
        self.tree3()
        self.live["C1"] = {"kind": "interactive", "status": "idle"}
        tg = S.targets("hub/r~auto")
        pv = S.preview("stop", tg, self.live, me="G1")
        self.assertEqual(pv["text"], "stop 1 session? (2 skipped)")

    def test_folder_targets_and_hide_and_broadcast(self):
        self.tree3()
        T.append({"event": "start", "session_id": "N", "name": "docs/n", "node": "docs"})
        self.assertEqual({s["session_id"] for s in S.targets(node="docs")}, {"N"})
        tf = os.path.join(self.dir, "tasks", "g1.md")
        os.makedirs(os.path.dirname(tf), exist_ok=True)
        with open(tf, "w", encoding="utf-8") as f:
            f.write("# T\n\n" + HEAD + "\n\n(no updates yet)\n")
        T.append({"event": "start", "session_id": "G1", "task": tf})
        r = S.run_group("broadcast", S.targets("hub/r/c1~auto"), words="Stop at 18:00.", live=self.live)
        self.assertEqual(dict(r["done"])["hub/r/c1/g1~auto"], "later")  # working: delivered when idle
        self.assertIn("Stop at 18:00.", open(tf, encoding="utf-8").read())
        S.run_group("hide", S.targets("hub/r~auto"), live=self.live)
        self.assertTrue(all(T.sessions()[k]["hidden"] for k in ("R", "C1", "G1")))


class TestTeams(Base):
    def test_team_order(self):
        S.TEAMS = os.path.join(self.dir, "teams")
        os.makedirs(S.TEAMS)
        with open(os.path.join(S.TEAMS, "t3.toml"), "w", encoding="utf-8") as f:
            f.write('[defaults]\nmodel = "haiku"\n\n[[role]]\nname = "plan"\ntask = "Plan {goal}."\n\n'
                    '[[role]]\nname = "build"\ncount = 2\nafter = ["plan"]\ntask = "Build after {deps}."\n\n'
                    '[[role]]\nname = "check"\nafter = ["build"]\nwhen = "done"\n')
        self.start("P", "hub/p")
        r = S.start_team("t3", "P", "a tiny thing")
        self.assertTrue(r["ok"], r)
        m = r["members"]
        self.assertEqual([x["role"] for x in m], ["plan", "build", "build", "check"])
        self.assertEqual(m[1]["after"], ["p/t3-plan"])
        self.assertEqual(m[3]["after"], ["p/t3-build-1", "p/t3-build-2"])
        argv = [c for c in self.calls if "--name" in c][3]
        self.assertIn("--when", argv)
        self.assertIn("haiku", argv)

    def test_bad_team(self):
        S.TEAMS = os.path.join(self.dir, "teams")
        os.makedirs(S.TEAMS)
        with open(os.path.join(S.TEAMS, "bad.toml"), "w", encoding="utf-8") as f:
            f.write('[[role]]\nname = "a"\nafter = ["b"]\n\n[[role]]\nname = "b"\n')
        with self.assertRaises(ValueError):
            S.load_team("bad")

    def test_shipped_teams_load(self):
        for f in os.listdir(os.path.join(HERE, "teams")):
            if f.endswith(".toml"):
                S.load_team.__globals__["TEAMS"] = os.path.join(HERE, "teams")
                self.assertTrue(S.load_team(f[:-5])["role"])


class TestSpawnQueue(Base):
    def spawn(self, *args):
        import spawn
        sys.argv = ["spawn.py"] + list(args)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as e:
            spawn.main()
        return e.exception.code, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_after_queues_with_task_file(self):
        self.start("P", "work/api/p", node="work/api")
        T.append({"event": "start", "session_id": "P", "cwd": os.path.join(T.HUB, "work", "api")})
        self.start("D", "work/api/p/dep~auto", "P")
        code, out = self.spawn("--parent", "P", "--name", "kid", "--auto", "--after", "D", "--task", "do x")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["queued"])
        self.assertTrue(os.path.exists(out["task_file"]))
        text = open(out["task_file"], encoding="utf-8").read()
        self.assertIn("swarm.py", text)
        self.assertIn("result --name", text)
        q = T.sessions()[out["queue_id"]]
        self.assertEqual((q["after"], q["when"], q["opts"]["auto"]), (["D"], "released", True))
        code, out = self.spawn("--parent", "P", "--name", "kid2", "--after", "nobody-here")
        self.assertEqual(code, 1)
        # a child that spawn.py just started: only its pending file exists, no registry row yet
        os.makedirs(T.PENDING, exist_ok=True)
        with open(os.path.join(T.PENDING, T.slug("hub/p/new~auto", 200) + ".json"), "w", encoding="utf-8") as f:
            f.write("{}")
        code, out = self.spawn("--parent", "P", "--name", "kid3", "--after", "hub/p/new~auto")
        self.assertEqual(code, 0, out)
        self.assertEqual(S.tick(), [])  # not in the registry yet: not released


if __name__ == "__main__":
    unittest.main(verbosity=1)
