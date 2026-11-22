"""Test for the short `hubtree where` view (tree_view.py --text). Fake registry, temporary files only.

Run: python -m pytest tests/test_where.py -q
"""
import io
import json
import os
import re
import sys
import time
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "hubtree"))
import tree_lib as T  # noqa: E402
import tree_view  # noqa: E402


@pytest.fixture
def world(monkeypatch, tmp_path):
    """114 fake sessions: 3 you, 2 work, 2 tab, 107 closed, one hidden. Returns (fake registry, states)."""
    now = time.time()
    fake, states = {}, {}

    def add(i, state, hidden=False):
        sid = f"sid{i:03d}"
        tp = str(tmp_path / (sid + ".jsonl"))
        with open(tp, "w") as f:
            f.write("{}\n")
        t = now - i * 60  # a higher number is older
        os.utime(tp, (t, t))
        node = f"hub/area{i % 4}"
        fake[sid] = {"session_id": sid, "name": f"{node}/session-{i:03d}-with-a-rather-long-name-for-the-cut-test",
                     "node": node, "transcript": tp, "created": f"2026-10-{1 + i % 28:02d}", "hidden": hidden,
                     "cwd": "D:/x"}
        states[sid] = state

    kinds = ["you"] * 3 + ["work"] * 2 + ["tab"] * 2
    for i, k in enumerate(kinds):
        add(i, k)
    for i in range(len(kinds), 114):
        add(i, "closed")
    add(200, "closed", hidden=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(T, "sessions", lambda: fake)
    monkeypatch.setattr(T, "live_agents", lambda max_age=None: {})
    monkeypatch.setattr(T, "health", lambda s, live, now_s=None: (states[s["session_id"]], {}))
    monkeypatch.setattr(T, "why", lambda st, info: "waits for you" if st == "you" else "")
    monkeypatch.setattr(T, "STATE", str(state_dir))
    return fake, states, len(kinds)


def run(*args):
    old = sys.argv
    sys.argv = ["tree_view.py", *args]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            tree_view.main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_short_view(world):
    fake, states, live_n = world
    short = run("--text").splitlines()
    assert len(short) <= 20, f"short view has {len(short)} lines"
    for sid, st in states.items():
        if st == "you":
            assert any(f"[you] {fake[sid]['name'][:20]}" in ln for ln in short), sid
    assert short[0] == "Waits for you: 3", short[0]
    assert short[-1] == f"{114 - live_n - 5} closed hidden. hubtree where --all", short[-1]
    rows = [ln for ln in short if re.match(r"^ ?\d+  \[", ln)]
    assert len(rows) == live_n + 5, len(rows)
    assert all(len(ln) <= 62 for ln in rows), max(len(ln) for ln in rows)
    got = [ln.split("]")[0].split("[")[1] for ln in rows]
    assert got == ["you"] * 3 + ["work"] * 2 + ["tab"] * 2 + ["closed"] * 5, got
    assert [int(ln.split()[0]) for ln in rows] == list(range(1, 13))
    # the 5 closed rows are the newest ones (lowest i)
    assert "session-007" in rows[7] and "session-011" in rows[11], rows[7:]


def test_all_waiting_node_and_json(world):
    # --all keeps the full tree: every non-hidden session, none of the hidden ones
    full = run("--text", "--all").splitlines()
    assert sum(1 for ln in full if "] session-" in ln) == 114
    assert not any("session-200" in ln for ln in full)
    assert run("--all") == run("--all", "--text")
    # --waiting is unchanged; --node filters the short view
    assert run("--waiting").splitlines()[0] == "Waits for you: 3"
    node = run("--text", "--node", "hub/area1")
    assert all("area1" in ln or not re.match(r"^ ?\d+  \[", ln) for ln in node.splitlines())
    # the JSON output is the rows for the pane
    data = json.loads(run())
    assert sum(1 for r in data["rows"] if r["kind"] == "session") == 114


def test_pick(world):
    run("--text")
    with open(os.path.join(T.STATE, "where_pick.json"), encoding="utf-8") as f:
        saved = json.load(f)
    assert len(saved) == 12 and set(saved[0]) == {"n", "sid", "name", "state", "cwd"}
    assert not [p for p in os.listdir(T.STATE) if p.endswith(".tmp")]
    out = run("--pick", "3")
    assert json.loads(out) == saved[2] and saved[2]["state"] == "you"
    for bad in ("99", "x"):
        with pytest.raises(SystemExit) as e:
            run("--pick", bad)
        assert e.value.code == 1
    os.remove(os.path.join(T.STATE, "where_pick.json"))
    with pytest.raises(SystemExit) as e:
        run("--pick", "1")
    assert e.value.code == 1
