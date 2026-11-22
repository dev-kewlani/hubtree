"""Headless test: the cursor stays put across refreshes; hide, show hidden, delete.

Everything runs on a temporary registry with 3 fixture sessions and temporary transcripts.
"""
import asyncio
import json
import os

import pytest
from textual.widgets import Tree

import app as hubtree
import tree_lib as T

NODE = "work/fixture"
NAMES = {"r": NODE, "a": NODE + "/hide-me~auto", "b": NODE + "/delete-me~auto"}
END = [{"type": "user", "message": {"content": "hi"}},
       {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}},
       {"type": "system", "subtype": "turn_duration"}]


@pytest.fixture
def reg(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(T, "STATE", str(state))
    monkeypatch.setattr(T, "REGISTRY", str(state / "sessions.jsonl"))
    monkeypatch.setattr(T, "ALERTS", str(state / "alerts"))
    monkeypatch.setattr(T, "live_agents", lambda *a, **k: (T.LIVE_OK.__setitem__(0, True), {})[1])
    monkeypatch.setattr(hubtree, "old_transcripts", lambda known: {})
    sids = {}
    for key, name in NAMES.items():
        sid = f"00000000-0000-4000-8000-00000000000{len(sids) + 1}"
        sids[key] = sid
        tp = tmp_path / (sid + ".jsonl")
        tp.write_text("".join(json.dumps(r) + "\n" for r in END), encoding="utf-8")
        T.append({"event": "start", "session_id": sid, "name": name, "node": NODE,
                  "parent": None if key == "r" else sids["r"], "level": 2 if key == "r" else 3, "cwd": str(tmp_path),
                  "auto": key != "r", "transcript": str(tp)})
        T.append({"event": "end", "session_id": sid})
    return sids


def find(app, name):
    tree = app.query_one(Tree)
    for n in app._walk(tree.root):
        sid = n.data.get("sid") if n.data else None
        if sid and app.data.get(sid, {}).get("name") == name:
            return n
    return None


async def wait_for(pilot, cond, secs=10):
    for _ in range(int(secs / 0.25)):
        await pilot.pause(0.25)
        if cond():
            return True
    return False


async def _main(sids):
    app = hubtree.HubTree()
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_for(pilot, lambda: find(app, NAMES["b"]) is not None)
        tree = app.query_one(Tree)
        tree.move_cursor(find(app, NAMES["a"]))
        line = tree.cursor_line
        await pilot.pause(7)  # two refreshes
        assert tree.cursor_line == line, "cursor kept across refreshes"

        await pilot.press("d")
        assert await wait_for(pilot, lambda: find(app, NAMES["a"]) is None), "hidden session gone"
        assert T.sessions()[sids["a"]].get("hidden")
        await pilot.press("H")
        assert await wait_for(pilot, lambda: find(app, NAMES["a"]) is not None), "shown again with H"
        tree.move_cursor(find(app, NAMES["a"]))
        await pilot.pause(0.2)
        await pilot.press("d")  # unhide
        await pilot.press("H")  # hidden sessions out of view again
        assert await wait_for(pilot, lambda: find(app, NAMES["a"]) is not None
                              and not T.sessions()[sids["a"]].get("hidden"))

        tp = app.data[sids["b"]].get("transcript")
        tree.move_cursor(find(app, NAMES["b"]))
        await pilot.pause(0.2)
        await pilot.press("D")
        await pilot.pause(0.3)
        for ch in "yes":
            await pilot.press(ch)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: bool(T.sessions()[sids["b"]].get("deleted")), 20), "delete recorded"
        assert not os.path.exists(tp), "transcript removed"
        assert await wait_for(pilot, lambda: find(app, NAMES["b"]) is None), "deleted session gone from the tree"
        assert find(app, NAMES["a"]) is not None and os.path.exists(app.data[sids["a"]]["transcript"])
        app.exit()


def test_hide_unhide_delete(reg):
    asyncio.run(_main(reg))
