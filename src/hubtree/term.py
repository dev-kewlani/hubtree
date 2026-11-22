"""The terminal layer: start a command in a new terminal tab, and start a process that outlives its caller.

Back ends ([hubtree] terminal in hubtree.toml, or env HUBTREE_TERMINAL):
    wt    Windows Terminal: one new tab per session (`wt -w 0 new-tab`), PowerShell inside it.
    tmux  macOS and Linux: one tmux window per session. Inside tmux, the window opens in the current tmux
          session; outside tmux, in the detached tmux session "hubtree" (attach: `tmux attach -t hubtree`).
    auto  wt on Windows, tmux elsewhere (the default).
    print test back end: write the command as one JSON line to env HUBTREE_TERM_LOG (or stdout); start nothing.

Background sessions (`claude --bg`) need no terminal; they run under Claude Code itself.
"""
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import htconf  # noqa: E402

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: a console child shows no window
TMUX_SESSION = "hubtree"


def backend():
    b = os.environ.get("HUBTREE_TERMINAL") or htconf.load().terminal or "auto"
    if b == "auto":
        return "wt" if os.name == "nt" else "tmux"
    return b


def claude_args():
    """The extra arguments of every session that hubtree starts ([hubtree] claude_args)."""
    return list(htconf.load().claude_args)


def child_env(extra=None):
    """The environment of a new session: not a child session of the caller, and with a saved transcript."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_CHILD_SESSION", None)  # else the child hides from --resume and claude agents
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    env.update(extra or {})
    return env


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _wt_argv(title, cwd, argv, env_extra):
    # A wt tab gets the Windows Terminal environment, not ours: the env values go into the PowerShell command.
    # -EncodedCommand keeps ";" and quotes away from the wt parser.
    ps = ["Remove-Item Env:CLAUDE_CODE_CHILD_SESSION -ErrorAction SilentlyContinue",
          "$env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE = '1'"]
    for k, v in (env_extra or {}).items():
        ps.append(f"$env:{k} = {_ps_quote(v)}")
    ps.append("& " + " ".join(_ps_quote(a) for a in argv))
    enc = base64.b64encode("\n".join(ps).encode("utf-16-le")).decode()
    return ["wt", "-w", "0", "new-tab", "--title", title, "-d", cwd, "powershell", "-NoExit", "-EncodedCommand", enc]


def _tmux_argv(title, cwd, argv, env_extra):
    env = {"CLAUDE_CODE_FORCE_SESSION_PERSISTENCE": "1"}
    env.update(env_extra or {})
    sets = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
    # The shell stays open after claude exits, as the PowerShell tab does with -NoExit.
    line = f"unset CLAUDE_CODE_CHILD_SESSION; {sets} {shlex.join(argv)}; exec \"${{SHELL:-sh}}\""
    if os.environ.get("TMUX"):
        return [["tmux", "new-window", "-n", title, "-c", cwd, line]]
    has = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True).returncode == 0 \
        if shutil.which("tmux") else False
    if not has:
        return [["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", title, "-c", cwd, line]]
    return [["tmux", "new-window", "-t", TMUX_SESSION + ":", "-n", title, "-c", cwd, line]]


def plan(title, cwd, argv, env_extra=None):
    """The commands (a list of argv lists) that open `argv` in a new tab. No side effects except a tmux probe."""
    b = backend()
    if b == "wt":
        return [_wt_argv(title, cwd, argv, env_extra)]
    if b == "tmux":
        return _tmux_argv(title, cwd, argv, env_extra)
    if b == "print":
        return [["print", title, cwd] + list(argv)]
    raise ValueError(f"unknown terminal back end: {b} (use auto, wt, tmux or print)")


def open_tab(title, cwd, argv, env_extra=None):
    """Open `argv` (for example ["claude", "--resume", sid]) in a new tab titled `title`, in folder `cwd`.
    Returns the commands that ran. Raises RuntimeError when the back end is missing."""
    cmds = plan(title, cwd, argv, env_extra)
    b = backend()
    if b == "print":
        line = json.dumps({"title": title, "cwd": cwd, "argv": list(argv), "env": env_extra or {}})
        log = os.environ.get("HUBTREE_TERM_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        else:
            print(line)
        return cmds
    exe = "wt" if b == "wt" else "tmux"
    if not shutil.which(exe):
        raise RuntimeError(f"{exe} is not on PATH: install it, or set [hubtree] terminal in hubtree.toml")
    for c in cmds:
        if b == "wt":
            subprocess.Popen(c, env=child_env(), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            subprocess.run(c, env=child_env(), check=True, capture_output=True)
    return cmds


def start_detached(argv, cwd=None):
    """Start a process that outlives the caller (a hook, or a claude process that ends). Returns True if started."""
    kw = dict(close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=cwd)
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | NO_WINDOW  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB first; a job that forbids it refuses with an error
            try:
                subprocess.Popen(argv, creationflags=flags | extra, **kw)
                return True
            except OSError:
                continue
        return False
    try:
        subprocess.Popen(argv, start_new_session=True, **kw)
        return True
    except OSError:
        return False


def python():
    """The Python that runs hubtree (the pipx or venv interpreter)."""
    return sys.executable
