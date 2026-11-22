"""Swarm web: a local page of the hubtree session tree with a few safe actions. Standard library only. No model.

  hubtree web serve [--port N]             the page on 127.0.0.1 only (the daemon starts it when [web] enabled = true)
  hubtree web snapshot [--out F]           build a snapshot and write <state>/web/snapshot.json
  hubtree web render --out F [--age-min M] write the static page (tests and screenshots)
  hubtree web message --sid X --text T     the message action: swarm.deliver (same path as the app)
  hubtree web token [--new | --check]      print the login token; --new makes a new one; --check prints a fingerprint

Login token: env HUBTREE_WEB_TOKEN, else the file <state>/web_token (made on the first serve, mode 0600 where the
OS supports it). The server prints the path of that file, never the token.
Data: tree_view.py (rows, states, cost, marks) and swarm.py results (open results). Every text goes through
redact.redact. Actions run the existing CLIs as children (no window) and write one line to <state>/web/actions.jsonl:
  accept     swarm.py accept ID                 (its commit check stays on)
  reject     swarm.py reject ID --why W
  group      swarm.py group pause|resume --root SID [--yes]   (without --yes: the preview)
  budget     swarm_obs.py budget --sid SID --mult K | --usd N
  message    swarm_web.py message --sid SID --text T
Security: 127.0.0.1 only; a Host allow list; a token or a signed cookie on every request but /login and /healthz;
POST needs the header X-Hub-Web and a same-origin Origin; 10 failed logins in 10 minutes lock the login.
"""
import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import htconf  # noqa: E402
import term  # noqa: E402
import tree_lib as T  # noqa: E402

PY = term.python()
TREE_VIEW = os.path.join(HERE, "tree_view.py")
SWARM = os.path.join(HERE, "swarm.py")
OBS = os.path.join(HERE, "swarm_obs.py")
DEFAULTS = {"enabled": False, "port": 8765, "hosts": [], "stale_min": 10, "cache_s": 15, "cookie_days": 30}
WAITING = ("you", "stuck", "died")
COOKIE = "hubweb"
TOKEN_ENV = "HUBTREE_WEB_TOKEN"
TOKEN_FILE = "web_token"  # in the state folder
TOKEN_MIN = 24  # a shorter token counts as no token: every request is refused


# ---------------------------------------------------------------- config and small helpers

def cfg():
    out = dict(DEFAULTS)
    try:
        raw = htconf.load().raw.get("web") or {}
        for k, v in raw.items():
            if k in DEFAULTS and type(v) is type(DEFAULTS[k]):
                out[k] = v
    except Exception:  # noqa: BLE001 - a broken config keeps the defaults
        pass
    return out


def wdir(*parts):
    return os.path.join(T.STATE, "web", *parts)


def _child_env():
    env = term.child_env()
    for k in ("CLAUDE_CODE_SESSION_ID", "HUBTREE_SESSION", "CLAUDECODE"):
        env.pop(k, None)  # the action is the user's, not the session that started the server
    env.pop(TOKEN_ENV, None)
    return env


def _run(argv, timeout=120):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=HERE,
                           timeout=timeout, creationflags=term.NO_WINDOW, env=_child_env(), stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", f"{type(e).__name__}: {e}"


RUN = [_run]  # tests replace RUN[0] with a stub


def redact(x):
    """Every string in x through redact.redact (the one redaction function of hubtree)."""
    try:
        import redact as guard
    except Exception:  # noqa: BLE001 - no guard: no text leaves
        guard = None
    if isinstance(x, str):
        return guard.redact(x)[0] if guard else x
    if isinstance(x, list):
        return [redact(v) for v in x]
    if isinstance(x, dict):
        return {k: redact(v) for k, v in x.items()}
    return x


