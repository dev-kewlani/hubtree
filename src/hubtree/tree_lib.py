"""Shared logic of the session tree: nodes, names, and the append-only registry.

Registry: <state>/sessions.jsonl. One JSON event per line; nothing is ever removed.
  {"event": "start"|"resume"|"end"|"rename"|"rotate", "session_id", "name", "parent", "node", "level",
   "cwd", "auto", "fork", "tab", "ts", "transcript", ...}
Pending spawns: <state>/pending/<name>.json, written by spawn.py before it launches a session,
read by the SessionStart hook to link the new session to its parent (env vars do not pass through every terminal).
The root folder and the state folder come from hubtree.toml (htconf.py).
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import htconf  # noqa: E402

_CFG = htconf.load()
HUB = _CFG.root  # the tree root; a broken config gives the defaults, so the session hooks never stop
STATE = _CFG.state
LEVELS = _CFG.levels
REGISTRY = os.path.join(STATE, "sessions.jsonl")
PENDING = os.path.join(STATE, "pending")
AUTO_LIMIT = 5  # Claude-started sessions running at once, per node


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].strip("-") or "task"


def node_of(cwd):
    """Return (node, level, node_dir) for a working folder. Levels: root 0, then one per folder below the root,
    up to [hubtree] levels. A folder outside the root is the node "outside"."""
    c = os.path.normpath(cwd or HUB)
    low = os.path.normcase(c)
    hub = os.path.normcase(HUB)
    if low == hub or low.startswith(hub.rstrip(os.sep) + os.sep):
        parts = [p for p in os.path.relpath(c, HUB).split(os.sep) if p not in (".", "")]
        if parts and parts[0].startswith((".", "_")):
            parts = []
        parts = parts[:LEVELS]
        if parts and parts[-1] == "archive":
            parts = parts[:-1]
        node = "/".join(parts) or "hub"
        return node, (0 if node == "hub" else len(parts)), os.path.join(HUB, *parts)
    return "outside", 1, c


PROJECTS = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")


def project_key(path):
    """The folder name Claude Code uses under ~/.claude/projects for a working folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(path))


def resolve_cwd(cwd):
    """Return a folder that exists for a session's old working folder: itself, its nearest existing parent, or the root."""
    c = os.path.normpath(cwd or HUB)
    while c and not os.path.isdir(c):
        up = os.path.dirname(c)
        if up == c:
            return HUB
        c = up
    return c or HUB


def prepare_resume(session_id, cwd, transcript=None):
    """Make `claude --resume <id>` work from a folder that exists. Returns the folder to start in.

    If the old folder is gone, copy the transcript (and its side folder) into the project folder of the new place.
    The original transcript stays where it is.
    """
    import shutil
    target = resolve_cwd(cwd)
    if os.path.exists(os.path.join(PROJECTS, project_key(target), session_id + ".jsonl")):
        return target  # Claude Code finds the transcript in this folder's project already
    src = transcript
    if not src or not os.path.exists(src):
        hits = [p for p in (os.path.join(PROJECTS, d, session_id + ".jsonl") for d in (os.listdir(PROJECTS) if os.path.isdir(PROJECTS) else [])) if os.path.exists(p)]
        src = max(hits, key=os.path.getsize) if hits else None
    if src and os.path.exists(src):
        dst_dir = os.path.join(PROJECTS, project_key(target))
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(src))
        if not os.path.exists(dst) or os.path.getsize(dst) < os.path.getsize(src):
            shutil.copy2(src, dst)
        side = src[:-6]
        if os.path.isdir(side) and not os.path.exists(dst[:-6]):
            shutil.copytree(side, dst[:-6])
    return target


def entry_files(node):
    """Files a session at this node reads first: its own entry file, then its parent's, then the root's."""
    out = []
    parts = [] if node in ("hub", "outside", None) else node.split("/")
    for i in range(len(parts), -1, -1):
        d = os.path.join(HUB, *parts[:i])
        name = parts[i - 1] if i else ""
        for cand in ([f"{name}.md"] if name else []) + ["CLAUDE.md", "README.md", "INDEX.md"]:
            p = os.path.join(d, cand)
            if os.path.exists(p):
                out.append(p)
                break
    return out


