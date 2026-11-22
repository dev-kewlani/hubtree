"""Headless test: sub-tree view, newest-first order, rename of a root session (and back)."""
import asyncio
import json
import os
import time

import pytest
from textual.widgets import Tree

import app as hubtree
import tree_lib as T

END = [{"type": "user", "message": {"content": "hi"}},
       {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}},
       {"type": "system", "subtype": "turn_duration"}]
# (key, name, node, age in seconds: a larger age is older)
SESSIONS = [("w", "work/app", "work/app", 500), ("w1", "work/app/old-one", "work/app", 400),
            ("w2", "work/app/new-one", "work/app", 100), ("o", "other", "other", 300)]


@pytest.fixture
def reg(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(T, "STATE", str(state))
    monkeypatch.setattr(T, "REGISTRY", str(state / "sessions.jsonl"))
    monkeypatch.setattr(T, "ALERTS", str(state / "alerts"))
    monkeypatch.setattr(T, "live_agents", lambda *a, **k: (T.LIVE_OK.__setitem__(0, True), {})[1])
    monkeypatch.setattr(hubtree, "old_transcripts", lambda known: {})
    sids, root_of = {}, {}
    for key, name, node, age in SESSIONS:
        sid = f"00000000-0000-4000-8000-00000000000{len(sids) + 1}"
        sids[key] = sid
        tp = tmp_path / (sid + ".jsonl")
        tp.write_text("".join(json.dumps(r) + "\n" for r in END), encoding="utf-8")
        os.utime(tp, (time.time() - age, time.time() - age))
        root = name == node
        root_of.setdefault(node, sid) if root else None
        T.append({"event": "start", "session_id": sid, "name": name, "node": node,
                  "parent": None, "level": 2, "cwd": str(tmp_path), "transcript": str(tp)})
        T.append({"event": "end", "session_id": sid})
    return sids


def rows(app):
    out = []

    def walk(n, d=0):
        out.append((d, n))
        for c in n.children:
            walk(c, d + 1)
    walk(app.query_one(Tree).root)
    return out


async def load(app, pilot):
    for _ in range(40):
        await pilot.pause(0.25)
        if app.data:
            break
    await pilot.pause(1.0)


async def _main(sids):
    app = hubtree.HubTree("work")
    async with app.run_test(size=(160, 60)) as pilot:
        await load(app, pilot)
        rs = rows(app)
        assert str(app.query_one(Tree).root.label) == "work/"
        nodes = {app.data[n.data["sid"]].get("node") for _, n in rs if n.data and n.data.get("sid")}
        assert nodes and all(x == "work" or x.startswith("work/") for x in nodes), nodes
        sess = [app.data[n.data["sid"]] for _, n in rs if n.data and n.data.get("sid")]
        assert app.is_root(sess[0]), "root first"
        rest = [s.get("recent") for s in sess[1:]]
        assert rest == sorted(rest, reverse=True), "others newest first"
        app.exit()

    app = hubtree.HubTree()
    async with app.run_test(size=(160, 60)) as pilot:
        await load(app, pilot)
        sid = sids["w"]
        tp = app.data[sid]["transcript"]
        app.do_rename(sid, "app-main")
        last = open(tp, encoding="utf-8").read().splitlines()[-1]
        assert T.sessions()[sid]["name"] == "work/app-main"
        assert json.loads(last).get("customTitle") == "work/app-main"
        await pilot.pause(4)
        assert app.is_root(app.data[sid]), "still a root after rename"
        app.do_rename(sid, "work/app")
        assert T.sessions()[sid]["name"] == "work/app"
        app.exit()


def test_subtree_order_and_rename(reg):
    asyncio.run(_main(reg))