def _json_tail(out):
    for line in reversed((out or "").strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    try:
        return json.loads(out)
    except ValueError:
        return None


def last_actions(n=8):
    try:
        with open(wdir("actions.jsonl"), encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        return []


def log_action(rec):
    os.makedirs(wdir(), exist_ok=True)
    with open(wdir("actions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- snapshot

def build_snapshot():
    """{"ts", "rows", "counts", "results", "actions", "errors"} from the existing tools, redacted."""
    errors = []
    code, out, err = RUN[0]([PY, TREE_VIEW])
    tree = _json_tail(out) if code == 0 else None
    if not isinstance(tree, dict):
        errors.append(f"tree_view.py exit {code}: {(err or out)[-200:]}")
        tree = {"rows": [], "counts": {}}
    code, out, err = RUN[0]([PY, SWARM, "results"])
    res = _json_tail(out)
    if not isinstance(res, dict) or not res.get("ok"):
        errors.append(f"swarm.py results exit {code}: {(err or out)[-200:]}")
        res = {"results": []}
    snap = {"ts": time.time(), "rows": tree.get("rows", []), "counts": tree.get("counts", {}),
            "results": [{k: r.get(k) for k in ("id", "child", "child_name", "ts", "body", "status")}
                        for r in res.get("results", [])],
            "actions": last_actions(), "errors": errors}
    return redact(snap)


def public_copy(snap):
    """The snapshot for a read-only page: no live step, no agent lines, no folders on disk."""
    rows = [{k: v for k, v in r.items() if k not in ("activity", "agent_lines", "cwd", "live_id")} for r in snap["rows"]]
    return dict(snap, rows=rows, actions=[{k: a.get(k) for k in ("t", "act", "ok", "name")} for a in snap["actions"]])


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def save_snapshot(snap):
    """Write <state>/web/snapshot.json and return a short summary."""
    write_atomic(wdir("snapshot.json"), json.dumps(snap, ensure_ascii=False))
    return {"rows": len(snap["rows"]), "results": len(snap["results"]), "errors": snap["errors"]}


# ---------------------------------------------------------------- rendering (one renderer for both pages)

def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def _tv():
    try:
        import tree_view
        return tree_view
    except Exception:  # noqa: BLE001
        return None


def cost_text(r):
    tv = _tv()
    return tv.cost_text(r).strip() if tv else ""


def ctl_text(r):
    tv = _tv()
    return tv.ctl_text(r).strip() if tv else ""


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def age_html(ts, now, stale_min, live):
    mins = max(0, int((now - ts) // 60))
    stale = now - ts > stale_min * 60
    word = "STALE" if stale else "fresh"
    when = time.strftime("%a %d %b %H:%M:%S", time.localtime(ts))
    return (f'<div id="age" class="age {"stale" if stale else "fresh"}" data-ts="{int(ts)}" '
            f'data-stale="{int(stale_min * 60)}" data-live="{1 if live else 0}">'
            f'<b>{word}</b> snapshot {esc(when)}, {mins} min old'
            f'<span class="sub"> (stale after {esc(hhmm(ts + stale_min * 60))})</span></div>')


def shown_rows(rows):
    """Mark each row: "keep" when it is not closed or has a row under it that is not closed (closed rows hide)."""
    keep = [False] * len(rows)
    for i, r in enumerate(rows):
        if r["kind"] == "session" and r.get("state") != "closed":
            keep[i] = True
            d = r["depth"]
            for j in range(i - 1, -1, -1):  # the rows above with a smaller depth are its ancestors
                if rows[j]["depth"] < d:
                    keep[j] = True
                    d = rows[j]["depth"]
                    if d == 0:
                        break
    return keep


def row_line(r):
    """'[state] label  $1.23 (tree $4.56)  marks  (2 YOU)  2 results' (the same parts as tree_view.py --text)."""
    if r["kind"] == "folder":
        parts = [esc(r["label"]), esc(cost_text(r))]
    else:
        parts = [f'<span class="st st-{esc(r.get("state"))}">[{esc(r.get("state"))}]</span> <b>{esc(r["label"])}</b>',
                 esc(cost_text(r)), f'<span class="mk">{esc(r.get("marks"))}</span>' if r.get("marks") else "",
                 esc(r.get("agent_summary") or ""), esc(ctl_text(r))]
    if r.get("below"):
        parts.append(f'<span class="mk">({esc(r["below"])})</span>')
    return " ".join(p for p in parts if p)


def btn(label, act, **data):
    attrs = " ".join(f'data-{k}="{esc(v)}"' for k, v in data.items())
    return f'<button type="button" data-act="{esc(act)}" {attrs}>{esc(label)}</button>'


def session_detail(r, live):
    out = []
    if r.get("why"):
        out.append(f"<div>{esc(r['why'])}</div>")
    b = r.get("budget") or {}
    if b.get("text"):
        out.append(f'<div>{esc(b["text"])}</div>')
    if r.get("flag_text"):
        out.append(f'<div class="mk">{esc(r["flag_text"])}</div>')
    if r.get("conflict"):
        out.append(f'<div class="mk">CONFLICT {esc(r["conflict"])}</div>')
    if r.get("wait"):
        out.append(f'<div>{esc(r["wait"])}</div>')
    out.append(f'<div class="sub">{esc(r.get("name"))}</div>')
    if live:
        act = r.get("activity") or []
        if act:
            out.append('<ul class="act">' + "".join(f"<li>{esc(a)}</li>" for a in act[:10]) + "</ul>")
        if r.get("agent_lines"):
            out.append('<ul class="act">' + "".join(f"<li>{esc(a)}</li>" for a in r["agent_lines"][:20]) + "</ul>")
        sid = r["sid"]
        acts = [btn("Resume sub-tree" if r.get("paused") or r.get("state") == "paused" else "Pause sub-tree",
                    "group_preview", sid=sid,
                    action="resume" if r.get("paused") or r.get("state") == "paused" else "pause")]
        if b:
            acts.append(btn("Budget x2", "budget", sid=sid, mult="2"))
        acts.append('<input name="usd" inputmode="decimal" placeholder="USD" size="5">' +
                    btn("Set budget", "budget", sid=sid))
        out.append(f'<div class="acts">{"".join(acts)}</div>')
        out.append('<div class="acts"><input name="text" placeholder="message to this session" maxlength="2000">'
                   + btn("Send", "message", sid=sid) + "</div>")
    return "".join(out)


def tree_html(rows, live):
    keep = shown_rows(rows)
    n_closed = sum(1 for r, k in zip(rows, keep) if r["kind"] == "session" and not k)
    out = [f'<label class="tog"><input type="checkbox" id="showclosed"> show closed sessions ({n_closed})</label>',
           '<div class="tree">']
    for r, k in zip(rows, keep):
        cls = "" if k else " closed"
        pad = f' style="--d:{int(r["depth"])}"'
        if r["kind"] == "folder":
            out.append(f'<div class="row folder{cls}"{pad} data-name="{esc(r["name"])}">{row_line(r)}</div>')
        else:
            out.append(f'<details class="row sess{cls}"{pad} data-key="{esc(r["sid"])}" data-name="{esc(r["name"])}" '
                       f'data-state="{esc(r.get("state"))}" data-cost="{esc(cost_text(r))}">'
                       f'<summary>{row_line(r)}</summary><div class="det">{session_detail(r, live)}</div></details>')
    out.append("</div>")
    return "".join(out)


def waiting_html(rows):
    w = [r for r in rows if r["kind"] == "session" and (r.get("state") in WAITING or r.get("flags")
                                                      or "OVER BUDGET" in (r.get("marks") or ""))]
    if not w:
        return "<p>Waits for you: none</p>"
    return "<ul>" + "".join(f'<li><span class="st st-{esc(r.get("state"))}">[{esc(r.get("state"))}]</span> '
                            f'{esc(r.get("name"))}: {esc(r.get("why") or r.get("marks") or r.get("flag_text"))}</li>'
                            for r in w) + "</ul>"


def results_html(results, live):
    if not results:
        return "<p>No result waits.</p>"
    out = []
    for r in results:
        b = r.get("body") or {}
        lines = "".join(f"<div><b>{k}:</b> {esc(b.get(k))}</div>" for k in ("done", "proof", "commits", "open",
                                                                             "decisions") if b.get(k))
        acts = ""
        if live:
            acts = (f'<div class="acts">{btn("Accept", "accept", id=r["id"])}'
                    f'<input name="why" placeholder="reason (reject)" maxlength="300">{btn("Reject", "reject", id=r["id"])}</div>')
        out.append(f'<div class="card"><div><b>#{esc(r["id"])}</b> {esc(r.get("child_name"))} '
                   f'<span class="sub">{esc(hhmm(r["ts"]) if r.get("ts") else "")}</span></div>{lines}{acts}</div>')
    return "".join(out)


def actions_html(acts):
    if not acts:
        return ""
    li = "".join(f'<li>{esc(time.strftime("%H:%M", time.localtime(a.get("t") or 0)))} {esc(a.get("act"))} '
                 f'{esc(a.get("name") or "")}: {"ok" if a.get("ok") else "failed"}</li>' for a in reversed(acts))
    return f"<h2>Last actions from this page</h2><ul class='sub'>{li}</ul>"


def counts_html(snap):
    c = snap.get("counts") or {}
    hub = next((r for r in snap["rows"] if r["kind"] == "folder" and r.get("name") == "hub"), None)
    parts = [f"{esc(k)} {v}" for k, v in sorted(c.items(), key=lambda kv: (kv[0] == "closed", kv[0]))]
    if hub and hub.get("usd") is not None:
        parts.append(f"all ${hub['usd']:,.2f}")
    return '<div class="counts">' + " &middot; ".join(parts) + "</div>"


def section(key, title, body, open_=True):
    return (f'<details class="sec" data-key="sec-{key}"{" open" if open_ else ""}><summary><h2>{title}</h2></summary>'
            f"{body}</details>")


def main_html(snap, live):
    err = "".join(f'<div class="mk">{esc(e)}</div>' for e in snap.get("errors") or [])
    rows, res = snap["rows"], snap["results"]
    nwait = sum(1 for r in rows if r["kind"] == "session" and (r.get("state") in WAITING or r.get("flags")
                                                               or "OVER BUDGET" in (r.get("marks") or "")))
    return (err + section("wait", f"Waits for you ({nwait})", waiting_html(rows))
            + section("res", f"Results to accept ({len(res)})", results_html(res, live))
            + section("tree", "Tree", tree_html(rows, live)) + actions_html(snap.get("actions")))


def header_html(snap, now, live):
    return age_html(snap["ts"], now, cfg()["stale_min"], live) + counts_html(snap)


CSS = """
:root{color-scheme:light dark;--fg:#1b1b1b;--bg:#fafafa;--mut:#666;--red:#b3261e;--grn:#1e7b34;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--fg:#e8e8e8;--bg:#141414;--mut:#9a9a9a;--red:#ff6b61;--grn:#5cc977;
--card:#1e1e1e;--line:#333}}
*{box-sizing:border-box;min-width:0}body{margin:0;overflow-wrap:anywhere;font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--fg);
background:var(--bg)}header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
padding:8px 12px;z-index:2}main{padding:4px 12px 40px;max-width:1100px}h1{font-size:18px;margin:0 0 4px}
h2{font-size:15px;margin:0;display:inline}.sec{margin-top:12px}.sec>summary{border-bottom:1px solid var(--line);padding:4px 0}.age{padding:4px 8px;border-radius:6px}
.age.fresh{background:color-mix(in srgb,var(--grn) 18%,transparent)}.age.stale{background:var(--red);color:#fff}
.sub{color:var(--mut);font-size:13px}.age.stale .sub{color:#fff}.counts{font-size:13px;color:var(--mut);margin-top:4px}
.mk{color:var(--red)}.st{font-family:ui-monospace,Consolas,monospace;font-size:13px}.st-you,.st-stuck,.st-died{color:var(--red);font-weight:600}
.st-work,.st-tab{color:var(--grn)}.st-closed{color:var(--mut)}.st-paused,.st-queued{color:#b26b00}
.tree .row{padding:3px 0 3px calc(var(--d) * 14px);border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.tree .folder{color:var(--mut)}.tree .closed{display:none}
.tog{display:block;margin:4px 0;font-size:13px}.tog:has(#showclosed:checked) + .tree .closed{display:block}
summary{cursor:pointer}.det{padding:6px 0 6px 14px;font-size:14px}.act{margin:4px 0;padding-left:18px;font-size:12px;
color:var(--mut);font-family:ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0;overflow-wrap:anywhere}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);
min-height:36px}button.confirm{background:var(--red);color:#fff}input{font:inherit;padding:6px;border-radius:6px;
border:1px solid var(--line);background:var(--card);color:var(--fg);min-height:36px;flex:1;min-width:8em}
header .acts button{min-height:30px;padding:3px 10px;font-size:13px}input[type=checkbox]{min-width:0;min-height:0;flex:none;width:auto}#msg{white-space:pre-wrap;font-size:13px;margin:6px 0}#msg.bad{color:var(--red)}
"""

STATIC_JS = """(function(){var e=document.getElementById('age');if(!e)return;function t(){var a=Date.now()/1000-
+e.dataset.ts,m=Math.floor(a/60),s=a>+e.dataset.stale;e.className='age '+(s?'stale':'fresh');e.querySelector('b')
.textContent=s?'STALE':'fresh';var n=e.childNodes[1];if(n)n.textContent=n.textContent.replace(/, \\d+ min old/,', '+m+
' min old');}t();setInterval(t,15000);document.getElementById('showclosed').addEventListener('change',function(x){
document.querySelectorAll('.tree .closed').forEach(function(r){r.style.display=x.target.checked?'block':''})})})();"""


def page(snap, live=True, now=None):
    """The whole page. live: the served page (buttons, live step, /app.js). Not live: a static read-only copy."""
    now = now if now is not None else time.time()
    script = '<script src="/app.js" defer></script>' if live else f"<script>{STATIC_JS}</script>"
    title = "Hubtree" if live else "Hubtree (read-only copy)"
    logout = ('<div class="acts top">' + '<form method="post" action="/logout"><button type="submit">Log out</button></form></div>'
              if live else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f"<title>{title}</title><style>{CSS}</style></head><body><header><h1>{title}</h1>"
            f'<div id="hdr">{header_html(snap, now, live)}</div>{logout}<div id="msg"></div></header>'
            f'<main id="main">{main_html(snap, live)}</main>{script}</body></html>')


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Hubtree: log in</title>
<style>body{font:16px system-ui,sans-serif;margin:24px;max-width:28em}input,button{font:inherit;padding:8px;width:100%;
margin:6px 0}</style></head><body><h1>Hubtree</h1><p>%MSG%</p><form method="post" action="/login">
<input name="token" type="password" autocomplete="current-password" placeholder="token" autofocus>
<button type="submit">Log in</button></form></body></html>"""


APP_JS = r"""(function(){
var busy=false;
function msg(t,bad){var m=document.getElementById('msg');m.textContent=t||'';m.className=bad?'bad':'';}
function ageTick(){var e=document.getElementById('age');if(!e)return;var a=Date.now()/1000-(+e.dataset.ts),
m=Math.floor(a/60),s=a>(+e.dataset.stale);e.className='age '+(s?'stale':'fresh');e.querySelector('b').textContent=
s?'STALE':'fresh';var n=e.childNodes[1];if(n)n.textContent=n.textContent.replace(/, \d+ min old/,', '+m+' min old');}
function opened(){var o={};document.querySelectorAll('details[open]').forEach(function(d){o[d.dataset.key]=1});return o;}
function typing(){var a=document.activeElement;if(a&&a.tagName==='INPUT'&&a.name!=='showclosed')return true;
var t=false;document.querySelectorAll('main input:not([type=checkbox])').forEach(function(i){if(i.value)t=true});return t;}
function refresh(fresh){if(busy||(!fresh&&typing()))return;var o=opened(),sc=document.getElementById('showclosed');
var showc=sc&&sc.checked;fetch('/view'+(fresh?'?fresh=1':''),{credentials:'same-origin',cache:'no-store'})
.then(function(r){if(r.status===401){location.reload();throw 0;}if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
.then(function(j){document.getElementById('hdr').innerHTML=j.header;document.getElementById('main').innerHTML=j.main;
document.querySelectorAll('details').forEach(function(d){if(o[d.dataset.key])d.open=true});
var s2=document.getElementById('showclosed');if(s2){s2.checked=showc;apply();}ageTick();})
.catch(function(e){if(e)msg('The PC does not answer. The page shows the last snapshot.',true);ageTick();});}
function apply(){var c=document.getElementById('showclosed');document.querySelectorAll('.tree .closed').forEach(
function(r){r.style.display=c&&c.checked?'block':''});}
function post(body){busy=true;msg('working: '+body.act+' ...');return fetch('/api/act',{method:'POST',
credentials:'same-origin',headers:{'Content-Type':'application/json','X-Hub-Web':'1'},body:JSON.stringify(body)})
.then(function(r){return r.json().then(function(j){j.http=r.status;return j;});})
.finally(function(){busy=false;});}
document.addEventListener('change',function(e){if(e.target.id==='showclosed')apply();});
document.addEventListener('click',function(e){var b=e.target.closest('button[data-act]');if(!b)return;
e.preventDefault();var d=b.dataset,box=b.closest('.acts')||b.parentNode,body={act:d.act};
['id','sid','key','action','mult'].forEach(function(k){if(d[k]!==undefined)body[k]=d[k];});
box.querySelectorAll('input').forEach(function(i){if(i.value)body[i.name]=i.value;});
post(body).then(function(j){
 if(d.act==='group_preview'&&j.ok){var p=j.preview||{};var c=document.createElement('button');c.type='button';
  c.className='confirm';c.dataset.act='group';c.dataset.sid=d.sid;c.dataset.action=d.action;c.textContent=
  'Confirm: '+(p.text||d.action);box.appendChild(c);msg((p.text||'')+'\n'+(p['do']||[]).map(function(x){return x.join(': ')}).join('\n'));return;}
 msg((j.ok?'done: ':'refused: ')+(j.out||j.error||''),!j.ok);refresh(true);
}).catch(function(){msg('The PC does not answer. Nothing was done.',true);});});
setInterval(ageTick,15000);setInterval(function(){refresh(false)},30000);
})();"""


# ---------------------------------------------------------------- the token and the login

def _token_path():
    return os.path.join(T.STATE, TOKEN_FILE)


def read_token():
    """The token: env HUBTREE_WEB_TOKEN first, then the file <state>/web_token. A short token counts as none."""
    v = os.environ.get(TOKEN_ENV)
    if not v or len(v) < TOKEN_MIN:
        try:
            with open(_token_path(), encoding="utf-8") as f:
                v = f.read().strip()
        except OSError:
            v = None
    return v if v and len(v) >= TOKEN_MIN else None


def write_token(force=False):
    """Make a new token in <state>/web_token (mode 0600 where the OS supports it). Returns the path, or None
    when a file exists and force is false."""
    path = _token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not force:
        return None
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secrets.token_urlsafe(32))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def ensure_token():
    """Make sure a token exists. Returns the path of a new file, or None when a token existed already."""
    return None if read_token() else write_token(force=True)


def sign(token, issued):
    return hmac.new(token.encode(), f"hubweb|{issued}".encode(), hashlib.sha256).hexdigest()


def make_cookie(token, now=None):
    issued = int(now if now is not None else time.time())
    return f"{issued}.{sign(token, issued)}"


def cookie_ok(token, value, now=None, days=30):
    try:
        issued_s, mac = value.split(".", 1)
        issued = int(issued_s)
    except (ValueError, AttributeError):
        return False
    now = now if now is not None else time.time()
    if issued > now + 60 or now - issued > days * 86400:
        return False
    return hmac.compare_digest(mac, sign(token, issued))


def cmd_token(a):
    if a.check:
        t = read_token()
        print(json.dumps({"ok": bool(t), "set": bool(t),
                          "fingerprint": hashlib.sha256(t.encode()).hexdigest()[:8] if t else None}))
        return 0 if t else 1
    if a.new:
        print(json.dumps({"ok": True, "file": write_token(force=True), "next": "hubtree web token prints it"}))
        return 0
    ensure_token()
    print(read_token() or "")  # the one place that prints the token: the user asked for it
    return 0


# ---------------------------------------------------------------- actions

RID = re.compile(r"^\d{1,9}$")


def _text(v, n):
    v = " ".join(str(v or "").split())
    return v[:n]


def _sid(v):
    v = str(v or "")
    s = T.sessions().get(v)
    if not s or s.get("deleted"):
        raise ValueError("unknown session")
    return s


def action_argv(body):
    """(argv, name) for one action, or ValueError. Every input is checked here; the CLIs check again."""
    act = body.get("act")
    if act in ("accept", "reject"):
        rid = str(body.get("id") or "")
        if not RID.match(rid):
            raise ValueError("bad result id")
        if act == "accept":
            return [PY, SWARM, "accept", rid], f"result {rid}"
        return [PY, SWARM, "reject", rid, "--why", _text(body.get("why"), 300)], f"result {rid}"
    if act in ("group_preview", "group"):
        action = body.get("action")
        if action not in ("pause", "resume"):
            raise ValueError("group action must be pause or resume")
        s = _sid(body.get("sid"))
        return ([PY, SWARM, "group", action, "--root", s["session_id"]] + (["--yes"] if act == "group" else []),
                s.get("name"))
    if act == "budget":
        s = _sid(body.get("sid"))
        argv = [PY, OBS, "budget", "--sid", s["session_id"], "--by", "web page"]
        if body.get("mult") is not None:
            m = float(body["mult"])
            if not 1 < m <= 10:
                raise ValueError("mult must be above 1 and at most 10")
            return argv + ["--mult", str(m)], s.get("name")
        usd = float(body.get("usd") or 0)
        if not 0 < usd <= 1000:
            raise ValueError("usd must be above 0 and at most 1000")
        return argv + ["--usd", str(usd)], s.get("name")
    if act == "message":
        s = _sid(body.get("sid"))
        text = str(body.get("text") or "").strip()[:2000]
        if not text:
            raise ValueError("empty message")
        return [PY, os.path.abspath(__file__), "message", "--sid", s["session_id"], "--text", text], s.get("name")
    raise ValueError(f"unknown action {act!r}")


def do_action(body, who="web"):
    try:
        argv, name = action_argv(body)
    except (ValueError, TypeError) as e:
        rec = {"t": time.time(), "act": body.get("act"), "ok": False, "error": str(e), "who": who}
        log_action(rec)
        return 400, {"ok": False, "error": str(e)}
    t0 = time.time()
    code, out, err = RUN[0](argv, timeout=240)
    text = (out.strip() + ("\n" + err.strip() if err.strip() else "")).strip()
    res = _json_tail(out)
    ok = code == 0 and not (isinstance(res, dict) and res.get("ok") is False)
    log_action({"t": t0, "act": body.get("act"), "name": name, "args": redact([str(x) for x in argv[2:]]),
                "exit": code, "ok": ok, "out": redact(text[-400:]), "secs": round(time.time() - t0, 1), "who": who})
    out_body = {"ok": ok, "exit": code, "out": redact(text[-600:])}
    if body.get("act") == "group_preview" and isinstance(res, dict):
        out_body["preview"] = res.get("preview")
    return 200, out_body


def cmd_message(a):
    import swarm as SW
    s = SW.find(a.sid)
    if not s:
        print(json.dumps({"ok": False, "error": f"no session {a.sid}"}))
        return 1
    how = SW.deliver(s, f"Message from the user (web page): {a.text}", by="web")
    SW.log_action("message", s, f"message from the web page: {how}", by="web", note=a.text[:200])
    ok = not str(how).startswith("failed")
    print(json.dumps({"ok": ok, "delivered": how}))
    return 0 if ok else 1


# ---------------------------------------------------------------- the server

class State:
    def __init__(self, port, build=None):
        self.port = port
        self.build = build or build_snapshot
        self.snap = None
        self.lock = threading.Lock()
        self.fails = []        # times of failed logins
        self.tok = (0.0, None)

    def token(self):
        t, v = self.tok
        if time.time() - t > 30:
            self.tok = (time.time(), read_token())
        return self.tok[1]

    def snapshot(self, fresh=False):
        with self.lock:
            if fresh or self.snap is None or time.time() - self.snap["ts"] > cfg()["cache_s"]:
                self.snap = self.build()
            return self.snap

    def locked(self, now):
        self.fails = [t for t in self.fails if now - t < 600]
        return len(self.fails) >= 10


class Handler(BaseHTTPRequestHandler):
    server_version = "hubweb"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ----- plumbing
    @property
    def st(self):
        return self.server.state

    def host_ok(self):
        h = (self.headers.get("Host") or "").strip().lower()
        p = self.st.port
        ok = {f"127.0.0.1:{p}", f"localhost:{p}"}
        for n in cfg()["hosts"]:
            n = str(n).lower()
            ok |= {n, f"{n}:443"}
        return h in ok

    def https(self):
        h = (self.headers.get("Host") or "").split(":")[0].lower()
        return h not in ("127.0.0.1", "localhost")

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")  # no-referrer makes Chrome send Origin: null on a form POST
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def jsend(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def authed(self):
        tok = self.st.token()
        if not tok:
            return False
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), tok.encode()):
            return True
        return cookie_ok(tok, self.cookies().get(COOKIE, ""), days=cfg()["cookie_days"])

    def origin_ok(self):
        o = self.headers.get("Origin")
        if not o:
            return True
        want = ("https://" if self.https() else "http://") + (self.headers.get("Host") or "")
        return o.rstrip("/").lower() == want.lower() or o.rstrip("/").lower() == want.lower().replace(":443", "")

    def body(self, limit=16384):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def refuse(self, api):
        if api:
            self.jsend(401, {"ok": False, "error": "log in first"})
        else:
            msg = "Type the token." if self.st.token() else "No token is set on the PC."
            self.send(401, LOGIN.replace("%MSG%", msg))

    # ----- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.host_ok():
            return self.send(421, "wrong host", "text/plain")
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/login":
            return self.refuse(False) if not self.authed() else self.send(303, "", headers={"Location": "/"})
        if not self.authed():
            return self.refuse(path.startswith("/api") or path == "/view")
        if path == "/":
            return self.send(200, page(self.st.snapshot(), live=True))
        if path == "/view":
            snap = self.st.snapshot(fresh=q.get("fresh") == ["1"])
            return self.jsend(200, {"ts": snap["ts"], "header": header_html(snap, time.time(), True),
                                    "main": main_html(snap, True)})
        if path == "/app.js":
            return self.send(200, APP_JS, "text/javascript; charset=utf-8")
        if path == "/api/snapshot":
            return self.jsend(200, self.st.snapshot(fresh=q.get("fresh") == ["1"]))
        return self.send(404, "not found", "text/plain")

    def do_POST(self):
        if not self.host_ok():
            return self.send(421, "wrong host", "text/plain")
        path = urlparse(self.path).path
        if not self.origin_ok():
            return self.send(403, "wrong origin", "text/plain")
        try:
            raw = self.body()
        except ValueError:
            return self.send(413, "too large", "text/plain")
        if path == "/login":
            return self.login(raw)
        if path == "/logout":
            return self.send(303, "", headers={"Location": "/login", "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
        if not self.authed():
            return self.refuse(True)
        if path != "/api/act":
            return self.send(404, "not found", "text/plain")
        if self.headers.get("X-Hub-Web") != "1":
            return self.jsend(403, {"ok": False, "error": "missing X-Hub-Web header"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.jsend(400, {"ok": False, "error": "bad JSON"})
        code, res = do_action(body, who=self.headers.get("Host", ""))
        self.st.snap = None if res.get("ok") and body.get("act") != "group_preview" else self.st.snap
        return self.jsend(code, res)

    def login(self, raw):
        now = time.time()
        if self.st.locked(now):
            return self.send(429, LOGIN.replace("%MSG%", "Too many failed logins. Wait 10 minutes."))
        tok = self.st.token()
        given = (parse_qs(raw.decode("utf-8", "replace")).get("token") or [""])[0].strip()
        good = bool(tok and given and hmac.compare_digest(given.encode(), tok.encode()))
        if not good:
            self.st.fails.append(now)
            log_action({"t": now, "act": "login", "ok": False, "who": self.headers.get("Host", "")})
            return self.send(401, LOGIN.replace("%MSG%", "Wrong token or code."))
        flags = "HttpOnly; SameSite=Strict; Path=/; Max-Age=" + str(cfg()["cookie_days"] * 86400)
        if self.https():
            flags += "; Secure"
        log_action({"t": now, "act": "login", "ok": True, "who": self.headers.get("Host", "")})
        return self.send(303, "", headers={"Location": "/", "Set-Cookie": f"{COOKIE}={make_cookie(tok, now)}; {flags}"})


def make_server(port, build=None):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # 127.0.0.1 only, by design
    srv.daemon_threads = True
    srv.state = State(srv.server_address[1], build)  # port 0 (tests): the port that the OS gave
    return srv


def serve(port=None):
    """Run the page until the process ends (the daemon calls this when [web] enabled = true). Returns an exit code."""
    port = port or cfg()["port"]
    try:
        srv = make_server(port)
    except OSError as e:
        print(json.dumps({"ok": False, "error": f"port {port}: {e}"}))
        return 2
    made = ensure_token()
    if made:
        print(json.dumps({"ok": True, "token_file": made, "note": "login token written; `hubtree web token` prints it"}),
              flush=True)
    os.makedirs(wdir(), exist_ok=True)
    with open(wdir("server.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": port, "t": time.time(), "token_set": bool(read_token())}, f)
    try:
        srv.serve_forever(poll_interval=1.0)
    finally:
        srv.server_close()
    return 0


def cmd_serve(a):
    return serve(a.port)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="swarm web: a private page of the session tree")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    sn = sub.add_parser("snapshot")
    sn.add_argument("--out")
    r = sub.add_parser("render")
    r.add_argument("--out", required=True)
    r.add_argument("--live", action="store_true", help="the served page (buttons), for screenshots")
    r.add_argument("--age-min", type=float, default=0.0, help="make the snapshot this many minutes old")
    m = sub.add_parser("message")
    m.add_argument("--sid", required=True)
    m.add_argument("--text", required=True)
    t = sub.add_parser("token")
    t.add_argument("--new", action="store_true")
    t.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        return cmd_serve(a)
    if a.cmd == "message":
        return cmd_message(a)
    if a.cmd == "token":
        return cmd_token(a)
    t0 = time.time()
    snap = build_snapshot()
    if a.cmd == "render":
        snap["ts"] -= a.age_min * 60
        write_atomic(a.out, page(snap if a.live else public_copy(snap), live=a.live))
        print(json.dumps({"ok": True, "out": a.out}))
        return 0
    res = save_snapshot(snap)
    if a.out:
        write_atomic(a.out, json.dumps(snap, ensure_ascii=False))
    res.update(ok=not snap["errors"], secs=round(time.time() - t0, 2))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