def read_events():
    if not os.path.exists(REGISTRY):
        return []
    out = []
    with open(REGISTRY, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def append(event):
    os.makedirs(STATE, exist_ok=True)
    event.setdefault("ts", now())
    with open(REGISTRY, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def sessions():
    """Fold the event log into {session_id: state}."""
    s = {}
    for e in read_events():
        sid = e.get("session_id")
        if not sid:
            continue
        cur = s.setdefault(sid, {"session_id": sid, "open": False, "created": e.get("ts")})
        if e["event"] in ("start", "resume"):
            for k in ("name", "parent", "node", "level", "cwd", "auto", "fork", "tab", "transcript", "task", "limit_min",
                      "budget_usd", "budget_tokens"):  # budget: swarm_obs.py
                if e.get(k) is not None and (e["event"] == "start" or k not in cur):
                    cur[k] = e[k]
            cur["open"] = True
            cur["last"] = e.get("ts")
            if e["event"] == "start" and e.get("name") and e.get("name") == e.get("node"):
                cur["root"] = True  # the first session named after its folder stays that folder's root, renamed or not
        elif e["event"] == "end":
            cur["open"] = False
            cur["last"] = e.get("ts")
        elif e["event"] == "rename":
            cur["name"] = e.get("name", cur.get("name"))
        elif e["event"] == "move":  # the topic says where a session belongs
            cur["node"] = e.get("node", cur.get("node"))
            cur["moved"] = True
            if e.get("cwd"):  # the folder that the session resumes in (its transcript is copied there on open)
                cur["cwd"] = e["cwd"]
        elif e["event"] in ("root", "unroot"):
            cur["root"] = e["event"] == "root"
        elif e["event"] in ("hide", "unhide"):
            cur["hidden"] = e["event"] == "hide"
        elif e["event"] == "delete":
            cur["deleted"] = True
            cur["open"] = False
        elif e["event"] == "done":  # the task finished
            cur["done"] = True
            cur["done_ts"] = e.get("ts")
        elif e["event"] == "archive":  # stale and hidden in /where; the transcript stays
            cur["archived"] = True
            cur["hidden"] = True
        elif e["event"] == "accept":  # the parent or the user accepted the result
            cur["accepted"] = True
            cur["accepted_ts"] = e.get("ts")
        elif e["event"] == "budget":  # swarm_obs.py: the user or the parent set a new budget
            for k in ("budget_usd", "budget_tokens"):
                if k in e:
                    cur[k] = e[k]
        elif e["event"] == "queued":  # swarm.py: a child that waits for its dependencies or a free slot
            for k in ("name", "parent", "node", "level", "cwd", "auto", "fork", "tab", "task", "limit_min",
                      "after", "when", "opts", "budget_usd", "budget_tokens"):
                if e.get(k) is not None:
                    cur[k] = e[k]
            cur["queued"], cur["queued_ts"] = True, e.get("ts")
        elif e["event"] in ("dequeued", "cancel_queue"):  # swarm.py: the queue row started or was cancelled
            cur["queued"], cur["gone"], cur["hidden"] = False, True, True
            cur["started_at" if e["event"] == "dequeued" else "cancelled_at"] = e.get("ts")
        elif e["event"] in ("paused", "unpaused"):  # swarm.py pause (a group action, or swarm_obs.py at the budget)
            cur["paused"] = e["event"] == "paused"
            if cur["paused"]:
                cur["paused_by"], cur["paused_why"], cur["ap_was_off"] = e.get("by"), e.get("why"), e.get("ap_was_off")
        elif e["event"] == "release":  # swarm.py: the session says its dependents may start
            cur["released"] = True
        elif e["event"] == "rotate":  # rotate.py: a new session with the same name took over 
            cur["rotated_to"] = e.get("to")
            cur["hidden"] = True
            cur["open"] = False
    # The children of a rotated session belong to its newest successor.
    for v in s.values():
        p, seen = v.get("parent"), set()
        while p in s and s[p].get("rotated_to") and p not in seen:
            seen.add(p)
            p = s[p]["rotated_to"]
        if p != v.get("parent"):
            v["parent"] = p
    return s


def by_name(name):
    """The session with this name. A name can repeat after a rotation: the newest one that is not rotated wins."""
    hits = [v for v in sessions().values() if v.get("name") == name]
    live = [v for v in hits if not v.get("rotated_to") and not v.get("deleted") and not v.get("gone")]
    return (live or hits or [None])[-1]


def unique_name(base, taken):
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


LIVE_OK = [True]  # False when the last `claude agents` call failed: then no session counts as died


LIVE_CACHE_S = 4  # one `claude agents` call serves every pane, hubtree and job for 4 s


def live_agents(max_age=None):
    """Live sessions from `claude agents --json`: {session_id: {"kind", "state", "status", "pid", ...}}.
    Interactive rows carry `status` (busy or idle); background rows carry `state` (working or blocked).

    A good answer is shared through <state>/live_agents.json for LIVE_CACHE_S seconds, so many open tabs start one
    claude process, not one each. A failed call is never cached. max_age=0 forces a fresh call."""
    import subprocess
    import time
    cache = os.path.join(STATE, "live_agents.json")
    age = LIVE_CACHE_S if max_age is None else max_age
    if age > 0:
        try:
            if time.time() - os.path.getmtime(cache) < age:
                with open(cache, encoding="utf-8") as f:
                    out = json.load(f)
                LIVE_OK[0] = True
                return out
        except (OSError, ValueError):
            pass
    try:
        r = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW: no flash
        out = {a.get("sessionId"): a for a in json.loads(r.stdout or "[]")}
        LIVE_OK[0] = r.returncode == 0
        if LIVE_OK[0]:
            try:
                tmp = f"{cache}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(out, f)
                os.replace(tmp, cache)
            except OSError:
                pass
        return out
    except Exception:
        LIVE_OK[0] = False
        return {}


# ---------- health: stuck, waits for the user, died ----------
def _tree_cfg(key, default):
    """A positive number from the [tree] table of hubtree.toml, else the default (the hooks never fail on config)."""
    try:
        v = _CFG.raw.get("tree", {}).get(key, default)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default
    except Exception:
        return default


STUCK_MIN = _tree_cfg("stuck_min", 20)    # minutes with no transcript write while busy; spawn.py --limit N per session
DIED_DAYS = _tree_cfg("died_days", 7)     # an older stop in the middle of a turn shows as closed, not died
ALERT_KEEP_DAYS = _tree_cfg("alert_keep_days", 30)  # flag files in <state>/alerts older than this are removed
ASK_TOOLS = ("AskUserQuestion", "ExitPlanMode")  # an open call of these waits for the user
WAITING = ("you", "stuck", "died")
_TAIL_CACHE = {}     # transcript path -> (mtime, size, result)


def heartbeat(s):
    """Newest write time (epoch s) of the session transcript and of its sub-agent transcripts; 0 if none."""
    tp = s.get("transcript")
    if not tp:
        return 0.0
    best = 0.0
    for p in [tp] + glob.glob(os.path.join(tp[:-6], "subagents", "*.jsonl")):
        try:
            best = max(best, os.path.getmtime(p))
        except OSError:
            pass
    return best


def tail_state(tp, size=300_000):
    """How the transcript ends: {"done": the last turn ended, "tool": name of an open tool call or None}."""
    try:
        st = os.stat(tp)
    except (OSError, TypeError):
        return {"done": True, "tool": None}
    hit = _TAIL_CACHE.get(tp)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        with open(tp, "rb") as f:
            f.seek(max(0, st.st_size - size))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()[1:]
    except OSError:
        return {"done": True, "tool": None}
    done, open_calls = True, {}
    for line in lines:
        if '"assistant"' not in line and '"user"' not in line and '"turn_duration"' not in line:
            continue  # a fast skip of the many other record types
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t, m = r.get("type"), r.get("message") or {}
        c = m.get("content")
        if t == "system" and r.get("subtype") == "turn_duration":
            done, open_calls = True, {}
        elif t == "assistant" and isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_use":
                    open_calls[x.get("id")] = x.get("name")
            done = m.get("stop_reason") == "end_turn" and not open_calls
        elif t == "user" and not r.get("isMeta") and not r.get("isCompactSummary"):
            # a compact summary is not a new turn: an idle compaction leaves the turn done
            if isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "tool_result":
                        open_calls.pop(x.get("tool_use_id"), None)
            done = False
    res = {"done": done, "tool": list(open_calls.values())[-1] if open_calls else None}
    _TAIL_CACHE[tp] = (st.st_mtime, st.st_size, res)
    return res


def health(s, live, now_s=None):
    """(state, info) for one session record. States: tab, work, you, idle, stuck, died, closed, old.

    Signals: the live list from `claude agents` (busy or idle), the transcript write time (heartbeat),
    and the open tool call at the end of the transcript. info = {"age_min", "tool", "limit_min"} for
    stuck, you and died; {} otherwise."""
    import time
    now_s = now_s or time.time()
    limit = float(s.get("limit_min") or STUCK_MIN)
    a = live.get(s["session_id"])
    if s.get("queued"):  # swarm.py: a child that waits for its dependencies or a free slot (it has no process)
        return "queued", {"after": s.get("after") or [], "when": s.get("when") or "released", "held": s.get("paused")}
    if s.get("paused") and not (a and (a.get("status") == "busy" or a.get("state") == "working")):
        return "paused", {"by": s.get("paused_by"), "pwhy": s.get("paused_why")}  # swarm.py pause; a busy one shows busy
    if a:
        if a.get("status") == "waiting":  # the live list says what it waits for, e.g. "permission prompt"
            return "you", {"wait": a.get("waitingFor") or "an answer"}
        if a.get("kind") == "interactive":
            base, busy = "tab", a.get("status") == "busy"
        else:
            base = {"working": "work", "blocked": "you"}.get(a.get("state"), "idle")
            busy = a.get("state") == "working"
        if not busy:
            return base, {}
        hb = heartbeat(s)
        age = (now_s - hb) / 60 if hb else 0.0
        if age < 0.5:  # it wrote in the last 30 s: it works, no file read
            return base, {}
        t = tail_state(s.get("transcript"))
        if t["done"]:
            # The last turn ended: "busy" with no write is an idle compaction or a stale status, not a hang
            # (false alarms seen 2026-10-29). A new prompt writes a record at once, so a hung new turn is not done.
            return base, {}
        tool = t["tool"]
        info = {"age_min": round(age, 1), "tool": tool, "limit_min": limit}
        if tool in ASK_TOOLS:
            return "you", info
        if age > limit:
            return "stuck", info
        return base, {}
    if s.get("old"):
        return "old", {}
    if s.get("open") and LIVE_OK[0] and not s.get("deleted") and not _just_started(s, now_s):
        hb = heartbeat(s)
        if hb and now_s - hb < DIED_DAYS * 86400:
            t = tail_state(s.get("transcript"))
            # A rotated session (rotate.py ends it in the middle of a turn) is not died, also when the registry
            # read came before its rotate event (hubtree reads the registry, then the slow live list).
            if not t["done"] and not os.path.exists(os.path.join(STATE, "rotation", s["session_id"] + ".json")):
                return "died", {"age_min": round((now_s - hb) / 60, 1), "tool": t["tool"], "limit_min": limit}
    return "closed", {}


START_GRACE_S = 120  # a session whose start or resume event is newer than this is never "died"


def _just_started(s, now_s):
    """True when the newest start or resume event is less than START_GRACE_S old. The shared live list is cached for
    LIVE_CACHE_S, so a new session can be in the registry before it is in the live list."""
    try:
        t = datetime.fromisoformat((s.get("last") or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return False
    return now_s - t < START_GRACE_S


def why(state, info):
    """One short reason for a waiting row, for example 'Bash, 34 min with no write (limit 20)'."""
    if state == "you" and not info:
        return "waits for you"
    if state == "queued":
        if info.get("held"):
            return "held in the queue (paused)"
        after = [str(x).split("/")[-1] for x in info.get("after") or []]
        return f"waits for {', '.join(after)} ({info.get('when')})" if after else "waits for a free slot"
    if state == "paused":
        return f"paused by {info.get('by') or 'the user'}" + (f": {info['pwhy']}" if info.get("pwhy") else "")
    if not info:
        return ""
    if state == "you" and info.get("wait"):
        return f"waits for you: {info['wait']}"
    tool = info.get("tool")
    age = info.get("age_min", 0)
    if state == "stuck":
        return f"{tool + ', ' if tool else ''}{age:.0f} min with no write (limit {info.get('limit_min', STUCK_MIN):.0f})"
    if state == "died":
        return f"stopped in the middle of a turn{' in ' + tool if tool else ''}, {age:.0f} min ago"
    if state == "you":
        return f"waits for an answer ({tool})" if tool else "waits for you"
    return ""


ALERTS = os.path.join(STATE, "alerts")


def sweep_alerts(now_s=None):
    """Remove alert flag files older than ALERT_KEEP_DAYS. Returns the count removed. Never raises."""
    import time
    cut = (now_s or time.time()) - ALERT_KEEP_DAYS * 86400
    n = 0
    try:
        for f in glob.glob(os.path.join(ALERTS, "*.flag")):
            try:
                if os.path.getmtime(f) < cut:
                    os.remove(f)
                    n += 1
            except OSError:
                pass
    except Exception:
        pass
    return n


def tell_parent(s, state, info):
    """One inbox message to the parent per stuck or died episode (a flag file makes it once only).
    The parent reads it at its next prompt (the hubtree prompt hook). Fails open; returns True if sent."""
    par = s.get("parent")
    if state not in ("stuck", "died") or not par:
        return False
    import time
    hb = int(heartbeat(s))
    os.makedirs(ALERTS, exist_ok=True)
    flag = os.path.join(ALERTS, f"{s['session_id']}_{state}_{hb}.flag")
    try:
        fd = os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        return False  # sent before for this episode
    sweep_alerts()
    try:
        import hubdb
        con = hubdb.connect()
        try:
            hubdb.send(con, par, f"child {s.get('name') or s['session_id'][:8]} is {state} at "
                                 f"{time.strftime('%Y-%m-%d %H:%M')}: {why(state, info)}. "
                                 "Check /where first: it can be over now. The tree does not stop it. Check its last lines (hubtree), then send it a "
                                 "message or stop its known process.", from_sid="hubtree")
        finally:
            con.close()
        return True
    except Exception:
        try:
            os.remove(flag)  # not sent: try again at the next refresh
        except OSError:
            pass
        return False


def running_auto(node):
    """Auto sessions at a node that work or wait for the user (idle ones do not count)."""
    live = live_agents()
    n = 0
    for sid, v in sessions().items():
        a = live.get(sid)
        if v.get("auto") and v.get("node") == node and a and a.get("state") in ("working", "blocked"):
            n += 1
    return n


def node_dir(node):
    """The folder of a node: hub -> the root, work/app -> <root>/work/app."""
    return HUB if node in ("hub", "", None) else os.path.join(HUB, *node.split("/"))


def move(session_id, node):
    """Move a session (its children follow it) to another node, and resume it in that node's folder from now on."""
    append({"event": "move", "session_id": session_id, "node": node, "cwd": node_dir(node)})


def make_root(session_id):
    """Make a session the root of its node; the old root of that node becomes a normal session."""
    s = sessions()
    me = s.get(session_id) or {}
    node = me.get("node")
    for sid, v in s.items():
        if sid != session_id and v.get("root") and v.get("node") == node and not v.get("deleted"):
            append({"event": "unroot", "session_id": sid})
            if v.get("name") == node:
                append({"event": "rename", "session_id": sid, "name": f"{node}/first-root-status"})
    append({"event": "unhide", "session_id": session_id})
    append({"event": "root", "session_id": session_id})
    append({"event": "rename", "session_id": session_id, "name": node})
