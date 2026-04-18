"""Pytest rootdir conftest — ensures tests run against THIS worktree's source,
not the sibling editable install rooted at ~/Projects/agent-core.

sitecustomize.py prepends the main project path to sys.path on venv init,
which shadows the worktree copy of core/, handler/, swarm/, home_ops/, etc.
We move the worktree path to the front so `import core.*` resolves here.
"""

import sys
from pathlib import Path

_WORKTREE_ROOT = str(Path(__file__).resolve().parent)
_MAIN_ROOT = "/Users/johncornelius/Projects/agent-core"

sys.path[:] = [p for p in sys.path if p != _WORKTREE_ROOT and p != _MAIN_ROOT]
sys.path.insert(0, _WORKTREE_ROOT)

# Evict anything the main checkout's sitecustomize may have already imported.
for mod_name in list(sys.modules):
    if mod_name == "core" or mod_name.startswith((
        "core.", "handler.", "swarm.", "home_ops.", "daemon.", "nudge.", "briefs.",
    )):
        del sys.modules[mod_name]
