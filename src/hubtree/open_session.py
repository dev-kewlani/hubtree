"""Open a session in a new terminal tab. Works when the session's old folder was moved.

Usage: hubtree open --sid <session id> --cwd <old folder> --title <name> [--attach <background id>]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import term  # noqa: E402
import tree_lib as T  # noqa: E402


def open_tab(sid, cwd, title, attach=None, transcript=None):
    """Open a resumed session (or attach to a background one) in a new tab. Returns the folder it starts in."""
    if attach:
        folder, argv = T.resolve_cwd(cwd), ["claude", "attach", attach]
    else:
        folder, argv = T.prepare_resume(sid, cwd, transcript), ["claude", "--resume", sid] + term.claude_args()
    term.open_tab(title or sid[:8], folder, argv)
    return folder


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--cwd", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--attach")
    a = ap.parse_args()
    print(open_tab(a.sid, a.cwd, a.title, a.attach))
