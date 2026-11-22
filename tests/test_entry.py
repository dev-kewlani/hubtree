"""The command entry, the hook entry, init and uninstall, run as the installed command runs them (subprocesses)."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def run(args, env, stdin=""):
    e = dict(os.environ)
    e.update(env)
    e["PYTHONPATH"] = SRC
    e.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
    return subprocess.run([sys.executable, "-m", "hubtree"] + args, input=stdin, capture_output=True, text=True,
                          env=e, timeout=120, encoding="utf-8")


@pytest.fixture()
def box():
    d = tempfile.mkdtemp(prefix="hubtree-entry-")
    root = os.path.join(d, "root")
    os.makedirs(os.path.join(root, "work", "api"))
    env = {"HUBTREE_HOME": os.path.join(d, "home"), "HUBTREE_ROOT": root, "HUBTREE_STATE": os.path.join(d, "state"),
           "HUBTREE_DB": os.path.join(d, "state", "hubtree.db"), "CLAUDE_CONFIG_DIR": os.path.join(d, "claude"),
           "HUBTREE_NO_DAEMON": "1", "HUBTREE_TERMINAL": "print"}
    return d, root, env


def test_every_module_compiles():
    import py_compile
    pkg = os.path.join(SRC, "hubtree")
    for n in sorted(os.listdir(pkg)):
        if n.endswith(".py"):
            py_compile.compile(os.path.join(pkg, n), doraise=True)


def test_start_prompt_posttool_end(box):
    d, root, env = box
    cwd = os.path.join(root, "work", "api")
    r = run(["hook", "start"], env, json.dumps({"session_id": "s1", "cwd": cwd, "source": "startup"}))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["sessionTitle"] == "work/api"
    r = run(["hook", "prompt"], env, json.dumps({"session_id": "s1", "cwd": cwd, "prompt": "hi"}))
    assert r.returncode == 0 and "decision" not in r.stdout
    r = run(["hook", "posttool"], env, json.dumps({"session_id": "s1", "cwd": cwd, "tool_name": "Write",
                                                   "tool_input": {"file_path": "a.py"}}))
    assert r.returncode == 0
    with open(os.path.join(env["HUBTREE_STATE"], "ledger", "s1.jsonl"), encoding="utf-8") as f:
        row = json.loads(f.readline())
    assert row["path"] == os.path.normpath(os.path.join(cwd, "a.py"))
    assert run(["hook", "end"], env, json.dumps({"session_id": "s1"})).returncode == 0
    assert not os.path.exists(os.path.join(env["HUBTREE_STATE"], "hook_errors.log"))
    r = run(["where"], env)
    assert r.returncode == 0 and "work/api" in r.stdout


def test_init_uninstall_byte_equal(box):
    d, root, env = box
    os.makedirs(env["CLAUDE_CONFIG_DIR"])
    sp = os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json")
    orig = b'{\r\n  "theme": "dark",\r\n  "env": {"X": "1"}\r\n}\r\n'  # CRLF and odd spacing: bytes must come back
    with open(sp, "wb") as f:
        f.write(orig)
    r = run(["init", "--no-pane", "--root", root], env)
    assert r.returncode == 0, r.stderr
    with open(sp, encoding="utf-8") as f:
        s = json.load(f)
    assert len(s["hooks"]) == 6 and s["theme"] == "dark"
    assert os.path.isdir(os.path.join(env["CLAUDE_CONFIG_DIR"], "skills", "spawn"))
    r = run(["uninstall"], env)
    assert r.returncode == 0, r.stderr
    with open(sp, "rb") as f:
        assert f.read() == orig
    assert not os.path.exists(os.path.join(env["CLAUDE_CONFIG_DIR"], "skills", "spawn"))


def test_uninstall_keeps_other_changes(box):
    d, root, env = box
    r = run(["init", "--no-pane", "--root", root], env)
    assert r.returncode == 0, r.stderr
    sp = os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json")
    with open(sp, encoding="utf-8") as f:
        s = json.load(f)
    s["model"] = "sonnet"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    assert run(["uninstall"], env).returncode == 0
    with open(sp, encoding="utf-8") as f:
        assert json.load(f) == {"model": "sonnet"}
