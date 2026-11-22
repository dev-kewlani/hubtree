"""hubtree.db: the coordination database of all tree sessions (messages, claims, leases, heartbeats).

SQLite in WAL mode (readers never wait for the writer). No daemon: each call opens the db, runs one short
transaction and closes it. Tables: sessions (heartbeats), leases (time-limited locks), claims (paths that a
session works on), inbox (messages between sessions), events (audit log of every lease and claim call).

Lease rules:
  - A lease has a holder, the holder's pid and the pid's start time, and an expiry. It is dead when it expired,
    when the pid no longer runs, or when the pid runs with another start time (a reused PID).
  - Lock order: sync < area < repo/file/data/state/index < git < gpu, then the resource name. A holder may only
    take a lease that sorts after every lease it holds; anything else raises LockOrderError (no deadlock).
  - Every wait has a timeout. A crash leaves a row that the next caller removes as dead.
"""
import ctypes
import json
import os
import random
import sqlite3
import sys
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def db_path():
    """HUBTREE_DB (tests use a scratch db), else <state>/hubtree.db (htconf.py)."""
    if os.environ.get("HUBTREE_DB"):
        return os.environ["HUBTREE_DB"]
    import htconf
    return os.path.join(htconf.load().state, "hubtree.db")


RANKS = {"sync": 0, "area": 1, "repo": 2, "file": 2, "data": 2, "state": 2, "index": 2, "git": 3, "gpu": 4}
TTL = {"sync": 1800, "area": 600, "repo": 7200, "file": 7200, "data": 7200, "state": 60, "index": 1800,
       "git": 60, "gpu": 3600}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, name TEXT, node TEXT, cwd TEXT, pid INT, pid_start TEXT,
  kind TEXT, state TEXT, last_beat REAL, started REAL, ended REAL);
CREATE TABLE IF NOT EXISTS leases(res TEXT PRIMARY KEY, holder TEXT, pid INT, pid_start TEXT, mode TEXT,
  acquired REAL, expires REAL, note TEXT);
CREATE TABLE IF NOT EXISTS claims(path TEXT, holder TEXT, pid INT, pid_start TEXT, task TEXT, acquired REAL,
  expires REAL, PRIMARY KEY(path, holder));
CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, to_sid TEXT, from_sid TEXT, ts REAL, body TEXT, read_at REAL);
CREATE TABLE IF NOT EXISTS events(ts REAL, sid TEXT, op TEXT, res TEXT, ok INT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class Busy(Exception):
    pass


class LockOrderError(Exception):
    pass


# ---------- process identity ----------

if os.name == "nt":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4


def _pid_start_posix(pid):
    """POSIX: the start time from `ps -o lstart=`; None if the pid does not run, "?" if it runs but ps fails."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return None
    try:
        import subprocess
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        s = r.stdout.strip()
        return s or "?"
    except Exception:
        return "?"


def pid_start(pid):
    """Start time of a running process as a string, or None if it does not run (or cannot be read)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name != "nt":
        return _pid_start_posix(pid)
    h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "?" if ctypes.get_last_error() == 5 else None  # access denied: it runs, start time unknown
    try:
        code = ctypes.c_ulong()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        c, e, k, u = (ctypes.c_ulonglong() for _ in range(4))
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return "?"
        return str(c.value)
    finally:
        _k32.CloseHandle(h)


def alive(pid, start):
    s = pid_start(pid)
    return s is not None and (start in (None, "?") or s in ("?", start))


def me():
    """(holder id, pid that lives as long as the holder). A Claude session is the holder of its claims; a lease
    belongs to one process of that holder (see try_acquire). Without a session pid, the parent (the shell) counts."""
    sid = os.environ.get("HUBTREE_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    pid = os.environ.get("HUBTREE_PID") or os.environ.get("CLAUDE_PID") or os.getppid()
    return (sid or f"pid:{os.getpid()}"), int(pid)


# ---------- database ----------

def connect(path=None):
    """Open hubtree.db. The schema script needs a short write lock; under many parallel connects on a busy PC it can
    pass the busy timeout ("database is locked", seen 2026-11-19 in test_lost_update_under_lease), so retry with
    backoff before giving up."""
    path = path or db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in range(8):
        con = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.executescript(SCHEMA)
            return con
        except sqlite3.OperationalError as e:
            con.close()
            if "locked" not in str(e).lower() and "busy" not in str(e).lower() or attempt == 7:
                raise
            time.sleep(min(2.0, 0.05 * 2 ** attempt) + random.random() * 0.05)


@contextmanager
def tx(con):
    """One write transaction. BEGIN IMMEDIATE takes the writer lock at once, so check-then-insert is atomic."""
    for i in range(6):
        try:
            con.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == 5:
                raise
            time.sleep(0.05 * 2 ** i + random.random() * 0.05)
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # keep the first error
        raise


def log(con, sid, op, res, ok, detail=""):
    con.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (time.time(), sid, op, res, int(ok), detail))


def rank(res):
    kind = res.split(":", 1)[0]
    if kind not in RANKS:
        raise ValueError(f"unknown resource kind in {res!r}; known: {', '.join(RANKS)}")
    return RANKS[kind], res


def _dead(row, now):
    expires, pid, start = row
    return expires < now or not alive(pid, start)


# ---------- leases ----------
# A lease belongs to one process (holder name + os.getpid()), so two tools or sub-agents of one session exclude
# each other too. The same process may take a lease again (depth count); only the last release deletes the row.

_depth = {}


class LeaseLost(Exception):
    pass


def try_acquire(con, res, ttl=None, note="", holder=None, pid=None):
    """Take lease `res` or raise Busy. Returns the expiry time."""
    order = rank(res)
    holder, pid = holder or me()[0], int(pid or os.getpid())
    start = pid_start(pid)
    if start is None:
        raise ValueError(f"holder pid {pid} does not run")
    ttl = ttl or TTL[res.split(":", 1)[0]]
    now = time.time()
    with tx(con):
        row = con.execute("SELECT holder, expires, pid, pid_start, note FROM leases WHERE res=?", (res,)).fetchone()
        if row and row[0] == holder and row[2] == pid and not _dead(row[1:4], now):  # re-entrant
            con.execute("UPDATE leases SET expires=? WHERE res=?", (now + ttl, res))
            _depth[(res, holder, pid)] = _depth.get((res, holder, pid), 1) + 1
            return now + ttl
        held = [r for (r,) in con.execute(
            "SELECT res FROM leases WHERE holder=? AND pid=? AND res<>? AND expires>=?", (holder, pid, res, now))]
        top = max((rank(r) for r in held), default=None)
        if top is not None and order <= top:
            log(con, holder, "acquire", res, 0, f"lock order: holds {top[1]}")
            raise LockOrderError(f"{holder} holds {top[1]}; {res} must be taken first (lock order: "
                                 f"sync < area < repo < git < gpu)")
        if row and not _dead(row[1:4], now):
            log(con, holder, "acquire", res, 0, f"busy: {row[0]}")
            raise Busy(f"{res} is held by {row[0]} (pid {row[2]}) until "
                       f"{time.strftime('%H:%M:%S', time.localtime(row[1]))}")
        if row:
            log(con, holder, "reap", res, 1, f"dead holder {row[0]}")
        con.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?,?)",
                    (res, holder, pid, start, "x", now, now + ttl, note))
        log(con, holder, "acquire", res, 1, "" if note.startswith("{") else note[:200])  # never log a token
    _depth[(res, holder, pid)] = 1
    return now + ttl


def acquire(con, res, wait=30.0, **kw):
    """Take lease `res`, waiting with jittered back-off (0.5, 1, 2, 4 s) up to `wait` seconds."""
    t0, delay = time.time(), 0.5
    while True:
        try:
            return try_acquire(con, res, **kw)
        except Busy:
            left = wait - (time.time() - t0)
            if left <= 0:
                raise
            time.sleep(min(left, delay * (0.5 + random.random())))
            delay = min(delay * 2, 4.0)


