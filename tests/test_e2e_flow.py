"""End to end: install, one parent session, 2 children (1 tab, 1 background), the tree, a result, accept, uninstall.

Sessions need a Claude login, so a stand-in `claude` (tests/stub_claude.py) plays the background start, the live
list and stop. Each session start runs the exact hook command that `hubtree init` wrote into settings.json, as
Claude Code runs it. Environment:
    HUBTREE_E2E_CMD       the installed `hubtree` command (default: python -m hubtree from src/)
    HUBTREE_E2E_TERMINAL  the terminal back end for the tab child (default: print; CI uses tmux)
    HUBTREE_E2E_PANE      1: also install and remove the side pane plugin (needs the real `claude` CLI)
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
STUB = os.path.join(HERE, "stub_claude.py")


def base_cmd():
    c = os.environ.get("HUBTREE_E2E_CMD")
    return [c] if c else [sys.executable, "-m", "hubtree"]


def make_env(d):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("HUBTREE_", "CLAUDE_CODE_"))}
    e.update({"HUBTREE_HOME": os.path.join(d, "home"), "CLAUDE_CONFIG_DIR": os.path.join(d, "claude"),
              "HUBTREE_TERMINAL": os.environ.get("HUBTREE_E2E_TERMINAL", "print"),
              "HUBTREE_TERM_LOG": os.path.join(d, "tabs.jsonl"),
              "HUBTREE_CLAUDE": json.dumps([sys.executable, STUB]),
              "HUBTREE_SWARM_CLAUDE": json.dumps([sys.executable, STUB]),
              "HUBTREE_AUTOPILOT_CLAUDE": json.dumps([sys.executable, STUB]),
              "HUBTREE_NO_DAEMON": "1", "STUB_DIR": d})
    if not os.environ.get("HUBTREE_E2E_CMD"):
        e["PYTHONPATH"] = SRC
    return e


def ht(args, env, stdin=None, check=True):
    r = subprocess.run(base_cmd() + args, input=stdin, capture_output=True, text=True, env=env, timeout=180,
                       encoding="utf-8")
    if check:
        assert r.returncode == 0, f"{args}: exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def hook_cmd(settings_path, event):
    with open(settings_path, encoding="utf-8") as f:
        s = json.load(f)
    for g in s["hooks"][event]:
        for h in g["hooks"]:
            if "-m hubtree hook" in h["command"]:
                return h["command"]
    raise AssertionError(f"no hubtree hook for {event}")


def fire(settings_path, event, payload, env):
    """Run the hook command of settings.json the way Claude Code does: through the shell, JSON on stdin."""
    cmd = hook_cmd(settings_path, event)
    if os.name == "nt":
        r = subprocess.run(cmd, shell=True, input=json.dumps(payload), capture_output=True, text=True, env=env,
                           timeout=60, encoding="utf-8")
    else:
        r = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True, env=env,
                           timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_flow():
    d = tempfile.mkdtemp(prefix="hubtree-e2e-")
    root = os.path.join(d, "root")
    work = os.path.join(root, "work", "demo")
    os.makedirs(work)
    env = make_env(d)
    os.makedirs(env["CLAUDE_CONFIG_DIR"])
    sp = os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json")
    orig = b'{\n  "theme": "dark",\n  "permissions": {"allow": ["Bash(ls:*)"]}\n}\n'
    with open(sp, "wb") as f:
        f.write(orig)
    pane = os.environ.get("HUBTREE_E2E_PANE") == "1"
    env_init = dict(env)
    if pane:  # the plugin commands need the real claude CLI
        env_init.pop("HUBTREE_CLAUDE")
    ht(["init", "--root", root] + ([] if pane else ["--no-pane"]), env_init)
    print(ht(["doctor"], env, check=False).stdout)

    # 1. the parent session starts in work/demo
    env_s = dict(env, HUBTREE_E2E_SETTINGS=sp)
    out = fire(sp, "SessionStart", {"session_id": "parent-1", "cwd": work, "source": "startup",
                                    "transcript_path": os.path.join(d, "parent-1.jsonl")}, env_s)
    assert json.loads(out)["hookSpecificOutput"]["sessionTitle"] == "work/demo"

    # 2. a tab child and a background child
    r = ht(["spawn", "--parent", "parent-1", "--name", "work/demo/tab-child", "--task", "write the notes",
            "--cwd", work], env)
    assert json.loads(r.stdout.strip().splitlines()[-1])["mode"] == "tab"
    if env["HUBTREE_TERMINAL"] == "print":
        with open(env["HUBTREE_TERM_LOG"], encoding="utf-8") as f:
            tab = json.loads(f.readline())
        assert tab["title"] == "work/demo/tab-child" and "--dangerously-skip-permissions" not in tab["argv"]
    elif env["HUBTREE_TERMINAL"] == "tmux":
        w = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_name}"], capture_output=True, text=True)
        assert "work/demo/tab-child" in w.stdout, w.stdout
    # the tab session starts: Claude Code runs the start hook with the title that spawn gave it
    fire(sp, "SessionStart", {"session_id": "tab-1", "cwd": work, "source": "startup",
                              "session_title": "work/demo/tab-child"}, env_s)

    r = ht(["spawn", "--parent", "parent-1", "--name", "work/demo/bg-child", "--task", "check the links",
            "--cwd", work, "--auto"], env_s)
    assert json.loads(r.stdout.strip().splitlines()[-1])["mode"] == "background", r.stdout

    # 3. both children show in /where and in the rows of the app and the pane
    where = ht(["where"], env).stdout
    assert "tab-child" in where and "bg-child" in where, where
    rows = json.loads(ht(["view", "--me", "parent-1"], env).stdout)
    names = [x.get("name") for x in (rows if isinstance(rows, list) else rows.get("rows", []))]
    assert "work/demo/tab-child" in names and "work/demo/bg-child~auto" in names, names

    # 4. the background child posts a result; the parent accepts it
    with open(os.path.join(d, "stub_sessions.json"), encoding="utf-8") as f:
        bg_sid = json.load(f)["work/demo/bg-child~auto"]
    posted = ht(["swarm", "result", "--sid", bg_sid, "--done", "links checked", "--proof", "tests/links.txt"], env)
    rid = json.loads(posted.stdout.strip().splitlines()[-1])["id"]
    listed = ht(["swarm", "results", "--parent", "parent-1"], env).stdout
    assert "links checked" in listed, listed
    acc = ht(["swarm", "accept", "--force", str(rid)], env, check=False)
    assert acc.returncode == 0, acc.stdout + acc.stderr
    from_reg = [json.loads(x) for x in open(os.path.join(env["HUBTREE_HOME"], "state", "sessions.jsonl"),
                                            encoding="utf-8")]
    assert any(e.get("event") == "accept" and e.get("session_id") == bg_sid for e in from_reg)

    # 5. uninstall: settings.json is byte for byte its backup
    fire(sp, "SessionEnd", {"session_id": "parent-1"}, env_s)
    ht(["uninstall"], env_init)
    with open(sp, "rb") as f:
        assert f.read() == orig
    assert not os.path.exists(os.path.join(env["CLAUDE_CONFIG_DIR"], "skills", "spawn"))
    if env["HUBTREE_TERMINAL"] == "tmux":
        subprocess.run(["tmux", "kill-session", "-t", "hubtree"], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)
