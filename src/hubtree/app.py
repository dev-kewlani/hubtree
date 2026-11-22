"""hubtree: a live tree of all sessions, in its own terminal tab. It uses no tokens.

Run: hubtree   (or python app.py)

Tree: folder nodes (root > folder > sub-folder), then sessions, then each session's children.
Sessions from before the registry (old transcripts) show under their node with the mark "old".
State marks: [tab] open in a tab, [work] background and working, [YOU] needs you, [idle] background and idle,
             [STUCK] busy with no transcript write for its limit, [died] stopped in the middle of a turn,
             [closed] closed (open it again with Enter), [old] old transcript. A folder or parent row shows
             the count of waiting sessions under it, for example "(1 stuck, 2 YOU)".
Keys: enter open | n new child (tab) | f fork | a auto child | N new session at a folder node
      r rename | m move to another folder | * make root | s stop a background session
      d hide or unhide | D delete | H show hidden | t sub-tree tab
      w only the sessions that wait for you | / filter | : command | q quit
Swarm control (swarm.py): g start a queued row now | x cancel a queued row
      A accept the result of the selected child (closes it) | C ask it for a change | X reject the result
      G group action on the selected session and its descendants, or on a folder: pause, resume, stop, hide,
      broadcast <words>; a preview count asks for yes first. Marks: [queued] [PAUSED], "3 results", CONFLICT.
"""
import glob
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_lib as T  # noqa: E402
import term  # noqa: E402
from open_session import open_tab  # noqa: E402
import swarm  # noqa: E402  queue, claims, results, group actions
try:
    import swarm_obs as O  # noqa: E402  cost, budgets, live step, agents, flags
except Exception:  # the app never fails on the swarm observe layer
    O = None

from rich.markup import escape  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.widgets import Footer, Header, Input, Static, Tree  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = T.PROJECTS
SPAWN = os.path.join(HERE, "spawn.py")
MARK = {"tab": ("[tab]", "green"), "work": ("[work]", "cyan"), "you": ("[YOU]", "bold red"),
        "idle": ("[idle]", "yellow"), "closed": ("[closed]", "grey50"), "old": ("[old]", "grey37"),
        "stuck": ("[STUCK]", "bold magenta"), "died": ("[died]", "bold red"),
        "queued": ("[queued]", "blue"), "paused": ("[PAUSED]", "bold yellow")}


_OLD_CACHE = {}   # transcript path -> (mtime, record)
_HEAD_CACHE = {}  # transcript path -> (first title, cwd): the start of a file does not change


