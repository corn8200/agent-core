"""Pytest rootdir conftest — make sure tests resolve `core.*` against this
worktree, not any other agent-core copy that may be installed editable in
the same venv."""

import sys
from pathlib import Path

_WORKTREE_ROOT = str(Path(__file__).resolve().parent)

sys.path[:] = [p for p in sys.path if p != _WORKTREE_ROOT]
sys.path.insert(0, _WORKTREE_ROOT)

for mod_name in list(sys.modules):
    if mod_name == "core" or mod_name.startswith(("core.",)):
        del sys.modules[mod_name]
