"""The one config file of hubtree: hubtree.toml. No secrets live in it.

Where things live:
    home    env HUBTREE_HOME, else ~/.hubtree. It holds hubtree.toml and, by default, the state folder.
    config  <home>/hubtree.toml (written by `hubtree init`; a missing file gives the defaults).
    root    [hubtree] root: the folder whose sub-folders make the tree nodes (default: the user home).
    state   env HUBTREE_STATE, else [hubtree] state, else <home>/state: the registry, task files, the message db.

Use:
    from htconf import load
    cfg = load()           # cached; cfg.root, cfg.state, cfg.raw (the whole toml as a dict)
    cfg.raw.get("tree", {}).get("stuck_min", 20)

A broken config file gives the defaults (the session hooks must never stop a session); `hubtree doctor`
reports the parse error.
"""
import functools
import os
import tomllib

FILE_NAME = "hubtree.toml"

DEFAULT_TOML = """\
# hubtree config. Every key is optional; a missing key takes the default shown here.

[hubtree]
root = "{root}"          # the folder whose sub-folders become the tree nodes
state = ""               # empty: <HUBTREE_HOME>/state (registry, task files, messages)
levels = 2               # folder depth below the root that makes a node: root/work/app -> node "work/app"
terminal = "auto"        # auto | wt | tmux: where `hubtree` opens a session (auto: wt on Windows, else tmux)
claude_args = []         # extra arguments for every session hubtree starts, for example ["--model", "sonnet"]

[tree]                   # health marks
stuck_min = 20           # minutes with no transcript write while busy: STUCK
died_days = 7            # an older stop in the middle of a turn shows as closed, not died
alert_keep_days = 30     # alert flag files older than this are removed

[swarm]                  # queue and limits
max_working_per_node = 3 # working background children per node; more wait as queued
max_working_total = 8    # working background children in total
conflict_window_min = 30 # two live sessions that write one file within this many minutes: CONFLICT

[rotation]               # a new session with the same name instead of a compaction
enabled = true
threshold = 800000       # context tokens at a prompt that start a rotation
handoff_tokens = 60000

[autopilot]
continue_after_limit = true  # continue a session after a usage limit resets

[web]                    # the local page; it listens on 127.0.0.1 only
enabled = false          # true: the daemon keeps the page running (hubtree web --help)
port = 8765
hosts = []
stale_min = 10

[daemon]
tick_s = 60              # seconds between two daemon ticks (queue, budgets, auto-continue)
"""


class ConfigError(ValueError):
    pass


def home():
    env = os.environ.get("HUBTREE_HOME")
    return os.path.normpath(os.path.abspath(os.path.expanduser(env or "~/.hubtree")))


def config_path():
    return os.path.join(home(), FILE_NAME)


class Config:
    def __init__(self, raw, error=None):
        self.raw = raw or {}
        self.error = error
        ht = self.raw.get("hubtree") or {}
        root = ht.get("root") or "~"
        self.root = os.path.normpath(os.path.abspath(os.path.expanduser(root)))
        st = os.environ.get("HUBTREE_STATE") or ht.get("state") or os.path.join(home(), "state")
        self.state = os.path.normpath(os.path.abspath(os.path.expanduser(st)))
        lv = ht.get("levels", 2)
        self.levels = lv if isinstance(lv, int) and not isinstance(lv, bool) and lv > 0 else 2
        self.terminal = ht.get("terminal") or "auto"
        ca = ht.get("claude_args") or []
        self.claude_args = [str(x) for x in ca] if isinstance(ca, list) else []


def _read(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f), None
    except FileNotFoundError:
        return {}, None
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {}, f"{type(e).__name__}: {e}"


@functools.lru_cache(maxsize=4)
def _load(path, state_env, root_env):
    raw, err = _read(path)
    if root_env:
        raw.setdefault("hubtree", {})["root"] = root_env
    return Config(raw, err)


def load():
    """The config (cached per config path and env). Env HUBTREE_ROOT overrides [hubtree] root (tests)."""
    return _load(config_path(), os.environ.get("HUBTREE_STATE"), os.environ.get("HUBTREE_ROOT"))


def reload():
    _load.cache_clear()
    return load()


def hub_root():
    """The tree root folder (the name stays from the code this package came from)."""
    return load().root


def default_text(root):
    return DEFAULT_TOML.replace("{root}", root.replace("\\", "/"))
