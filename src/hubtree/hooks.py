"""The Claude Code hook entry of hubtree: `hubtree hook <event>` with the hook JSON on stdin.

Events (hubtree init writes them into settings.json):
    start       SessionStart: name the session, link it to its parent, record it; context lines and unread messages.
    prompt      UserPromptSubmit: a rotation near the context limit (blocks the prompt), else unread messages.
    stop        Stop: heartbeat (idle).
    end         SessionEnd: record that the session closed.
    precompact  PreCompact: an auto compaction becomes a rotation.
    posttool    PostToolUse (Write, Edit, MultiEdit, NotebookEdit): one line in <state>/ledger/<sid>.jsonl, which the
                CONFLICT marks and the result check read.
A hook never stops a session: every error is written to <state>/hook_errors.log and the exit code is 0.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_MSGS = 5
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _log(event, err):
    try:
        import tree_lib as T
        os.makedirs(T.STATE, exist_ok=True)
        with open(os.path.join(T.STATE, "hook_errors.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": T.now(), "event": event, "error": err[-800:]}) + "\n")
    except Exception:
        pass


def _beat_and_messages(event, data):
    """Heartbeat in the message db; at start and prompt, the unread messages as context lines."""
    import hubdb
    import tree_lib as T
    sid = data.get("session_id") or hubdb.me()[0]
    cwd = data.get("cwd") or os.getcwd()
    pid = int(os.environ.get("CLAUDE_PID") or os.getppid())
    state = {"start": "working", "prompt": "working", "stop": "idle", "end": "ended"}[event]
    node = T.node_of(cwd)[0]
    name = (T.sessions().get(sid) or {}).get("name") if event == "start" else None
    lines = []
    con = hubdb.connect()
    try:
        hubdb.beat(con, state, name=name, node=node, cwd=cwd, sid=sid, pid=pid)
        if event == "start":
            hubdb.reap(con)
        if event in ("start", "prompt"):
            msgs = hubdb.inbox(con, sid=sid)
            for m in msgs[:MAX_MSGS]:
                lines.append(f"message from {m['from'][:40]}: {m['body'][:300]}")
            if len(msgs) > MAX_MSGS:
                lines.append(f"{len(msgs) - MAX_MSGS} more messages: run `hubtree inbox`")
    finally:
        con.close()
    return ["[hubtree] " + x for x in lines]


def ledger(data):
    """Append the written path of a file tool call to the session's write ledger."""
    if data.get("tool_name") not in FILE_TOOLS:
        return
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    sid = data.get("session_id")
    if not path or not sid:
        return
    import tree_lib as T
    if not os.path.isabs(path):
        path = os.path.join(data.get("cwd") or os.getcwd(), path)
    safe = "".join(ch for ch in str(sid) if ch.isalnum() or ch in "-_")[:80]
    d = os.path.join(T.STATE, "ledger")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, safe + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": T.now(), "sid": sid, "tool": data.get("tool_name"),
                            "path": os.path.normpath(path)}) + "\n")


def run(event, data):
    """Returns the text for stdout ('' for none)."""
    out = ""
    if event == "start":
        import session_hook
        res = session_hook.start(data) or {}
        try:
            lines = _beat_and_messages("start", data)
        except Exception:
            _log(event, traceback.format_exc())
            lines = []
        if lines and res.get("hookSpecificOutput"):
            h = res["hookSpecificOutput"]
            h["additionalContext"] = (h.get("additionalContext", "") + "\n" + "\n".join(lines)).strip()
        out = json.dumps(res) if res else ""
        if not os.environ.get("HUBTREE_NO_DAEMON"):
            import daemon
            daemon.ensure_running()
    elif event == "prompt":
        try:
            import rotate
            reason = rotate.prompt_step(data)
        except Exception:
            _log(event, traceback.format_exc())
            reason = None
        if reason:
            return json.dumps({"decision": "block", "reason": reason})
        out = "\n".join(_beat_and_messages("prompt", data))
    elif event == "stop":
        _beat_and_messages("stop", data)
    elif event == "end":
        import session_hook
        session_hook.end(data)
        _beat_and_messages("end", data)
    elif event == "precompact":
        import rotate
        res = rotate.precompact_step(data)
        out = json.dumps(res) if res else ""
    elif event == "posttool":
        ledger(data)
    else:
        raise ValueError(f"unknown hook event: {event}")
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        data = json.loads(raw or "{}")
        text = run(event, data)
        if text:
            sys.stdout.write(text)
    except Exception:
        _log(event, traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
