---
name: spawn
description: Start a child session in the hubtree session tree, as a new terminal tab or as a background auto session. Use when the user types /spawn, or asks to start a sub-session, hand a task to a new session or fork this one.
---

# /spawn

Start one child session of this session with the `hubtree` command.

1. Run `hubtree spawn --help` once. Use the option names it prints. Do not guess options.
2. Write a short task for the child from the user's goal. The task has three parts:
   - Goal: one sentence.
   - Done test: the check that shows the work is finished.
   - Budget: the most time, or money, that the child may use. Pass a money limit with `--budget-usd`.
3. Run `hubtree spawn --parent ${CLAUDE_SESSION_ID} --name <short-name> --task "<the task>"`.
   - Default: the child opens in a new terminal tab. The user works with it there.
   - Add `--auto` when the user wants a background session that runs without a tab.
   - Add `--fork` when the child must start with a copy of this session's chat.
   - Add `--after <name>` to queue the child until another session is released.
4. Read the one JSON line that the command prints. When `ok` is true, say the child name and its mode. When `ok` is false, show the `error` text and stop.

Rules:
- Start one child per request. Start more only when the user asks.
- Do not add flags that the user did not ask for.
- Do not edit the registry by hand.

The user can see the child in the tree: run `/where`, or open `hubtree` in a terminal.
