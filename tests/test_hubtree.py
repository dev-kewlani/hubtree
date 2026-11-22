"""Headless test of the tree app: load, build the tree, move the cursor, show the detail panel. Temporary state only."""
import asyncio
import json
import os

import pytest
from textual.widgets import Static, Tree

import app as hubtree
import tree_lib as T

NODE = "work/app"
END = [{"type": "user", "message": {"content": "hi"}},
       {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}},
       {"type": "system", "subtype": "turn_duration"}]


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """A temporary registry with a root session and two children, and their transcripts."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(T, "STATE", str(state))
    monkeypatch.setattr(T, "REGISTRY", str(state / "sessions.jsonl"))
    monkeypatch.setattr(T, "ALERTS", str(state / "alerts"))
    monkeypatch.setattr(T, "live_agents", lambda *a, **k: (T.LIVE_OK.__setitem__(0, True), {})[1])
    monkeypatch.setattr(hubtree, "old_transcripts", lambda known: {})
    sids = {}
    for key, name in (("r", NODE), ("a", NODE + "/first~auto"), ("b", NODE + "/second~auto")):
        sid = f"00000000-0000-4000-8000-00000000000{len(sids) + 1}"
        sids[key] = sid
        tp = tmp_path / (sid + ".jsonl")
        tp.write_text("".join(json.dumps(r) + "\n" for r in END), encoding="utf-8")
        T.append({"event": "start", "session_id": sid, "name": name, "node": NODE,
                  "parent": None if key == "r" else sids["r"], "level": 2 if key == "r" else 3, "cwd": str(tmp_path),
                  "auto": key != "r", "transcript": str(tp)})
        T.append({"event": "end", "session_id": sid})
    return sids


async def _main(sids):
    app = hubtree.HubTree()
    async with app.run_test(size=(160, 50)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.data:
                break
        await pilot.pause(0.5)
        tree = app.query_one(Tree)

        def walk(n, d=0):
            yield d, n
            for c in n.children:
                yield from walk(c, d + 1)
        rows = list(walk(tree.root))
        labels = [str(n.label.plain if hasattr(n.label, "plain") else n.label) for _, n in rows]
        assert len(app.data) == 3
        assert any("first~auto" in x for x in labels) and any("second~auto" in x for x in labels)
        assert not {"T", "i"} & {b.key for b in hubtree.HubTree.BINDINGS}  # no extra panel keys
        await pilot.press("down", "down", "down")
        await pilot.pause(0.3)
        detail = str(app.query_one("#detail", Static).render())
        assert "state:" in detail
        app.exit()


def test_tree_loads_and_shows_detail(reg):
    asyncio.run(_main(reg))


def test_old_session_opens_with_resume_args(reg, tmp_path):
    """Enter on a closed session opens a tab with claude --resume <id> and the configured extra args."""
    log = tmp_path / "term.jsonl"
    os.environ["HUBTREE_TERM_LOG"] = str(log)
    try:
        from open_session import open_tab
        open_tab(reg["a"], str(tmp_path), "first", transcript=None)
    finally:
        del os.environ["HUBTREE_TERM_LOG"]
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["argv"][:3] == ["claude", "--resume", reg["a"]]
    assert not [a for a in line["argv"] if a.startswith(("--dangerously", "--remote"))]
