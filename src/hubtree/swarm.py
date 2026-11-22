"""Swarm control for the hubtree session tree: queue, limits, claims, results, group actions, teams. No model calls.

Parts:
- Queue and dependencies: spawn.py --after <name or id> [--after ...] [--when released|done] writes a registry row
  "queued" (id q-<hex>) with its task file. `tick` starts a queued child when its dependencies are met and a slot is
  free: [swarm] max_working_per_node and max_working_total in hubtree.toml count background children that work.
  released = the dependency posted a result that is not rejected, or it has a release, done, accept or end event.
  done = a done, accept or end event.
- Claims: the paths each session wrote (a write ledger, <state>/ledger/<sid>.jsonl, one JSON line per write with
  "ts" and "path", plus its hubtree.db claims). CONFLICT: two live sessions wrote the same path within [swarm] conflict_window_min.
  The parent of each gets one hubtree.db message (a flag file per pair, path and day).
- Result inbox: a child posts a result (done, proof, commits, open, decisions) into hubtree.db table `results`. Accept
  checks that the work is committed, then closes the child (accept and end events, claude stop for a background
  child). Ask for change writes the user's words into the child's task file (owner_update.py) and wakes it.
  Reject marks the result and tells the child.
- Group actions on a sub-tree (a session and its descendants, or a folder): pause, resume, stop, hide, broadcast,
  with a preview count. Each action writes one line per session to <state>/swarm/actions.jsonl.
- Teams: teams/<name>.toml in the package; `team` starts every role as a child, wired with --after.

CLI (prints JSON):
  swarm.py tick [--dry-run]                       the daemon job "swarm" every [daemon] tick_s; also after each result
  swarm.py queue                                  queued children and what they wait for
  swarm.py start-now <name or id> | cancel <name or id>
  swarm.py result [--sid S | --name N] --done T [--proof T] [--commits a,b] [--open T] [--decisions T] [--json J]
  swarm.py results [--parent P] [--child C] [--all]
  swarm.py accept <id> [--force] | change <id> --words W | reject <id> [--why W]
  swarm.py claims [--sid S]                       claimed paths of the live sessions, and the conflicts
  swarm.py group pause|resume|stop|hide|broadcast (--root <name or id> | --node <folder>) [--words W] [--yes]
  swarm.py team <name> --parent P --goal G [--dry-run]
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import htconf  # noqa: E402
import term  # noqa: E402
import tree_lib as T  # noqa: E402

SPAWN = os.path.join(HERE, "spawn.py")
TEAMS = os.path.join(HERE, "teams")
NO_WINDOW = term.NO_WINDOW
DEFAULTS = {"max_working_per_node": 3, "max_working_total": 8, "conflict_window_min": 30, "start_grace_s": 120}
CFG_OVERRIDE = {}   # tests
CLAUDE = json.loads(os.environ["HUBTREE_SWARM_CLAUDE"]) if os.environ.get("HUBTREE_SWARM_CLAUDE") else ["claude"]
RESUME_PROMPT = ("Resume: the user resumed this session (group action in hubtree). Go on from where you "
                 "stopped. Read your task file again first: it can have a user update.")
PAUSE_MSG = ("The user paused this session and its sub-tree (group action in hubtree). Finish the current "
             "step at a safe point, then stop and wait. Do not go on until a resume message comes.")
UNPAUSE_MSG = "The user resumed this session (group action in hubtree). Go on with your task."
RSCHEMA = ("CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY, child TEXT, parent TEXT, child_name TEXT, "
           "ts REAL, body TEXT, status TEXT DEFAULT 'open', acted REAL, note TEXT)")


# ---------- small helpers ----------

def cfg():
    out = dict(DEFAULTS)
    try:
        raw = htconf.load().raw.get("swarm") or {}
        for k in DEFAULTS:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[k] = v
    except Exception:  # noqa: BLE001 - a broken config keeps the defaults
        pass
    out.update(CFG_OVERRIDE)
    return out


def sdir(*parts):
    return os.path.join(T.STATE, "swarm", *parts)


def log_action(action, s, what, ok=True, by="user", note=""):
    line = {"t": T.now(), "action": action, "sid": s.get("session_id"), "name": s.get("name"), "what": what,
            "ok": ok, "by": by, "note": note}
    try:
        os.makedirs(sdir(), exist_ok=True)
        with open(sdir("actions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _run(argv, cwd=None, timeout=90):
    env = term.child_env()
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


RUN = [_run]        # tests replace RUN[0] with a stub
LIVE = [None]       # tests replace LIVE[0] with a function that returns the live list


def live_now(fresh=False):
    if LIVE[0]:
        return LIVE[0]()
    if os.environ.get("HUBTREE_SWARM_CLAUDE"):  # tests of the daemon job: a stub claude prints the live list
        code, out = RUN[0](CLAUDE + ["agents", "--json"])
        return {a.get("sessionId"): a for a in json.loads(out or "[]")} if code == 0 else {}
    return T.live_agents(max_age=0 if fresh else None)


def me_sid():
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("HUBTREE_SESSION")


def find(ref, every=None):
    """A session by id or name. A queue row that started or was cancelled ("gone") loses to the real session."""
    every = every if every is not None else T.sessions()
    s = every.get(ref)
    if s:
        seen = set()
        while s.get("rotated_to") and s["rotated_to"] in every and s["session_id"] not in seen:
            seen.add(s["session_id"])
            s = every[s["rotated_to"]]
        return s
    hits = [v for v in every.values() if v.get("name") == ref and not v.get("deleted") and not v.get("rotated_to")]
    real = [v for v in hits if not v.get("gone")]
    return (real or hits or [None])[-1]


def _ts(iso):
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            return 0.0


def node_of_row(s):
    return s.get("node") or "hub"


# ---------- hubtree.db: results ----------

def db():
    import hubdb
    con = hubdb.connect()
    con.execute(RSCHEMA)
    return con


def _rows(con, where="", args=()):
    cur = con.execute(f"SELECT id, child, parent, child_name, ts, body, status, acted, note FROM results {where}", args)
    out = []
    for i, c, p, n, ts, b, st, acted, note in cur.fetchall():
        try:
            body = json.loads(b or "{}")
        except ValueError:
            body = {"done": b}
        out.append({"id": i, "child": c, "parent": p, "child_name": n, "ts": ts, "body": body, "status": st,
                    "acted": acted, "note": note})
    return out


def results(parent=None, child=None, status=None):
    try:
        con = db()
    except Exception:  # noqa: BLE001 - no db: no results
        return []
    try:
        where, args = [], []
        for k, v in (("parent", parent), ("child", child), ("status", status)):
            if v:
                where.append(f"{k}=?")
                args.append(v)
        return _rows(con, ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", tuple(args))
    finally:
        con.close()


def get_result(rid):
    con = db()
    try:
        r = _rows(con, "WHERE id=?", (int(rid),))
        return r[0] if r else None
    finally:
        con.close()


def set_status(rid, status, note=""):
    import hubdb
    con = db()
    try:
        with hubdb.tx(con):
            con.execute("UPDATE results SET status=?, acted=?, note=? WHERE id=?", (status, time.time(), note, int(rid)))
    finally:
        con.close()


def _norm_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()] if "," in v or ";" in v else [v]
    return [str(x) for x in v]


def post_result(child_ref, body, kick=True):
    """Store a child's result for its parent. body: {done, proof, commits, open, decisions}."""
    import hubdb
    every = T.sessions()
    s = find(child_ref, every)
    if not s:
        return {"ok": False, "error": f"no session {child_ref}"}
    b = {"done": str(body.get("done") or "").strip(), "proof": str(body.get("proof") or "").strip(),
         "commits": _norm_list(body.get("commits")), "open": _norm_list(body.get("open")),
         "decisions": _norm_list(body.get("decisions"))}
    if not b["done"]:
        return {"ok": False, "error": "a result needs --done (what was done)"}
    con = db()
    try:
        with hubdb.tx(con):
            cur = con.execute("INSERT INTO results(child, parent, child_name, ts, body) VALUES(?,?,?,?,?)",
                              (s["session_id"], s.get("parent"), s.get("name"), time.time(), json.dumps(b)))
            rid = cur.lastrowid
        if s.get("parent"):
            try:
                hubdb.send(con, s["parent"], f"result {rid} from child {s.get('name')}: {b['done'][:200]}. "
                           f"Hubtree: select the child, then A accept, C ask for change, X reject; or "
                           f"`hubtree swarm accept {rid}`.", from_sid=s["session_id"])
            except Exception:  # noqa: BLE001 - the result row is the record; the message is a pointer
                pass
    finally:
        con.close()
    log_action("result", s, f"posted result {rid}", by=s.get("name") or "child")
    if kick:
        kick_tick()
    return {"ok": True, "id": rid, "child": s.get("name"), "parent": s.get("parent")}


