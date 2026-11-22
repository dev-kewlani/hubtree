"""Print the hubtree session tree as JSON rows, for the side pane. Light: registry plus live sessions only.

Usage: python tree_view.py [--me <session id>]
Output: {"rows": [{"depth", "kind": "folder"|"session", "label", "state", "sid", "name", "me"}], "counts": {...}}
States: tab, work, you, idle, stuck, died, closed (same marks as hubtree; tree_lib.health).
--text starts with a "Waits for you" block (you, stuck, died). --waiting prints only that block.
--text is the short view: that block, a numbered list of live sessions, the 5 newest closed ones, a count line.
--text --all prints the full tree. --node <folder> filters both views.
The short view saves its numbered list to <state>/where_pick.json. --pick N prints the row of number N.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_lib as T  # noqa: E402
try:
    import swarm_obs as O  # noqa: E402  cost, budgets, activity, agents, flags
except Exception:  # the tree view never fails on the swarm layer
    O = None
try:
    import swarm as SW  # noqa: E402  queue, claims and conflicts, results, pause
except Exception:
    SW = None


def state(s, live):
    return T.health(s, live)[0]


def rollup(rows):
    """Add "below": a count of waiting sessions (you, stuck, died) under each folder and session row.
    Rows come in tree order with a depth, so the rows under a row are the next rows with a larger depth."""
    for i, r in enumerate(rows):
        n = {}
        for x in rows[i + 1:]:
            if x["depth"] <= r["depth"]:
                break
            if x["kind"] == "session" and x["state"] in T.WAITING:
                n[x["state"]] = n.get(x["state"], 0) + 1
        if n:
            r["below"] = ", ".join(f"{v} {'YOU' if k == 'you' else k}" for k, v in sorted(n.items()))


def swarm_fields(row, s, live, me):
    """swarm_obs facts on a session row: usd, tok, marks, budget, activity (live rows), agents, agent_summary.
    This session's own children also get the one-per-episode parent messages (flags and budget marks)."""
    if O is None:
        return
    try:
        inf = O.info(s, live)
        if me and s.get("parent") == me:
            O.notify(s, inf, live_now=s["session_id"] in live)
        row.update({"usd": round(inf["usd"], 4), "tok": inf["tok"], "marks": O.marks(inf), "budget": inf["budget"],
                    "activity": inf["activity"], "agent_summary": inf["agent_summary"],
                    "agent_lines": O.agent_lines(inf["agents"])[:AGENT_LINES], "flags": [k for k, _ in inf["flags"]],
                    "flag_text": "; ".join(t for _, t in inf["flags"]), "paused": s.get("paused")})
    except Exception as e:  # noqa: BLE001  a bad transcript never stops the tree
        row["marks"] = f"(cost error {type(e).__name__})"


AGENT_LINES = 60  # agent lines per row in the JSON; hubtree shows every agent


def cost_rollup(rows):
    """usd_sub and tok_sub on each session row (itself and every row under it), usd and tok on each folder row."""
    for i, r in enumerate(rows):
        u, t = (r.get("usd") or 0.0, r.get("tok") or 0) if r["kind"] == "session" else (0.0, 0)
        for x in rows[i + 1:]:
            if x["depth"] <= r["depth"]:
                break
            if x["kind"] == "session":
                u, t = u + (x.get("usd") or 0.0), t + (x.get("tok") or 0)
        if r["kind"] == "session":
            r["usd_sub"], r["tok_sub"] = round(u, 4), t
        else:
            r["usd"], r["tok"] = round(u, 4), t


def cost_text(r):
    """' $1.23' for a row, ' $1.23 (tree $4.56)' when the rows under it cost more; '' without the swarm layer."""
    if O is None or r.get("usd") is None:
        return ""
    sub = r.get("usd_sub")
    if r["usd"] < 0.005 and (sub or 0) < 0.005:
        return ""  # no cost yet: the name keeps its room
    tree = f" (tree {O.fmt_usd(sub)})" if sub is not None and sub - r["usd"] > 0.005 else ""
    return f"  {O.fmt_usd(r['usd'])}{tree}"


def control_fields(rows, every, live, me):
    """swarm.py facts on the session rows: results (open results of its children), result and result_id (its own
    newest result), conflict, wait (a queued row), paused. The pane of a session also sends the conflict messages."""
    if SW is None:
        return
    ex = SW.row_extras(every, live, alert=bool(me))
    for r in rows:
        if r["kind"] == "session" and r["sid"] in ex:
            r.update({k: v for k, v in ex[r["sid"]].items() if k != "paused" or v})


