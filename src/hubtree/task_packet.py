"""The task packet of a child session. Standard library only.

  python task_packet.py show <session id or name>      print the task.json of that session
  python task_packet.py accept <session id or name>    the parent accepts the result: registry event + accepted true

spawn.py calls build() and write(). The packet sits next to the task file: tasks/<name>.task.json.
Fields: name, parent_id, parent_name, goal, done_test, out_of_scope, scope_path, budget, inputs, reply_to, created,
accepted. A field that the task text does not hold is null (inputs is an empty list).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _after(text, label):
    """The text after a line that starts with `label` (case-insensitive). If that line has no text after the
    label, the next lines up to a blank line or a heading. None when no line matches."""
    lines = text.splitlines()
    pat = re.compile(r"^[\s>#*\-]*" + re.escape(label) + r"\b[^:\n]*?[:\s\-]*(.*)$", re.I)
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if not m:
            continue
        rest = m.group(1).strip(" *`")
        if rest:
            return rest
        out = []
        for nxt in lines[i + 1:i + 8]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                break
            out.append(nxt.strip())
        return " ".join(out) or None
    return None


def _scope_path(text):
    m = re.search(r"([A-Za-z]:[\/][^\s`'\"]*SCOPE[^\s`'\"]*\.md|[^\s`'\"]*SCOPE[^\s`'\"]*\.md)", text)
    return m.group(1).rstrip(".,;)") if m else None


def _budget(text):
    m = re.search(r"Budget:\s*(.+)", text)
    return m.group(1).strip() if m else None


def build(name, parent_id, parent_name, task, created):
    task = task or ""
    return {"name": name, "parent_id": parent_id, "parent_name": parent_name, "goal": task.strip() or None,
            "done_test": _after(task, "Done test"), "out_of_scope": _after(task, "Out of scope"),
            "scope_path": _scope_path(task), "budget": _budget(task), "inputs": [],
            "reply_to": {"name": parent_name, "id": parent_id}, "created": created, "accepted": False}


def packet_path(task_file):
    return re.sub(r"\.md$", "", task_file) + ".task.json"


def write(task_file, packet):
    p = packet_path(task_file)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=1)
    return p


def _find(ref):
    import tree_lib as T
    s = T.sessions()
    return s.get(ref) or T.by_name(ref)


def _packet_file(sess):
    t = sess.get("task")
    return packet_path(t) if t else None


def show(ref):
    s = _find(ref)
    if not s:
        print(f"error: no session {ref}")
        return 1
    p = _packet_file(s)
    if not p or not os.path.exists(p):
        print(f"no task packet for {s.get('name')} (accepted: {bool(s.get('accepted'))})")
        return 1
    with open(p, encoding="utf-8") as f:
        print(f.read())
    return 0


def accept(ref):
    import tree_lib as T
    s = _find(ref)
    if not s:
        print(f"error: no session {ref}")
        return 1
    T.append({"event": "accept", "session_id": s["session_id"]})
    p = _packet_file(s)
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["accepted"] = True
        d["accepted_ts"] = T.now()
        write(s["task"], d)
    print(f"accepted {s.get('name')}")
    return 0


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) != 2 or a[0] not in ("show", "accept"):
        print(__doc__)
        return 2
    return show(a[1]) if a[0] == "show" else accept(a[1])


if __name__ == "__main__":
    sys.exit(main())