def kick_tick():
    """Run one tick in a detached process, so that a dependent child starts now and the poster does not wait."""
    term.start_detached([term.python(), os.path.abspath(__file__), "tick"], cwd=HERE)


# ---------- git check before accept ----------

def _git(args, cwd):
    code, out = RUN[0](["git"] + args, cwd=cwd, timeout=30)
    return code, out


_TOP = {}


def repo_of(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if not d:
        return None
    if d in _TOP:
        return _TOP[d]
    code, out = _git(["rev-parse", "--show-toplevel"], d)
    top = os.path.normcase(os.path.normpath(out.strip().splitlines()[-1])) if code == 0 and out.strip() else None
    _TOP[d] = top
    return top


def uncommitted(s, body):
    """Problems that block an accept: a named commit that no repo has, a written file with changes git does not
    hold. Files outside a git repo (scratch, temp) and ignored files do not count."""
    probs = []
    paths = sorted({p for p, _ in ledger_paths(s["session_id"]).values() if os.path.exists(p)})
    repos = {}
    for p in paths:
        top = repo_of(p)
        if top:
            repos.setdefault(top, []).append(p)
    for h in body.get("commits") or []:
        cands = list(repos) + [x for x in (repo_of(s.get("cwd") or T.HUB), repo_of(T.HUB)) if x]
        if not any(_git(["cat-file", "-e", f"{h}^{{commit}}"], r)[0] == 0 for r in dict.fromkeys(cands)):
            probs.append(f"commit {h} is in no repo")
    for top, ps in repos.items():
        for i in range(0, len(ps), 40):
            code, out = _git(["status", "--porcelain", "--"] + ps[i:i + 40], top)
            if code == 0:
                probs += [f"not committed: {ln[3:].strip()}" for ln in out.splitlines() if ln.strip()]
    return probs


# ---------- deliver a message to a session ----------

def resume(s, text, live=None):
    """Wake a background session with `text` as its prompt: stop it if it runs, then claude --bg --resume."""
    import re
    sid = s["session_id"]
    live = live if live is not None else live_now(fresh=True)
    a = live.get(sid) or {}
    if a.get("id"):
        code, out = RUN[0](CLAUDE + ["stop", a["id"]])
        if code != 0:
            return False, f"claude stop exit {code}: {out.strip()[-160:]}"
        _wait_gone(sid, a.get("pid"))
    for attempt in (1, 2):
        code, out = RUN[0](CLAUDE + ["--bg", "--resume", sid, text], cwd=T.resolve_cwd(s.get("cwd")))
        m = re.search(r"started a copy as (\w+)", out)
        if not m:
            return code == 0, f"claude --bg --resume exit {code}: {out.strip()[-200:]}"
        # Claude Code still saw the session as running (the live list can drop it before the
        # process ends): stop the copy, wait, try once more.
        RUN[0](CLAUDE + ["stop", m.group(1)])
        _wait_gone(sid, a.get("pid"))
        time.sleep(STOP_SETTLE_S)
    return False, f"claude --bg --resume started a copy twice: {out.strip()[-200:]}"


STOP_SETTLE_S = 3  # after claude stop: the session store needs a moment after the process ends


def _wait_gone(sid, pid, wait_s=30):
    """Wait until the session left the live list and its process ended (at most wait_s), then STOP_SETTLE_S more."""
    import hubdb
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if sid not in live_now(fresh=True) and not (pid and hubdb.pid_start(pid)):
            break
        time.sleep(1)
    time.sleep(STOP_SETTLE_S)


def deliver(s, text, live=None, by="hubtree"):
    """Get `text` to a session. A tab or a session that is not a background one: a hubtree.db inbox message (read at
    its next prompt). A working background session: a pending file that the tick delivers when it is idle. An idle,
    waiting or closed background session: resumed with the text as its prompt."""
    live = live if live is not None else live_now(fresh=True)
    a = live.get(s["session_id"]) or {}
    background = a.get("kind") == "background" or (not a and s.get("auto"))
    if not background:
        try:
            import hubdb
            con = hubdb.connect()
            try:
                hubdb.send(con, s["session_id"], text, from_sid=by)
            finally:
                con.close()
            return "inbox"
        except Exception as e:  # noqa: BLE001
            return f"failed: {e}"
    if a.get("state") == "working":
        os.makedirs(sdir("deliver"), exist_ok=True)
        with open(sdir("deliver", s["session_id"] + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": T.now(), "text": text, "by": by}) + "\n")
        return "later"
    ok, note = resume(s, text, live)
    return "resumed" if ok else f"failed: {note}"


def deliver_pending(every, live):
    acts = []
    d = sdir("deliver")
    if not os.path.isdir(d):
        return acts
    for fn in os.listdir(d):
        if not fn.endswith(".jsonl"):
            continue
        sid = fn[:-6]
        s = every.get(sid)
        if not s or (live.get(sid) or {}).get("state") == "working":
            continue
        p = os.path.join(d, fn)
        with open(p, encoding="utf-8") as f:
            texts = [json.loads(x)["text"] for x in f if x.strip()]
        os.remove(p)
        ok, note = resume(s, "\n\n".join(texts), live)
        acts.append({"act": "delivered" if ok else "deliver failed", "name": s.get("name"), "why": note})
    return acts


# ---------- queue, dependencies, limits ----------

def released_children():
    """{child sid: number of results that are not rejected}."""
    out = {}
    for r in results():
        if r["status"] != "rejected":
            out[r["child"]] = out.get(r["child"], 0) + 1
    return out


def dep_status(ref, every, rel):
    """(released, done, label) of one dependency."""
    s = find(ref, every)
    if not s:
        starting = os.path.exists(os.path.join(T.PENDING, T.slug(ref, 200) + ".json"))
        return False, False, f"{ref} ({'starting' if starting else 'not found'})"
    nm = s.get("name") or ref
    if s.get("queued"):
        return False, False, f"{nm} (queued)"
    if s.get("gone"):
        return False, False, f"{nm} (cancelled)"
    done = bool(s.get("done") or s.get("accepted") or not s.get("open"))
    released = done or bool(s.get("released")) or rel.get(s["session_id"], 0) > 0
    return released, done, nm


def deps_open(q, every, rel):
    """The dependency labels that are not met yet (empty: all met)."""
    when = q.get("when") or "released"
    out = []
    for ref in q.get("after") or []:
        released, done, nm = dep_status(ref, every, rel)
        if not (done if when == "done" else released):
            out.append(nm)
    return out


def queued(every=None):
    every = every if every is not None else T.sessions()
    return sorted((v for v in every.values() if v.get("queued") and not v.get("gone")),
                  key=lambda v: v.get("queued_ts") or "")


def working_counts(every, live, now=None):
    """Background sessions that work, per node and in total. A child that the queue started in the last
    start_grace_s and that is not in the live list yet counts too."""
    now = now or time.time()
    per, total = {}, 0
    live_names = set()
    for sid, a in live.items():
        live_names.add(a.get("name"))
        if a.get("kind") != "background" or a.get("state") != "working":
            continue
        s = every.get(sid) or {}
        node = s.get("node") or T.node_of(a.get("cwd"))[0]
        per[node] = per.get(node, 0) + 1
        total += 1
    # Children that start now: not in the live list yet. A queue row that started (started_at), a new registry row
    # (created), or a pending file of spawn.py (the session hook removes it at the start), younger than the grace.
    grace, counted = cfg()["start_grace_s"], set()
    starting = []
    real = {s.get("name") for s in every.values() if not s.get("queued") and not s.get("gone")}
    for s in every.values():
        if s.get("started_at") and s.get("name") in real:
            continue  # its real session is in the registry: that row counts (or not) by itself
        t = _ts(s.get("started_at")) or (_ts(s.get("created")) if s.get("open") and not s.get("queued") else 0)
        if s.get("auto") and t and now - t < grace:
            starting.append((s.get("name"), node_of_row(s), s["session_id"]))
    try:
        import glob as _glob
        for p in _glob.glob(os.path.join(T.PENDING, "*.json")):
            if now - os.path.getmtime(p) < grace:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("auto") and d.get("name"):
                    node = (every.get(d.get("parent")) or {}).get("node") or "hub"
                    starting.append((d["name"], d.get("node") or node, None))
    except (OSError, ValueError):
        pass
    for name, node, sid in starting:
        if not name or name in counted or name in live_names or (sid and sid in live):
            continue
        counted.add(name)
        per[node] = per.get(node, 0) + 1
        total += 1
    return per, total


def wait_text(q, every=None, rel=None):
    every = every if every is not None else T.sessions()
    rel = rel if rel is not None else released_children()
    if q.get("paused"):
        return "held: paused"
    left = deps_open(q, every, rel)
    if left:
        return f"waits for {', '.join(left)} ({q.get('when') or 'released'})"
    return "waits for a free slot" if q.get("auto") else "starts at the next tick"


def start(qid):
    """Start one queued child through spawn.py --from-queue. Returns the parsed spawn JSON."""
    code, out = RUN[0]([term.python(), SPAWN, "--from-queue", qid], cwd=HERE, timeout=120)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": f"spawn exit {code}: {out.strip()[-200:]}"}


def tick(dry=False, now=None):
    """Start the queued children whose dependencies are met, in queue order, within the limits; deliver pending
    messages; send the conflict messages. Returns a list of actions."""
    if os.path.exists(os.path.join(T.STATE, "KILL")):
        return [{"why": "KILL file set"}]
    import hubdb
    os.makedirs(sdir(), exist_ok=True)
    try:
        with hubdb.os_mutex(sdir("tick.lock"), wait=60):
            return _tick(dry, now)
    except hubdb.Busy:
        return [{"why": "another tick runs"}]


def _tick(dry, now):
    now = now or time.time()
    every, live, rel, c = T.sessions(), live_now(fresh=True), released_children(), cfg()
    per, total = working_counts(every, live, now)
    acts = []
    for q in queued(every):
        if q.get("paused"):
            continue
        left = deps_open(q, every, rel)
        if left:
            continue
        node = node_of_row(q)
        if q.get("auto"):
            if per.get(node, 0) >= c["max_working_per_node"]:
                acts.append({"act": "wait", "name": q.get("name"), "why": f"node {node} has {per.get(node, 0)} working"})
                continue
            if total >= c["max_working_total"]:
                acts.append({"act": "wait", "name": q.get("name"), "why": f"{total} working in total"})
                continue
        if dry:
            acts.append({"act": "would start", "name": q.get("name")})
            continue
        r = start(q["session_id"])
        acts.append({"act": "started" if r.get("ok") else "start failed", "name": q.get("name"),
                     "why": r.get("error") or r.get("mode")})
        if r.get("ok") and q.get("auto"):
            per[node] = per.get(node, 0) + 1
            total += 1
        log_action("start", q, "started from the queue" if r.get("ok") else "start failed", r.get("ok", False),
                   by="tick", note=str(r.get("error") or ""))
    if not dry:
        acts += deliver_pending(every, live)
        acts += alert_conflicts(every, live, now)
    return acts


def start_now(ref):
    q = find(ref)
    if not q or not q.get("queued") or q.get("gone"):
        return {"ok": False, "error": f"{ref} is not a queued child"}
    r = start(q["session_id"])
    log_action("start-now", q, "started now by the user", r.get("ok", False), note=str(r.get("error") or ""))
    return r


def cancel(ref, by="user"):
    q = find(ref)
    if not q or not q.get("queued") or q.get("gone"):
        return {"ok": False, "error": f"{ref} is not a queued child"}
    T.append({"event": "cancel_queue", "session_id": q["session_id"], "by": by})
    log_action("cancel", q, "cancelled in the queue", by=by)
    return {"ok": True, "name": q.get("name")}


# ---------- claims and conflicts ----------

_LEDGER = {}


def ledger_rows(sid):
    """[(epoch, path)] from the file ledger of one session (cached by size and time)."""
    safe = "".join(ch for ch in str(sid) if ch.isalnum() or ch in "-_")[:80]
    p = os.path.join(T.STATE, "ledger", safe + ".jsonl")
    try:
        st = os.stat(p)
    except OSError:
        return []
    hit = _LEDGER.get(p)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    rows = []
    with open(p, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("path"):
                rows.append((_ts(r.get("ts")), r["path"]))
    _LEDGER[p] = ((st.st_mtime, st.st_size), rows)
    return rows


def ledger_paths(sid, since=0.0):
    """{normalized path: (path, newest write epoch)} for the writes at or after `since`."""
    out = {}
    for t, p in ledger_rows(sid):
        if t >= since:
            k = os.path.normcase(os.path.normpath(p))
            if k not in out or out[k][1] < t:
                out[k] = (p, t)
    return out


def db_claims(sid):
    try:
        import hubdb
        con = hubdb.connect()
        try:
            return [c for (c,) in con.execute("SELECT path FROM claims WHERE holder=? AND expires>=?", (sid, time.time()))]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []


def conflicts(sids, now=None):
    """{sid: [(path, [other sids])]}: paths that two or more of these sessions wrote within the window."""
    now = now or time.time()
    since = now - cfg()["conflict_window_min"] * 60
    by_path = {}
    for sid in sids:
        for k, (p, _t) in ledger_paths(sid, since).items():
            by_path.setdefault(k, (p, set()))[1].add(sid)
    out = {}
    for k, (p, who) in by_path.items():
        if len(who) < 2:
            continue
        for sid in who:
            out.setdefault(sid, []).append((p, sorted(who - {sid})))
    return out


def alert_conflicts(every, live, now=None):
    """One hubtree.db message to each parent per conflict (pair of sessions, path, day). Returns the actions."""
    acts = []
    cf = conflicts([sid for sid in live if sid], now)
    done = set()
    for sid, lst in cf.items():
        for p, others in lst:
            pair = tuple(sorted([sid] + others))
            key = hashlib.sha1(f"{pair}|{os.path.normcase(p)}|{time.strftime('%Y-%m-%d')}".encode()).hexdigest()[:20]
            if key in done:
                continue
            done.add(key)
            os.makedirs(sdir("conflicts"), exist_ok=True)
            try:
                fd = os.open(sdir("conflicts", key + ".flag"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except OSError:
                continue  # sent before
            names = [(every.get(x) or {}).get("name") or (live.get(x) or {}).get("name") or x[:8] for x in pair]
            parents = {(every.get(x) or {}).get("parent") for x in pair} - {None}
            msg = (f"CONFLICT: sessions {' and '.join(names)} both wrote {p} within "
                   f"{cfg()['conflict_window_min']:.0f} min. Decide which one owns the file and tell the other one "
                   "to stop editing it.")
            sent = 0
            try:
                import hubdb
                con = hubdb.connect()
                try:
                    for par in parents:
                        hubdb.send(con, par, msg, from_sid="swarm")
                        sent += 1
                finally:
                    con.close()
            except Exception:  # noqa: BLE001
                pass
            acts.append({"act": "conflict", "path": p, "names": names, "parents_told": sent})
            with open(sdir("conflicts.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": T.now(), "path": p, "sids": list(pair), "names": names}) + "\n")
    return acts


# ---------- the row fields for the views ----------

def row_extras(every, live, sids=None, alert=False):
    """{sid: {"results": open results of its children, "result": status of its newest result, "conflict": text,
    "paused": bool, "wait": text}} for tree_view, hubtree and the pane. Never raises."""
    out = {}
    try:
        res = results()
        rel = {}
        for r in res:
            if r["status"] != "rejected":
                rel[r["child"]] = rel.get(r["child"], 0) + 1
            if r["status"] == "open" and r["parent"]:
                out.setdefault(r["parent"], {})["results"] = out.get(r["parent"], {}).get("results", 0) + 1
            out.setdefault(r["child"], {})["result"] = r["status"]
            out[r["child"]]["result_id"] = r["id"]
        cf = conflicts([sid for sid in live if sid])
        for sid, lst in cf.items():
            p, others = lst[0]
            nm = ", ".join(((every.get(o) or {}).get("name") or o[:8]).split("/")[-1] for o in others)
            out.setdefault(sid, {})["conflict"] = f"{os.path.basename(p)} (with {nm})" + \
                (f" +{len(lst) - 1} more" if len(lst) > 1 else "")
        for sid, s in every.items():
            if s.get("paused"):
                out.setdefault(sid, {})["paused"] = True
            if s.get("queued") and not s.get("gone"):
                out.setdefault(sid, {})["wait"] = wait_text(s, every, rel)
        if alert and cf:
            alert_conflicts(every, live)
    except Exception:  # noqa: BLE001 - the views never fail on swarm data
        pass
    return {k: v for k, v in out.items() if sids is None or k in sids}


def detail_lines(sid, every=None, live=None):
    """Lines for the hubtree detail view: queue wait, pause, claims, conflicts, results."""
    every = every if every is not None else T.sessions()
    live = live if live is not None else live_now()
    s = every.get(sid) or {}
    out = []
    try:
        if s.get("queued") and not s.get("gone"):
            out.append(f"QUEUED: {wait_text(s, every)}. g starts it now, x cancels it.")
        if s.get("paused"):
            out.append(f"PAUSED by {s.get('paused_by') or 'the user'}{': ' + s['paused_why'] if s.get('paused_why') else ''}")
        win = cfg()["conflict_window_min"]
        mine = ledger_paths(sid, time.time() - win * 60)
        cl = sorted(mine.values(), key=lambda x: -x[1])
        dbc = db_claims(sid)
        if cl or dbc:
            out.append(f"claims (writes in the last {win:.0f} min: {len(cl)}; hubtree.db claims: {len(dbc)}):")
            out += [f"  {p}" for p, _t in cl[:8]] + ([f"  ... {len(cl) - 8} more"] if len(cl) > 8 else [])
            out += [f"  {p} (hubtree.db claim)" for p in dbc[:4]]
        if sid in live:
            for p, others in conflicts([x for x in live if x]).get(sid, []):
                nm = ", ".join((every.get(o) or {}).get("name") or o[:8] for o in others)
                out.append(f"CONFLICT: {p} also written by {nm}")
        kids = [r for r in results(parent=sid) if r["status"] == "open"]
        if kids:
            out.append(f"results from children ({len(kids)} open; select the child, then A, C or X):")
            out += [f"  #{r['id']} {(r['child_name'] or '').split('/')[-1]}: {r['body'].get('done', '')[:90]}" for r in kids]
        mine_r = results(child=sid)
        if mine_r:
            r = mine_r[-1]
            b = r["body"]
            out.append(f"RESULT #{r['id']} ({r['status']}): {b.get('done', '')}")
            for k in ("proof", "commits", "open", "decisions"):
                if b.get(k):
                    v = b[k] if isinstance(b[k], str) else "; ".join(b[k])
                    out.append(f"  {k}: {v[:300]}")
            if r["status"] == "open":
                out.append("  A accept (closes the child) | C ask for change | X reject")
    except Exception as e:  # noqa: BLE001
        out.append(f"(swarm data not read: {type(e).__name__})")
    return out


# ---------- accept, change, reject ----------

def accept(rid, force=False, by="user", live=None):
    r = get_result(rid)
    if not r:
        return {"ok": False, "error": f"no result {rid}"}
    if r["status"] in ("accepted", "rejected"):
        return {"ok": False, "error": f"result {rid} is {r['status']} already"}
    every = T.sessions()
    s = every.get(r["child"]) or {"session_id": r["child"], "name": r["child_name"]}
    if not force:
        probs = uncommitted(s, r["body"])
        if probs:
            return {"ok": False, "error": "not accepted: the work is not committed: " + "; ".join(probs[:5]),
                    "problems": probs}
    set_status(rid, "accepted", f"by {by}")
    T.append({"event": "accept", "session_id": s["session_id"], "by": by, "result": int(rid)})
    live = live if live is not None else live_now(fresh=True)
    a = live.get(s["session_id"]) or {}
    how = "end event"
    if a.get("kind") == "background" and a.get("id"):
        code, out = RUN[0](CLAUDE + ["stop", a["id"]])
        how = "stopped" if code == 0 else f"claude stop exit {code}"
    elif a.get("kind") == "interactive":
        how = "end event; it is open in a tab: close that tab"
    T.append({"event": "end", "session_id": s["session_id"], "reason": f"result {rid} accepted"})
    _packet_accept(s)
    log_action("accept", s, f"accepted result {rid}: {how}", by=by)
    return {"ok": True, "id": int(rid), "child": s.get("name"), "closed": how}


def _packet_accept(s):
    """Mark the task packet accepted (task_packet.py), quietly."""
    try:
        import contextlib
        import io
        import task_packet
        p = task_packet.packet_path(s["task"]) if s.get("task") else None
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            d["accepted"], d["accepted_ts"] = True, T.now()
            with contextlib.redirect_stdout(io.StringIO()):
                task_packet.write(s["task"], d)
    except Exception:  # noqa: BLE001 - the registry event is the record
        pass


def change(rid, words, by="the user in hubtree", live=None):
    import owner_update
    r = get_result(rid)
    if not r:
        return {"ok": False, "error": f"no result {rid}"}
    if not words.strip():
        return {"ok": False, "error": "ask for change needs the user's words"}
    s, tf = owner_update.task_file_of(r["child"])
    if not s or not tf or not os.path.exists(tf):
        return {"ok": False, "error": f"no task file for {r['child_name']}"}
    owner_update.add_update(tf, words, by)
    set_status(rid, "change", words[:500])
    msg = (f"User update: your task file {tf} has a new line in the section '{owner_update.HEAD[3:]}' (the user asks for "
           f"a change to your result {rid}). Read the task file again, do the change, then post a new result.")
    how = deliver(s, msg, live, by="swarm")
    log_action("change", s, f"asked for a change to result {rid}: {how}", by=by, note=words[:200])
    return {"ok": True, "id": int(rid), "child": s.get("name"), "delivered": how}


def reject(rid, why="", by="user"):
    r = get_result(rid)
    if not r:
        return {"ok": False, "error": f"no result {rid}"}
    set_status(rid, "rejected", why[:500])
    try:
        import hubdb
        con = hubdb.connect()
        try:
            hubdb.send(con, r["child"], f"The user rejected your result {rid}{': ' + why if why else ''}. Do not go "
                       "on with it; wait for a user update in your task file.", from_sid="swarm")
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass
    log_action("reject", {"session_id": r["child"], "name": r["child_name"]}, f"rejected result {rid}", by=by, note=why)
    return {"ok": True, "id": int(rid), "child": r["child_name"]}


def newest_open(child_sid):
    rs = [r for r in results(child=child_sid) if r["status"] == "open"]
    return rs[-1] if rs else None


# ---------- pause and resume (shared with swarm_obs.py for the budget pause) ----------

def pause(sid, by="user", why="", live=None):
    """Pause one session: claude stop for a live background session, autopilot off, a "paused" event; a tab gets
    a hubtree.db message. Returns what was done."""
    import autopilot
    every = T.sessions()
    s = every.get(sid) or {"session_id": sid}
    live = live if live is not None else live_now(fresh=True)
    a = live.get(sid) or {}
    was_off = autopilot.is_off(sid)
    autopilot.set_off(sid, True)
    what = "held in the queue" if s.get("queued") else "paused"
    if a.get("kind") == "background" and a.get("id"):
        code, out = RUN[0](CLAUDE + ["stop", a["id"]])
        what = "stopped and paused" if code == 0 else f"paused; claude stop exit {code}"
    elif a.get("kind") == "interactive":
        try:
            import hubdb
            con = hubdb.connect()
            try:
                hubdb.send(con, sid, PAUSE_MSG + (f" Reason: {why}" if why else ""), from_sid="swarm")
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass
        what = "paused (tab: message, no auto-continue)"
    T.append({"event": "paused", "session_id": sid, "by": by, "why": why, "ap_was_off": was_off})
    log_action("pause", s, what, by=by, note=why)
    return what


def unpause(sid, by="user", live=None, wake=True):
    import autopilot
    every = T.sessions()
    s = every.get(sid) or {"session_id": sid}
    if not s.get("ap_was_off"):
        autopilot.set_off(sid, False)
    T.append({"event": "unpaused", "session_id": sid, "by": by})
    live = live if live is not None else live_now(fresh=True)
    a = live.get(sid) or {}
    what = "unpaused"
    if s.get("queued") and not s.get("gone"):
        what = "released in the queue"
    elif a.get("kind") == "interactive" or (not a and not s.get("auto")):
        try:
            import hubdb
            con = hubdb.connect()
            try:
                hubdb.send(con, sid, UNPAUSE_MSG, from_sid="swarm")
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass
        what = "resumed (message)"
    elif wake and not s.get("accepted") and not s.get("deleted"):
        # The pause stopped it, so its SessionEnd hook wrote an end event: wake it whether open or not.
        ok, note = resume(s, RESUME_PROMPT, live)
        what = "resumed in the background" if ok else f"resume failed: {note}"
    log_action("resume", s, what, by=by)
    return what


# ---------- group actions on a sub-tree ----------

ACTIONS = ("pause", "resume", "stop", "hide", "broadcast")


def targets(root=None, node=None, every=None):
    """A session and all its descendants, or every session at a folder node and below."""
    every = every if every is not None else T.sessions()
    vis = [v for v in every.values() if v.get("name") and not v.get("deleted") and not v.get("rotated_to")
           and not v.get("gone") and not v.get("hidden")]
    if node:
        node = node.strip("/")
        return [v for v in vis if node == "hub" or node_of_row(v) == node or node_of_row(v).startswith(node + "/")]
    r = find(root, every) if root else None
    if not r:
        return []
    kids = {}
    for v in vis:
        kids.setdefault(v.get("parent"), []).append(v)
    out, stack, seen = [], [r], set()
    while stack:
        s = stack.pop(0)
        if s["session_id"] in seen:
            continue
        seen.add(s["session_id"])
        out.append(s)
        stack += kids.get(s["session_id"], [])
    return out


def plan(action, tg, live, me=None):
    """([(session, what)], [(session, why skipped)]) for one group action."""
    do, skip = [], []
    for s in tg:
        sid, a = s["session_id"], live.get(s["session_id"]) or {}
        q = s.get("queued") and not s.get("gone")
        if me and sid == me:
            skip.append((s, "this session"))
        elif action == "pause":
            if s.get("paused"):
                skip.append((s, "paused already"))
            elif q:
                do.append((s, "hold in the queue"))
            elif a.get("kind") == "background":
                do.append((s, "stop and pause"))
            elif a.get("kind") == "interactive":
                do.append((s, "message, no auto-continue"))
            else:
                skip.append((s, "not running"))
        elif action == "resume":
            if not s.get("paused"):
                skip.append((s, "not paused"))
            else:
                do.append((s, "release in the queue" if q else ("message" if a.get("kind") == "interactive"
                                                                  else "resume in the background")))
        elif action == "stop":
            if q:
                do.append((s, "cancel in the queue"))
            elif a.get("kind") == "background":
                do.append((s, "stop"))
            elif a.get("kind") == "interactive":
                skip.append((s, "open in a tab: close it by hand"))
            else:
                skip.append((s, "not running"))
        elif action == "hide":
            do.append((s, "hide" + (" and stop" if a.get("kind") == "background" else "")))
        elif action == "broadcast":
            if q or a or s.get("open"):
                do.append((s, "user update"))
            else:
                skip.append((s, "closed"))
    return do, skip


def preview(action, tg, live, me=None):
    do, skip = plan(action, tg, live, me)
    text = f"{action} {len(do)} session{'s' if len(do) != 1 else ''}?"
    if skip:
        text += f" ({len(skip)} skipped)"
    return {"text": text, "n": len(do), "do": [(s.get("name"), w) for s, w in do],
            "skip": [(s.get("name"), w) for s, w in skip]}


def run_group(action, tg, words="", by="user", me=None, live=None):
    """Do one group action. Returns {"done": [(name, result)], "skip": [...]}."""
    import owner_update
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action {action}; known: {', '.join(ACTIONS)}"}
    if action == "broadcast" and not words.strip():
        return {"ok": False, "error": "broadcast needs the user's words"}
    live = live if live is not None else live_now(fresh=True)
    do, skip = plan(action, tg, live, me)
    out = []
    for s, what in do:
        sid, a = s["session_id"], live.get(s["session_id"]) or {}
        try:
            if action == "pause":
                res = pause(sid, by=by, why="group pause", live=live)
            elif action == "resume":
                res = unpause(sid, by=by, live=live)
            elif action == "stop":
                if s.get("queued") and not s.get("gone"):
                    res = cancel(sid, by=by)["ok"] and "cancelled"
                else:
                    code, o = RUN[0](CLAUDE + ["stop", a["id"]])
                    T.append({"event": "end", "session_id": sid, "reason": "group stop in hubtree"})
                    res = "stopped" if code == 0 else f"claude stop exit {code}"
            elif action == "hide":
                if a.get("kind") == "background" and a.get("id"):
                    RUN[0](CLAUDE + ["stop", a["id"]])
                    T.append({"event": "end", "session_id": sid, "reason": "hidden by a group action"})
                T.append({"event": "hide", "session_id": sid})
                res = "hidden"
            else:
                _s, tf = owner_update.task_file_of(sid)
                if not tf or not os.path.exists(tf):
                    res = "no task file"
                else:
                    owner_update.add_update(tf, words, by)
                    msg = (f"User update: your task file {tf} has a new line in the section '{owner_update.HEAD[3:]}'. "
                           "Read the task file again and follow the newest update.")
                    res = "queued update" if s.get("queued") else deliver(s, msg, live, by="swarm")
        except Exception as e:  # noqa: BLE001 - one bad row does not stop the group
            res = f"failed: {type(e).__name__}: {e}"
        if action not in ("pause", "resume"):  # pause and unpause log their own line
            log_action(f"group {action}", s, str(res), by=by, note=words[:200])
        out.append((s.get("name"), res))
    return {"ok": True, "done": out, "skip": [(s.get("name"), w) for s, w in skip]}


# ---------- teams ----------

def load_team(name):
    import tomllib
    p = os.path.join(TEAMS, name + ".toml")
    if not os.path.exists(p):
        have = sorted(f[:-5] for f in os.listdir(TEAMS) if f.endswith(".toml")) if os.path.isdir(TEAMS) else []
        raise ValueError(f"no team {name}; teams: {', '.join(have) or 'none'}")
    with open(p, "rb") as f:
        t = tomllib.load(f)
    roles = t.get("role") or []
    names = [r["name"] for r in roles]
    for r in roles:
        for d in r.get("after") or []:
            if d not in names or names.index(d) >= names.index(r["name"]):
                raise ValueError(f"team {name}: role {r['name']} waits for {d}, which is not an earlier role")
    return t


def spawn_has(flag):
    try:
        with open(SPAWN, encoding="utf-8") as f:
            return f'"{flag}"' in f.read()
    except OSError:
        return False


def start_team(team, parent, goal, dry=False):
    """Start every role of a team as a child of `parent`, in file order; a role with `after` waits as queued."""
    t = load_team(team)
    d = t.get("defaults") or {}
    tag = f"{team}-{time.strftime('%m%d-%H%M%S')}"
    team_dir = sdir("teams", tag)
    if not dry:
        os.makedirs(team_dir, exist_ok=True)
    names, out = {}, []
    for r in t["role"]:
        n = int(r.get("count", 1))
        for i in range(1, n + 1):
            child = f"{team}-{r['name']}" + (f"-{i}" if n > 1 else "")
            deps = [nm for dr in (r.get("after") or []) for nm in names.get(dr, [])]
            text = (r.get("task") or d.get("task") or "{goal}").format(
                goal=goal, team=team, role=r["name"], i=i, n=n, team_dir=team_dir, deps=", ".join(deps) or "none")
            text = f"Team `{team}` ({tag}), role `{r['name']}`" + (f" {i} of {n}" if n > 1 else "") + \
                f". Team goal: {goal}\nShared team folder: {team_dir}\n\n{text}"
            argv = [term.python(), SPAWN, "--parent", parent, "--name", child, "--task", text]
            model = r.get("model") or d.get("model")
            if model:
                argv += ["--model", model]
            if r.get("auto", d.get("auto", True)):
                argv.append("--auto")
            for dep in deps:
                argv += ["--after", dep]
            if deps and (r.get("when") or d.get("when")):
                argv += ["--when", r.get("when") or d.get("when")]
            budget = r.get("budget_usd", d.get("budget_usd"))
            if budget and spawn_has("--budget-usd"):  # swarm_obs.py pauses a child at its budget
                argv += ["--budget-usd", str(budget)]
            if dry:
                out.append({"role": r["name"], "name": child, "after": deps, "argv": argv[2:]})
                names.setdefault(r["name"], []).append(child)
                continue
            code, o = RUN[0](argv, cwd=HERE, timeout=120)
            try:
                res = json.loads(o.strip().splitlines()[-1])
            except (ValueError, IndexError):
                res = {"ok": False, "error": f"spawn exit {code}: {o.strip()[-200:]}"}
            if not res.get("ok"):
                return {"ok": False, "error": f"role {r['name']}: {res.get('error')}", "started": out}
            names.setdefault(r["name"], []).append(res["name"])
            out.append({"role": r["name"], "name": res["name"], "queued": bool(res.get("queued")), "after": deps})
    return {"ok": True, "team": team, "tag": tag, "team_dir": team_dir, "members": out}


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description="swarm control for the hubtree session tree")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    sub.add_parser("queue")
    for c in ("start-now", "cancel"):
        sub.add_parser(c).add_argument("ref")
    r = sub.add_parser("result")
    r.add_argument("--sid")
    r.add_argument("--name")
    for k in ("done", "proof", "commits", "open", "decisions", "json"):
        r.add_argument(f"--{k}")
    rs = sub.add_parser("results")
    rs.add_argument("--parent")
    rs.add_argument("--child")
    rs.add_argument("--all", action="store_true")
    ac = sub.add_parser("accept")
    ac.add_argument("id")
    ac.add_argument("--force", action="store_true")
    ch = sub.add_parser("change")
    ch.add_argument("id")
    ch.add_argument("--words", required=True)
    rj = sub.add_parser("reject")
    rj.add_argument("id")
    rj.add_argument("--why", default="")
    cl = sub.add_parser("claims")
    cl.add_argument("--sid")
    g = sub.add_parser("group")
    g.add_argument("action", choices=ACTIONS)
    g.add_argument("--root")
    g.add_argument("--node")
    g.add_argument("--words", default="")
    g.add_argument("--yes", action="store_true", help="do it; without --yes only the preview prints")
    tm = sub.add_parser("team")
    tm.add_argument("name")
    tm.add_argument("--parent", required=True)
    tm.add_argument("--goal", required=True)
    tm.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "tick":
        out = {"ok": True, "actions": tick(a.dry_run)}
    elif a.cmd == "queue":
        every = T.sessions()
        out = {"ok": True, "queued": [{"id": q["session_id"], "name": q.get("name"), "wait": wait_text(q, every)}
                                      for q in queued(every)]}
    elif a.cmd == "start-now":
        out = start_now(a.ref)
    elif a.cmd == "cancel":
        out = cancel(a.ref)
    elif a.cmd == "result":
        body = json.loads(a.json) if a.json else {}
        for k in ("done", "proof", "commits", "open", "decisions"):
            if getattr(a, k):
                body[k] = getattr(a, k)
        out = post_result(a.sid or a.name or me_sid() or "", body)
    elif a.cmd == "results":
        par = find(a.parent)["session_id"] if a.parent and find(a.parent) else a.parent
        chi = find(a.child)["session_id"] if a.child and find(a.child) else a.child
        out = {"ok": True, "results": [x for x in results(parent=par, child=chi) if a.all or x["status"] == "open"]}
    elif a.cmd == "accept":
        out = accept(a.id, a.force)
    elif a.cmd == "change":
        out = change(a.id, a.words)
    elif a.cmd == "reject":
        out = reject(a.id, a.why)
    elif a.cmd == "claims":
        live = live_now()
        sids = [a.sid] if a.sid else [x for x in live if x]
        win = cfg()["conflict_window_min"] * 60
        out = {"ok": True, "claims": {s: [p for p, _t in ledger_paths(s, time.time() - win).values()] for s in sids},
               "conflicts": conflicts([x for x in live if x])}
    elif a.cmd == "group":
        if not (a.root or a.node):
            ap.error("group needs --root or --node")
        tg, live = targets(a.root, a.node), live_now(fresh=True)
        pv = preview(a.action, tg, live, me_sid())
        out = {"ok": True, "preview": pv}
        if a.yes:
            out = run_group(a.action, tg, a.words, me=me_sid(), live=live)
            out["preview"] = pv["text"]
    else:
        par = find(a.parent)
        if not par:
            out = {"ok": False, "error": f"no session {a.parent}"}
        else:
            try:
                out = start_team(a.name, par["session_id"], a.goal, a.dry_run)
            except ValueError as e:
                out = {"ok": False, "error": str(e)}
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
