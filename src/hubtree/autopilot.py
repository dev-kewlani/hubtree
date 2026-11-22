"""Auto-continue after a usage limit. Standard library only.

A usage limit ends a turn with a synthetic assistant record: "You've hit your 5-hour limit · resets 3:30am (...)",
with apiError "usage_limit_reached" and the exact reset epoch in apiErrorParams.rate_limit_info.resetsAt.
Then the session idles until a new prompt. This helper finds that state and decides when to continue.

  python autopilot.py check  --sid SID [--transcript P] [--now EPOCH]       when to continue (JSON, exit 0)
  python autopilot.py fire   --sid SID --key KEY [--by mod|daemon] [--now E]  the last gate before a continue
                             (exit 0: send the prompt; exit 3: refused, the reason is in the JSON)
  python autopilot.py off|on|status --sid SID                               the per-session off switch
  python autopilot.py scan   [--now EPOCH] [--dry-run]                     daemon: continue background sessions

Who continues:
- A session with the hub-tree mod (every tab, and background sessions too) runs `check` at each main turn end and
  at session start, and `fire` at the due time; then the mod submits PROMPT as the user's words.
- The hubtree daemon runs `scan` on each tick for background sessions in the registry. A background session whose process
  still runs gets GRACE_S after the due time for its own mod. Then the daemon stops it (`claude stop <id>`) and wakes it
  with `claude --bg --resume <session id> PROMPT` (no other flags: with flags Claude Code starts a copy).

Safety (every gate is in `fire`, so the mod and the daemon share it):
- only when the transcript ends with a limit record (no user prompt after it, no interrupt, and the prompt
  before it is not a stop word);
- once per reset time per session: an exclusive flag file <state>/autocontinue/<sid>__<reset minute>.flag;
- never when <state>/KILL exists; never when hubtree.toml [autopilot] continue_after_limit = false; never when the
  session is off (`/autopilot off` writes <state>/autocontinue/off/<sid>).
Each continue writes one line in <state>/autocontinue/log.jsonl.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tree_lib as T  # noqa: E402
import term  # noqa: E402

PROMPT = ("Continue: the usage limit has reset. Go on from where you stopped; if a workflow or sub-agents failed at "
          "the limit, resume them.")
DELAY_S = 120          # continue at reset plus 2 minutes
GRACE_S = 600          # the daemon waits this long after the due time for the mod inside a live background session
STOP_WAIT_S = 30       # the daemon waits this long for a stopped background session to leave the live list
TAIL_BYTES = 400_000
STOP_WORDS = re.compile(r"^\W*(stop|halt|pause|wait|cancel|abort|don'?t continue|do not continue)\b", re.I)
INTERRUPT = "[Request interrupted by user"


def state_dir():
    return os.environ.get("HUBTREE_STATE") or T.STATE


def ac_dir(*parts):
    return os.path.join(state_dir(), "autocontinue", *parts)


# ---------- settings and switches ----------

def enabled():
    """hubtree.toml [autopilot] continue_after_limit (default true). The env var HUBTREE_AUTOPILOT_OFF=1 turns it off."""
    if os.environ.get("HUBTREE_AUTOPILOT_OFF") == "1":
        return False
    try:
        import htconf
        return htconf.load().raw.get("autopilot", {}).get("continue_after_limit", True) is not False
    except Exception:  # noqa: BLE001 - a broken config keeps the default
        return True


def kill_set():
    return os.path.exists(os.path.join(state_dir(), "KILL"))


def is_off(sid):
    return os.path.exists(ac_dir("off", sid))


def set_off(sid, off):
    p = ac_dir("off", sid)
    if off:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(T.now() + "\n")
    elif os.path.exists(p):
        os.remove(p)


# ---------- the transcript ----------

def transcript_of(sid):
    s = T.sessions().get(sid) or {}
    tp = s.get("transcript")
    if tp and os.path.exists(tp):
        return tp
    hits = glob.glob(os.path.join(T.PROJECTS, "*", sid + ".jsonl"))
    return max(hits, key=os.path.getsize) if hits else None


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _limit_info(r, now):
    """(reset epoch, kind, text) of a limit record, or None."""
    m = r.get("message") or {}
    text = _text(m.get("content")).strip()
    api = r.get("apiError") == "usage_limit_reached" or r.get("error") == "rate_limit"
    if not api and not (m.get("model") == "<synthetic>" and re.search(r"hit your .{0,30}limit", text, re.I)):
        return None
    rl = ((r.get("apiErrorParams") or {}).get("rate_limit_info") or {})
    q = r.get("quotaLimits") or {}
    reset = rl.get("resetsAt") or q.get("resetsAt")
    kind = rl.get("rateLimitType") or q.get("rateLimitType") or ""
    if not reset:
        reset = parse_reset(text, _ts(r) or now)
        if not reset:
            return None
    return float(reset), kind, text


_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_RESET = re.compile(r"resets\s+(?:[A-Za-z]{3,9}\.?\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*)?"
                    r"(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)(?:\s*\((?P<tz>[^)]+)\))?", re.I)


def parse_reset(text, ref):
    """The reset epoch from a limit message such as "resets 3:30am (America/New_York)" or "resets Nov 20, 3:30am".
    A clock time with no date is the next one after `ref` (the record time). A tz name in brackets is used when it
    is known, else local time. None when the text has no reset time."""
    m = _RESET.search(text or "")
    if not m:
        return None
    h, minute = int(m.group("h")) % 12, int(m.group("m") or 0)
    if m.group("ap").lower() == "pm":
        h += 12
    tz = None
    if m.group("tz"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group("tz").strip())
        except Exception:  # noqa: BLE001 - an unknown tz name: local time
            tz = None
    base = datetime.fromtimestamp(ref, tz)
    try:
        mon = (m.group("mon") or "").lower()
        if mon in _MONTHS and m.group("day"):
            t = base.replace(month=_MONTHS[mon], day=int(m.group("day")), hour=h, minute=minute, second=0, microsecond=0)
            if t.timestamp() < ref - 86400:
                t = t.replace(year=t.year + 1)
            return t.timestamp()
        t = base.replace(hour=h, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    ts = t.timestamp()
    return ts if ts > ref else ts + 86400


def _ts(r):
    try:
        return datetime.fromisoformat(str(r.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _human(r):
    """The text of a user prompt record, or None for a tool result, a meta record or a system notice."""
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return None
    t = _text(c).strip()
    if not t or t.startswith("<task-notification>") or t.startswith("<system-reminder>"):
        return None
    if t.startswith("<local-command") or t.startswith("<command-name>"):
        return None
    return t


def read_tail(tp, size=TAIL_BYTES):
    with open(tp, "rb") as f:
        f.seek(0, 2)
        n = f.tell()
        f.seek(max(0, n - size))
        lines = f.read().decode("utf-8", errors="ignore").splitlines()
    if n > size:
        lines = lines[1:]
    out = []
    for ln in lines:
        if '"assistant"' not in ln and '"user"' not in ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def last_limit(tp, now=None):
    """How the transcript ends: {"ok": True, reset, kind, text} when its last turn ended at a usage limit and
    nothing after it came from the user; else {"ok": False, "why": ...}."""
    now = now or time.time()
    if not tp or not os.path.exists(tp):
        return {"ok": False, "why": "no transcript"}
    lim, prompt, why = None, None, "no limit record at the end"
    for r in read_tail(tp):
        if r.get("isSidechain"):
            continue
        if r.get("type") == "assistant":
            info = _limit_info(r, now)
            if info:
                lim, why = info, ""
            elif (r.get("message") or {}).get("model") != "<synthetic>":
                lim, why = None, "no limit record at the end"
            continue
        h = _human(r)
        if h is None:
            continue
        if INTERRUPT in h:
            lim, why = None, "the user interrupted the turn"
            continue
        prompt = h
        if lim:
            lim, why = None, "the user wrote after the limit"
    if not lim:
        return {"ok": False, "why": why}
    if prompt and STOP_WORDS.match(prompt):
        return {"ok": False, "why": "the user said stop before the limit"}
    reset, kind, text = lim
    return {"ok": True, "reset": reset, "kind": kind, "text": text[:200]}


# ---------- the plan ----------

def label(due, now):
    d, n = time.localtime(due), time.localtime(now)
    hm = time.strftime("%H:%M", d)
    if (d.tm_year, d.tm_yday) == (n.tm_year, n.tm_yday):
        return hm
    return time.strftime("%a %d %b ", d) + hm


def key_of(reset):
    return str(int(reset // 60))  # the reset minute: the record epoch and the parsed text give the same key


def flag_path(sid, key):
    return ac_dir(f"{sid}__{key}.flag")


def check(sid, tp=None, now=None):
    now = now or time.time()
    tp = tp or transcript_of(sid)
    st = last_limit(tp, now)
    out = {"sid": sid, "enabled": enabled(), "off": is_off(sid), "kill": kill_set()}
    if not st["ok"]:
        return dict(out, ok=False, why=st["why"])
    due = st["reset"] + DELAY_S
    key = key_of(st["reset"])
    out.update(ok=True, reset=st["reset"], due=due, key=key, kind=st["kind"], label=label(due, now),
               text=st["text"], done=os.path.exists(flag_path(sid, key)))
    if out["done"]:
        out.update(ok=False, why="continued once already for this reset")
    elif not out["enabled"]:
        out.update(ok=False, why="off in hubtree.toml [autopilot]")
    elif out["off"]:
        out.update(ok=False, why="off for this session (/autopilot on turns it on)")
    elif _over_budget(sid):
        out.update(ok=False, why="over its budget (raise it: hubtree key $ or swarm_obs.py budget)")
    return out


def _over_budget(sid):
    """swarm_obs: a session at 100% of its budget gets no auto-continue. Fails open."""
    try:
        import swarm_obs
        return swarm_obs.over_budget(sid)
    except Exception:
        return False


def _ledger(sid, by, ok, note):
    line = {"t": T.now(), "sid": sid, "by": by, "ok": ok, "note": note}
    try:
        os.makedirs(ac_dir(), exist_ok=True)
        with open(ac_dir("log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def fire(sid, key, by="mod", tp=None, now=None):
    """The last gate. Creates the flag file (exclusive) and logs; the caller sends PROMPT only on ok."""
    now = now or time.time()
    if kill_set():
        return {"ok": False, "why": "KILL file set"}
    c = check(sid, tp, now)
    if not c["ok"]:
        return {"ok": False, "why": c["why"]}
    if c["key"] != key:
        return {"ok": False, "why": f"the limit changed (key {c['key']}, asked {key})"}
    if now < c["due"]:
        return {"ok": False, "why": f"not due before {c['label']}"}
    os.makedirs(ac_dir(), exist_ok=True)
    try:
        fd = os.open(flag_path(sid, key), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return {"ok": False, "why": "continued once already for this reset"}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"t": T.now(), "by": by, "reset": c["reset"], "kind": c["kind"]}, f)
    _ledger(sid, by, True, f"continue after {c['kind'] or 'usage'} limit, reset {label(c['reset'], now)}")
    return {"ok": True, "prompt": PROMPT, "label": c["label"]}


# ---------- daemon: background sessions ----------

def _run(argv, cwd=None, timeout=60):
    r = subprocess.run(argv, cwd=cwd, env=term.child_env(), capture_output=True, text=True, timeout=timeout,
                       creationflags=term.NO_WINDOW)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


RUN = [_run]  # tests replace RUN[0] with a stub
# The claude command as an argv prefix. Tests set HUBTREE_AUTOPILOT_CLAUDE to a JSON list (a stub script).
T_CLAUDE = json.loads(os.environ["HUBTREE_AUTOPILOT_CLAUDE"]) if os.environ.get("HUBTREE_AUTOPILOT_CLAUDE") else ["claude"]


def _live_all():
    if os.environ.get("HUBTREE_AUTOPILOT_CLAUDE"):
        code, out = RUN[0](T_CLAUDE + ["agents", "--json"])
        return {a.get("sessionId"): a for a in json.loads(out or "[]")} if code == 0 else {}
    return T.live_agents(max_age=0)


def _live(sid):
    return _live_all().get(sid) or {}


def continue_background(s, now=None):
    """Stop a live background session (the documented way to free it), then wake it with PROMPT."""
    sid = s["session_id"]
    live = _live(sid)
    if live.get("pid") and live.get("id"):
        code, out = RUN[0](T_CLAUDE + ["stop", live["id"]])
        if code != 0:
            return False, f"claude stop exit {code}: {out.strip()[-160:]}"
        for _ in range(STOP_WAIT_S):
            if not _live(sid).get("pid"):
                break
            time.sleep(1)
        else:
            return False, "the session still runs after claude stop"
    cwd = T.resolve_cwd(s.get("cwd"))
    code, out = RUN[0](T_CLAUDE + ["--bg", "--resume", sid, PROMPT], cwd=cwd)
    copy = "started a copy" in out
    ok = code == 0 and not copy
    return ok, f"claude --bg --resume exit {code}{' (a copy started)' if copy else ''}: {out.strip()[-200:]}"



def scan(now=None, dry=False):
    """Continue the background sessions of the registry that a limit stopped. Returns a list of actions."""
    now = now or time.time()
    acts = []
    if kill_set() or not enabled():
        return [{"why": "KILL file set" if kill_set() else "off in hubtree.toml"}]
    live_all = _live_all()
    for sid, s in T.sessions().items():
        if s.get("deleted") or s.get("rotated_to") or not s.get("open"):
            continue
        lv = live_all.get(sid) or {}
        if lv.get("kind") == "interactive":
            continue  # a tab: the mod continues it
        if not (s.get("auto") or lv.get("kind") == "background"):
            continue
        c = check(sid, s.get("transcript"), now)
        if not c["ok"] or now < c["due"]:
            continue
        if lv.get("pid") and now < c["due"] + GRACE_S:
            acts.append({"sid": sid, "name": s.get("name"), "act": "wait", "why": "its own mod has the first turn"})
            continue
        if dry:
            acts.append({"sid": sid, "name": s.get("name"), "act": "would continue"})
            continue
        f = fire(sid, c["key"], by="daemon", tp=s.get("transcript"), now=now)
        if not f["ok"]:
            acts.append({"sid": sid, "name": s.get("name"), "act": "skip", "why": f["why"]})
            continue
        ok, note = continue_background(s, now)
        if not ok:
            _ledger(sid, "daemon", False, note)
        acts.append({"sid": sid, "name": s.get("name"), "act": "continued" if ok else "failed", "why": note})
    return acts


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description="auto-continue after a usage limit")
    ap.add_argument("cmd", choices=["check", "fire", "off", "on", "status", "scan"])
    ap.add_argument("--sid")
    ap.add_argument("--key")
    ap.add_argument("--by", default="mod")
    ap.add_argument("--transcript")
    ap.add_argument("--now", type=float)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "scan" and not a.sid:
        ap.error("--sid is needed")
    if a.cmd == "check":
        print(json.dumps(check(a.sid, a.transcript, a.now)))
        return 0
    if a.cmd == "fire":
        r = fire(a.sid, a.key or "", a.by, a.transcript, a.now)
        print(json.dumps(r))
        return 0 if r["ok"] else 3
    if a.cmd in ("off", "on"):
        set_off(a.sid, a.cmd == "off")
        print(json.dumps({"ok": True, "off": a.cmd == "off",
                          "msg": f"Autopilot continue is {a.cmd} for this session."}))
        return 0
    if a.cmd == "status":
        c = check(a.sid, a.transcript, a.now)
        if c.get("ok"):
            msg = f"Auto-continue at {c['label']}."
        else:
            msg = f"No auto-continue planned: {c['why']}."
        msg += f" Session switch: {'off' if c['off'] else 'on'}; hubtree.toml: {'on' if c['enabled'] else 'off'}."
        print(json.dumps(dict(c, msg=msg)))
        return 0
    acts = scan(a.now, a.dry_run)
    print(json.dumps({"ok": True, "actions": acts}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
