"""Every test runs against a temporary hubtree home and state folder: no test touches a real tree."""
import os
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "hubtree")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_HOME = tempfile.mkdtemp(prefix="hubtree-test-home-")
os.environ["HUBTREE_HOME"] = _HOME
os.environ.setdefault("HUBTREE_STATE", os.path.join(_HOME, "state"))
os.environ.setdefault("HUBTREE_ROOT", os.path.join(_HOME, "root"))
os.environ.setdefault("HUBTREE_DB", os.path.join(_HOME, "state", "hubtree.db"))
os.environ["HUBTREE_TERMINAL"] = "print"
os.environ["HUBTREE_NO_DAEMON"] = "1"
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(_HOME, "claude")  # never the real Claude Code folder
os.makedirs(os.environ["HUBTREE_ROOT"], exist_ok=True)
