"""`hubtree daemon`: one small loop for the work that no session does by itself.

Every tick ([daemon] tick_s, default 60 s), each job runs as a child process with a time limit:
    swarm.py tick          the queue: start a queued child when its dependencies released and a slot is free
    swarm_obs.py enforce   budgets: a mark at the warn share, a pause at 100%, one message to the parent
    autopilot.py scan      every 5 minutes: continue background sessions after a usage limit resets
The local page (`swarm_web.py serve`) runs as a child when [web] enabled = true and a token exists.

One daemon per state folder: <state>/daemon.pid holds its pid. `ensure_running()` (called by the SessionStart hook)
starts it when no live daemon holds the file. `hubtree daemon --once` runs one tick and exits.
Log: <state>/daemon.log (one line per job run; the file is cut to its last 2,000 lines at start).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import htconf  # noqa: E402

AUTOPILOT_EVERY_S = 300
JOB_TIMEOUT_S = 240
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _state():
    return htconf.load().state


def pid_file():
    return os.path.join(_state(), "daemon.pid")


def log_file():
    return os.path.join(_state(), "daemon.log")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    try:
        os.makedirs(_state(), exist_ok=True)
        with open(log_file(), "a", encoding="utf-8") as f:
            f.write(f"{_now()} {msg}\n")
    except OSError:
        pass


def _alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _read_pid():
    try:
        with open(pid_file(), encoding="utf-8") as f:
            return int(json.load(f).get("pid") or 0)
    except (OSError, ValueError, AttributeError):
        return 0


def status():
    pid = _read_pid()
    if pid and _alive(pid):
        return f"running (pid {pid})"
    return "not running"


def stop():
    """Stop the daemon by its own pid file (never by name). Returns one line."""
    pid = _read_pid()
    if not pid or not _alive(pid):
        return "daemon: not running"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return f"daemon: stop failed ({e})"
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    try:
        os.remove(pid_file())
    except OSError:
        pass
    return f"daemon: stopped pid {pid}"


def ensure_running():
    """Start the daemon in the background when none runs. Never raises. Returns True if it started one."""
    try:
        if _alive(_read_pid()):
            return False
        import term
        return term.start_detached([sys.executable, os.path.join(HERE, "daemon.py")], cwd=HERE)
    except Exception:
        return False


def run_job(name, argv, timeout=JOB_TIMEOUT_S):
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, argv[0])] + argv[1:], capture_output=True, text=True,
                           timeout=timeout, cwd=HERE, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        log(f"{name} exit {r.returncode} in {time.time() - t0:.1f} s {tail[0][:200]}")
        return r.returncode
    except subprocess.TimeoutExpired:
        log(f"{name} timeout after {timeout} s")
        return -1


class Web:
    """The `swarm_web.py serve` child, kept alive while [web] enabled = true."""

    def __init__(self):
        self.p = None
        self.next_try = 0.0

    def tick(self, now):
        web = htconf.load().raw.get("web") or {}
        if not web.get("enabled", False):
            return
        if self.p and self.p.poll() is None:
            return
        if now < self.next_try:
            return
        if self.p:
            log(f"web server pid {self.p.pid} stopped (exit {self.p.returncode})")
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "swarm_web.py"), "serve"], cwd=HERE,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=NO_WINDOW)
        log(f"web server started, pid {self.p.pid}")
        self.next_try = now + 60

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()


def tick(now, last, web=None):
    """One round of the jobs. `last` holds the time of the last autopilot scan."""
    run_job("swarm", ["swarm.py", "tick"])
    run_job("budget", ["swarm_obs.py", "enforce"])
    if now - last.get("autopilot", 0) >= AUTOPILOT_EVERY_S:
        run_job("autopilot", ["autopilot.py", "scan"])
        last["autopilot"] = now
    if web:
        web.tick(now)


def _trim_log(keep=2000):
    try:
        with open(log_file(), encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(log_file(), "w", encoding="utf-8") as f:
                f.writelines(lines[-keep:])
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hubtree daemon")
    ap.add_argument("--once", action="store_true", help="run one tick and exit")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args(argv)
    if a.status:
        print(status())
        return 0
    if a.stop:
        print(stop())
        return 0
    last = {}
    if a.once:
        tick(time.time(), last)
        return 0
    other = _read_pid()
    if other and other != os.getpid() and _alive(other):
        print(f"daemon: already running (pid {other})")
        return 0
    os.makedirs(_state(), exist_ok=True)
    with open(pid_file(), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "started": _now()}, f)
    _trim_log()
    log(f"daemon start pid {os.getpid()}")
    web = Web()
    step = (htconf.load().raw.get("daemon") or {}).get("tick_s", 60)
    step = step if isinstance(step, (int, float)) and step >= 5 else 60
    try:
        while True:
            t0 = time.time()
            if _read_pid() != os.getpid():  # another daemon took over, or uninstall removed the file
                log("pid file changed: exit")
                break
            try:
                tick(t0, last, web)
            except Exception as e:  # noqa: BLE001
                log(f"tick error {type(e).__name__}: {e}")
            time.sleep(max(1.0, step - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        web.stop()
        log("daemon stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