def renew(con, res, ttl=None, holder=None, pid=None):
    """Extend a lease that this process holds. Raises LeaseLost if it expired or another process took it."""
    holder, pid = holder or me()[0], int(pid or os.getpid())
    ttl = ttl or TTL[res.split(":", 1)[0]]
    now = time.time()
    with tx(con):
        n = con.execute("UPDATE leases SET expires=? WHERE res=? AND holder=? AND pid=? AND expires>=?",
                        (now + ttl, res, holder, pid, now)).rowcount
    if not n:
        raise LeaseLost(f"{res} expired or was taken by another process")
    return now + ttl


def release(con, res, holder=None, pid=None):
    holder, pid = holder or me()[0], int(pid or os.getpid())
    key = (res, holder, pid)
    if _depth.get(key, 1) > 1:
        _depth[key] -= 1
        return True
    _depth.pop(key, None)
    with tx(con):
        n = con.execute("DELETE FROM leases WHERE res=? AND holder=? AND pid=?", (res, holder, pid)).rowcount
        log(con, holder, "release", res, n > 0)
    return n > 0


class Keeper:
    """Renews a lease every ttl/3 s in a thread with its own connection. `lost` is set if a renew fails."""

    def __init__(self, res, ttl=None, holder=None):
        import threading
        self.res, self.ttl, self.holder = res, ttl or TTL[res.split(":", 1)[0]], holder or me()[0]
        self.pid = os.getpid()
        self.stop, self.lost = threading.Event(), threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        con = connect()
        try:
            while not self.stop.wait(self.ttl / 3):
                try:
                    renew(con, self.res, self.ttl, self.holder, self.pid)
                except LeaseLost:
                    self.lost.set()
                    return
                except sqlite3.Error:
                    pass  # try again at the next tick; 2/3 of the TTL is still left
        finally:
            con.close()

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.t.join(timeout=5)


@contextmanager
def lease(res, wait=30.0, con=None, **kw):
    """with lease("area:work") as con: ...   Renewed in the background while the block runs."""
    own = con is None
    con = con or connect()
    try:
        acquire(con, res, wait=wait, **kw)
        try:
            with Keeper(res, kw.get("ttl"), kw.get("holder")) as k:
                yield con
            if k.lost.is_set():
                raise LeaseLost(f"{res} was lost while the block ran")
        finally:
            release(con, res, kw.get("holder"))
    finally:
        if own:
            con.close()


def reap(con):
    """Remove dead leases and claims (expired, pid gone, or pid reused). Returns the number removed."""
    now, n = time.time(), 0
    with tx(con):
        for table, key in (("leases", "res"), ("claims", "path")):
            for k, holder, exp, pid, start in con.execute(
                    f"SELECT {key}, holder, expires, pid, pid_start FROM {table}").fetchall():
                if _dead((exp, pid, start), now):
                    con.execute(f"DELETE FROM {table} WHERE {key}=? AND holder=?", (k, holder))
                    log(con, holder, "reap", f"{table}:{k}", 1)
                    n += 1
    return n


# ---------- claims (paths a session works on) ----------

def norm(path):
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def _overlap(a, b):
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def claimers(con, path, now=None):
    """Live claims [(path, holder, task)] that overlap `path`."""
    now, p = now or time.time(), norm(path)
    return [(c, h, t) for c, h, t, exp, pid, st in
            con.execute("SELECT path, holder, task, expires, pid, pid_start FROM claims").fetchall()
            if _overlap(p, c) and not _dead((exp, pid, st), now)]


def claim(con, path, task="", ttl=7200, holder=None, pid=None):
    h, pp = me()
    holder, pid = holder or h, pid or pp
    p, now = norm(path), time.time()
    with tx(con):
        others = [(c, hh, t) for c, hh, t in claimers(con, p, now) if hh != holder]
        if others:
            log(con, holder, "claim", p, 0, f"busy: {others[0][1]}")
            raise Busy(f"{path} overlaps {others[0][0]}, claimed by {others[0][1]}"
                       + (f" ({others[0][2]})" if others[0][2] else ""))
        con.execute("INSERT OR REPLACE INTO claims VALUES(?,?,?,?,?,?,?)",
                    (p, holder, pid, pid_start(pid), task, now, now + ttl))
        log(con, holder, "claim", p, 1, task)


