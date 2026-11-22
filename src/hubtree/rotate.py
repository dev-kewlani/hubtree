"""Session rotation instead of compaction.

A session never compacts. When its context reaches the threshold, the next user prompt is blocked, a handoff file is
written, a new session with the same name starts (same cwd, node, parent, kind and model), and the old claude
process ends. The new session reads the handoff file, then answers the blocked prompt.

  python rotate.py status [--sid SID] [--transcript PATH]   the context size and the threshold of one session
  python rotate.py finish OLD_SID                            the finisher (normally started detached by the hook)
  python rotate.py sweep                                     run every unfinished finisher again (state files)

Hook entry points (hooks.py): prompt_step(payload) and precompact_step(payload). Both fail open: any error
returns None and the prompt or the compaction goes on as before.

Context size: the status line writes <state>/context/<sid>.json (write_context, current_usage of the last API call).
The reader also reads the newest assistant usage in the transcript (input + cache read + cache creation tokens) and
takes the larger value. A compact boundary after the newest usage record makes the transcript value 0.
Threshold: env HUBTREE_ROTATE_THRESHOLD, else hubtree.toml [rotation] threshold, else 800000. HUBTREE_ROTATE_OFF=1 or
[rotation] enabled = false turns rotation off. The env value goes on to the new session (the test threshold stays).

The finisher (one per rotation, state file <state>/rotation/<old sid>.json):
  1. waits up to 120 s for the new session to start (the session hook writes the registry event "rotate");
     no start: the old session stays, the pending link is removed, rotation pauses for 30 min for that session;
  2. ends the old claude process (the PID that the hook found; its start time must match) or stops a background one;
  3. writes the registry event "end" (reason rotate). The old transcript stays where it is.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import term  # noqa: E402
import tree_lib as T  # noqa: E402

DEFAULT_THRESHOLD = 800_000
HANDOFF_TOKENS = 60_000      # the last turns verbatim, at most this many tokens (about 4 characters per token)
HANDOFF_SHARE = 0.3          # and at most this share of the threshold, so a new session never starts over it
TOOL_CUT = 1500              # characters of one tool input or tool result in the handoff
LINK_WAIT_S = 120            # the finisher waits this long for the new session to start
PAUSE_S = 1800               # after a failed start, no rotation of that session for this long
STORM_MIN_AGE_S = 600       # a session that a rotation started does not rotate again before this age
STORM_MAX_PER_HOUR = 4      # and one name rotates at most this often in one hour
CHARS_PER_TOKEN = 4


def state_dir():
    return os.environ.get("HUBTREE_STATE") or T.STATE


def ctx_dir():
    return os.path.join(state_dir(), "context")


def rot_dir():
    d = os.path.join(state_dir(), "rotation")
    os.makedirs(d, exist_ok=True)
    return d


def handoff_dir():
    d = os.path.join(state_dir(), "handoff")
    os.makedirs(d, exist_ok=True)
    return d


def log(event, **kw):
    try:
        kw.update(event=event, ts=T.now())
        with open(os.path.join(rot_dir(), "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- config
def _toml():
    """The [rotation] table of hubtree.toml, or {} (tests replace this function)."""
    try:
        from htconf import load
        return load().raw.get("rotation", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def config():
    """{"enabled", "threshold", "handoff_tokens"} from env and hubtree.toml [rotation]. Never raises."""
    raw = _toml()
    thr = raw.get("threshold", DEFAULT_THRESHOLD)
    env = os.environ.get("HUBTREE_ROTATE_THRESHOLD")
    if env:
        try:
            thr = int(env)
        except ValueError:
            pass
    if not isinstance(thr, int) or isinstance(thr, bool) or thr <= 0:
        thr = DEFAULT_THRESHOLD
    ht = raw.get("handoff_tokens", HANDOFF_TOKENS)
    ht = ht if isinstance(ht, int) and ht > 0 else HANDOFF_TOKENS
    enabled = raw.get("enabled", True) is not False and os.environ.get("HUBTREE_ROTATE_OFF") != "1"
    return {"enabled": enabled, "threshold": thr, "handoff_tokens": min(ht, int(thr * HANDOFF_SHARE))}


# ---------------------------------------------------------------- context size
def write_context(data, folder=None):
    """Status line: save the context size of one session. data is the status line JSON. Returns the path or None."""
    sid = data.get("session_id")
    if not sid:
        return None
    cw = data.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    tokens = None
    if isinstance(cu, dict) and cu:
        tokens = sum(int(cu.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens"))
    elif cw.get("used_percentage") is not None and cw.get("context_window_size"):
        tokens = int(round(float(cw["used_percentage"]) * float(cw["context_window_size"]) / 100))
    rec = {"sid": sid, "tokens": tokens, "size": cw.get("context_window_size"), "pct": cw.get("used_percentage"),
           "total_input_tokens": cw.get("total_input_tokens"), "t": time.time(), "ts": T.now(),
           "transcript": data.get("transcript_path")}
    d = folder or ctx_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "".join(c for c in sid if c.isalnum() or c == "-") + ".json")
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    return path


def _ts_epoch(s):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _tail_records(tp, max_bytes=16 << 20):
    """Records of the transcript from the end backwards (newest first), reading at most max_bytes."""
    try:
        size = os.path.getsize(tp)
    except (OSError, TypeError):
        return
    with open(tp, "rb") as f:
        pos, rest, read = size, b"", 0
        while pos > 0 and read < max_bytes:
            n = min(1 << 20, pos)
            pos -= n
            f.seek(pos)
            chunk = f.read(n) + rest
            read += n
            lines = chunk.split(b"\n")
            rest = lines[0] if pos > 0 else b""
            for line in reversed(lines[1:] if pos > 0 else lines):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def transcript_usage(tp):
    """(tokens, model, boundary epoch). tokens = the context of the newest main-chain API call; 0 after a compact
    boundary that follows it, or when there is none."""
    for r in _tail_records(tp):
        if r.get("isSidechain"):
            continue
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            return 0, None, _ts_epoch(r.get("timestamp"))
        if r.get("type") == "user" and r.get("isCompactSummary"):
            return 0, None, _ts_epoch(r.get("timestamp"))
        m = r.get("message") or {}
        u = m.get("usage")
        if r.get("type") == "assistant" and u and m.get("model") != "<synthetic>":
            tok = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens"))
            return tok, m.get("model"), 0.0
    return 0, None, 0.0


def context_tokens(sid, tp):
    """(tokens, source, model): the larger of the status line value and the transcript value."""
    tok_t, model, boundary = transcript_usage(tp) if tp else (0, None, 0.0)
    tok_s = 0
    try:
        with open(os.path.join(ctx_dir(), sid + ".json"), encoding="utf-8") as f:
            rec = json.load(f)
        if rec.get("tokens") and float(rec.get("t") or 0) > boundary:
            tok_s = int(rec["tokens"])
    except (OSError, ValueError, TypeError):
        pass
    if tok_s > tok_t:
        return tok_s, "statusline", model
    return tok_t, ("transcript" if tok_t else "none"), model


# ---------------------------------------------------------------- the claude process
class _PE(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong), ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


CLAUDE_EXES = ("claude.exe", "claude")


def _process_table_posix():
    """{pid: (parent pid, command name)} from `ps` (macOS and Linux)."""
    out = {}
    try:
        txt = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in txt.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
    return out


def _pid_start(pid):
    """A start-time string of a running process, or None when it does not run (to tell a reused pid)."""
    if os.name == "nt":
        import hubdb
        return hubdb.pid_start(pid)
    try:
        r = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout.strip() or None


def process_table():
    """{pid: (parent pid, exe name)} of all processes (Windows Toolhelp snapshot, or `ps` elsewhere)."""
    if os.name != "nt":
        return _process_table_posix()
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PE)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.CreateToolhelp32Snapshot(0x2, 0)
    out = {}
    try:
        e = _PE()
        e.dwSize = ctypes.sizeof(_PE)
        ok = k.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = k.Process32NextW(h, ctypes.byref(e))
    finally:
        k.CloseHandle(h)
    return out


def claude_pid(start=None, table=None):
    """The nearest ancestor process named claude (claude.exe on Windows) of this process, or None."""
    table = table or process_table()
    pid = start or os.getpid()
    for _ in range(8):
        ppid, exe = table.get(pid, (None, ""))
        if pid != (start or os.getpid()) and exe.lower() in CLAUDE_EXES:
            return pid
        if not ppid or ppid == pid:
            return None
        pid = ppid
    return None


def end_process(pid, start):
    """Terminate one claude process when its start time still matches. Returns a short result word."""
    if _pid_start(pid) is None:
        return "gone"
    if start not in (None, "?") and _pid_start(pid) != start:
        return "pid reused"
    if (process_table().get(pid, (0, ""))[1] or "").lower() not in CLAUDE_EXES:
        return "not claude"
    if os.name != "nt":
        import signal
        try:
            os.kill(int(pid), signal.SIGTERM)
            return "ended"
        except PermissionError:
            return "no access"
        except OSError:
            return "terminate failed"
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x0001 | 0x1000, False, int(pid))  # PROCESS_TERMINATE | QUERY_LIMITED_INFORMATION
    if not h:
        return "no access"
    try:
        return "ended" if k.TerminateProcess(h, 0) else "terminate failed"
    finally:
        k.CloseHandle(h)


# ---------------------------------------------------------------- the handoff file
def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for x in content or []:
        if isinstance(x, dict) and x.get("type") == "text":
            out.append(x.get("text") or "")
    return "\n".join(out)


def _cut(s, n=TOOL_CUT):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f" [... {len(s) - n} more characters cut]"


def render_record(r):
    """One transcript record as handoff text, or None. User and assistant text verbatim; tool data cut."""
    if r.get("isSidechain") or r.get("isMeta"):
        return None
    t, m = r.get("type"), r.get("message") or {}
    c = m.get("content")
    ts = (r.get("timestamp") or "")[:19]
    if t == "user":
        if r.get("isCompactSummary"):
            return f"### Compact summary ({ts})\n\n{_text_of(c)}"
        if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            parts = []
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_result":
                    body = x.get("content")
                    body = _text_of(body) if isinstance(body, list) else (body or "")
                    parts.append(f"[tool result] {_cut(body)}")
            return "\n".join(parts)
        text = _text_of(c).strip()
        return f"### User ({ts})\n\n{text}" if text else None
    if t == "assistant" and isinstance(c, list):
        parts = []
        for x in c:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and (x.get("text") or "").strip():
                parts.append(x["text"])
            elif x.get("type") == "tool_use":
                parts.append(f"[tool call {x.get('name')}] {_cut(x.get('input'))}")
        return ("### Assistant (%s)\n\n" % ts + "\n\n".join(parts)) if parts else None
    return None


def last_turns(tp, budget_chars):
    """The newest transcript records as text, oldest first, at most budget_chars."""
    out, used = [], 0
    for r in _tail_records(tp, max_bytes=64 << 20):
        s = render_record(r)
        if not s:
            continue
        if used + len(s) > budget_chars:
            if not out:
                out.append(s[-budget_chars:])
            break
        out.append(s)
        used += len(s) + 2
    return "\n\n".join(reversed(out))


def last_user_prompt(tp):
    for r in _tail_records(tp):
        if r.get("type") != "user" or r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary"):
            continue
        c = (r.get("message") or {}).get("content")
        if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            continue
        text = _text_of(c).strip()
        if text:
            return text
    return ""


def last_todos(tp):
    for r in _tail_records(tp):
        for x in ((r.get("message") or {}).get("content") or []) if r.get("type") == "assistant" else []:
            if isinstance(x, dict) and x.get("type") == "tool_use" and x.get("name") in ("TodoWrite", "TaskUpdate"):
                return _cut(x.get("input"), 4000)
    return ""


def _fence(text):
    n = 3
    while "`" * n in text:
        n += 1
    return "`" * n


def write_handoff(me, old_sid, new_sid, tp, prompt, why, tokens, cfg, continue_turn=False):
    name = me.get("name") or old_sid
    path = os.path.join(handoff_dir(), f"{T.slug(name, 120)}_{time.strftime('%Y%m%dT%H%M%S')}.md")
    node = me.get("node") or "hub"
    task = me.get("task")
    task_text = ""
    if task and os.path.exists(task):
        with open(task, encoding="utf-8", errors="replace") as f:
            task_text = f.read(20000)
    parent = (T.sessions().get(me.get("parent")) or {}).get("name") if me.get("parent") else None
    f1 = _fence(prompt)
    lines = [
        f"# Handoff: `{name}`",
        "",
        f"The session `{name}` (old id {old_sid}) reached {tokens:,} context tokens ({why}). hubtree ended it and "
        f"started this session (id {new_sid}) with the same name. Written {T.now()}.",
        "",
        "## 1. The user's newest prompt (verbatim)",
        "",
        ("The old session stopped while it worked on this prompt. Check the last turns (section 5) and finish the work."
         if continue_turn else "hubtree blocked this prompt in the old session. Answer it now."),
        "",
        f1, prompt, f1,
        "",
        "## 2. Where this session sits",
        "",
        f"- Node `{node}`, cwd `{me.get('cwd') or ''}`, parent `{parent or 'none'}`, kind "
        f"{'background' if me.get('auto') else 'tab'}.",
        f"- Entry files: {', '.join('`' + x + '`' for x in T.entry_files(node))}.",
        f"- Task file: `{task}`." if task else "- Task file: none.",
        "",
        "## 3. The old session (for search)",
        "",
        f"- Old session id: `{old_sid}`. Old transcript: `{tp}` (it stays where it is; read it if you need more detail).",
        "",
        "## 4. Open items",
        "",
    ]
    todos = last_todos(tp)
    lines += [f"- Last task list of the old session: {todos}" if todos else "- The old session had no task list.",
              "- Unread messages: run `hubtree inbox`."]
    lines.append("")
    if task_text:
        f2 = _fence(task_text)
        lines += ["### The task file", "", f2, task_text, f2, ""]
    budget = cfg["handoff_tokens"] * CHARS_PER_TOKEN
    lines += [f"## 5. The last turns of the old session (verbatim, at most about {cfg['handoff_tokens']:,} tokens; "
              f"tool inputs and results cut at {TOOL_CUT} characters)", "", last_turns(tp, budget), ""]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- start the new session
def first_prompt(name, old_sid, handoff):
    return (f"Session rotation: you continue the session {name} (old id {old_sid}), which reached its context "
            f"limit. Read the whole handoff file {handoff} (in parts if it is long). "
            "Then answer the user's newest prompt: it is section 1 of the handoff file.")


def launch(kind, name, cwd, new_sid, model, prompt, dry=False):
    """Start the new session. Returns the command (list) that ran."""
    args = term.claude_args() + ["--name", name, "--session-id", new_sid] + (["--model", model] if model else [])
    folder = T.resolve_cwd(cwd)
    if kind == "background":
        cmd = ["claude", "--bg"] + args + [prompt]
        if not dry:
            subprocess.run(cmd, cwd=folder, env=term.child_env(), capture_output=True, text=True, timeout=60,
                           creationflags=term.NO_WINDOW)
        return cmd
    cmd = ["claude"] + args + [prompt]
    if not dry:
        env_extra = {k: os.environ[k] for k in ("HUBTREE_ROTATE_THRESHOLD", "HUBTREE_ROTATE_OFF") if os.environ.get(k)}
        term.open_tab(name, folder, cmd, env_extra=env_extra)
    return cmd


def start_detached(argv):
    """Start a process that outlives this hook and the old claude process."""
    return term.start_detached(argv, cwd=HERE)


# ---------------------------------------------------------------- the rotation
def _paused(sid):
    p = os.path.join(rot_dir(), sid + ".failed")
    try:
        return time.time() - os.path.getmtime(p) < PAUSE_S
    except OSError:
        return False


def storm(sid, now=None):
    """A reason when a rotation now would be part of a rotation storm, else None. A storm can happen when a mid-turn rotation told the new session to finish a turn that alone filled the window again,
    so each new session rotates again soon after its start."""
    now = now or time.time()
    reg = T.sessions()
    me = reg.get(sid) or {}
    if any(v.get("rotated_to") == sid for v in reg.values()):
        age = now - _ts_epoch(me.get("created"))
        if age < STORM_MIN_AGE_S:
            return f"this session started from a rotation {age:.0f} s ago (minimum {STORM_MIN_AGE_S} s)"
    name = me.get("name")
    n = sum(1 for e in T.read_events() if e.get("event") == "rotate" and e.get("name") == name
            and now - _ts_epoch(e.get("ts")) < 3600)
    if name and n >= STORM_MAX_PER_HOUR:
        return f"{name} rotated {n} times in the last hour (maximum {STORM_MAX_PER_HOUR})"
    return None


def rotate(p, why, prompt, continue_turn=False, tokens=0, model=None, dry=False):
    """Rotate one session. Returns the message for the old tab, or None when it does not rotate."""
    cfg = config()
    sid, tp = p.get("session_id"), p.get("transcript_path")
    me = T.sessions().get(sid) or {}
    if not sid or not me.get("name") or me.get("rotated_to"):
        log("skip", sid=sid, why="not in the registry" if not me.get("name") else "rotated before")
        return None
    lock = os.path.join(rot_dir(), sid + ".json")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return f"[hubtree rotation] This session already rotates to a new tab named {me['name']}. Use that tab."
    name, kind = me["name"], ("background" if me.get("auto") else "tab")
    new_sid = str(uuid.uuid4())
    table = process_table()
    pid = claude_pid(table=table)
    st = {"old": sid, "new": new_sid, "name": name, "kind": kind, "transcript": tp, "pid": pid,
          "pid_start": _pid_start(pid) if pid else None, "tokens": tokens, "why": why, "ts": T.now(),
          "stage": "started", "dry": dry}
    try:
        handoff = write_handoff(me, sid, new_sid, tp, prompt, why, tokens, cfg, continue_turn)
        st["handoff"] = handoff
        os.makedirs(T.PENDING, exist_ok=True)
        pend = {"name": name, "parent": me.get("parent"), "auto": bool(me.get("auto")), "fork": False,
                "tab": kind == "tab", "level": me.get("level"), "task": me.get("task"), "ts": T.now(),
                "limit_min": me.get("limit_min"), "rotated_from": sid, "new_sid": new_sid}
        with open(os.path.join(T.PENDING, T.slug(name, 200) + ".json"), "w", encoding="utf-8") as f:
            json.dump(pend, f)
        st["cmd"] = launch(kind, name, me.get("cwd") or p.get("cwd"), new_sid, model,
                           first_prompt(name, sid, handoff), dry=dry)[:12]
        st["stage"] = "launched"
    finally:
        os.write(fd, json.dumps(st, ensure_ascii=False).encode("utf-8"))
        os.close(fd)
    log("rotate", **{k: v for k, v in st.items() if k != "cmd"})
    if not dry:
        st["finisher_breakaway"] = start_detached([sys.executable, os.path.abspath(__file__), "finish", sid])
        _save_state(st)
    return (f"[hubtree rotation] The context of this session is {tokens:,} tokens (threshold {cfg['threshold']:,}). "
            f"A new tab named {name} opens now and answers this prompt from a handoff file ({handoff}). "
            "This session ends in a few seconds. If no new tab opens within 2 minutes, send the prompt here again.")


def prompt_step(p):
    """UserPromptSubmit: the block reason when this prompt starts a rotation, else None. Fails open."""
    try:
        cfg = config()
        if not cfg["enabled"]:
            return None
        sid, tp = p.get("session_id"), p.get("transcript_path")
        if not sid or _paused(sid):
            return None
        tokens, src, model = context_tokens(sid, tp)
        if tokens < cfg["threshold"]:
            return None
        st = storm(sid)
        if st:
            log("storm", sid=sid, where="prompt", why=st, tokens=tokens)
            return None
        return rotate(p, f"over the threshold at a prompt, from the {src}", p.get("prompt") or "",
                      tokens=tokens, model=model)
    except Exception as e:  # noqa: BLE001  fail open
        log("error", where="prompt_step", error=f"{type(e).__name__}: {e}"[:300])
        return None


def precompact_step(p):
    """PreCompact: a block decision (dict) for an auto compaction, else None. A manual /compact goes on.
    A compaction in the middle of a turn also rotates (the new session finishes the turn); an idle compaction
    (the last turn ended) is only blocked: the next prompt rotates if the context is over the threshold.
    A rotation storm (storm) or a rotation that cannot start lets the compaction go on."""
    try:
        if (p.get("trigger") or "auto") == "manual" or not config()["enabled"]:
            return None
        sid, tp = p.get("session_id"), p.get("transcript_path")
        reason = "[hubtree rotation] Auto compaction is off: sessions rotate instead (hubtree rotate)."
        if T.tail_state(tp)["done"] or _paused(sid or ""):
            log("precompact_block", sid=sid, rotated=False)
            return {"decision": "block", "reason": reason}
        st = storm(sid)
        if st:  # the compaction is the safety net: a rotation storm never ends on its own
            log("storm", sid=sid, where="precompact", why=st)
            return None
        tokens, src, model = context_tokens(sid, tp)
        msg = rotate(p, "an auto compaction in the middle of a turn", last_user_prompt(tp), continue_turn=True,
                     tokens=tokens, model=model)
        log("precompact_block", sid=sid, rotated=bool(msg))
        # no rotation (not in the registry, rotated before): a block in the middle of a turn would only fail it
        return {"decision": "block", "reason": msg} if msg else None
    except Exception as e:  # noqa: BLE001  fail open: the compaction goes on
        log("error", where="precompact_step", error=f"{type(e).__name__}: {e}"[:300])
        return None


# ---------------------------------------------------------------- the finisher
def _state_path(sid):
    return os.path.join(rot_dir(), sid + ".json")


def _load_state(sid):
    with open(_state_path(sid), encoding="utf-8") as f:
        return json.load(f)


def _save_state(st):
    tmp = _state_path(st["old"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, _state_path(st["old"]))


def _linked(old, new):
    return (T.sessions().get(old) or {}).get("rotated_to") == new or new in T.sessions()


def _run(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=term.child_env(),
                           creationflags=term.NO_WINDOW)
        return r.returncode, (r.stdout or "")[-300:] + (r.stderr or "")[-300:]
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def finish(old_sid, link_wait=LINK_WAIT_S, sleep=time.sleep):
    """Finish one rotation (see the module text). Safe to run again: each stage is saved in the state file."""
    st = _load_state(old_sid)
    if st.get("stage") in ("done", "failed") or st.get("dry"):
        return st
    if st.get("stage") == "launched":
        t0 = time.time()
        while not _linked(old_sid, st["new"]) and time.time() - t0 < link_wait:
            sleep(2)
        if not _linked(old_sid, st["new"]):
            st["stage"] = "failed"
            st["error"] = f"the new session did not start within {link_wait} s"
            try:
                os.remove(os.path.join(T.PENDING, T.slug(st["name"], 200) + ".json"))
            except OSError:
                pass
            open(os.path.join(rot_dir(), old_sid + ".failed"), "w").close()
            os.replace(_state_path(old_sid), os.path.join(rot_dir(), f"{old_sid}.{int(time.time())}.failedstate"))
            log("failed", old=old_sid, error=st["error"])
            return st
        sleep(3)  # the old tab shows the block message first
        if st["kind"] == "background":
            a = T.live_agents().get(old_sid) or {}
            st["end"] = _run(["claude", "stop", a.get("id") or old_sid], 60)[0] == 0 and "stopped" or "stop failed"
        elif st.get("pid"):
            st["end"] = end_process(st["pid"], st.get("pid_start"))
        else:
            st["end"] = "no pid"
        T.append({"event": "end", "session_id": old_sid, "reason": "rotate"})
        st["stage"] = "ended"
        _save_state(st)
        log("ended", old=old_sid, result=st["end"])
    st["stage"] = "done"
    _save_state(st)
    log("done", old=old_sid)
    return st


def sweep():
    """Run the finisher for every rotation that has not finished (for example after a reboot)."""
    out = []
    for f in os.listdir(rot_dir()):
        if f.endswith(".json") and f != "log.jsonl" and not f.endswith(".tmp"):
            sid = f[:-5]
            try:
                st = _load_state(sid)
            except (OSError, ValueError):
                continue
            if st.get("stage") not in ("done", "failed") and not st.get("dry"):
                out.append(finish(sid, link_wait=5).get("stage"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="hubtree session rotation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--sid")
    s.add_argument("--transcript")
    f = sub.add_parser("finish")
    f.add_argument("sid")
    sub.add_parser("sweep")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        sid = a.sid or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
        tp = a.transcript or (T.sessions().get(sid) or {}).get("transcript")
        tok, src, model = context_tokens(sid, tp)
        print(json.dumps({"sid": sid, "tokens": tok, "source": src, "model": model, **config()}))
    elif a.cmd == "finish":
        try:
            st = finish(a.sid)
        except Exception as e:  # noqa: BLE001
            log("error", where="finish", old=a.sid, error=f"{type(e).__name__}: {e}"[:300])
            return 1
        print(json.dumps({k: st.get(k) for k in ("old", "new", "stage", "end")}))
    elif a.cmd == "sweep":
        print(json.dumps(sweep()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
