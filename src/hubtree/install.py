"""`hubtree init`, `hubtree uninstall` and `hubtree doctor`.

init:
  1. Writes <HUBTREE_HOME>/hubtree.toml (kept if it exists) with the tree root (--root, default: this folder).
  2. Backs up the Claude Code settings.json byte for byte to <HUBTREE_HOME>/backup/ (only the first time).
  3. Adds 6 hook entries (SessionStart, UserPromptSubmit, Stop, SessionEnd, PreCompact, PostToolUse for file
     writes) that run
     `<python> -m hubtree hook <event>`.
  4. Copies the side pane to <HUBTREE_HOME>/pane, writes its paths.ts, and installs it as a Claude Code plugin
     from that folder (`claude plugin marketplace add` and `claude plugin install hubtree@hubtree`). --no-pane skips it.
  5. Copies the /spawn and /where skills into the Claude Code skills folder; an existing skill is never overwritten.
  6. Records every step in <HUBTREE_HOME>/install.json, so uninstall undoes exactly that.
uninstall:
  Stops the daemon, removes the plugin and its marketplace, removes the skills init copied (when unchanged), and
  removes the hook entries. When nothing else changed in settings.json since init, it writes the backup back byte for
  byte. Otherwise it keeps the other changes, removes only the hubtree entries, and says so.
  The state folder (the tree) stays; --purge removes <HUBTREE_HOME> too.
The Claude Code folder is env CLAUDE_CONFIG_DIR, else ~/.claude.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import htconf  # noqa: E402

EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "Stop": "stop", "SessionEnd": "end",
          "PreCompact": "precompact", "PostToolUse": "posttool"}
MATCHERS = {"PostToolUse": "Write|Edit|MultiEdit|NotebookEdit"}
MARK = "-m hubtree hook "  # how uninstall knows a hubtree hook entry
PLUGIN = "hubtree@hubtree"
MARKETPLACE = "hubtree"
SKILLS = ("spawn", "where")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def claude_dir():
    return os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))


def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def manifest_path():
    return os.path.join(htconf.home(), "install.json")


def backup_path():
    return os.path.join(htconf.home(), "backup", "settings.json")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _tree_sha(folder):
    h = hashlib.sha256()
    for base, _, files in sorted(os.walk(folder)):
        for n in sorted(files):
            p = os.path.join(base, n)
            h.update(os.path.relpath(p, folder).replace("\\", "/").encode())
            with open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _read_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if text.strip() else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".hubtree.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def hook_command(event):
    py = sys.executable.replace("\\", "/")
    return f'"{py}" {MARK}{event}'


def add_hooks(settings):
    hooks = settings.setdefault("hooks", {})
    for ev, short in EVENTS.items():
        groups = hooks.setdefault(ev, [])
        if any(MARK in (h.get("command") or "") for g in groups for h in g.get("hooks", [])):
            continue
        g = {"hooks": [{"type": "command", "command": hook_command(short), "timeout": 30}]}
        if ev in MATCHERS:
            g = {"matcher": MATCHERS[ev], **g}
        groups.append(g)
    return settings


def remove_hooks(settings):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            kept = [h for h in g.get("hooks", []) if MARK not in (h.get("command") or "")]
            if kept:
                groups.append({**g, "hooks": kept})
            elif not g.get("hooks"):
                groups.append(g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if not hooks:
        del settings["hooks"]
    return settings


def _claude(*args, timeout=120):
    exe = shutil.which("claude")
    if not exe:
        return 127, "claude is not on PATH"
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, env=env,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _ts_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def install_pane(man):
    dst = os.path.join(htconf.home(), "pane")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(HERE, "pane"), dst, ignore=shutil.ignore_patterns("tests", "node_modules"))
    with open(os.path.join(dst, "hooks", "paths.ts"), "w", encoding="utf-8", newline="\n") as f:
        f.write("// Written by `hubtree init`: the Python that runs hubtree and the package folder.\n")
        f.write(f"export const PY = {_ts_str(sys.executable)}\n")
        f.write(f"export const DIR = {_ts_str(HERE)}\n")
    man["pane_dir"] = dst
    rc, out = _claude("plugin", "marketplace", "add", dst)
    man["marketplace"] = rc == 0
    if rc != 0:
        return f"pane: marketplace add failed ({rc}): {out[-300:]}"
    rc, out = _claude("plugin", "install", PLUGIN)
    man["plugin"] = rc == 0
    return "pane: installed" if rc == 0 else f"pane: install failed ({rc}): {out[-300:]}"


def install_skills(man):
    done, skipped = [], []
    base = os.path.join(claude_dir(), "skills")
    for name in SKILLS:
        src = os.path.join(HERE, "skills", name)
        dst = os.path.join(base, name)
        if not os.path.isdir(src):
            continue
        if os.path.exists(dst):
            skipped.append(name)
            continue
        shutil.copytree(src, dst)
        done.append({"name": name, "path": dst, "sha": _tree_sha(dst)})
    man["skills"] = done
    msg = "skills: " + (", ".join("/" + d["name"] for d in done) or "none copied")
    if skipped:
        msg += f" (kept your own: {', '.join('/' + s for s in skipped)})"
    return msg


def init(a):
    home = htconf.home()
    os.makedirs(home, exist_ok=True)
    cfg = htconf.config_path()
    lines = []
    if not os.path.exists(cfg):
        root = os.path.abspath(os.path.expanduser(a.root or os.getcwd()))
        with open(cfg, "w", encoding="utf-8", newline="\n") as f:
            f.write(htconf.default_text(root))
        lines.append(f"config: wrote {cfg} (root {root})")
    else:
        lines.append(f"config: kept {cfg}")
    c = htconf.reload()
    os.makedirs(c.state, exist_ok=True)

    man = _read_json(manifest_path()) if os.path.exists(manifest_path()) else {}
    sp, bp = settings_path(), backup_path()
    if "settings_existed" not in man:
        man["settings_existed"] = os.path.exists(sp)
        os.makedirs(os.path.dirname(bp), exist_ok=True)
        if man["settings_existed"]:
            shutil.copy2(sp, bp)
            lines.append(f"settings: backup at {bp}")
        else:
            lines.append("settings: no settings.json before init (uninstall removes the one init makes)")
    man["settings_path"] = sp
    settings = add_hooks(_read_json(sp))
    _write_json(sp, settings)
    lines.append(f"hooks: {len(EVENTS)} events in {sp}")
    _write_json(manifest_path(), man)

    if not a.no_pane:
        lines.append(install_pane(man))
    lines.append(install_skills(man))
    man["after_init_sha"] = _sha(sp)
    _write_json(manifest_path(), man)
    lines.append("daemon: starts with the next Claude Code session (or run `hubtree daemon`)")
    print("\n".join(lines))
    print("Done. Start Claude Code in a folder under the root; run `hubtree` to see the tree.")
    return 0


def uninstall(a):
    man = _read_json(manifest_path())
    lines = []
    try:
        import daemon
        lines.append(daemon.stop())
    except Exception as e:  # noqa: BLE001
        lines.append(f"daemon: not stopped ({type(e).__name__})")
    if man.get("plugin"):
        rc, out = _claude("plugin", "uninstall", PLUGIN)
        lines.append("pane: plugin removed" if rc == 0 else f"pane: plugin uninstall failed: {out[-200:]}")
    if man.get("marketplace"):
        rc, out = _claude("plugin", "marketplace", "remove", MARKETPLACE)
        lines.append("pane: marketplace removed" if rc == 0 else f"pane: marketplace remove failed: {out[-200:]}")
    for s in man.get("skills", []):
        p = s["path"]
        if os.path.isdir(p) and _tree_sha(p) == s["sha"]:
            shutil.rmtree(p)
            lines.append(f"skills: removed /{s['name']}")
        elif os.path.isdir(p):
            lines.append(f"skills: kept /{s['name']} (you changed it)")

    sp, bp = man.get("settings_path") or settings_path(), backup_path()
    if os.path.exists(sp):
        cleaned = remove_hooks(_read_json(sp))
        before = _read_json(bp) if man.get("settings_existed") and os.path.exists(bp) else {}
        for k in [k for k, v in cleaned.items() if v in ({}, []) and k not in before]:
            del cleaned[k]  # an empty table left by the plugin commands (enabledPlugins, extraKnownMarketplaces)
        if cleaned == before:
            if man.get("settings_existed"):
                shutil.copyfile(bp, sp)
                lines.append("settings: restored byte for byte from the backup")
            else:
                os.remove(sp)
                lines.append("settings: removed (there was no settings.json before init)")
        else:
            _write_json(sp, cleaned)
            lines.append("settings: hubtree hooks removed; your other changes since init are kept")
    if os.path.exists(manifest_path()):
        os.remove(manifest_path())
    if a.purge:
        shutil.rmtree(htconf.home(), ignore_errors=True)
        lines.append(f"purge: removed {htconf.home()}")
    else:
        lines.append(f"kept: {htconf.home()} (the tree state and the settings backup; --purge removes it)")
    print("\n".join(lines))
    return 0


def doctor(a):
    c = htconf.reload()
    ok = True
    rows = [("config", htconf.config_path() + ("" if os.path.exists(htconf.config_path()) else " (missing: run hubtree init)")),
            ("config error", c.error or "none"), ("root", c.root), ("state", c.state), ("python", sys.executable)]
    s = _read_json(settings_path()) if os.path.exists(settings_path()) else {}
    n = sum(1 for g in (s.get("hooks") or {}).values() for x in g for h in x.get("hooks", [])
            if MARK in (h.get("command") or ""))
    rows.append(("hooks", f"{n} of {len(EVENTS)} in {settings_path()}"))
    ok &= n == len(EVENTS) and not c.error
    rc, out = _claude("--version", timeout=30)
    rows.append(("claude", out.splitlines()[0] if rc == 0 and out else "not found"))
    ok &= rc == 0
    try:
        import term
        b = term.backend()
        rows.append(("terminal", f"{b} ({'found' if b == 'print' or shutil.which('wt' if b == 'wt' else 'tmux') else 'NOT FOUND'})"))
    except Exception as e:  # noqa: BLE001
        rows.append(("terminal", f"error {e}"))
    try:
        import daemon
        rows.append(("daemon", daemon.status()))
    except Exception as e:  # noqa: BLE001
        rows.append(("daemon", f"error {type(e).__name__}"))
    for k, v in rows:
        print(f"{k:13} {v}")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hubtree")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--root", help="the folder whose sub-folders become the tree nodes (default: this folder)")
    i.add_argument("--no-pane", action="store_true", help="do not install the side pane plugin")
    u = sub.add_parser("uninstall")
    u.add_argument("--purge", action="store_true", help="also remove the hubtree home folder (the tree state)")
    sub.add_parser("doctor")
    a = ap.parse_args(argv)
    return {"init": init, "uninstall": uninstall, "doctor": doctor}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
