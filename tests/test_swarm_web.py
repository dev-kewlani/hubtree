"""Tests of swarm_web.py: the login and its refusals, the token file, the actions and their checks, the renderer
(stale mark, the read-only copy), the snapshot with stub tools. Temporary state only; the server runs on a free port
with a stub snapshot and a stub runner, so no real session is touched.

Run: python -m pytest tests/test_swarm_web.py -q
"""
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request

import swarm_web as W  # noqa: E402
import tree_lib as T  # noqa: E402

TOKEN = "t" * 40


def check(name, cond, detail=""):
    assert cond, name + (f"  ({detail})" if detail else "")


def row(name, state, depth, kind="session", **kw):
    return dict({"depth": depth, "kind": kind, "label": name.split("/")[-1], "name": name, "state": state,
                 "sid": "sid-" + name.replace("/", "-")}, **kw)


def snap(ts=None):
    rows = [{"depth": 0, "kind": "folder", "label": "hub/", "name": "hub", "usd": 1.5, "tok": 10},
            row("hub/a", "tab", 1, usd=1.0, usd_sub=1.5, activity=["step: running Bash: secret-ish"],
                agent_lines=["agent x"], cwd="D:/x"),
            row("hub/a/b~auto", "you", 2, usd=0.5, usd_sub=0.5, why="waits for you",
                budget={"text": "budget 50%: $0.50 of $1.00", "level": None}),
            row("hub/c", "closed", 1, usd=0.0, usd_sub=0.0)]
    return {"ts": ts if ts is not None else time.time(), "rows": rows, "counts": {"tab": 1, "you": 1, "closed": 1},
            "results": [{"id": 7, "child": "sid-hub-a-b~auto", "child_name": "hub/a/b~auto", "ts": time.time(),
                         "body": {"done": "did it", "proof": "ran it"}, "status": "open"}],
            "actions": [], "errors": []}


class Env:
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="swarmweb_")
        self.saved = (T.STATE, W.RUN[0], os.environ.get(W.TOKEN_ENV), T.sessions)
        T.STATE = self.dir
        os.environ[W.TOKEN_ENV] = TOKEN
        self.calls = []

        def run(argv, timeout=120):
            self.calls.append(argv)
            if "group" in argv:
                return 0, json.dumps({"ok": True, "preview": {"text": "pause 2 sessions?", "do": [["hub/a", "x"]]}}), ""
            return 0, json.dumps({"ok": True}), ""
        W.RUN[0] = run
        T.sessions = lambda: {"sid-hub-a": {"session_id": "sid-hub-a", "name": "hub/a"}}
        return self

    def __exit__(self, *a):
        T.STATE, W.RUN[0], tok, T.sessions = self.saved
        if tok is None:
            os.environ.pop(W.TOKEN_ENV, None)
        else:
            os.environ[W.TOKEN_ENV] = tok
        shutil.rmtree(self.dir, ignore_errors=True)


