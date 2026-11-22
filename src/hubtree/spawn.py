"""Start a child session in the session tree.

Usage:
  python spawn.py --parent <session id> --name <child name> --task "<what to do>" [--auto] [--fork] [--cwd <dir>] [--remote]
                  [--model <model id>] [--ultracode] [--interactive] [--after <name or id> ...] [--when released|done]
  python spawn.py --parent <session id> --team <team name> --goal "<the team goal>"
  python spawn.py --from-queue <queue id>        (swarm.py tick: start a queued child)

- The child name is joined to the parent name: parent `work/app` + `s07-construct`
  -> `work/app/s07-construct`. A name that starts with the node path of the folder is used as is.
- Default: a new terminal tab with an interactive session (terminal back end: term.py).
- --auto: a background session (claude --bg) that Claude starts on its own. When the node or the tree has its limit
  of working background children (hubtree.toml [swarm] max_working_per_node, max_working_total), the child waits as
  "queued" and swarm.py tick starts it when a slot frees.
- --after <name or id> (repeatable): the child waits as "queued" until each dependency is released (default: it
  posted a result, or ended) or, with --when done, is done (accepted or ended). Its task file is written at once.
- --team <name> --goal "...": start every role of teams/<name>.toml as children, wired with --after.
- --remote: the tab starts with Remote Control on (claude --remote-control <name>).
- Every child gets a task file (<state>/tasks/<name>.md) and a task packet (<name>.task.json).
  The task file text comes from TEMPLATE below; a file <HUBTREE_HOME>/task_template.md replaces it.
- --fork: the child starts as a copy of the parent's chat (claude --resume <parent> --fork-session).
- Autopilot is the default: the task file gets the "Autopilot" block (decide ordinary choices, never wait).
  --interactive leaves it out, for a session where the user wants to talk. --autopilot says the default out loud.
- Every task file has an "Owner updates" section: owner_update.py writes the user's new words there.
- Every task file asks the child to post a structured result (swarm.py result) when it is done.
Prints one JSON line: {"ok": true, "name": ..., "mode": "tab"|"background"|"queued", "task_file": ...}.
"""
import argparse
import json
import os
import string
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import htconf  # noqa: E402
import task_packet  # noqa: E402
import term  # noqa: E402
import tree_lib as T  # noqa: E402

SWARM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swarm.py")
OPT_KEYS = ("auto", "fork", "model", "remote", "no_remote", "ultracode", "limit", "interactive", "budget_usd", "budget_tokens")

# The task file text. Placeholders (string.Template): ${name} ${parent_name} ${parent_id} ${started} ${task}
# ${updates} ${autopilot} ${result_cmd}. To change the rules, put your own text in <HUBTREE_HOME>/task_template.md.
UPDATES = (
    "## Owner updates\n\n"
    "The parent session writes the user's new words here: verbatim, with the time. These lines are the user's "
    "instructions, the same as the task text above, and the newest line wins. When a message says that this file "
    "has an update, read this file again and follow the newest update.\n\n"
    "(no updates yet)\n\n")

AUTOPILOT = (
    "## Autopilot: this session works without the user\n\n"
    "1. Decide each ordinary choice yourself: take the recommended or the safest option. Write each choice and its "
    "reason in one line in a notes file.\n"
    "2. Ask the user only for a decision that only the user can make: money spent, a message or post that goes "
    "out, a delete of the user's data.\n"
    "3. Never wait for an answer. Take the safest reversible option and go on. Put each open decision in the "
    "`--decisions` field of the result.\n"
    "4. Do not use AskUserQuestion.\n\n")

TEMPLATE = """\
# Task for session `${name}`

Parent session: `${parent_name}` (id ${parent_id}). Started ${started}.

## Goal

${task}

## Done test

Write the test that shows the task is done before you start. When it passes, say so and stop.

## Budget

Keep to the budget in the task text. If it has none, keep the work small and report when you need more.

${updates}${autopilot}## Rules for this session

1. Do what the task asks. Add no extra features.
2. Report only numbers that a file or a command shows.
3. Send no message, post nothing and spend no money without the user's approval.
4. Write files as you go.

## When the task is done

1. Post a structured result, so that the parent's tree row shows it and the sessions that wait for you can start:
   `${result_cmd}` (leave out an empty field).
2. Send the result to the parent session `${parent_name}` with the SendMessage tool, in 10 lines or fewer.
   If that name is not reachable, run ListAgents and use the row whose session id is `${parent_id}`.
   If the parent is closed, write the result at the end of this task file instead.
"""


