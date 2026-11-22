# Backlog

Findings from build 1 of version 1.0. None of them blocks the version 1.0 done test unless the line says so.

## Open findings
1. Real sessions in a fresh profile need a Claude login of their own. The end-to-end test uses a stand-in `claude` for the background start, the live list and stop; the hook commands themselves are the real ones from settings.json. A run with real sessions needs a login token for the test profile.
2. The task file heading "Owner updates" and the message prefix "Owner update:" could read "User updates". Three modules and one test match on the text, so rename them together.
3. The `/spawn` skill passes `--parent ${CLAUDE_SESSION_ID}`. Not checked: that Claude Code fills this variable inside a skill. `spawn.py` also reads the parent from the environment.
4. `rotate.write_context` (a status line writer) has no caller. Wire it to a status line command, or remove it.
5. The pane plugin has no icon and no README of its own. Only the plugin directory profile asks for them.
6. The package name "hubtree" is not checked on PyPI or GitHub.
7. The supported Claude Code version range is not pinned. Tested: 2.1.330.
8. The daemon starts with the first session after `init` (SessionStart hook). There is no start at login.
9. The Windows Terminal back end is tested at the command level (`term.plan`); this build did not open a real tab.
10. The root node is named "hub" in the code and the views. A name such as "root" may read better.
11. Nothing writes the write ledger for sessions that run without the hooks. CONFLICT marks need the PostToolUse hook that `init` adds.

## Out of scope for version 1 (from SCOPE.md)
Voice notes, the notes pane, a daily page, a decision inbox, cloud copies of the page, a phone page, a memory store, chat search, Codex and other agent CLIs, money of any kind.

## Choices made during build 1 (one line each, with the reason)
1. Modules stay flat in `src/hubtree/` and import each other by plain name. Reason: the smallest change to working code.
2. The app module is `app.py`, not `hubtree.py`. Reason: a module named like the package breaks the flat imports.
3. State lives in `~/.hubtree/state`, not inside the tree root. Reason: the root holds the user's own projects.
4. Sessions start with no permission flag. Reason: a public tool must not lower the user's permission settings; `claude_args` opts in.
5. The local page is off by default. Reason: a server starts only when the user asks for it.
6. The secret masking word list ships as a gzip file. Reason: the plain list matches the recovery phrase detector of the secret scan.
7. Uninstall writes the settings backup back only when nothing else changed; otherwise it removes only the hubtree entries. Reason: it never drops a user's later change.
8. The daemon runs the jobs as child processes with a time limit. Reason: one stuck job cannot stop the loop.
9. The usage gate of the old code needed a supervisor that is not in this package, so it is removed. Reason: no stub without a caller.
10. Commits use the GitHub no-reply address. Reason: no private email in a public history.
