# hubtree

Running agents is easy. Managing them is where almost everyone struggles.

hubtree puts every Claude Code session you run into one tree. Each child knows its parent, its task, its budget and what it waits for, and its result comes back up to you to accept or send back.

## What it does

- **A tree, not a list.** Every session knows the session that started it. A folder row counts the sessions below it that are stuck or wait for you.
- **Work goes down with a task file.** `/spawn` writes the goal, the done test and the budget into a file first, then starts the child in a new tab or in the background.
- **Results come back up.** A child posts a result. You accept it, ask for a change, or reject it.
- **Cost per sub-tree, and budgets.** A child pauses at 100% of its budget, and its parent gets one message.
- **Dependencies and a queue.** "Start B when A has released", with limits per folder and in total.
- **Group actions.** Pause, resume, stop or message a whole sub-tree.
- **Health marks.** `YOU` (waits for you), `STUCK` (busy with no output past a time limit), `died` (stopped in the middle of a turn).
- **Long sessions continue.** A new session with the same name takes over near the context limit, and a session continues by itself after a usage limit resets.

Claude Code has its own agent view (`claude agents`), which shows what runs and what needs you. hubtree uses the same sessions and adds who started what, what it cost, what waits on what, and what came back.

## Install

You need Python 3.11 or newer, Claude Code, and a terminal back end: Windows Terminal on Windows, tmux on macOS and Linux.

```
pipx install hubtree        # or: uv tool install hubtree
cd ~/projects               # the folder whose sub-folders become the tree
hubtree init
```

`hubtree init` does these steps and prints one line for each:

1. Writes `~/.hubtree/hubtree.toml`. The tree root is the folder where you ran `init` (`--root DIR` sets another one).
2. Copies your Claude Code `settings.json` to `~/.hubtree/backup/` before it changes anything.
3. Adds 6 hook entries to `settings.json` (session start and end, prompt, stop, pre-compact, and file writes). Each one runs `python -m hubtree hook <event>`.
4. Installs the side pane as a Claude Code plugin from `~/.hubtree/pane` (`--no-pane` skips it).
5. Copies the `/spawn` and `/where` skills into `~/.claude/skills/`. It never overwrites a skill with the same name.

The small daemon (`hubtree daemon`) starts with your next Claude Code session. It starts queued children, checks budgets and continues sessions after a usage limit.

Check the install with `hubtree doctor`.

## Use

- Use Claude Code as normal. Every session joins the tree. A session in `~/projects/work/api` sits under the node `work/api`.
- `/spawn` in a session starts a child: in a new tab, or in the background with `--auto`.
- `hubtree` opens the tree app in the terminal. `hubtree work` shows only the `work` sub-tree.
- `/where` (or `hubtree where`) prints the tree as text.
- `/tree` opens or closes the side pane in a Claude Code session.
- `hubtree web` serves a local page on 127.0.0.1 with a token login. It is off by default (`[web] enabled` in `hubtree.toml`).

Nothing leaves your machine. hubtree has no account and no server. It reads the transcripts that Claude Code already writes under `~/.claude/projects`.

## A short replay (stand-in names)

```
you      > /spawn "add rate limits to the api" --budget-usd 5
           started work/api/rate-limits in a new tab (task file written)
you      > /spawn "write the load test" --auto --after work/api/rate-limits
           queued work/api/load-test: waits for rate-limits (released)
...      rate-limits posts a result: "limits on 4 routes, tests pass, commit 3f2c1a9"
you      > hubtree      (the row shows "result ready"; press A to accept)
           accepted; load-test starts in the background
...      load-test reaches 80% of its budget: one message to its parent
```

## Configuration

`~/.hubtree/hubtree.toml` holds every setting, with comments. The main keys:

| Key | Default | Meaning |
|---|---|---|
| `[hubtree] root` | the folder of `hubtree init` | The folder whose sub-folders become the nodes |
| `[hubtree] levels` | 2 | How many folder levels below the root make a node |
| `[hubtree] terminal` | `auto` | `wt`, `tmux` or `auto` (wt on Windows, tmux elsewhere) |
| `[hubtree] claude_args` | `[]` | Extra arguments for every session that hubtree starts |
| `[tree] stuck_min` | 20 | Minutes with no output while busy before a row shows STUCK |
| `[swarm] max_working_per_node` | 3 | Background children that work at once per node; more wait in the queue |
| `[swarm] max_working_total` | 8 | Background children that work at once in total |
| `[web] enabled` | false | Keep the local page running |

hubtree starts sessions with your normal Claude Code permission settings. It adds no permission flag by itself. To add one, put it in `claude_args`.

## Uninstall

```
hubtree uninstall           # --purge also removes ~/.hubtree (the tree state)
pipx uninstall hubtree
```

`hubtree uninstall` stops the daemon, removes the plugin and the skills it copied, and removes its hook entries. When you made no other change to `settings.json` since `init`, the file is byte for byte the same as the backup. When you did make other changes, it keeps them and removes only the hubtree entries.

## Platforms

Windows (Windows Terminal), macOS and Linux (tmux). Tested with Claude Code 2.1.330.

## License

MIT