def req(port, path, method="GET", body=None, headers=None, host=None):
    h = dict(headers or {})
    if host:
        h["Host"] = host
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=h)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        with op.open(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def test_cookie():
    c = W.make_cookie(TOKEN, now=1000)
    check("cookie: a fresh cookie is good", W.cookie_ok(TOKEN, c, now=1000))
    check("cookie: another token refuses it", not W.cookie_ok("x" * 40, c, now=1000))
    check("cookie: a changed mac is refused", not W.cookie_ok(TOKEN, c[:-1] + ("0" if c[-1] != "0" else "1"), now=1000))
    check("cookie: older than 30 days is refused", not W.cookie_ok(TOKEN, c, now=1000 + 31 * 86400))
    check("cookie: junk is refused", not W.cookie_ok(TOKEN, "abc", now=1000))


def test_render():
    now = time.time()
    live = W.page(snap(now), live=True, now=now)
    check("render: fresh mark", 'class="age fresh"' in live)
    check("render: live page has buttons and the live step", 'data-act="accept"' in live and "secret-ish" in live)
    old = W.page(W.public_copy(snap(now - 11 * 60)), live=False, now=now)
    check("render: a snapshot 11 minutes old is STALE", 'class="age stale"' in old and "<b>STALE</b>" in old)
    check("render: 9 minutes old is fresh", 'class="age fresh"' in W.page(snap(now - 9 * 60), live=False, now=now))
    check("render: the read-only copy has no buttons", "data-act=" not in old)
    check("render: the read-only copy has no live step, agents or folders",
          "secret-ish" not in old and "agent x" not in old and "D:/x" not in old)
    check("render: the read-only copy has its age script", "<script>" in old and "/app.js" not in old)
    keep = W.shown_rows(snap()["rows"])
    check("render: closed rows hide, live rows and ancestors stay", keep == [True, True, True, False], str(keep))
    check("render: text is escaped", "&lt;b&gt;" in W.page(dict(snap(now), errors=["<b>x</b>"]), now=now))


def test_actions():
    with Env() as e:
        bad = [{"act": "accept", "id": "1; rm"}, {"act": "nope"}, {"act": "budget", "sid": "sid-hub-a", "mult": "50"},
               {"act": "budget", "sid": "unknown", "mult": "2"},
               {"act": "group", "sid": "sid-hub-a", "action": "stop"}, {"act": "message", "sid": "sid-hub-a", "text": " "}]
        for b in bad:
            code, res = W.do_action(b)
            check(f"action refused: {b}", code == 400 and not res["ok"])
        check("actions: no child ran for a refused action", e.calls == [])
        W.do_action({"act": "accept", "id": "7"})
        check("accept runs swarm.py accept with no --force", e.calls[-1][1:] == [W.SWARM, "accept", "7"])
        code, res = W.do_action({"act": "group_preview", "sid": "sid-hub-a", "action": "pause"})
        check("group preview has no --yes and returns the preview",
              "--yes" not in e.calls[-1] and res.get("preview", {}).get("text") == "pause 2 sessions?")
        W.do_action({"act": "group", "sid": "sid-hub-a", "action": "pause"})
        check("group adds --yes", e.calls[-1][-1] == "--yes")
        W.do_action({"act": "budget", "sid": "sid-hub-a", "mult": "2"})
        check("budget x2 runs swarm_obs.py budget --mult", e.calls[-1][1] == W.OBS and "--mult" in e.calls[-1])
        W.do_action({"act": "message", "sid": "sid-hub-a", "text": "hello"})
        check("message runs swarm_web.py message", e.calls[-1][2] == "message" and e.calls[-1][-1] == "hello")
        with open(W.wdir("actions.jsonl"), encoding="utf-8") as f:
            lines = [json.loads(x) for x in f]
        check("every action has one log line", len(lines) == len(bad) + 5, str(len(lines)))
        env = W._child_env()
        check("children get no session id and no token", "CLAUDE_CODE_SESSION_ID" not in env and W.TOKEN_ENV not in env)


def test_server():
    with Env() as e:
        srv = W.make_server(0, build=lambda: snap())
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            b = {"Authorization": "Bearer " + TOKEN}
            check("server: healthz without login", req(port, "/healthz")[0] == 200)
            for p in ("/", "/view", "/api/snapshot", "/app.js"):
                code, body, _ = req(port, p)
                check(f"server: anonymous {p} is 401 with no data", code == 401 and "hub/a" not in body, str(code))
            code, body, _ = req(port, "/api/act", "POST", b'{"act":"accept","id":"7"}', {"X-Hub-Web": "1"})
            check("server: anonymous action is 401 and runs nothing", code == 401 and e.calls == [])
            check("server: wrong bearer is 401", req(port, "/api/snapshot", headers={"Authorization": "Bearer " + "x" * 40})[0] == 401)
            check("server: bearer gets the snapshot", req(port, "/api/snapshot", headers=b)[0] == 200)
            check("server: wrong Host is 421", req(port, "/api/snapshot", headers=b, host="evil.example.com")[0] == 421)
            code, _, h = req(port, "/", headers=b)
            check("server: security headers", "frame-ancestors 'none'" in h.get("Content-Security-Policy", "")
                  and h.get("Cache-Control") == "no-store")
            act = dict(b, **{"X-Hub-Web": "1", "Content-Type": "application/json"})
            check("server: a cross-site Origin is 403",
                  req(port, "/api/act", "POST", b'{"act":"accept","id":"7"}', dict(act, Origin="https://evil.example.com"))[0] == 403)
            check("server: no X-Hub-Web header is 403",
                  req(port, "/api/act", "POST", b'{"act":"accept","id":"7"}', b)[0] == 403)
            code, body, _ = req(port, "/api/act", "POST", b'{"act":"accept","id":"7"}',
                                dict(act, Origin=f"http://127.0.0.1:{port}"))
            check("server: a same-origin action runs", code == 200 and json.loads(body)["ok"] and len(e.calls) == 1)
            code, _, h = req(port, "/login", "POST", f"token={TOKEN}".encode())
            ck = h.get("Set-Cookie", "")
            check("server: the token logs in", code == 303 and "HttpOnly" in ck and "SameSite=Strict" in ck)
            check("server: no Secure flag on 127.0.0.1 (plain http)", "Secure" not in ck)
            cookie = ck.split(";")[0]
            check("server: the cookie opens the page", req(port, "/", headers={"Cookie": cookie})[0] == 200)
            for _ in range(10):
                req(port, "/login", "POST", b"token=wrong")
            check("server: 10 failed logins lock the login (429)",
                  req(port, "/login", "POST", f"token={TOKEN}".encode())[0] == 429)
        finally:
            srv.shutdown()
            srv.server_close()
    with Env():
        os.environ[W.TOKEN_ENV] = "short"
        srv = W.make_server(0, build=lambda: snap())
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            check("server: a short token counts as none: all refused",
                  req(port, "/api/snapshot", headers={"Authorization": "Bearer short"})[0] == 401)
        finally:
            srv.shutdown()
            srv.server_close()


def test_snapshot():
    with Env() as e:
        def run(argv, timeout=120):
            if argv[1] == W.TREE_VIEW:
                return 0, json.dumps({"rows": snap()["rows"], "counts": {"tab": 1}}), ""
            return 1, "", "boom"
        W.RUN[0] = run
        s = W.build_snapshot()
        check("snapshot: rows from tree_view", len(s["rows"]) == 4)
        check("snapshot: a failed tool is an error line, not a crash",
              s["results"] == [] and any("swarm.py results" in x for x in s["errors"]))
        out = W.save_snapshot(s)
        check("snapshot: snapshot.json is written", os.path.exists(W.wdir("snapshot.json")) and out["rows"] == 4)


def test_token_file():
    with Env() as e:
        os.environ.pop(W.TOKEN_ENV, None)
        check("token: none at first", W.read_token() is None)
        made = W.ensure_token()
        path = os.path.join(e.dir, W.TOKEN_FILE)
        check("token: the first call writes <state>/web_token", made == path and os.path.exists(path))
        tok = W.read_token()
        check("token: the file token is long enough", tok is not None and len(tok) >= W.TOKEN_MIN)
        check("token: a second call keeps it", W.ensure_token() is None and W.read_token() == tok)
        if os.name != "nt":
            check("token: the file mode is 0600", (os.stat(path).st_mode & 0o777) == 0o600)
        check("token: the environment wins", (os.environ.update({W.TOKEN_ENV: "e" * 40}) or W.read_token()) == "e" * 40)
        os.environ.pop(W.TOKEN_ENV, None)
        check("token: a new token replaces the file", W.write_token(force=True) == path and W.read_token() != tok)