def ctl_text(r):
    """'  2 results  CONFLICT x.py (with b)' for the text views; '' when there is nothing."""
    out = f"  {r['results']} result{'s' if r['results'] != 1 else ''}" if r.get("results") else ""
    if r.get("result") == "open":
        out += "  result ready"
    return out + (f"  CONFLICT {r['conflict']}" if r.get("conflict") else "")


LIVE_ORDER = {"you": 0, "stuck": 1, "died": 2, "work": 3, "tab": 4, "idle": 5}  # waiting first, then work, tab, idle
CLOSED_SHOWN = 5   # the short view lists this many closed sessions, newest first
MAX_LINES = 20     # the short view stays within this many lines; live rows beyond it move to /where --all
ROW_WIDTH = 60    # the short view cuts a long session path to about this many characters


def cut(name, width=ROW_WIDTH - 12):
    """Cut the middle of a long session path. The first and last parts stay, so the node and the name show."""
    if len(name) <= width:
        return name
    keep = width - 3
    return name[:keep // 2] + "..." + name[-(keep - keep // 2):]


def pick_path():
    return os.path.join(T.STATE, "where_pick.json")


def save_pick(pick):
    """Write the numbered list of the short view, so a later command can use a number. Temp file, then replace."""
    try:
        os.makedirs(T.STATE, exist_ok=True)
        tmp = pick_path() + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pick, f, ensure_ascii=False)
        os.replace(tmp, pick_path())
    except OSError:
        pass  # a failed save never stops the view


def pick_row(n):
    """The saved row with number n, or None."""
    try:
        with open(pick_path(), encoding="utf-8") as f:
            return next((r for r in json.load(f) if r.get("n") == n), None)
    except (OSError, ValueError, TypeError):
        return None


