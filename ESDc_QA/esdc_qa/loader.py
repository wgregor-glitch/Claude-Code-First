"""Read ESDC definitions from a local Git checkout (read-only).

Pull the committed expansion.json straight from a git ref via `git show`, so QA
can run against the source of truth (prod) or a branch (staged) without a manual
download. This is strictly read-only — it never writes, commits, or pushes.
"""
from __future__ import annotations

import subprocess
from typing import Dict

from .expansion_loader import load_expansion_text
from .model import ESDC


def git_show(repo: str, path_in_repo: str, ref: str = "HEAD") -> str:
    """Return the raw file contents at `ref` from the git checkout at `repo`
    (read-only). `ref` may be a branch/tag/commit. Run `git fetch` first for
    remote-tracking branches."""
    return subprocess.check_output(
        ["git", "-C", repo, "show", f"{ref}:{path_in_repo}"],
        text=True, stderr=subprocess.STDOUT,
    )


def load_expansion_from_git(repo: str, path_in_repo: str, ref: str = "HEAD") -> Dict[str, ESDC]:
    """Parse the ESDC definitions at `ref` from the git checkout at `repo`."""
    return load_expansion_text(git_show(repo, path_in_repo, ref))
