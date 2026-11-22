"""Swarm observe: cost, budgets, live activity, agents inside a session, loop and waste flags.

No model calls. One incremental reader parses each transcript once: it reads only the new bytes since the last call
and keeps a small state per file in <state>/swarm_obs/cache.json (offset, size, totals, rings). Every
caller (tree_view.py for the pane and /where, the app, the daemon job `enforce`) shares that cache.

Usage records: Claude Code writes one transcript record per content block, each with the same `usage` of the same
API message. The reader counts one message id once (the largest value of each field). A session's cost is its main
transcript plus every sub-agent and workflow agent transcript under `<session>/subagents`.
Tokens = input + cache read + cache write + output. USD = list price from the table in this file; hubtree.toml [cost] overrides it.

CLI:
  python swarm_obs.py info --sid <id>                     one session as JSON (cost, budget, activity, agents, flags)
  python swarm_obs.py budget --sid <id> [--usd N] [--tokens N] [--mult K] [--by NAME]   set or raise a budget
  python swarm_obs.py enforce [--dry]                     the daemon job: marks, messages, pause at 100% (one JSON line)
  python swarm_obs.py verify --sid <id> [--sid ...]       compare the cached sums with a fresh full read
  python swarm_obs.py warm                                read every registry transcript to the end (first fill)
"""
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import term  # noqa: E402
import tree_lib as T  # noqa: E402

DIR = os.path.join(T.STATE, "swarm_obs")
CACHE = os.path.join(DIR, "cache.json")
CACHE_VERSION = 2  # 2: the loop hash leaves out "description"
MAX_BYTES_PER_CALL = 96_000_000  # a first fill spreads over several calls, so one pane refresh stays short
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
LIMIT_ERRORS = ("rate_limit", "usage_limit_reached", "model_requires_usage_credits")
RING = 50   # tool calls kept per file (the loop window is at most this)
EVENTS = 10  # short event lines kept per file
NOT_INPUT = ("description",)  # tool input keys left out of the loop check (Bash writes a new description per call)
F_IN, F_CR, F_CW5, F_CW1, F_OUT = range(5)


# ---------------------------------------------------------------- config
def cfg(key, default):
    return T._tree_cfg(key, default)


def _cost_cfg():
    try:
        from htconf import load
        return load().raw.get("cost", {}) or {}
    except Exception:
        return {}


# List prices in USD per million tokens: [input, output, cache read]. These are list prices that can be out of
# date: check them against the provider price page. hubtree.toml [cost.models] adds or replaces a row, and
# [cost] cache_write_5m and cache_write_1h set the cache write factors (a multiple of the input price).
DEFAULT_PRICES = {
    "claude-fable-5-2": [8, 40, 0.20], "claude-fable-5-1": [10, 50, 0.25], "claude-fable-5": [10, 50, 1.0],
    "claude-opus-5-5": [4, 20, 0.20], "claude-opus-5": [5, 25, 0.50], "claude-opus-4": [5, 25, 0.50],
    "claude-sonnet-5-5": [2, 10, 0.10], "claude-sonnet-5": [2, 10, 0.20], "claude-sonnet-4": [3, 15, 0.30],
    "claude-haiku-5-5": [0.10, 0.50, 0.01], "claude-haiku-4": [1, 5, 0.10],
    "fable": [10, 50, 1.0], "opus": [5, 25, 0.50], "sonnet": [3, 15, 0.30], "haiku": [1, 5, 0.10],
}
DEFAULT_CACHE_WRITE = {"cache_write_5m": 1.25, "cache_write_1h": 2.0}
_PRICES = []


def prices():
    """[(prefix, (input, output, cache read))], longest prefix first: DEFAULT_PRICES, then hubtree.toml [cost.models]."""
    if not _PRICES:
        c = _cost_cfg()
        table = dict(DEFAULT_PRICES)
        for k, v in (c.get("models") or {}).items():
            if isinstance(v, list) and len(v) == 3:
                table[str(k)] = v
        rows = [(k.lower(), tuple(float(x) for x in v)) for k, v in table.items()]
        _PRICES.extend(sorted(rows, key=lambda r: -len(r[0])))
        _PRICES.append(("__w", (float(c.get("cache_write_5m", DEFAULT_CACHE_WRITE["cache_write_5m"])),
                                   float(c.get("cache_write_1h", DEFAULT_CACHE_WRITE["cache_write_1h"])), 0.0)))
    return _PRICES


