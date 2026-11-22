"""hubtree: one tree for every Claude Code session you run.

The modules in this folder import each other by their plain names (tree_lib, swarm, ...), so this folder goes on
sys.path once. Use the `hubtree` command (cli.py); the modules are not a stable library API.
"""
import os
import sys

__version__ = "1.0.0"
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
