"""The documents a newcomer reads name no one's machine.

Every Markdown file at the root, everything under `docs/`, and the issue and PR templates. In a git
checkout only tracked files count, so a contributor's untracked scratch note cannot fail the suite.
A release's source tarball has no `.git`, and there every file in the tree counts.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A home folder or a per-user temporary folder, spelled any of the ways a path into one gets
# written. `~/.substrate`, the engine's own folder, and `~/.cache/`, where uv and the model libraries
# keep theirs, name no machine.
HOME = re.compile(
    r"/Users/|/home/\w|/var/folders/|[A-Za-z]:\\Users\\|\$\{?HOME\}?/"
    r"|~/(?!\.substrate(?![\w.-])|\.cache/)[\w.-]"
)
# Agent guidance that still describes the maintainer's machine. Rewriting it is its own change.
EXEMPT = {"CLAUDE.md"}


NAMED = ("README.md", "SECURITY.md", "RELEASING.md", "docs/history/README.md",
         ".github/pull_request_template.md")


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                              check=True).stdout
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError):
        return None


def _tracked() -> set[str] | None:
    """The tracked paths, or None when this tree is not the root of its own checkout (a source
    tarball, or a copy inside some other repository)."""
    top = _git("rev-parse", "--show-toplevel")
    if top is None or Path(top.strip()).resolve() != ROOT:
        return None
    out = _git("ls-files", "-z")
    return None if out is None else set(out.split("\0")) - {""}


def _documents() -> list[Path]:
    found = [p for p in sorted(ROOT.glob("*.md")) if p.name not in EXEMPT]
    # `docs/history/`'s dated records keep their original text and are not scanned; its index is.
    found += [p for p in sorted((ROOT / "docs").rglob("*.md"))
              if p.relative_to(ROOT / "docs").parts[0] != "history"]
    found += [ROOT / name for name in NAMED[3:]]
    found += [p for p in sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*")) if p.is_file()]
    tracked = _tracked()
    if tracked is None:
        return [p for p in found if p.is_file()]
    # A new document counts once it is staged, as `git add -N` or `git add` leaves it.
    return [p for p in found if p.relative_to(ROOT).as_posix() in tracked]


def test_the_public_documents_name_no_ones_machine() -> None:
    documents = _documents()
    names = {p.relative_to(ROOT).as_posix() for p in documents}
    if _tracked() is not None:
        assert set(NAMED) <= names, set(NAMED) - names
    assert {"README.md", "SECURITY.md", "RELEASING.md"} <= names
    for path in documents:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not HOME.search(line), f"{path.relative_to(ROOT).as_posix()}:{number}: {line}"


def test_the_home_pattern_allows_only_the_engine_and_cache_folders() -> None:
    for spelling in ("/Users/someone/notes", "/home/Someone", "/home/someone", "$HOME/notes",
                     "${HOME}/notes", "~/vaults/core", "~/Documents/x", "~/.substrate-old/x",
                     "~/.substrate.bak", "/var/folders/ab/T/tmp.x",
                     "C:\\Users\\someone"):
        assert HOME.search(spelling), spelling
    for spelling in ("~/.substrate/serve.toml", "~/.substrate", "`~/.substrate`", "~/.cache/uv",
                     "~/.cache/torch", "/Volumes/External/models", "uv run"):
        assert not HOME.search(spelling), spelling