def price(model):
    """(input, output, cache read) USD per million tokens. A full id prefix first, then a family word, then opus."""
    m = (model or "").lower()
    rows = [r for r in prices() if r[0] != "__w"]
    for k, p in rows:
        if k.startswith("claude-") and m.startswith(k):
            return p
    for k, p in rows:
        if not k.startswith("claude-") and k in m:
            return p
    return dict(rows).get("opus", rows[0][1])


def usd_of(model, f):
    """USD of one model's token fields [in, cache read, cache write 5m, cache write 1h, out]."""
    pin, pout, pcr = price(model)
    w5, w1, _ = dict(prices())["__w"]
    return (f[F_IN] * pin + f[F_OUT] * pout + f[F_CR] * pcr + f[F_CW5] * pin * w5 + f[F_CW1] * pin * w1) / 1e6


def fields(u):
    """The 5 token fields of one usage record. A cache write with no TTL split counts as a 5-minute write."""
    cw = int(u.get("cache_creation_input_tokens") or 0)
    split = u.get("cache_creation") or {}
    w1 = int(split.get("ephemeral_1h_input_tokens") or 0)
    w5 = cw - w1 if cw >= w1 else int(split.get("ephemeral_5m_input_tokens") or 0)
    return [int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0), w5, w1,
            int(u.get("output_tokens") or 0)]


# ---------------------------------------------------------------- cache
_CACHE = {"loaded": False, "files": {}, "dirty": False}


def _load_cache():
    if _CACHE["loaded"]:
        return
    _CACHE["loaded"] = True
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") == CACHE_VERSION:
            _CACHE["files"] = d.get("files") or {}
    except (OSError, ValueError):
        pass


def save():
    """Write the cache (temp file, then replace). Never raises."""
    if not _CACHE["dirty"]:
        return
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = f"{CACHE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": CACHE_VERSION, "files": _CACHE["files"]}, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, CACHE)
        _CACHE["dirty"] = False
    except OSError:
        pass


def _new_state():
    return {"off": 0, "size": 0, "m": {}, "lid": None, "lu": None, "lm": None, "turns": 0, "tools": 0,
            "ring": [], "ev": [], "since": 0, "last": None, "pend": {}, "comp": [], "model": None,
            "end": False, "api": None, "wf": {}}