def template():
    """The task template: <HUBTREE_HOME>/task_template.md when it exists, else TEMPLATE."""
    try:
        with open(os.path.join(htconf.home(), "task_template.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return TEMPLATE


def out(d, code=0):
    print(json.dumps(d))
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent")
    ap.add_argument("--name")
    ap.add_argument("--task", default="")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--fork", action="store_true")
    ap.add_argument("--cwd")
    ap.add_argument("--remote", action="store_true", help="start the tab with Remote Control on")
    ap.add_argument("--no-remote", dest="no_remote", action="store_true", help="kept for old callers: no effect")
    ap.add_argument("--model", help="model for the child, for example sonnet (default: the Claude Code default)")
    ap.add_argument("--limit", type=float, help=f"minutes with no transcript write before the tree marks the child stuck (default {T.STUCK_MIN})")
    ap.add_argument("--budget-usd", type=float, help="list-price USD budget of the child (swarm_obs.py): a mark at 80%%, a pause at 100%%")
    ap.add_argument("--budget-tokens", type=int, help="token budget of the child (input + cache read + cache write + output)")
    ap.add_argument("--ultracode", action="store_true", help="put the keyword ultracode in the first prompt (multi-agent workflows allowed)")
    ap.add_argument("--after", action="append", default=[], help="wait as queued until this session (name or id) is released")
    ap.add_argument("--when", choices=["released", "done"], default="released",
                    help="released (default): a result or an end; done: accepted or ended")
    ap.add_argument("--team", help="start the roles of teams/<team>.toml as children (with --goal)")
    ap.add_argument("--goal", default="", help="the team goal (with --team)")
    ap.add_argument("--from-queue", help="start the queued child with this queue id (swarm.py tick)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--autopilot", dest="interactive", action="store_false",
                      help="the default: the child decides ordinary choices and never waits for the user")
    mode.add_argument("--interactive", dest="interactive", action="store_true",
                      help="no autopilot block: for a session where the user wants to talk")
    ap.set_defaults(interactive=False)
    a = ap.parse_args()

    if a.from_queue:
        return from_queue(a.from_queue)
    if a.team:
        if not a.parent or not a.goal:
            ap.error("--team needs --parent and --goal")
        import swarm
        par = swarm.find(a.parent)
        if not par:
            out({"ok": False, "error": f"parent {a.parent} is not in the registry"}, 1)
        try:
            r = swarm.start_team(a.team, par["session_id"], a.goal)
        except ValueError as e:
            r = {"ok": False, "error": str(e)}
        out(r, 0 if r.get("ok") else 1)
    if not a.parent or not a.name:
        ap.error("--parent and --name are needed")

    known = T.sessions()
    par = known.get(a.parent) or T.by_name(a.parent)
    if not par:
        out({"ok": False, "error": f"parent {a.parent} is not in the registry"}, 1)
    pid = par["session_id"]
    cwd = a.cwd or par.get("cwd") or T.HUB
    node, level, _ = T.node_of(cwd)
    top = node.split("/")[0]
    if a.name.split("/")[0] == top:
        name = a.name
    elif par["name"].split("/")[0] == top and par.get("node") == node:
        name = f"{par['name']}/{T.slug(a.name)}"  # a child in the parent's own folder: under the parent's path
    else:
        name = f"{node}/{T.slug(a.name)}"  # a child in another folder: named by that folder
    if a.auto:
        name += "~auto"
    name = T.unique_name(name, {v.get("name") for v in known.values()})

    for ref in a.after:  # a child that spawn.py just started has only its pending file until its session hook runs
        if not (known.get(ref) or T.by_name(ref) or os.path.exists(os.path.join(T.PENDING, T.slug(ref, 200) + ".json"))):
            out({"ok": False, "error": f"--after {ref}: no such session or queued child"}, 1)
    wait = "after" if a.after else (_limit_full(node, known) if a.auto else None)

    task_file = write_task(name, par, a.task, a.interactive)
    opts = {k: getattr(a, k) for k in OPT_KEYS}
    lvl = max(level, (par.get("level") or 0) + 1)
    if wait:
        qid = "q-" + uuid.uuid4().hex[:12]
        T.append({"event": "queued", "session_id": qid, "name": name, "parent": pid, "node": node, "level": lvl,
                  "cwd": cwd, "auto": a.auto, "fork": a.fork, "tab": not a.auto, "task": task_file,
                  "limit_min": a.limit, "after": a.after, "when": a.when, "opts": opts,
                  "budget_usd": a.budget_usd, "budget_tokens": a.budget_tokens, "why": wait})
        out({"ok": True, "name": name, "mode": "queued", "queued": True, "queue_id": qid, "task_file": task_file,
             "waits": wait, "after": a.after, "autopilot": not a.interactive})
    launch(name, pid, cwd, lvl, task_file, opts)


def _limit_full(node, known):
    """"limit" when the node or the tree has its limit of working background children, or when an older queued
    auto child at this node waits only for a slot (queue order); else None."""
    try:
        import swarm
        c = swarm.cfg()
        per, total = swarm.working_counts(known, swarm.live_now(fresh=True))
        if per.get(node, 0) >= c["max_working_per_node"] or total >= c["max_working_total"]:
            return "limit"
        if any(q.get("auto") and not q.get("after") and not q.get("paused") and (q.get("node") or "hub") == node
               for q in swarm.queued(known)):
            return "limit"
    except Exception:  # noqa: BLE001 - no swarm data: no limit
        return None
    return None


def write_task(name, par, task, interactive):
    pid = par["session_id"]
    tasks = os.path.join(T.STATE, "tasks")
    os.makedirs(tasks, exist_ok=True)
    task_file = os.path.join(tasks, T.slug(name, 200) + ".md")
    result_cmd = (f"{term.python()} {SWARM} result --name \"{name}\" --done \"<what was done>\" "
                  "--proof \"<the command or file that shows it works>\" --commits \"<hash,hash>\" "
                  "--open \"<open items>\" --decisions \"<decisions the user must make>\"")
    text = string.Template(template()).safe_substitute(
        name=name, parent_name=par["name"], parent_id=pid, started=T.now(), result_cmd=result_cmd,
        task=task or "(No task text. Ask the user what to do.)", updates=UPDATES,
        autopilot="" if interactive else AUTOPILOT)
    with open(task_file, "w", encoding="utf-8") as f:
        f.write(text)
    packet = task_packet.build(name, pid, par["name"], task, T.now())
    packet["autopilot"] = not interactive
    task_packet.write(task_file, packet)
    return task_file


def from_queue(qid):
    """Start a queued child (swarm.py tick, start-now)."""
    s = T.sessions().get(qid)
    if not s or not s.get("queued") or s.get("gone"):
        out({"ok": False, "error": f"{qid} is not a queued child"}, 1)
    launch(s["name"], s["parent"], s.get("cwd") or T.HUB, s.get("level"), s["task"], dict(s.get("opts") or {}), qid=qid)


def launch(name, pid, cwd, level, task_file, o, qid=None):
    """Write the pending link, then start the session (background or a terminal tab). Prints the JSON line."""
    auto = bool(o.get("auto"))
    os.makedirs(T.PENDING, exist_ok=True)
    pend = {"name": name, "parent": pid, "auto": auto, "fork": bool(o.get("fork")), "tab": not auto,
            "level": level, "task": task_file, "ts": T.now(), "node": T.node_of(cwd)[0],  # node: swarm.py limits
            "limit_min": o.get("limit"), "autopilot": not o.get("interactive"),
            "budget_usd": o.get("budget_usd"), "budget_tokens": o.get("budget_tokens")}
    with open(os.path.join(T.PENDING, T.slug(name, 200) + ".json"), "w", encoding="utf-8") as f:
        json.dump(pend, f)

    prompt = f"Read the task file {task_file} and do the task."
    if o.get("ultracode"):
        prompt = "ultracode. " + prompt
    args = term.claude_args() + ["--name", name]
    if o.get("model"):
        args += ["--model", o["model"]]
    if o.get("fork"):
        args += ["--resume", pid, "--fork-session"]
    if o.get("remote") and not auto:
        args += ["--remote-control", name]

    if auto:
        cmd = ["claude", "--bg"] + args + [prompt]
        r = subprocess.run(cmd, cwd=cwd, env=term.child_env(), capture_output=True, text=True, timeout=60,
                           creationflags=term.NO_WINDOW)
        ok = r.returncode == 0
        if ok and qid:
            T.append({"event": "dequeued", "session_id": qid, "name": name})
        out({"ok": ok, "name": name, "mode": "background", "task_file": task_file, "from_queue": qid,
             "autopilot": not o.get("interactive"), "out": (r.stdout or r.stderr).strip()[-300:]}, 0 if ok else 1)

    try:
        term.open_tab(name, T.resolve_cwd(cwd), ["claude"] + args + [prompt])
    except (RuntimeError, OSError) as e:
        out({"ok": False, "name": name, "error": f"could not open a tab: {e}"}, 1)
    if qid:
        T.append({"event": "dequeued", "session_id": qid, "name": name})
    out({"ok": True, "name": name, "mode": "tab", "task_file": task_file, "from_queue": qid,
         "autopilot": not o.get("interactive")})


if __name__ == "__main__":
    main()
