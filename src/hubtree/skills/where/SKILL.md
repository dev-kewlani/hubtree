---
name: where
description: Show the hubtree session tree as text, with the state of each session. Use when the user types /where, or asks what is running, to show the tree, or which sessions need them.
---

# /where

Show the session tree as plain text.

1. Run `hubtree where`.
2. Show the command output to the user as it is, in a code block. Do not change it.
3. After the output, add at most 2 lines: how many sessions wait for the user (marks YOU, STUCK, died), and the name of the first one.

State marks in the output:
- tab: open in a terminal tab.
- work: a background session that is working.
- YOU: the session needs the user.
- idle: a background session that is idle.
- STUCK: busy with no transcript write for its limit.
- died: stopped in the middle of a turn.
- closed: finished or closed. Open it again with `hubtree open`, or from the `hubtree` app.

When the command fails, show its error text and stop.