def _ts(r):
    try:
        return datetime.fromisoformat((r.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _safe(text, n=90):
    """One short line with every secret or ID shape replaced (redact.redact), never the raw match."""
    t = " ".join(str(text or "").split())[:n * 2]
    try:
        import redact
        t = redact.redact(t)[0]
    except Exception:
        t = "[text hidden: redact not loaded]"
    return t[:n]


def target(name, inp):
    """A short target for a tool call: a file name, a command head, a pattern or a description."""
    if not isinstance(inp, dict):
        return ""
    for k in ("file_path", "notebook_path", "path"):
        if isinstance(inp.get(k), str) and inp[k]:
            return os.path.basename(inp[k].rstrip("\\/")) or inp[k]
    if isinstance(inp.get("command"), str):
        return inp["command"].strip().splitlines()[0][:60] if inp["command"].strip() else ""
    for k in ("pattern", "skill", "description", "url", "query", "to", "subagent_type"):
        if isinstance(inp.get(k), str) and inp[k]:
            return inp[k][:60]
    if name == "Workflow":
        return inp.get("name") or os.path.basename(str(inp.get("scriptPath") or "")) or "script"
    return ""


def _ev(st, line):
    st["ev"].append(_safe(line, 100))
    del st["ev"][:-EVENTS]


def _record(st, r):
    """Fold one transcript record into the file state."""
    t = r.get("type")
    m = r.get("message") or {}
    c = m.get("content")
    if t == "assistant":
        model = m.get("model")
        if r.get("apiError") or r.get("isApiErrorMessage"):
            st["api"] = r.get("apiError") or "api_error"
            st["end"] = False
            return
        st["api"] = None
        if model and model != "<synthetic>" and not st["model"]:
            st["model"] = model
        u = m.get("usage")
        if isinstance(u, dict) and model != "<synthetic>":
            f = fields(u)
            mid = m.get("id") or r.get("requestId")
            if mid and mid == st["lid"] and st["lu"]:
                d = [max(a, b) - b for a, b in zip(f, st["lu"])]
                st["lu"] = [max(a, b) for a, b in zip(f, st["lu"])]
            else:
                d, st["lid"], st["lu"] = f, mid, f
            if any(d):
                row = st["m"].setdefault(model or "?", [0, 0, 0, 0, 0])
                for i in range(5):
                    row[i] += d[i]
                st["since"] += sum(d)
        progress = False
        if isinstance(c, list):
            for x in c:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "text" and (x.get("text") or "").strip():
                    progress = True
                    _ev(st, "text: " + x["text"])
                elif x.get("type") == "tool_use":
                    name, inp = x.get("name") or "?", x.get("input")
                    tg = target(name, inp)
                    try:  # the free-text "description" of a call is not part of what it does
                        key = {k: v for k, v in inp.items() if k not in NOT_INPUT} if isinstance(inp, dict) else inp
                        h = hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode("utf-8",
                                                                                                     "ignore")).hexdigest()[:12]
                    except (TypeError, ValueError):
                        h = "?"
                    st["tools"] += 1
                    st["ring"].append([name, h, None, x.get("id"), _safe(tg, 60)])
                    del st["ring"][:-RING]
                    st["pend"][x.get("id")] = name
                    if len(st["pend"]) > 200:
                        st["pend"] = dict(list(st["pend"].items())[-100:])
                    st["last"] = [name, _safe(tg, 60), _ts(r)]
                    _ev(st, f"tool {name}: {tg}")
                    if name in WRITE_TOOLS:
                        progress = True
        if progress:
            st["since"] = 0
        st["end"] = m.get("stop_reason") == "end_turn" and not st["pend"]
    elif t == "user":
        if r.get("isMeta") or r.get("isCompactSummary"):
            return
        tur = r.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("runId") and tur.get("workflowName"):
            st["wf"][tur["runId"]] = tur["workflowName"]
        if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            for x in c:
                if not isinstance(x, dict) or x.get("type") != "tool_result":
                    continue
                tid = x.get("tool_use_id")
                name = st["pend"].pop(tid, None)
                err = bool(x.get("is_error"))
                for row in reversed(st["ring"]):
                    if row[3] == tid:
                        row[2] = 1 if err else 0
                        break
                if err:
                    body = x.get("content")
                    if isinstance(body, list):
                        body = " ".join(y.get("text", "") for y in body if isinstance(y, dict))
                    _ev(st, f"error in {name or 'tool'}: {body}")
            return
        txt = c if isinstance(c, str) else " ".join(
            x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
        txt = " ".join((txt or "").split())
        if txt and not txt.startswith("<"):
            st["turns"] += 1
            st["end"] = False
            _ev(st, "you: " + txt)
    elif t == "system" and r.get("subtype") == "compact_boundary":
        st["comp"].append(_ts(r) or time.time())
        del st["comp"][:-10]
        _ev(st, "compaction")


_BUDGET = [MAX_BYTES_PER_CALL]


def file_state(path, main=True):
    """The state of one transcript, read up to its end (or up to this call's byte budget). None if missing.
    main=False (a sub-agent file): the tool ring and the event lines are not kept, so the cache stays small."""
    _load_cache()
    try:
        stt = os.stat(path)
    except (OSError, TypeError):
        return None
    st = _CACHE["files"].get(path)
    if st and st.get("size") == stt.st_size and st.get("off", 0) >= stt.st_size:
        return st
    if not st or stt.st_size < st.get("off", 0):
        st = _new_state()  # new, or rewritten shorter: read again from the start
    if _BUDGET[0] <= 0:
        return st
    try:
        with open(path, "rb") as f:
            f.seek(st["off"])
            data = f.read(min(stt.st_size - st["off"], max(_BUDGET[0], 1)))
    except OSError:
        return st
    end = data.rfind(b"\n") + 1  # a last line without a newline waits for the next call
    if end == 0 and len(data) >= _BUDGET[0]:
        end = 0
    _BUDGET[0] -= end
    for line in data[:end].splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            _record(st, r)
    st["off"] += end
    st["size"] = stt.st_size if st["off"] >= stt.st_size else st["off"]
    st["partial"] = st["off"] < stt.st_size
    if not main:
        st["ring"], st["ev"], st["last"] = [], [], None
    _CACHE["files"][path] = st
    _CACHE["dirty"] = True
    return st


def totals(st):
    """(usd, tokens, {model: usd}) of one file state."""
    usd, tok, by = 0.0, 0, {}
    for model, f in (st or {}).get("m", {}).items():
        u = usd_of(model, f)
        usd += u
        tok += sum(f)
        by[model] = by.get(model, 0.0) + u
    return usd, tok, by


# ---------------------------------------------------------------- agents inside a session
def _meta(p):
    try:
        with open(p[:-6] + ".meta.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _agent_state(st, live, age_s):
    if not st:
        return "failed", "no transcript"
    if st.get("api") in LIMIT_ERRORS:
        return "failed (limit)", st["api"]
    if st.get("api"):
        return "failed", st["api"]
    if st.get("end"):
        return "done", ""
    if live and age_s < T.STUCK_MIN * 60:
        return "running", ""
    return "failed", "stopped before the end"


def agents(tp, live, now=None):
    """Sub-agent and workflow rows of a session: [{kind, label, model, state, why, tok, usd, phase, wf}]."""
    now = now or time.time()
    if not tp:
        return []
    side = tp[:-6]
    out = []
    for p in sorted(glob.glob(os.path.join(side, "subagents", "agent-*.jsonl"))):
        st = file_state(p, main=False)
        meta = _meta(p)
        age = now - (os.path.getmtime(p) if os.path.exists(p) else 0)
        state, why = _agent_state(st, live, age)
        usd, tok, _ = totals(st)
        out.append({"kind": "agent", "label": meta.get("description") or meta.get("agentType") or os.path.basename(p)[6:-6],
                    "model": (st or {}).get("model") or "?", "state": state, "why": why, "tok": tok, "usd": usd,
                    "phase": "", "wf": ""})
    main = _CACHE["files"].get(tp) or {}
    for wd in sorted(glob.glob(os.path.join(side, "subagents", "workflows", "wf_*"))):
        wf = os.path.basename(wd)
        last, phase = {}, ""
        try:
            with open(os.path.join(wd, "journal.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    k = e.get("key") or e.get("agentId")
                    if not k:
                        continue
                    if e.get("type") == "started":
                        last[k] = dict(e)
                        phase = e.get("phase") or phase
                    elif e.get("type") in ("result", "failed") and k in last:
                        last[k]["type"] = e["type"]
                        last[k]["agentId"] = e.get("agentId") or last[k].get("agentId")
        except OSError:
            pass
        wname = (main.get("wf") or {}).get(wf) or wf
        for k, e in last.items():
            p = os.path.join(wd, f"agent-{e.get('agentId')}.jsonl")
            st = file_state(p, main=False) if os.path.exists(p) else None
            if e["type"] == "result":
                state, why = "done", ""
            elif e["type"] == "failed":
                api = (st or {}).get("api")
                state, why = ("failed (limit)", api) if api in LIMIT_ERRORS else ("failed", api or "")
            else:
                age = now - (os.path.getmtime(p) if os.path.exists(p) else 0)
                state, why = ("running", "") if live and age < T.STUCK_MIN * 60 else ("failed", "stopped before the end")
            usd, tok, _ = totals(st)
            out.append({"kind": "wf", "label": e.get("label") or k[:12], "model": (st or {}).get("model") or "?",
                        "state": state, "why": why, "tok": tok, "usd": usd, "phase": e.get("phase") or "",
                        "wf": wname, "wf_phase": phase})
    return out


def agent_summary(rows):
    """'+74 agents, 3 failed, 2 running' or ''."""
    if not rows:
        return ""
    n = {}
    for r in rows:
        k = "failed" if r["state"].startswith("failed") else r["state"]
        n[k] = n.get(k, 0) + 1
    lim = sum(1 for r in rows if r["state"] == "failed (limit)")
    parts = [f"+{len(rows)} agent{'s' if len(rows) != 1 else ''}"]
    if n.get("failed"):
        parts.append(f"{n['failed']} failed" + (f" ({lim} at a usage limit)" if lim else ""))
    if n.get("running"):
        parts.append(f"{n['running']} running")
    return ", ".join(parts)


def agent_lines(rows):
    """Short lines for the detail views: one header per workflow, then one line per agent."""
    out, wf = [], None
    for r in rows:
        if r["kind"] == "wf" and r["wf"] != wf:
            wf = r["wf"]
            sub = [x for x in rows if x["kind"] == "wf" and x["wf"] == wf]
            out.append(f"workflow {wf} (phase {r.get('wf_phase') or '-'}): {agent_summary(sub)[1:]}")
        pad = "  " if r["kind"] == "wf" else ""
        ph = f" [{r['phase']}]" if r.get("phase") else ""
        why = f" {r['why']}" if r.get("why") and r["state"] != "done" else ""
        out.append(_safe(f"{pad}{r['state']}{why}: {r['label']}{ph} {short_model(r['model'])} {fmt_tok(r['tok'])}", 110))
    return out


# ---------------------------------------------------------------- session info
def fmt_usd(x):
    return f"${x:,.2f}" if x < 1000 else f"${x:,.0f}"


def fmt_tok(n):
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def short_model(m):
    return (m or "?").replace("claude-", "")


def session_files(tp):
    if not tp:
        return []
    side = tp[:-6]
    return [tp] + sorted(glob.glob(os.path.join(side, "subagents", "agent-*.jsonl"))) + \
        sorted(glob.glob(os.path.join(side, "subagents", "workflows", "wf_*", "agent-*.jsonl")))


def cost(s):
    """{"usd", "tok", "models": {model: usd}, "partial"} of a session with all its agent transcripts."""
    usd, tok, by, part = 0.0, 0, {}, False
    tp = s.get("transcript")
    for p in session_files(tp):
        st = file_state(p, main=p == tp)
        if not st:
            continue
        u, t, b = totals(st)
        usd, tok = usd + u, tok + t
        for k, v in b.items():
            by[k] = by.get(k, 0.0) + v
        part = part or bool(st.get("partial"))
    return {"usd": usd, "tok": tok, "models": by, "partial": part}


def budget(s, c):
    """{"usd", "tokens", "pct", "level": None|"warn"|"over", "text"} or None when the session has no budget."""
    bu, bt = s.get("budget_usd"), s.get("budget_tokens")
    if not bu and not bt:
        return None
    pcts = []
    if bu:
        pcts.append(c["usd"] / float(bu) * 100)
    if bt:
        pcts.append(c["tok"] / float(bt) * 100)
    pct = max(pcts)
    warn = cfg("budget_warn_pct", 80)
    level = "over" if pct >= 100 else "warn" if pct >= warn else None
    parts = ([f"{fmt_usd(c['usd'])} of {fmt_usd(float(bu))}"] if bu else []) + \
            ([f"{fmt_tok(c['tok'])} of {fmt_tok(int(bt))} tokens"] if bt else [])
    return {"usd": bu, "tokens": bt, "pct": round(pct, 1), "level": level,
            "text": f"budget {pct:.0f}%: " + ", ".join(parts)}


def _rotations(s, now):
    n = 0
    name = s.get("name")
    if name:
        for e in T.read_events():
            if e.get("event") == "rotate" and e.get("name") == name:
                try:
                    if now - datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp() < 3600:
                        n += 1
                except (KeyError, ValueError, AttributeError):
                    pass
    return n


def flags(s, st, now=None, rot=None):
    """[(kind, text)] loop and waste flags of a session's main transcript state."""
    now = now or time.time()
    out = []
    if not st:
        return out
    ring = st.get("ring") or []
    win = ring[-int(cfg("loop_window", 50)):]
    cnt = {}
    for row in win:
        cnt[(row[0], row[1])] = cnt.get((row[0], row[1]), 0) + 1
    if cnt:
        (name, h), n = max(cnt.items(), key=lambda kv: kv[1])
        if n >= int(cfg("loop_repeat", 5)):
            tg = next((r[4] for r in reversed(win) if r[0] == name and r[1] == h), "")
            out.append(("loop", f"loop: {name} {n} times with the same input ({tg})"))
    done = [r for r in ring if r[2] is not None][-int(cfg("error_window", 30)):]
    errs = sum(r[2] for r in done)
    if len(done) >= int(cfg("error_min_calls", 10)) and errs / len(done) > float(cfg("error_rate", 0.5)):
        out.append(("errors", f"errors: {errs} of the last {len(done)} tool calls failed"))
    comp = sum(1 for t in st.get("comp") or [] if t and now - t < 3600)
    rot = _rotations(s, now) if rot is None else rot
    if comp + rot >= int(cfg("rotations_per_hour", 3)):
        out.append(("rotations", f"rotations: {rot} rotations and {comp} compactions in the last hour"))
    if st.get("since", 0) > cfg("waste_tokens", 5_000_000):
        out.append(("waste", f"waste: {fmt_tok(st['since'])} tokens with no file write and no new text"))
    return out


def activity(s, st, now=None):
    """Short live-step lines: the current step, the time since the last write, the turn count, the last events."""
    now = now or time.time()
    if not st:
        return []
    hb = T.heartbeat(s)
    last = st.get("last")
    step = "no tool call yet"
    if last:
        open_ = bool(st.get("pend")) and list(st["pend"].values())[-1] == last[0]
        ago = f", {int(now - last[2])} s ago" if last[2] else ""
        step = f"{'running' if open_ else 'last tool'} {last[0]}: {last[1]}{ago}"
    head = f"step: {step} | last write {int(now - hb) if hb else '?'} s ago | turns {st.get('turns', 0)}" \
           f" | tool calls {st.get('tools', 0)}"
    return [head] + list(st.get("ev") or [])


def info(s, live, now=None, with_agents=True):
    """All the swarm-observe facts of one registry session."""
    now = now or time.time()
    tp = s.get("transcript")
    a = (live or {}).get(s.get("session_id"))
    st = file_state(tp) if tp else None
    c = cost(s)
    out = {"usd": c["usd"], "tok": c["tok"], "models": c["models"], "partial": c["partial"],
           "budget": budget(s, c), "flags": [], "activity": [], "agents": [], "agent_summary": ""}
    if a:
        out["flags"] = flags(s, st, now)
        out["activity"] = activity(s, st, now)
    if with_agents:
        rows = agents(tp, bool(a), now)
        out["agents"], out["agent_summary"] = rows, agent_summary(rows)
    return out


def need_you(s, live):
    """[(kind, text)] for the "needs you" list: the flags of a live session, and a budget mark (any session).
    The caller runs save() once at the end. Never raises."""
    try:
        if s.get("session_id") not in (live or {}) and not (s.get("budget_usd") or s.get("budget_tokens")):
            return []
        inf = info(s, live, with_agents=False)
        out = list(inf["flags"])
        b = inf["budget"]
        if b and b["level"]:
            out.append(("budget", b["text"] + (" (paused)" if b["level"] == "over" else "")))
        return out
    except Exception:
        return []


def marks(inf):
    """The short mark text after a row: flags and the budget, for example 'LOOP  budget 85%'."""
    m = [k.upper() for k, _ in inf.get("flags") or []]
    b = inf.get("budget")
    if b and b["level"]:
        m.append(("OVER BUDGET" if b["level"] == "over" else "budget") + f" {b['pct']:.0f}%")
    return "  ".join(m)


# ---------------------------------------------------------------- parent messages (one per episode)
def _on(sid, kind):
    return os.path.join(T.ALERTS, f"{sid}_{kind}.on")


def _send(to, text):
    try:
        import hubdb
        con = hubdb.connect()
        try:
            hubdb.send(con, to, text, from_sid="hubtree")
        finally:
            con.close()
        return True
    except Exception:
        return False


def _still_due(sid, kind, inf):
    """Read the budget again from the registry just before a budget message: a raise between this caller's registry
    read and now must not send a stale 100% message."""
    if "usd" not in inf or "tok" not in inf:
        return True
    fresh = T.sessions().get(sid)
    b = budget(fresh, {"usd": inf["usd"], "tok": inf["tok"]}) if fresh else None
    return bool(b and (b["level"] == "over" if kind == "budget100" else b["level"]))


def notify(s, inf, live_now=True):
    """One parent message per episode for each flag and for the budget marks (an .on flag file per kind).
    A flag that ends removes its file, so a new episode sends again. Only a live session's flags end here.
    Returns the kinds sent."""
    sid, par = s.get("session_id"), s.get("parent")
    want = dict(inf.get("flags") or [])
    b = inf.get("budget")
    if b and b["level"]:
        want["budget80"] = b["text"]
    if b and b["level"] == "over":
        want["budget100"] = b["text"]
    sent = []
    os.makedirs(T.ALERTS, exist_ok=True)
    kinds = ("loop", "errors", "rotations", "waste", "budget80", "budget100")
    for kind in kinds:
        f = _on(sid, kind)
        if kind not in want:
            # The budget is known for every session; the other flags only for a live one (live_now).
            if os.path.exists(f) and (live_now or kind.startswith("budget")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            continue
        try:
            fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"t": T.now(), "text": want[kind]}).encode("utf-8"))
            os.close(fd)
        except OSError:
            continue  # sent before in this episode
        if not par or (kind == "budget80" and "budget100" in want):
            continue  # a jump past 80% straight to 100%: only the 100% message goes out
        if kind.startswith("budget") and not _still_due(sid, kind, inf):
            try:
                os.remove(f)  # the budget changed after this caller read the registry (a raise): no message
            except OSError:
                pass
            continue
        name = s.get("name") or sid[:8]
        if kind == "budget100":
            text = (f"child {name} reached its budget ({want[kind]}) at {time.strftime('%Y-%m-%d %H:%M')}. "
                    "The daemon job budget pauses a background child within 1 minute; a tab gets no auto-continue. "
                    "Raise the budget "
                    "with hubtree key $ or: python tree/swarm_obs.py budget --sid " + sid + " --mult 2")
        elif kind == "budget80":
            text = f"child {name} used {want[kind]} at {time.strftime('%Y-%m-%d %H:%M')}. At 100% it pauses."
        else:
            text = (f"child {name} is flagged at {time.strftime('%Y-%m-%d %H:%M')}: {want[kind]}. The tree does not "
                    "stop it. Check its live step in hubtree, then send it a message or stop its known process.")
        if _send(par, text):
            sent.append(kind)
        else:
            try:
                os.remove(f)  # not sent: try again at the next refresh
            except OSError:
                pass
    return sent


# ---------------------------------------------------------------- budget: set, raise, enforce
def set_budget(sid, usd=None, tokens=None, mult=None, by="user"):
    """Append a budget event. --mult raises the current budget (or sets nothing when there is none).
    A raise clears the budget flag files and lifts a pause that the budget made."""
    s = T.sessions().get(sid) or T.by_name(sid)
    if not s:
        raise LookupError(f"no session {sid}")
    sid = s["session_id"]
    ev = {"event": "budget", "session_id": sid, "by": by}
    if mult:
        if s.get("budget_usd"):
            ev["budget_usd"] = round(float(s["budget_usd"]) * mult, 4)
        if s.get("budget_tokens"):
            ev["budget_tokens"] = int(int(s["budget_tokens"]) * mult)
    if usd is not None:
        ev["budget_usd"] = float(usd)
    if tokens is not None:
        ev["budget_tokens"] = int(tokens)
    if "budget_usd" not in ev and "budget_tokens" not in ev:
        raise ValueError("the session has no budget to raise: give --usd or --tokens")
    T.append(ev)
    for kind in ("budget80", "budget100", "pause"):
        try:
            os.remove(_on(sid, kind))
        except OSError:
            pass
    if s.get("paused") and s.get("paused_by") == "budget":
        try:
            import swarm
            swarm.unpause(sid, by=by)
        except Exception:
            T.append({"event": "unpaused", "session_id": sid, "by": by})
    return ev


def over_budget(sid):
    """True when the session has a budget and spent 100% of it (the autopilot gate). Never raises."""
    try:
        s = T.sessions().get(sid)
        if not s:
            return False
        b = budget(s, cost(s))
        save()
        return bool(b and b["level"] == "over")
    except Exception:
        return False


def pause(s, why, live):
    """The 100% pause: swarm.pause (swarm-control) when it exists, else the same event shape and claude stop."""
    sid = s["session_id"]
    try:
        import swarm
        swarm.pause(sid, by="budget", why=why)
        return "swarm.pause"
    except ImportError:
        pass
    a = live.get(sid) or {}
    if a.get("kind") == "background" and a.get("id"):
        subprocess.run(["claude", "stop", a["id"]], capture_output=True, timeout=60,
                       creationflags=term.NO_WINDOW, env=term.child_env())
        T.append({"event": "end", "session_id": sid, "reason": f"budget: {why}"})
    T.append({"event": "paused", "session_id": sid, "by": "budget", "why": why})
    try:
        import autopilot
        autopilot.set_off(sid, True)
    except Exception:
        pass
    return "fallback"


def enforce(dry=False, now=None):
    """The daemon job: for every session with a budget, the 80% and 100% marks, one parent message per mark, and the
    pause at 100% (once per episode: the budget100 flag file)."""
    now = now or time.time()
    live = T.live_agents()
    acts = []
    for sid, s in T.sessions().items():
        if not (s.get("budget_usd") or s.get("budget_tokens")) or s.get("deleted") or s.get("rotated_to"):
            continue
        c = cost(s)
        b = budget(s, c)
        if not b or not b["level"]:
            continue
        if dry:
            acts.append({"sid": sid, "name": s.get("name"), "level": b["level"], "pct": b["pct"], "dry": True})
            continue
        sent = notify(s, {"budget": b, "flags": [], "usd": c["usd"], "tok": c["tok"]}, live_now=False)
        act = {"sid": sid, "name": s.get("name"), "level": b["level"], "pct": b["pct"], "sent": sent}
        if b["level"] == "over" and not s.get("paused") and \
                _still_due(sid, "budget100", {"usd": c["usd"], "tok": c["tok"]}):
            try:  # once per budget episode: the pause flag file (a raise removes it)
                os.close(os.open(_on(sid, "pause"), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                act["pause"] = pause(s, b["text"], live)
            except FileExistsError:
                pass
        acts.append(act)
    save()
    return acts


# ---------------------------------------------------------------- independent check
def direct_sum(paths):
    """A fresh full read with no cache: group usage by message id, keep the largest value of each field."""
    per = {}
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    m = r.get("message") or {}
                    if r.get("type") != "assistant" or not isinstance(m.get("usage"), dict) or \
                            m.get("model") == "<synthetic>":
                        continue
                    k = (p, m.get("id") or r.get("uuid"))
                    f5 = fields(m["usage"])
                    old = per.get(k)
                    per[k] = (m.get("model"), [max(a, b) for a, b in zip(f5, old[1])] if old else f5)
        except OSError:
            pass
    usd = sum(usd_of(model, f) for model, f in per.values())
    tok = sum(sum(f) for _, f in per.values())
    return {"usd": usd, "tok": tok, "messages": len(per)}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["info", "budget", "enforce", "verify", "warm"])
    ap.add_argument("--sid", action="append")
    ap.add_argument("--usd", type=float)
    ap.add_argument("--tokens", type=int)
    ap.add_argument("--mult", type=float)
    ap.add_argument("--by", default="user")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "warm":
        _BUDGET[0] = 1 << 62
        t0, n = time.time(), 0
        for s in T.sessions().values():
            tp = s.get("transcript")
            for p in session_files(tp):
                n += 1 if file_state(p, main=p == tp) else 0
        save()
        print(json.dumps({"files": n, "secs": round(time.time() - t0, 1)}))
        return
    if a.cmd == "enforce":
        print(json.dumps({"actions": enforce(dry=a.dry)}, ensure_ascii=False))
        return
    if not a.sid:
        ap.error("--sid is needed")
    if a.cmd == "budget":
        print(json.dumps(set_budget(a.sid[0], a.usd, a.tokens, a.mult, a.by)))
        return
    _BUDGET[0] = 1 << 62
    allreg = T.sessions()
    live = T.live_agents()
    for sid in a.sid:
        s = allreg.get(sid) or T.by_name(sid)
        if not s:
            print(json.dumps({"sid": sid, "error": "not in the registry"}))
            continue
        if a.cmd == "info":
            print(json.dumps(info(s, live), ensure_ascii=False, indent=1))
        else:
            c, d = cost(s), direct_sum(session_files(s.get("transcript")))
            print(json.dumps({"name": s.get("name"), "cached_usd": round(c["usd"], 6), "direct_usd": round(d["usd"], 6),
                              "cached_tok": c["tok"], "direct_tok": d["tok"], "messages": d["messages"],
                              "equal": abs(c["usd"] - d["usd"]) < 1e-6 and c["tok"] == d["tok"]}))
    save()


if __name__ == "__main__":
    main()
