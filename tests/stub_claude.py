"""A stand-in for the `claude` command in the end-to-end test (no login, no model call).

    --bg ... --name N ... PROMPT   start a "background session": run the SessionStart hook of settings.json with
                                   the title N, as Claude Code does, and print the new id
    agents --json                  the live list: every stub session is idle
    stop ID                        print ok
Env STUB_DIR: the folder of stub_sessions.json (name -> id). Env HUBTREE_E2E_SETTINGS: the settings.json to read.
"""
import json
import os
import subprocess
import sys
import uuid


def main(a):
    d = os.environ.get("STUB_DIR") or "."
    book = os.path.join(d, "stub_sessions.json")
    known = json.load(open(book, encoding="utf-8")) if os.path.exists(book) else {}
    if a[:1] == ["agents"]:
        print(json.dumps([{"sessionId": sid, "kind": "background", "state": "idle"} for sid in known.values()]))
        return 0
    if a[:1] == ["stop"]:
        print("ok")
        return 0
    if "--bg" in a:
        name = a[a.index("--name") + 1] if "--name" in a else "bg"
        sid = str(uuid.uuid4())
        known[name] = sid
        with open(book, "w", encoding="utf-8") as f:
            json.dump(known, f)
        sp = os.environ["HUBTREE_E2E_SETTINGS"]
        s = json.load(open(sp, encoding="utf-8"))
        cmd = next(h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"] if "hubtree hook" in h["command"])
        payload = json.dumps({"session_id": sid, "cwd": os.getcwd(), "source": "startup", "session_title": name})
        argv = cmd if os.name == "nt" else ["sh", "-c", cmd]
        r = subprocess.run(argv, shell=os.name == "nt", input=payload, capture_output=True, text=True)
        print(f"started {sid}")
        return r.returncode
    print("stub claude: unknown call " + " ".join(a), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
