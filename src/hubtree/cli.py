"""The `hubtree` command.

    hubtree [folder]            the tree app (a folder shows only that sub-tree)
    hubtree init [--root DIR]   set up: config, settings backup, hooks, side pane, /spawn and /where, daemon
    hubtree uninstall           undo init: settings.json back to its backup, pane and skills removed
    hubtree doctor              check the install
    hubtree daemon [--once]     the loop for the queue, budgets and auto-continue
    hubtree hook <event>        the Claude Code hook entry (stdin: hook JSON)
    hubtree where               the tree as text
    hubtree spawn ...           start a child session (see `hubtree spawn --help`)
    hubtree swarm ...           queue, results, group actions, teams
    hubtree obs ...             cost, budgets, flags
    hubtree web ...             the local page on 127.0.0.1
    hubtree view ...            tree rows as JSON (the side pane reads them)
    hubtree open ...            open a session in a new terminal tab
    hubtree autopilot ...       auto-continue after a usage limit
    hubtree rotate ...          session rotation
    hubtree inbox [--peek]      unread messages of this session
    hubtree version
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# subcommand -> module file in this folder (run as __main__ with the rest of the arguments)
SCRIPTS = {
    "spawn": "spawn", "swarm": "swarm", "obs": "swarm_obs", "web": "swarm_web", "view": "tree_view",
    "open": "open_session", "autopilot": "autopilot", "rotate": "rotate", "owner-update": "owner_update",
    "task": "task_packet",
}


def _run_script(mod, args):
    sys.argv = [os.path.join(HERE, mod + ".py")] + list(args)
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


def _inbox(args):
    import hubdb
    con = hubdb.connect()
    try:
        for m in hubdb.inbox(con, mark="--peek" not in args):
            print(f"{m['ts']}  from {m['from']}: {m['body']}")
    finally:
        con.close()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    cmd = argv[0] if argv else ""
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if cmd in ("version", "--version"):
        import importlib.metadata as md
        try:
            print(md.version("hubtree"))
        except md.PackageNotFoundError:
            print("hubtree (not installed)")
        return 0
    if cmd == "hook":
        import hooks
        return hooks.main(rest)
    if cmd in ("init", "uninstall", "doctor"):
        import install
        return install.main([cmd] + rest)
    if cmd == "daemon":
        import daemon
        return daemon.main(rest)
    if cmd == "where":
        return _run_script("tree_view", ["--text"] + rest)
    if cmd == "inbox":
        return _inbox(rest)
    if cmd in SCRIPTS:
        return _run_script(SCRIPTS[cmd], rest)
    return _run_script("app", argv)


if __name__ == "__main__":
    sys.exit(main())
