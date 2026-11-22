"""Claude Code hook of the session tree.

SessionStart: give the session its path name, link it to its parent, record it, and tell it where it sits.
SessionEnd:   record that the session closed.
Usage: through hooks.py (`hubtree hook start` and `hubtree hook end`), or python session_hook.py start | end
The hook never blocks a session: every error is swallowed and the exit code is 0.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_lib as T  # noqa: E402


def take_pending(title):
    """Return and delete the pending-spawn record that matches the session title, if any."""
    if not title:
        return None
    p = os.path.join(T.PENDING, T.slug(title, 200) + ".json")
    if os.path.exists(p):
        try:
            rec = json.load(open(p, encoding="utf-8"))
        finally:
            os.remove(p)
        return rec
    return None


def start(data):
    sid = data.get("session_id")
    cwd = data.get("cwd") or os.getcwd()
    source = data.get("source") or "startup"
    title = data.get("session_title") or ""
    node, level, _ = T.node_of(cwd)
    known = T.sessions()

    if sid in known and known[sid].get("name"):  # resume, compact, clear of a known session
        me = known[sid]
        T.append({"event": "resume", "session_id": sid, "source": source, "cwd": cwd})
        name, parent = me["name"], me.get("parent")
    else:
        pend = take_pending(title)
        taken = {v.get("name") for v in known.values()}
        if pend:
            name, parent = pend["name"], pend.get("parent")
            if pend.get("rotated_from"):  # rotate.py: this session takes over the name of the old one
                T.append({"event": "rotate", "session_id": pend["rotated_from"], "to": sid, "name": name})
            auto, fork, tab, task = pend.get("auto", False), pend.get("fork", False), pend.get("tab", True), pend.get("task")
            depth = pend.get("level", level)
        else:
            parent, auto, fork, tab, task, depth = None, False, source == "fork", True, None, level
            managers = [v for v in known.values()
                        if (v.get("root") and v.get("node") == node) or v.get("name") == node]
            default_title = title.lower().startswith(os.path.basename(cwd).lower()[:6])  # e.g. "app-3f"
            if title and not default_title:
                base = f"{node}/{T.slug(title)}" if node != "hub" else f"hub/{T.slug(title)}"
            else:
                base = node if not managers else f"{node}/session"
            if fork:
                base = f"{node}/fork"
            name = T.unique_name(base, taken)
        T.append({"event": "start", "session_id": sid, "name": name, "parent": parent, "node": node,
                  "level": depth, "cwd": cwd, "auto": auto, "fork": fork, "tab": tab, "task": task,
                  "source": source, "transcript": data.get("transcript_path"),
                  "limit_min": (pend or {}).get("limit_min"),
                  "budget_usd": (pend or {}).get("budget_usd"), "budget_tokens": (pend or {}).get("budget_tokens")})
        me = {"name": name, "parent": parent}

    pname = None
    if parent:
        pname = (T.sessions().get(parent) or {}).get("name", parent)
    files = ", ".join(f"`{f}`" for f in T.entry_files(node))
    ctx = (
        f"hubtree: this session is `{name}` (id {sid}). Node `{node}`, level {level}. "
        f"Parent: `{pname or 'none'}`."
        + (f" Read these entry files first: {files}." if files else "")
        + " Start a child session with /spawn; /where prints the tree."
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "sessionTitle": name, "additionalContext": ctx}}


def end(data):
    sid = data.get("session_id")
    T.append({"event": "end", "session_id": sid, "reason": data.get("reason")})
    # A run that saved no transcript (claude -p --no-session-persistence, a failed resume) cannot be opened again:
    # take it out of the tree so that nobody tries.
    tp = data.get("transcript_path") or (T.sessions().get(sid) or {}).get("transcript")
    if tp and not os.path.exists(tp):
        T.append({"event": "delete", "session_id": sid, "reason": "no transcript"})


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        res = {"start": start, "end": end}[sys.argv[1]](data)
        if res:
            sys.stdout.write(json.dumps(res))
    except Exception as e:  # never block a session
        try:
            T.append({"event": "hook_error", "error": repr(e)[:300]})
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