def _head(p):
    """The first title and the working folder, from the start of the file (read once per file)."""
    if p in _HEAD_CACHE:
        return _HEAD_CACHE[p]
    title, cwd, first = "", None, ""
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if not first and '"type":"user"' in line and '"isMeta":true' not in line:
                    try:
                        c = (json.loads(line).get("message") or {}).get("content")
                        txt = c if isinstance(c, str) else " ".join(
                            x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
                        txt = " ".join(txt.split())
                        if txt and not txt.startswith("<"):
                            first = " ".join(txt.split()[:7])
                    except ValueError:
                        pass
                if cwd is None and '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        pass
                if not title and ('"ai-title"' in line or '"custom-title"' in line):
                    try:
                        o = json.loads(line)
                        title = o.get("customTitle") or o.get("aiTitle") or ""
                    except ValueError:
                        pass
                if (cwd and title and first) or i > 20000:
                    break
    except OSError:
        pass
    _HEAD_CACHE[p] = (title or first, cwd)
    return title or first, cwd


def _tail_title(p, size=2_000_000):
    """The title that the resume picker shows: the newest custom title (a rename), else the newest AI title."""
    try:
        n = os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(max(0, n - size))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    custom = ai = ""
    for line in reversed(lines):
        if not custom and '"custom-title"' in line:
            try:
                custom = json.loads(line).get("customTitle") or ""
            except ValueError:
                pass
        elif not ai and '"ai-title"' in line:
            try:
                ai = json.loads(line).get("aiTitle") or ""
            except ValueError:
                pass
        if custom:
            break
    return custom or ai


def old_transcripts(known_ids):
    """Transcripts that the registry does not know: {session_id: {...}} with a title and cwd. Cached by mtime."""
    out = {}
    for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        sid = os.path.basename(p)[:-6]
        if sid in known_ids:
            continue
        if sid in out and os.path.getsize(out[sid]["transcript"]) >= os.path.getsize(p):
            continue  # a copy made for a resume in a moved folder: keep the larger one
        mt = os.path.getmtime(p)
        hit = _OLD_CACHE.get(p)
        if hit and hit[0] == mt:
            out[sid] = hit[1]
            continue
        first, cwd = _head(p)
        title = _tail_title(p) or first
        node, level, _ = T.node_of(cwd)
        rec = {"session_id": sid, "name": f"{node}/{T.slug(title or sid[:8])}", "title": title, "node": node,
               "level": level + 1, "cwd": cwd, "old": True, "open": False, "transcript": p,
               "last": datetime.fromtimestamp(mt).isoformat(timespec="seconds")}
        _OLD_CACHE[p] = (mt, rec)
        out[sid] = rec
    return out


def last_lines(transcript, n=6):
    """The last few user and assistant text lines of a transcript, for the detail panel."""
    if not transcript or not os.path.exists(transcript):
        return []
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", errors="ignore").splitlines()[1:]
    except OSError:
        return []
    out = []
    for line in reversed(tail):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") not in ("user", "assistant") or o.get("isMeta"):
            continue
        c = (o.get("message") or {}).get("content")
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
        text = " ".join(text.split())
        if text and not text.startswith("<"):
            out.append(f"{o['type']}: {text[:220]}")
        if len(out) >= n:
            break
    return list(reversed(out))


AREA_ORDER = []  # folders sort by name; list folder names here to pin their order


def _neg(s):
    """Sort key that puts a newer ISO time first in an ascending sort."""
    return "".join(chr(0x10FFFF - ord(c)) for c in s)


class HubTree(App):
    TITLE = "hubtree"
    SUB_TITLE = "the live tree of sessions"
    CSS = """
    #tree { width: 60%; border: round $accent; }
    #side { width: 40%; }
    #detail { height: 1fr; border: round $primary; padding: 0 1; }
    #cmd { display: none; }
    #cmd.show { display: block; }
    """
    BINDINGS = [
        Binding("o", "open", "Open"), Binding("n", "child", "New child"), Binding("f", "fork", "Fork"),
        Binding("a", "auto", "Auto child"), Binding("N", "new_root", "New at folder"), Binding("r", "rename", "Rename"),
        Binding("s", "stop", "Stop bg"), Binding("d", "hide", "Hide/unhide"), Binding("D", "delete", "Delete"),
        Binding("H", "toggle_hidden", "Show hidden"), Binding("t", "subtree", "Sub-tree tab"),
        Binding("m", "move", "Move"), Binding("asterisk", "make_root", "Make root"), Binding("w", "waiting", "Waits for you"), Binding("slash", "filter", "Filter"), Binding("colon", "command", "Command"),
        Binding("q", "quit", "Quit"),
        Binding("dollar_sign", "raise_budget", "Budget"), Binding("e", "agents", "Agents"),
        Binding("g", "queue_start", "Start queued"), Binding("x", "queue_cancel", "Cancel queued"),
        Binding("A", "result_accept", "Accept result"), Binding("C", "result_change", "Ask change"),
        Binding("X", "result_reject", "Reject result"), Binding("G", "group", "Group action"),
    ]

    def __init__(self, scope="hub"):
        super().__init__()
        self.scope = (scope or "hub").strip("/\\") or "hub"
        if self.scope != "hub":
            self.SUB_TITLE = f"sub-tree: {self.scope}"
        self.live, self.data, self.filter_text, self.pending, self.prev_state = {}, {}, "", None, {}
        self.last_sig, self.show_hidden = None, False
        self.health, self.below, self.only_waiting = {}, {}, False
        self.lock = threading.Lock()
        self.ctl = {}  # swarm.row_extras: results, result, conflict, wait, paused per session id
        self.obs, self.cost_sub = {}, {}  # swarm_obs.info per session id; usd under each tree key

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree(T.HUB if self.scope == "hub" else self.scope + "/", id="tree")
            with Vertical(id="side"):
                yield Static("Select a session.", id="detail")
                yield Input(placeholder="", id="cmd")
        yield Footer()

    def on_mount(self):
        self.query_one(Tree).root.expand()
        self.poll()
        self.set_interval(3, self.poll)

    # ---------- data ----------
    def poll(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        allreg = T.sessions()
        gone = {k for k, v in allreg.items() if v.get("deleted")}
        named = {k for k, v in allreg.items() if v.get("name")}
        old = old_transcripts(named | gone)
        live = T.live_agents()
        data = dict(old)
        for sid, v in allreg.items():
            if sid in gone:
                continue
            tp = v.get("transcript")
            if tp and not os.path.exists(tp) and sid not in live:  # never saved, or deleted outside the app
                continue
            merged = {**data.get(sid, {}), **v} if v.get("name") or sid in data else None
            if merged and merged.get("old") and v.get("moved") and not v.get("name"):
                merged["name"] = f"{merged['node']}/{T.slug(merged.get('title') or sid[:8])}"
            data[sid] = merged
        data = {k: v for k, v in data.items() if v}
        for v in data.values():
            tp = v.get("transcript")
            # all in UTC, so that the text sort is a time sort (registry times are UTC)
            try:  # a delete can remove the file between two checks
                mt = datetime.fromtimestamp(os.path.getmtime(tp), timezone.utc).isoformat(timespec="seconds") \
                    if tp else ""
            except OSError:
                mt = ""
            v["recent"] = mt or (v.get("last") if not v.get("old") else "") or v.get("created") or ""
        for sid, a in live.items():  # live sessions with no transcript and no registry entry yet
            if sid and sid not in data:
                node, level, _ = T.node_of(a.get("cwd"))
                data[sid] = {"session_id": sid, "name": f"{node}/{T.slug(a.get('name') or sid[:8])}", "node": node,
                             "level": level + 1, "cwd": a.get("cwd"), "old": True, "title": a.get("name")}
        health = {sid: T.health(v, live) for sid, v in data.items()}  # file reads stay off the UI thread
        ctl = swarm.row_extras(data, live, alert=True)  # also one message per conflict to each parent
        obs = self.load_obs(data, live)
        with self.lock:
            self.ctl = ctl
            self.obs = obs
            self.data = data
            self.live = live
            self.health = health
        self.call_from_thread(self.rebuild)

    def state(self, s):
        """tree_lib.health, computed in _load: tab, work, you, idle, stuck, died, closed or old."""
        return self.health.get(s["session_id"], ("old" if s.get("old") else "closed", {}))[0]

    def why(self, s):
        st, info = self.health.get(s["session_id"], ("closed", {}))
        return T.why(st, info)

    # ---------- tree ----------
    def label(self, s):
        mk, col = MARK[self.state(s)]
        nm = s.get("name") or s["session_id"][:8]
        t = Text()
        t.append(mk, style=col)
        t.append(" " + nm.split("/")[-1])
        if s.get("fork"):
            t.append(" (fork)", style="italic")
        if self.is_root(s):
            t.append(" (root)", style="bold magenta")
        if s.get("hidden"):
            t.append(" (hidden)", style="grey50")
        if s.get("old"):
            t.append(f"  {(s.get('last') or '')[5:16].replace('T', ' ')}", style="grey50")
        if self.below.get(s["session_id"]):
            t.append(f"  ({self.below[s['session_id']]})", style="bold red")
        if self.state(s) in T.WAITING or self.state(s) in ("queued", "paused"):
            t.append(f"  {self.why(s)}", style="italic")
        self.ctl_label(t, s)
        self.obs_label(t, s)
        return t

    def ctl_label(self, t, s):
        """Swarm control tags: open child results, a result ready for the parent, a file conflict."""
        c = self.ctl.get(s["session_id"]) or {}
        if c.get("results"):
            t.append(f"  {c['results']} result{'s' if c['results'] != 1 else ''}", style="bold green")
        if c.get("result") == "open":
            t.append("  result ready", style="green")
        if c.get("conflict"):
            t.append(f"  CONFLICT {c['conflict']}", style="bold red")

    def folder_label(self, key, node):
        b = self.below.get(key)
        c = self.cost_sub.get(key) or 0.0
        cost = f"  [green]{O.fmt_usd(c)}[/green]" if O is not None and c >= 0.005 else ""
        return f"[b]{node.split('/')[-1]}[/b]/" + cost + (f"  [bold red]({b})[/bold red]" if b else "")

    @staticmethod
    def is_root(s):
        return bool(s.get("root")) or s.get("name") == (s.get("node") or "hub")

    def in_scope(self, node):
        return self.scope == "hub" or node == self.scope or node.startswith(self.scope + "/")

    def plan(self):
        """The wanted tree as (key, parent_key, kind, record) rows.

        In each folder: the root session first, then the other sessions, newest activity first, then the
        sub-folders in a fixed order (so folders never jump). Children of a session also go newest first.
        """
        recent = lambda s: s.get("recent") or s.get("last") or s.get("created") or ""  # noqa: E731
        kids, by_node = {}, {}
        flt = self.filter_text.lower()
        keep = None
        if self.only_waiting:  # a waiting session and every session above it
            keep = set()
            for sid, s in self.data.items():
                if self.state(s) in T.WAITING:
                    while sid and sid in self.data and sid not in keep:
                        keep.add(sid)
                        sid = self.data[sid].get("parent")
        for s in self.data.values():
            if s.get("hidden") and not self.show_hidden:
                continue
            if keep is not None and s["session_id"] not in keep:
                continue
            par = s.get("parent") if s.get("parent") in self.data else None
            if par:
                kids.setdefault(par, []).append(s)
                continue
            node = s.get("node") or "hub"
            if not self.in_scope(node):
                continue
            if flt and flt not in (s.get("name") or "").lower() and flt not in (s.get("title") or "").lower():
                continue
            by_node.setdefault(node, []).append(s)
        nodes = set()
        for node in by_node:
            parts = node.split("/")
            for i in range(1, len(parts) + 1):
                nodes.add("/".join(parts[:i]))
        nodes.discard("hub")
        order = {n: i for i, n in enumerate(AREA_ORDER)}
        out = []
        top = "hub" if self.scope == "hub" else self.scope
        top_key = "hub" if self.scope == "hub" else f"F:{self.scope}"

        def add(pk, s):
            out.append((s["session_id"], pk, "session", s))
            self.agent_plan(out, s)
            for c in sorted(kids.get(s["session_id"], []), key=recent, reverse=True):
                add(s["session_id"], c)

        def emit(node, key):
            ss = by_node.get(node, [])
            for s in sorted(ss, key=lambda s: (not self.is_root(s), "" if self.is_root(s) else _neg(recent(s)))):
                add(key, s)
            depth = 0 if node == "hub" else node.count("/") + 1
            subs = [n for n in nodes if n.count("/") + 1 == depth + 1 and (node == "hub" or n.startswith(node + "/"))]
            for n in sorted(subs, key=lambda n: (order.get(n, 99), n.lower())):
                k = f"F:{n}"
                out.append((k, key, "folder", n))
                emit(n, k)

        emit(top, top_key)
        return out

    def rebuild(self):
        tree = self.query_one(Tree)
        plan = self.plan()
        self.rollup(plan)
        sig = tuple((k, pk) for k, pk, _, _ in plan)
        obs_recs = {k: rec for k, _pk, kind, rec in plan if kind == "obs"}
        if sig == self.last_sig:
            # Same shape: change the labels in place, so the cursor and the folds stay where they are.
            for n in self._walk(tree.root):
                sid = n.data.get("sid") if n.data else None
                if sid and sid in self.data:
                    n.set_label(self.label(self.data[sid]))
                elif n.data and n.data.get("key", "").startswith("F:"):
                    n.set_label(self.folder_label(n.data["key"], n.data["folder"]))
                elif n.data and n.data.get("key") in obs_recs:  # an agent row: fresh counts and states
                    n.data["obs"] = obs_recs[n.data["key"]]
                    n.set_label(Text(obs_recs[n.data["key"]]["label"]))
            self.alerts()
            self.show_detail()
            return
        cur = tree.cursor_node.data.get("key") if tree.cursor_node and tree.cursor_node.data else None
        expanded = {n.data["key"] for n in self._walk(tree.root) if n.data and n.is_expanded}
        first = self.last_sig is None
        self.last_sig = sig
        tree.clear()
        top_key = "hub" if self.scope == "hub" else f"F:{self.scope}"
        tree.root.data = {"key": top_key, "folder": self.scope}
        nodes = {top_key: tree.root}
        for key, pk, kind, rec in plan:
            parent = nodes.get(pk, tree.root)
            if kind == "folder":
                n = parent.add(self.folder_label(key, rec), data={"key": key, "folder": rec})
            elif kind == "obs":  # an agent group, a workflow or one agent (swarm observe)
                n = parent.add(Text(rec["label"]), data={"key": key, "obs": rec})
            else:
                n = parent.add(self.label(rec), data={"key": key, "sid": key})
            nodes[key] = n
        for key, n in nodes.items():
            if (first and not key.startswith(("G:", "W:"))) or key in expanded:  # agent groups start collapsed
                n.expand()
        if cur in nodes:
            target = nodes[cur]
            self.call_after_refresh(lambda: tree.move_cursor(target))
        self.alerts()
        self.show_detail()

    def _walk(self, node):
        yield node
        for c in node.children:
            yield from self._walk(c)

    def rollup(self, plan):
        """self.below[key] = "1 stuck, 2 YOU": the waiting sessions under each folder and session row."""
        up = {k: pk for k, pk, _, _ in plan}
        n = {}
        for k, _pk, kind, rec in plan:
            if kind != "session":
                continue
            st = self.state(rec)
            if st not in T.WAITING:
                continue
            p = up.get(k)
            while p:
                n.setdefault(p, {}).setdefault(st, 0)
                n[p][st] += 1
                p = up.get(p)
        self.below = {k: ", ".join(f"{c} {'YOU' if st == 'you' else st}" for st, c in sorted(v.items()))
                      for k, v in n.items()}
        self.cost_rollup(plan, up)

    def alerts(self):
        for sid, s in self.data.items():
            st = self.state(s)
            before = self.prev_state.get(sid)
            if st in ("stuck", "died") and before != st:
                T.tell_parent(s, st, self.health.get(sid, (st, {}))[1])  # once per episode (flag file)
                if before:
                    self.notify(f"{s.get('name')} is {st}: {self.why(s)}", severity="error", timeout=20)
                    self.bell()
            if s.get("auto") and before and before != st:
                if st == "you":
                    self.notify(f"{s.get('name')} needs you", severity="error", timeout=15)
                    self.bell()
                elif before == "work" and st == "idle":
                    self.notify(f"{s.get('name')} finished", severity="information", timeout=10)
            self.prev_state[sid] = st
            if O is not None and sid in self.obs:
                try:  # one parent message per flag or budget episode (swarm_obs .on flag files)
                    O.notify(s, self.obs[sid], live_now=sid in self.live)
                except Exception:  # noqa: BLE001
                    pass

    def selected(self):
        n = self.query_one(Tree).cursor_node
        if not n or not n.data:
            return None, None
        if n.data.get("sid"):
            return self.data.get(n.data["sid"]), None
        return None, n.data.get("folder")

    def on_tree_node_highlighted(self, _event):
        self.show_detail()

    def on_tree_node_selected(self, event):
        """Enter on a session opens it; Enter on a folder only folds or unfolds it (the Tree does that)."""
        if event.node.data and event.node.data.get("sid"):
            self.action_open()

    def show_detail(self):
        s, fold = self.selected()
        box = self.query_one("#detail", Static)
        if fold:
            box.update(f"[b]Folder node[/b] {fold}\n\nN: start a new session in this folder.")
            return
        if not s:
            n = self.query_one(Tree).cursor_node
            if n and n.data and n.data.get("obs"):
                box.update("\n".join(escape(x) for x in n.data["obs"].get("detail") or [n.data["obs"]["label"]]))
            return
        par = self.data.get(s.get("parent") or "", {}).get("name", s.get("parent") or "none")
        e = lambda x: escape(str(x))  # noqa: E731
        lines = [f"[b]{e(s.get('name'))}[/b]", f"state: {self.state(s)} {e(self.why(s))}   level: {s.get('level')}   node: {e(s.get('node'))}",
                 f"parent: {e(par)}", f"id: {s['session_id']}", f"folder: {e(s.get('cwd'))}",
                 f"created: {s.get('created') or '-'}   last: {s.get('last') or '-'}"]
        if s.get("title"):
            lines.append(f"title: {e(s['title'])}")
        if s.get("task"):
            lines.append(f"task file: {e(s['task'])}")
        lines += [e(x) for x in swarm.detail_lines(s["session_id"], self.data, self.live)]
        lines += [e(x) for x in self.obs_detail(s)]
        lines.append("")
        lines += [e(x) for x in last_lines(s.get("transcript"))] or ["(no transcript text)"]
        box.update("\n".join(lines))

    # ---------- swarm observe (swarm_obs.py) ----------
    def load_obs(self, data, live):
        """swarm_obs.info for every registry session (not the old transcripts). Runs off the UI thread."""
        if O is None:
            return {}
        out = {}
        for sid, v in data.items():
            if v.get("old"):
                continue
            try:
                out[sid] = O.info(v, live)
            except Exception:  # noqa: BLE001  one bad transcript never stops the tree
                pass
        O.save()
        return out

    def obs_label(self, t, s):
        inf = self.obs.get(s["session_id"])
        if not inf:
            return
        own, sub = inf["usd"], self.cost_sub.get(s["session_id"]) or 0.0
        if own >= 0.005 or sub >= 0.005:
            t.append(f"  {O.fmt_usd(own)}", style="green")
            if sub - own > 0.005:
                t.append(f" (tree {O.fmt_usd(sub)})", style="green")
        m = O.marks(inf)
        if m:
            t.append(f"  {m}", style="bold red" if "OVER" in m else "bold magenta")

    def agent_plan(self, out, s):
        """Agent rows under a session: a group row ("+74 agents, 3 failed"), one row per workflow, one per agent."""
        sid = s["session_id"]
        inf = self.obs.get(sid)
        if not inf or not inf.get("agents"):
            return
        g = f"G:{sid}"
        out.append((g, sid, "obs", {"label": inf["agent_summary"], "detail": O.agent_lines(inf["agents"])}))
        wfs = {}
        for i, a in enumerate(inf["agents"]):
            pk = g
            if a["kind"] == "wf":
                pk = wfs.get(a["wf"])
                if not pk:
                    pk = wfs[a["wf"]] = f"W:{sid}:{a['wf']}"
                    sub = [x for x in inf["agents"] if x["kind"] == "wf" and x["wf"] == a["wf"]]
                    out.append((pk, g, "obs", {"label": f"workflow {a['wf']} (phase {a.get('wf_phase') or '-'}): "
                                                         f"{O.agent_summary(sub)[1:]}", "detail": O.agent_lines(sub)}))
            line = O.agent_lines([dict(a, kind="agent")])[0]
            out.append((f"A:{sid}:{i}", pk, "obs", {"label": line, "detail": [line, f"model: {a['model']}",
                                                                             f"tokens: {a['tok']:,}  cost: {O.fmt_usd(a['usd'])}"]}))

    def cost_rollup(self, plan, up):
        """self.cost_sub[key]: the USD of a session and every session under it, and of every folder."""
        c = {}
        for k, _pk, kind, rec in plan:
            if kind != "session":
                continue
            u = (self.obs.get(k) or {}).get("usd") or 0.0
            p = k
            while p:
                c[p] = c.get(p, 0.0) + u
                p = up.get(p)
        self.cost_sub = c

    def obs_detail(self, s):
        """Cost, budget, flags, the live step and the last events, the agent count."""
        inf = self.obs.get(s["session_id"])
        if not inf:
            return []
        models = ", ".join(f"{O.short_model(m)} {O.fmt_usd(u)}" for m, u in sorted(inf["models"].items(), key=lambda kv: -kv[1]))
        out = [f"cost: {O.fmt_usd(inf['usd'])}, {O.fmt_tok(inf['tok'])} tokens"
               + (f" (tree {O.fmt_usd(self.cost_sub.get(s['session_id']) or 0)})" if s["session_id"] in self.cost_sub else "")
               + (f"  [{models}]" if models else "") + ("  (still counting)" if inf.get("partial") else "")]
        if inf.get("budget"):
            out.append(inf["budget"]["text"] + "   $: raise")
        out += [t for _, t in inf.get("flags") or []]
        if inf.get("agent_summary"):
            out.append(f"agents: {inf['agent_summary'][1:]}   e: show or hide")
        if inf.get("activity"):
            out.append("")
            out += inf["activity"]
        return out

    def action_raise_budget(self):
        s, _ = self.selected()
        if not s or s.get("old"):
            self.notify("Select a registry session to set its budget.", severity="warning")
            return
        b = (self.obs.get(s["session_id"]) or {}).get("budget")
        now = f"now {b['text']}" if b else "no budget now"
        self.ask(("budget", s["session_id"]), f"budget for {s.get('name')} ({now}): USD, or x2, or 500000 tok")

    def do_budget(self, sid, text):
        t = text.strip().lower().replace("$", "")
        try:
            if t.startswith("x"):
                ev = O.set_budget(sid, mult=float(t[1:]), by="user (hubtree)")
            elif t.endswith("tok"):
                ev = O.set_budget(sid, tokens=int(float(t[:-3].strip())), by="user (hubtree)")
            else:
                ev = O.set_budget(sid, usd=float(t), by="user (hubtree)")
        except (ValueError, LookupError) as e:
            self.notify(f"Budget not set: {e}", severity="error")
            return
        self.notify(f"Budget set: {ev.get('budget_usd', '-')} USD, {ev.get('budget_tokens', '-')} tokens.")

    def action_agents(self):
        s, _ = self.selected()
        tree = self.query_one(Tree)
        if not s:
            n = tree.cursor_node
            if n and n.data and n.data.get("obs"):
                n.toggle()
            return
        for n in self._walk(tree.root):
            if n.data and n.data.get("key") == f"G:{s['session_id']}":
                n.toggle()
                return
        self.notify(f"{s.get('name')} has no sub-agents or workflow agents.")

    # ---------- actions ----------
    def action_open(self):
        s, fold = self.selected()
        if fold or not s:
            return
        name = s.get("name") or s["session_id"]
        a = self.live.get(s["session_id"])
        if a and a.get("kind") != "interactive":  # background: work, idle, you or stuck
            open_tab(s["session_id"], s.get("cwd"), name, attach=a.get("id"))
        elif a:
            self.notify(f"{name} is open in another tab already. Switch to that tab.", severity="warning")
        else:
            folder = open_tab(s["session_id"], s.get("cwd"), name, transcript=s.get("transcript"))
            if s.get("cwd") and os.path.normpath(s["cwd"]).lower() != folder.lower():
                self.notify(f"Old folder {s['cwd']} is gone: opened in {folder}.")
        self.poll()

    def ask(self, what, placeholder):
        self.pending = what
        box = self.query_one("#cmd", Input)
        box.placeholder = placeholder
        box.value = ""
        box.add_class("show")
        box.focus()

    def action_child(self):
        self._spawn_ask("tab")

    def action_fork(self):
        self._spawn_ask("fork")

    def action_auto(self):
        self._spawn_ask("auto")

    def _spawn_ask(self, kind):
        s, _ = self.selected()
        if not s or s.get("old"):
            self.notify("Select a registry session first (not a folder, not an old transcript).", severity="warning")
            return
        self.ask(("spawn", kind, s["session_id"]), f"{kind} child of {s.get('name')}:  <name> | <task>")

    def action_new_root(self):
        s, fold = self.selected()
        folder_node = fold or (s or {}).get("node") or "hub"
        cwd = T.HUB if folder_node == "hub" else os.path.join(T.HUB, *folder_node.split("/"))
        try:
            term.open_tab(folder_node, cwd, ["claude"] + term.claude_args())
        except (RuntimeError, OSError, ValueError) as e:
            self.notify(f"Cannot open a tab: {e}", severity="error")
            return
        self.notify(f"New session in {cwd}")

    def action_rename(self):
        s, fold = self.selected()
        if not s:
            self.notify("Select a session to rename (folders keep their names).", severity="warning")
            return
        self.ask(("rename", s["session_id"]), f"new name for {s.get('name')} (the last part; a name with / is used as is)")

    def do_rename(self, sid, text):
        """Rename in the registry; also write the title into a closed session's transcript, as /rename does."""
        s = self.data.get(sid) or {}
        old = s.get("name") or sid
        new = text.strip() if "/" in text else (old.rsplit("/", 1)[0] + "/" if "/" in old else "") + T.slug(text)
        T.append({"event": "rename", "session_id": sid, "name": new})
        tp, live = s.get("transcript"), sid in self.live
        if tp and os.path.exists(tp) and not live:
            with open(tp, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "custom-title", "customTitle": new, "sessionId": sid}) + "\n")
            _OLD_CACHE.pop(tp, None)
            self.notify(f"Renamed {old} to {new}.")
        else:
            self.notify(f"Renamed {old} to {new} in the tree. The session shows the new title from its next resume.")

    def action_stop(self):
        s, _ = self.selected()
        a = self.live.get((s or {}).get("session_id"))
        if not a or a.get("kind") != "background" or not a.get("id"):
            msg = "is open in a tab: close that tab to stop it." if a else "is not running."
            self.notify(f"{(s or {}).get('name')} {msg}", severity="warning")
            return
        subprocess.run(["claude", "stop", a.get("id")], capture_output=True)
        T.append({"event": "end", "session_id": s["session_id"], "reason": "stopped in hubtree"})  # else: died
        self.notify(f"Stopped {s.get('name')}")
        self.poll()

    def action_hide(self):
        s, _ = self.selected()
        if not s:
            return
        hidden = bool(s.get("hidden"))
        a = self.live.get(s["session_id"])
        if not hidden and a and a.get("kind") == "background":
            subprocess.run(["claude", "stop", a.get("id")], capture_output=True)
            T.append({"event": "end", "session_id": s["session_id"], "reason": "stopped in hubtree"})
        T.append({"event": "unhide" if hidden else "hide", "session_id": s["session_id"]})
        self.notify(f"{'Shown' if hidden else 'Hidden'}: {s.get('name')}. Press H to see hidden sessions.")
        self.poll()

    def action_subtree(self):
        s, fold = self.selected()
        node = fold or (s or {}).get("node") or "hub"
        try:
            term.open_tab(f"hubtree {node}", T.HUB, [sys.executable, os.path.join(HERE, "app.py"), node])
        except (RuntimeError, OSError, ValueError) as e:
            self.notify(f"Cannot open a tab: {e}", severity="error")
            return
        self.notify(f"Opened the sub-tree view of {node} in a new tab.")

    def action_move(self):
        s, _ = self.selected()
        if not s:
            self.notify("Select a session to move.", severity="warning")
            return
        self.ask(("move", s["session_id"]), f"move {s.get('name')} to folder (for example work/app)")

    def do_move(self, sid, node):
        node = node.strip().strip("/").replace("\\", "/")
        if node.lower() in (T.HUB.replace(chr(92), "/").lower(), ""):
            node = "hub"
        if not os.path.isdir(T.node_dir(node)):
            self.notify(f"No folder {T.node_dir(node)}. Moves go to a folder under the root.", severity="error")
            return
        T.move(sid, node)
        name = (self.data.get(sid) or {}).get("name") or sid
        self.notify(f"Moved {name} to {node}. Its next resume starts in {T.node_dir(node)}.")

    def action_make_root(self):
        s, _ = self.selected()
        if not s:
            return
        self.ask(("root", s["session_id"]), f"type yes to make {s.get('name')} the root of {s.get('node')}")

    def action_toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self.notify("Hidden sessions shown." if self.show_hidden else "Hidden sessions not shown.")
        self.poll()

    def action_delete(self):
        s, _ = self.selected()
        if not s:
            return
        a = self.live.get(s["session_id"])
        if a and a.get("kind") == "interactive":
            self.notify(f"{s.get('name')} is open in a tab. Close that tab first.", severity="warning")
            return
        self.ask(("delete", s["session_id"]), f"type yes to DELETE {s.get('name')} (its transcript is deleted)")

    def do_delete(self, sid):
        """Stop the session, delete its transcript, record it."""
        s = self.data.get(sid) or {}
        name = s.get("name") or sid
        try:
            tp = s.get("transcript")
            a = self.live.get(sid)
            if a:
                subprocess.run(["claude", "stop", a.get("id")], capture_output=True)
                subprocess.run(["claude", "rm", a.get("id")], capture_output=True)
            if tp and os.path.exists(tp):
                os.remove(tp)
                side = tp[:-6]  # the session's own folder (subagents, tool results), if any
                if os.path.isdir(side):
                    import shutil
                    shutil.rmtree(side, ignore_errors=True)
            T.append({"event": "delete", "session_id": sid, "name": name})
            self.call_from_thread(self.notify, f"Deleted {name}.")
        except Exception as e:  # report, never crash the app
            self.call_from_thread(self.notify, f"Delete failed: {e!r}"[:200], severity="error")
        self.poll()

    def action_waiting(self):
        self.only_waiting = not self.only_waiting
        self.notify("Only the sessions that wait for you (YOU, STUCK, died)." if self.only_waiting else "All sessions.")
        self.poll()

    def action_filter(self):
        self.ask(("filter",), "filter by name (empty = all)")

    def action_command(self):
        self.ask(("cmd",), "spawn <parent> <child> | <task>  |  open <name>  |  rename <name> <new>  |  start <name>  |  "
                           "cancel <name>  |  team <team> <goal>  |  results")

    def on_input_submitted(self, event):
        box = self.query_one("#cmd", Input)
        box.remove_class("show")
        self.query_one(Tree).focus()
        what, text = self.pending, event.value.strip()
        self.pending = None
        if not what or (not text and what[0] != "filter"):
            return
        if what[0] == "filter":
            self.filter_text = text
        elif what[0] == "delete":
            if text.lower() == "yes":
                self.notify("Deleting ...")
                threading.Thread(target=self.do_delete, args=(what[1],), daemon=True).start()
            else:
                self.notify("Not deleted.")
        elif what[0] == "rename":
            self.do_rename(what[1], text)
        elif what[0] == "move":
            self.do_move(what[1], text)
        elif what[0] == "root":
            if text.lower() == "yes":
                T.make_root(what[1])
                self.notify("Done: it is the root of its folder now. The old root is a normal session.")
        elif what[0] == "spawn":
            self.run_spawn(what[2], text, what[1])
        elif what[0] == "budget":
            self.do_budget(what[1], text)
        elif what[0] == "cmd":
            self.run_command(text)
        elif what[0] in ("change", "reject", "group", "group_yes"):
            self.ctl_input(what, text)
        self.poll()

    def run_spawn(self, parent, text, kind):
        name, _, task = text.partition("|")
        args = [sys.executable, SPAWN, "--parent", parent, "--name", name.strip(), "--task", task.strip()]
        if kind == "fork":
            args.append("--fork")
        if kind == "auto":
            args.append("--auto")
        r = subprocess.run(args, capture_output=True, text=True)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"ok": False, "error": (r.stderr or r.stdout)[-200:]}
        self.notify(f"started {out.get('name')} ({out.get('mode')})" if out.get("ok") else f"failed: {out.get('error')}",
                    severity="information" if out.get("ok") else "error")

    def run_command(self, text):
        verb, _, rest = text.partition(" ")
        if verb == "spawn":
            parent, _, more = rest.partition(" ")
            s = T.by_name(parent)
            if not s:
                self.notify(f"no session named {parent}", severity="error")
                return
            self.run_spawn(s["session_id"], more, "tab")
        elif verb == "open":
            s = T.by_name(rest.strip())
            if s:
                open_tab(s["session_id"], s.get("cwd"), s["name"], transcript=s.get("transcript"))
        elif verb == "rename":
            old, _, new = rest.partition(" ")
            s = T.by_name(old)
            if s and new.strip():
                T.append({"event": "rename", "session_id": s["session_id"], "name": new.strip()})
        elif verb in ("start", "cancel"):
            r = swarm.start_now(rest.strip()) if verb == "start" else swarm.cancel(rest.strip())
            self.say(r, f"{verb}: {r.get('name') or rest.strip()}")
        elif verb == "team":
            s, _ = self.selected()
            team, _, goal = rest.strip().partition(" ")
            if not s or s.get("old") or not goal.strip():
                self.notify("Select the parent session first, then: team <team name> <goal>", severity="warning")
                return
            self.notify(f"Starting team {team} under {s.get('name')} ...")
            threading.Thread(target=self.do_team, args=(team, s["session_id"], goal.strip()), daemon=True).start()
        elif verb == "results":
            rs = swarm.results(status="open")
            self.notify("; ".join(f"#{r['id']} {(r['child_name'] or '').split('/')[-1]}" for r in rs[:10])
                        or "No open results.", timeout=15)
        else:
            self.notify(f"unknown command: {verb}", severity="error")

    # ---------- swarm control (swarm.py) ----------
    def say(self, r, ok_msg):
        if r.get("ok"):
            self.notify(ok_msg)
        else:
            self.notify(str(r.get("error"))[:300], severity="error", timeout=15)
        self.poll()

    def _queued_sel(self):
        s, _ = self.selected()
        if not s or not s.get("queued"):
            self.notify("Select a queued row first.", severity="warning")
            return None
        return s

    def action_queue_start(self):
        s = self._queued_sel()
        if s:
            self.notify(f"Starting {s.get('name')} ...")
            threading.Thread(target=self.bg_say, args=(swarm.start_now, (s["session_id"],), f"Started {s.get('name')}."),
                             daemon=True).start()

    def action_queue_cancel(self):
        s = self._queued_sel()
        if s:
            self.say(swarm.cancel(s["session_id"]), f"Cancelled {s.get('name')} in the queue.")

    def bg_say(self, fn, args, ok_msg):
        """Run a slow swarm call off the UI thread, then report it."""
        try:
            r = fn(*args)
        except Exception as e:  # noqa: BLE001 - report, never crash the app
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.call_from_thread(self.say, r, ok_msg)

    def _result_sel(self):
        s, _ = self.selected()
        r = swarm.newest_open(s["session_id"]) if s else None
        if not r:
            self.notify("Select a child row that shows 'result ready'.", severity="warning")
        return s, r

    def action_result_accept(self):
        s, r = self._result_sel()
        if r:
            self.notify(f"Accept #{r['id']}: checking that the work is committed ...")
            threading.Thread(target=self.bg_say, args=(swarm.accept, (r["id"],),
                                                       f"Accepted result #{r['id']}: {s.get('name')} is closed."),
                             daemon=True).start()

    def action_result_change(self):
        s, r = self._result_sel()
        if r:
            self.ask(("change", r["id"]), f"ask {s.get('name')} for a change (your words)")

    def action_result_reject(self):
        s, r = self._result_sel()
        if r:
            self.ask(("reject", r["id"]), f"reject result #{r['id']}: the reason (or - for none)")

    def action_group(self):
        s, fold = self.selected()
        if not s and not fold:
            return
        what = ("node", fold) if fold else ("root", s["session_id"])
        self.ask(("group", what), f"group action on {fold or s.get('name')} and below: pause | resume | stop | hide | "
                                  "broadcast <words>")

    def ctl_input(self, what, text):
        if what[0] == "change":
            threading.Thread(target=self.bg_say, args=(swarm.change, (what[1], text),
                                                       f"Asked for a change (result #{what[1]})."), daemon=True).start()
        elif what[0] == "reject":
            self.say(swarm.reject(what[1], "" if text == "-" else text), f"Rejected result #{what[1]}.")
        elif what[0] == "group":
            action, _, words = text.partition(" ")
            if action not in swarm.ACTIONS:
                self.notify(f"unknown group action {action}", severity="error")
                return
            kind, ref = what[1]
            tg = swarm.targets(root=ref) if kind == "root" else swarm.targets(node=ref)
            pv = swarm.preview(action, tg, self.live)
            if not pv["n"]:
                self.notify(pv["text"] + " Nothing to do.", severity="warning")
                return
            names = ", ".join(n.split("/")[-1] for n, _ in pv["do"][:6])
            self.set_timer(0.1, lambda: self.ask(("group_yes", action, tg, words), f"{pv['text']} type yes  ({names})"))
        elif what[0] == "group_yes":
            if text.lower() != "yes":
                self.notify("Nothing done.")
                return
            _, action, tg, words = what
            self.notify(f"{action}: working ...")
            threading.Thread(target=self.do_group, args=(action, tg, words), daemon=True).start()

    def do_group(self, action, tg, words):
        try:
            r = swarm.run_group(action, tg, words, by="user (hubtree)")
            msg = (f"{action}: {len(r.get('done', []))} done, {len(r.get('skip', []))} skipped" if r.get("ok")
                   else r.get("error"))
            self.call_from_thread(self.notify, str(msg)[:300], timeout=15)
        except Exception as e:  # noqa: BLE001 - report, never crash the app
            self.call_from_thread(self.notify, f"group {action} failed: {e!r}"[:200], severity="error")
        self.call_from_thread(self.poll)

    def do_team(self, team, parent, goal):
        try:
            r = swarm.start_team(team, parent, goal)
            msg = (f"team {team}: {len(r['members'])} members, {sum(1 for m in r['members'] if m['queued'])} queued"
                   if r.get("ok") else r.get("error"))
        except Exception as e:  # noqa: BLE001
            msg = f"team {team} failed: {e}"
        self.call_from_thread(self.notify, str(msg)[:300], timeout=15)
        self.call_from_thread(self.poll)


if __name__ == "__main__":
    HubTree(sys.argv[1] if len(sys.argv) > 1 else "hub").run()