def main():
    if "--pick" in sys.argv:
        i = sys.argv.index("--pick") + 1
        row = pick_row(int(sys.argv[i])) if i < len(sys.argv) and sys.argv[i].isdigit() else None
        if row is None:
            print("no such number in the last /where list; run /where first", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(row, ensure_ascii=False))
        return
    me = sys.argv[sys.argv.index("--me") + 1] if "--me" in sys.argv else None
    live = T.live_agents()
    reg = {k: v for k, v in T.sessions().items()
           if v.get("name") and not v.get("hidden") and not v.get("deleted")
           and (k in live or not v.get("transcript") or os.path.exists(v["transcript"]))}
    data = dict(reg)
    every = T.sessions()
    for sid, a in live.items():
        if sid and sid not in data and not (every.get(sid) or {}).get("hidden"):
            node, level, _ = T.node_of(a.get("cwd"))
            node = (every.get(sid) or {}).get("node") or node  # a topic move wins over the start folder
            data[sid] = {"session_id": sid, "name": f"{node}/{T.slug(a.get('name') or sid[:8])}",
                         "node": node, "level": level + 1, "cwd": a.get("cwd"), "unregistered": True}
    kids = {}
    for s in sorted(data.values(), key=lambda s: s.get("created") or ""):
        kids.setdefault(s.get("parent") if s.get("parent") in data else None, []).append(s)

    rows, folders = [], {}

    def folder(node):
        if node in folders:
            return folders[node]
        depth = 0 if node == "hub" else folder(node.rsplit("/", 1)[0] if "/" in node else "hub") + 1
        rows.append({"depth": depth, "kind": "folder", "label": node.split("/")[-1] + "/", "name": node})
        folders[node] = depth
        return depth

    def add(s, depth):
        st, info = T.health(s, live)
        if me and s.get("parent") == me:
            T.tell_parent(s, st, info)  # this session's own child: one inbox message per stuck or died episode
        rows.append({"depth": depth, "kind": "session", "label": (s.get("name") or "").split("/")[-1],
                     "state": st, "why": T.why(st, info), "sid": s["session_id"], "name": s.get("name"),
                     "me": s["session_id"] == me,
                     "fork": bool(s.get("fork")), "auto": bool(s.get("auto")), "parent": s.get("parent"),
                     "root": bool(s.get("root")) or s.get("name") == (s.get("node") or "hub"),
                     "cwd": s.get("cwd"), "live_id": (live.get(s["session_id"]) or {}).get("id")})
        swarm_fields(rows[-1], s, live, me)
        for c in sorted(kids.get(s["session_id"], []), key=lambda c: -recent(c)):
            add(c, depth + 1)

    folder("hub")
    roots = kids.get(None, [])
    # Closed sessions with no live child are kept out of the pane unless they are this session's parent line.
    def is_root(s):
        return bool(s.get("root")) or s.get("name") == (s.get("node") or "hub")

    def recent(s):
        tp = s.get("transcript")
        return os.path.getmtime(tp) if tp and os.path.exists(tp) else 0.0

    # Per folder: the root first, then the newest activity first.
    for s in sorted(roots, key=lambda s: ("" if (s.get("node") or "hub") == "hub" else s.get("node"), not is_root(s), -recent(s))):
        d = folder(s.get("node") or "hub") + 1
        add(s, d)
    counts = {}
    for r in rows:
        if r["kind"] == "session":
            counts[r["state"]] = counts.get(r["state"], 0) + 1
    rollup(rows)
    cost_rollup(rows)
    control_fields(rows, every, live, me)
    if O is not None:
        O.save()
    if "--text" in sys.argv or "--waiting" in sys.argv or "--all" in sys.argv:
        scope = sys.argv[sys.argv.index("--node") + 1] if "--node" in sys.argv else None
        inside = lambda r: not scope or r["name"] == scope or (r["name"] or "").startswith(scope + "/")  # noqa: E731
        wait = [r for r in rows if r["kind"] == "session" and r["state"] in T.WAITING and inside(r)]
        out = ["Waits for you: none"] if not wait else [f"Waits for you: {len(wait)}"] + \
            [f"  [{r['state']}] {r['name']}: {r['why']}" for r in wait]
        if "--waiting" in sys.argv:
            print("\n".join(out))
            return
        if "--all" not in sys.argv:
            by_sid = {sid: s for sid, s in data.items()}
            sess = [r for r in rows if r["kind"] == "session" and inside(r)]
            live_rows = sorted((r for r in sess if r["state"] != "closed"),
                               key=lambda r: (LIVE_ORDER.get(r["state"], 9), -recent(by_sid[r["sid"]])))
            closed = sorted((r for r in sess if r["state"] == "closed"), key=lambda r: -recent(by_sid[r["sid"]]))
            # Room for the live rows: the line cap minus the block, a blank line, closed rows, the count line.
            room = MAX_LINES - len(out) - 1 - min(len(closed), CLOSED_SHOWN) - 1
            n_wait = sum(1 for r in live_rows if r["state"] in T.WAITING)  # a waiting row is never cut
            live_rows, cut_live = live_rows[:max(room, n_wait)], len(live_rows) - max(room, n_wait)
            shown = live_rows + closed[:CLOSED_SHOWN]
            pick = [{"n": i, "sid": r["sid"], "name": r["name"], "state": r["state"], "cwd": r["cwd"]}
                    for i, r in enumerate(shown, 1)]
            extra = [cost_text(r) + (f"  {r['marks']}" if r.get("marks") else "") + ctl_text(r) for r in shown]
            save_pick(pick)
            out.append("")
            # The extra text (cost, marks) takes its room from the name, so a row stays within a narrow width.
            out += [f"{p['n']:>2}  [{p['state']}] {cut(p['name'] or '', max(16, ROW_WIDTH - 12 - len(x)))}"
                    f"{x[:max(0, ROW_WIDTH + 2 - 7 - len(p['state']) - 16)]}" for p, x in zip(pick, extra)]
            hidden = ([f"{cut_live} live"] if cut_live > 0 else []) + \
                     ([f"{len(closed) - CLOSED_SHOWN} closed"] if len(closed) > CLOSED_SHOWN else [])
            if hidden:
                out.append(" and ".join(hidden) + " hidden. hubtree where --all")
            print("\n".join(out))
            return
        out.append("")
        for r in rows:
            if not inside(r):
                continue
            pad = "  " * r["depth"]
            below = f"  ({r['below']})" if r.get("below") else ""
            if r["kind"] == "folder":
                out.append(f"{pad}{r['label']}{cost_text(r)}{below}")
            else:
                tags = (" (root)" if r.get("root") else "") + (" (fork)" if r.get("fork") else "") + \
                       ("  <- this session" if r.get("me") else "")
                out.append(f"{pad}[{r['state']}] {r['label']}{tags}{cost_text(r)}{below}" + (f"  {r['why']}" if r.get("why") else "")
                           + (f"  {r['marks']}" if r.get("marks") else "")
                           + (f"  {r['agent_summary']}" if r.get("agent_summary") else "") + ctl_text(r))
        print("\n".join(out))
        print(f"\ncounts: {counts}")
        return
    print(json.dumps({"rows": rows, "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