def unclaim(con, path, holder=None):
    holder = holder or me()[0]
    with tx(con):
        n = con.execute("DELETE FROM claims WHERE path=? AND holder=?", (norm(path), holder)).rowcount
        log(con, holder, "unclaim", norm(path), n > 0)
    return n > 0


# ---------- sessions, heartbeat, messages ----------

def beat(con, state="working", name=None, node=None, cwd=None, kind="interactive", sid=None, pid=None):
    """Heartbeat: upsert the session row and extend the leases and claims of a live holder."""
    h, p = me()
    sid, pid = sid or h, pid or p
    now, start = time.time(), pid_start(pid)
    with tx(con):
        con.execute("""INSERT INTO sessions(sid,name,node,cwd,pid,pid_start,kind,state,last_beat,started)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sid) DO UPDATE SET
                       name=COALESCE(excluded.name,name), node=COALESCE(excluded.node,node),
                       cwd=COALESCE(excluded.cwd,cwd), pid=excluded.pid, pid_start=excluded.pid_start,
                       state=excluded.state, last_beat=excluded.last_beat, ended=NULL""",
                    (sid, name, node, cwd, pid, start, kind, state, now, now))
        if state == "ended":
            con.execute("UPDATE sessions SET ended=? WHERE sid=?", (now, sid))
            con.execute("DELETE FROM leases WHERE holder=?", (sid,))
            con.execute("DELETE FROM claims WHERE holder=?", (sid,))
        elif start:
            con.execute("UPDATE claims SET expires=MAX(expires, ?) WHERE holder=? AND expires>=?",
                        (now + 7200, sid, now))


def who(con, stale=900):
    """Live sessions with their leases and claims. A session with no beat for `stale` s and a dead pid is out."""
    now, out = time.time(), []
    for sid, name, node, state, last, pid, st in con.execute(
            "SELECT sid,name,node,state,last_beat,pid,pid_start FROM sessions WHERE ended IS NULL ORDER BY last_beat DESC"):
        if now - last > stale and not alive(pid, st):
            continue
        holds = [r for (r,) in con.execute("SELECT res FROM leases WHERE holder=? AND expires>=?", (sid, now))]
        holds += [c for (c,) in con.execute("SELECT path FROM claims WHERE holder=? AND expires>=?", (sid, now))]
        out.append({"sid": sid, "name": name, "node": node, "state": state, "age_s": int(now - last), "holds": holds})
    return out


def send(con, to_sid, body, from_sid=None):
    with tx(con):
        con.execute("INSERT INTO inbox(to_sid,from_sid,ts,body) VALUES(?,?,?,?)",
                    (to_sid, from_sid or me()[0], time.time(), body))


def inbox(con, sid=None, mark=True):
    sid = sid or me()[0]
    with tx(con):
        rows = con.execute("SELECT id, from_sid, ts, body FROM inbox WHERE to_sid=? AND read_at IS NULL ORDER BY id",
                           (sid,)).fetchall()
        if mark and rows:
            con.execute(f"UPDATE inbox SET read_at=? WHERE id IN ({','.join('?' * len(rows))})",
                        (time.time(), *[r[0] for r in rows]))
    return [{"id": i, "from": f, "ts": t, "body": b} for i, f, t, b in rows]


# ---------- OS mutex for sub-second critical sections (released by the OS when the process dies) ----------

def _lock(f):
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f):
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def os_mutex(path, wait=30.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+b")
    t0 = time.time()
    while True:
        try:
            _lock(f)
            break
        except OSError:
            if time.time() - t0 > wait:
                f.close()
                raise Busy(f"{path} is locked by another process")
            time.sleep(0.05 + random.random() * 0.05)
    try:
        yield
    finally:
        _unlock(f)
        f.close()


if __name__ == "__main__":
    c = connect()
    print(json.dumps(who(c), indent=1))
