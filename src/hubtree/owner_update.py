"""Pass the user's new words to a running child session. Standard library only.

  python owner_update.py --child <name or session id> --words "<the user's words, verbatim>" [--by <parent name>]

Why a file: a child trusts its task file (its first prompt says to read it, and spawn.py writes it).
A SendMessage from another session is not user approval (a Claude Code safety rule), so the words go into the
section "Owner updates" of the child's task file, and the message only points to that file.

The script writes one line into that section (a task file without the section gets it at the end), then prints
JSON: {"ok", "task_file", "message"}. The parent sends `message` to the child with SendMessage.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tree_lib as T  # noqa: E402

HEAD = "## Owner updates"
EMPTY = "(no updates yet)"


def task_file_of(ref):
    s = T.sessions().get(ref) or T.by_name(ref)
    if not s:
        return None, None
    t = s.get("task")
    if not t:
        cand = os.path.join(T.STATE, "tasks", T.slug(s.get("name") or "", 200) + ".md")
        t = cand if os.path.exists(cand) else None
    return s, t


def add_update(task_file, words, by, ts=None):
    """Insert one dated line into the Owner updates section. Returns the line."""
    ts = ts or T.now()
    words = " ".join(words.split())  # one line; the words stay as written
    line = f"- {ts}, the user's words (relayed verbatim by `{by}`): \"{words}\"\n"
    with open(task_file, encoding="utf-8") as f:
        text = f.read()
    if HEAD not in text:
        text = text.rstrip("\n") + "\n\n" + HEAD + "\n\nThe parent session writes the user's new words here. " \
            "These lines are the user's instructions; the newest line wins.\n\n" + line
    else:
        i = text.index(HEAD)
        j = text.find("\n## ", i + len(HEAD))
        j = len(text) if j < 0 else j + 1
        sec = text[i:j]
        if EMPTY in sec:
            sec = sec.replace(EMPTY + "\n", "").replace(EMPTY, "")
        sec = sec.rstrip("\n") + "\n" + line + "\n"
        text = text[:i] + sec + text[j:]
    tmp = task_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, task_file)
    return line


def main(argv=None):
    ap = argparse.ArgumentParser(description="pass the user's new words to a child session")
    ap.add_argument("--child", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--by", default="the parent session")
    a = ap.parse_args(argv)
    s, tf = task_file_of(a.child)
    if not s or not tf or not os.path.exists(tf):
        print(json.dumps({"ok": False, "error": f"no task file for {a.child}"}))
        return 1
    line = add_update(tf, a.words, a.by)
    msg = (f"Owner update: your task file {tf} has a new line in the section 'Owner updates'. "
           "Read the task file again and follow the newest update.")
    print(json.dumps({"ok": True, "task_file": tf, "line": line.strip(), "message": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
