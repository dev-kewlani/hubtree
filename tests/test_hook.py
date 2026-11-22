"""Test of session_hook.py (start and end) against the temporary registry of conftest.py."""
import json
import os

import session_hook
import tree_lib as T


def _out(data):
    return data["hookSpecificOutput"]


def _node_dir(*parts):
    d = os.path.join(T.HUB, *parts)
    os.makedirs(d, exist_ok=True)
    return d


def test_root_session_gets_a_hub_name():
    o = _out(session_hook.start({"session_id": "A", "cwd": T.HUB, "source": "startup", "session_title": ""}))
    assert o["sessionTitle"] == "hub"
    assert "hubtree: this session is `hub`" in o["additionalContext"]


def test_node_session_second_one_gets_session_suffix():
    cwd = _node_dir("work", "app")
    first = _out(session_hook.start({"session_id": "B", "cwd": cwd, "source": "startup", "session_title": ""}))
    assert first["sessionTitle"] == "work/app"
    # a second session in the same folder with a plain title is named by its title under the node
    second = _out(session_hook.start({"session_id": "B2", "cwd": cwd, "source": "startup", "session_title": "plan"}))
    assert second["sessionTitle"] == "work/app/plan"


def test_spawned_child_takes_the_pending_name_and_parent():
    cwd = _node_dir("work", "app")
    session_hook.start({"session_id": "B", "cwd": cwd, "source": "startup", "session_title": ""})
    os.makedirs(T.PENDING, exist_ok=True)
    name = "work/app/s07-construct"
    with open(os.path.join(T.PENDING, T.slug(name, 200) + ".json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "parent": "B", "auto": False, "fork": False, "level": 3}, f)
    o = _out(session_hook.start({"session_id": "C", "cwd": cwd, "source": "startup", "session_title": name}))
    assert o["sessionTitle"] == name
    assert "Parent: `work/app`" in o["additionalContext"]
    assert not os.path.exists(os.path.join(T.PENDING, T.slug(name, 200) + ".json"))  # the pending file is taken


def test_resume_keeps_the_name():
    cwd = _node_dir("work", "app")
    session_hook.start({"session_id": "B", "cwd": cwd, "source": "startup", "session_title": ""})
    o = _out(session_hook.start({"session_id": "B", "cwd": cwd, "source": "resume", "session_title": "x"}))
    assert o["sessionTitle"] == "work/app"


def test_level_is_the_folder_depth():
    cwd = _node_dir("work", "app")
    o = _out(session_hook.start({"session_id": "D", "cwd": cwd, "source": "startup", "session_title": ""}))
    assert "level 2" in o["additionalContext"]


def test_end_closes_only_that_session_and_entry_files_are_listed():
    cwd = _node_dir("work", "app")
    with open(os.path.join(cwd, "CLAUDE.md"), "w", encoding="utf-8") as f:
        f.write("# app\n")
    session_hook.start({"session_id": "B", "cwd": cwd, "source": "startup", "session_title": ""})
    session_hook.start({"session_id": "C", "cwd": cwd, "source": "startup", "session_title": "other"})
    session_hook.end({"session_id": "C", "reason": "prompt_input_exit"})
    s = T.sessions()
    assert not s["C"]["open"]
    assert s["B"]["open"]
    assert [os.path.basename(f) for f in T.entry_files("work/app")] == ["CLAUDE.md"]
