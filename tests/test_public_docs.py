"""The files a newcomer reads name no one's machine.

Every Markdown file at the root, everything under `docs/`, and the issue and PR templates. Every
other file is scanned too, for the paths a person's home folder gives, and a nested `CLAUDE.md` or
`AGENTS.md` as strictly as a document. Left out: `docs/history/`, whose dated records keep their
original text, and what is in `tests/` apart from those two names, because tests exercise path
handling with invented paths. In a git checkout only tracked files count, so a contributor's
untracked scratch note cannot fail the suite. A release's source tarball has no `.git`, and there
every file counts but those in the folders tools and builds write.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The paths a person's home folder or per-user temporary folder gives, as literal text. A miss is
# silent and a false match is not, so only the two spellings that ordinary text also uses, a
# lowercase `/users/` and `/root/`, are let through when a name runs into them, as in a URL's
# `github.com/users/` or a tree's `build/root/`. `/Users/Shared` is no one's home: macOS makes it
# for every account, and this repository names it on purpose, as where the agent's snapshot is shared
# (`tools/atlas-agent-substrate-mcp`) and where the engine's pinned interpreter is installed
# (`tools/engine-interpreter.pin`). `<name>`, `$USER`, `{name}`, `*` and `...` after `/Users/` are
# placeholders.
_LITERAL = (r"(?:/Users/|(?<![\w.}-])/(?i:users)/)(?!Shared(?![\w-]|\.\w)|<|\$|\{|\*|\.\.\.)"
            r"|/home/\w|(?<![\w.}-])/root/|/var/folders/|(?i:[A-Z]:\\{1,2}Users\\{1,2}(?![\\<]))")
# A document also may not point into a home folder through `$HOME` or `~`. `~/.substrate`, the
# engine's own folder, and `~/.cache/`, where uv and the model libraries keep theirs, name no machine.
HOME = re.compile(_LITERAL + r"|\$\{?HOME\}?/|~/(?!\.substrate(?![\w.-])|\.cache/)[\w.-]")
# Code and configuration say `$HOME/.substrate/engine` and `~/.local/bin`, which name nobody, so
# only literal paths count there, and a mounted volume's, which documents may name as an example.
HOST_PATH = re.compile(_LITERAL + r"|/Volumes/\w")
# What a tree with no `.git` to say what is tracked skips: the folders git, tools and builds write.
# A worktree's `.git` is a file.
_UNTRACKED = {".git", ".venv", "node_modules", "__pycache__", ".demo", ".pytest_cache",
              ".hypothesis", ".ruff_cache", ".claude", ".lake", "out", "out-vault", "dist", "bin",
              ".baseline.json"}
GUIDANCE_NAMES = {"CLAUDE.md", "AGENTS.md"}

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
    found = sorted(ROOT.glob("*.md"))
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


def _hits(path: Path, pattern: re.Pattern[str], root: Path) -> list[str]:
    """`file:line` for each match. Never the line itself, which CI's log would keep. A symlink's
    target is checked as line 0, then what it points at; a stray byte that is not UTF-8 does not
    hide the rest of a file."""
    lines = [(0, os.readlink(path))] if path.is_symlink() else []
    if path.is_file():
        lines += enumerate(path.read_bytes().decode("utf-8", errors="replace").splitlines(), 1)
    return [f"{path.relative_to(root).as_posix()}:{number}"
            for number, line in lines if pattern.search(line)]


def test_the_public_documents_name_no_ones_machine() -> None:
    documents = _documents()
    names = {p.relative_to(ROOT).as_posix() for p in documents}
    if _tracked() is not None:
        assert set(NAMED) <= names, set(NAMED) - names
    assert {"README.md", "SECURITY.md", "RELEASING.md"} <= names
    assert [hit for path in documents for hit in _hits(path, HOME, ROOT)] == []


def test_the_home_pattern_allows_only_the_engine_and_cache_folders() -> None:
    for spelling in ("/Users/someone/notes", "/home/Someone", "/home/someone", "$HOME/notes",
                     "${HOME}/notes", "~/vaults/core", "~/Documents/x", "~/.substrate-old/x",
                     "~/.substrate.bak", "/var/folders/ab/T/tmp.x", "/users/someone/notes", "/root/x",
                     "C:\\Users\\someone"):
        assert HOME.search(spelling), spelling
    for spelling in ("~/.substrate/serve.toml", "~/.substrate", "`~/.substrate`", "~/.cache/uv",
                     "~/.cache/torch", "/Volumes/External/models", "uv run"):
        assert not HOME.search(spelling), spelling


def _scan_targets(root: Path, tracked: set[str] | None) -> list[Path]:
    """Every file but the Markdown at the root and under `docs/`, less what the module docstring
    leaves out. `tracked` is the set of tracked paths, or None for a tree with no checkout."""
    if tracked is None:
        names = []
        for here, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in _UNTRACKED)
            rel = Path(here).relative_to(root)
            # A link to a folder is listed among the folders and never entered.
            links = [d for d in dirs if os.path.islink(os.path.join(here, d))]
            names += [(rel / f).as_posix() for f in sorted(files + links) if f not in _UNTRACKED]
    else:
        names = sorted(tracked)
    chosen = []
    for name in names:
        parts, path = name.split("/"), root / name
        document = name.endswith(".md") and (len(parts) == 1 or parts[0] == "docs")
        left_out = (name.startswith("docs/history/")
                    or (parts[0] == "tests" and parts[-1] not in GUIDANCE_NAMES))
        if not document and not left_out and (path.is_file() or path.is_symlink()):
            chosen.append(path)
    return chosen


def _host_paths(root: Path, tracked: set[str] | None) -> list[str]:
    return [hit for path in _scan_targets(root, tracked)
            for hit in _hits(path, HOME if path.name in GUIDANCE_NAMES else HOST_PATH, root)]


def test_the_code_and_configuration_name_no_ones_home_folder() -> None:
    tracked = _tracked()
    read = {p.relative_to(ROOT).as_posix() for p in _scan_targets(ROOT, tracked)}
    assert {"tools/engine-interpreter.pin", "pyproject.toml", "substrate/cli.py"} <= read
    assert _host_paths(ROOT, tracked) == []


def test_the_scan_reads_everything_but_documents_and_tests(tmp_path: Path) -> None:
    wanted = {"tools/engine-interpreter.pin", ".github/workflows/x.yml", "pyproject.toml",
              ".coderabbit.yaml", "substrate/cli.py", "services/api/CLAUDE.md", "a/b/AGENTS.md",
              "experiments/x/README.md", "docs/x.svg", "tests/CLAUDE.md"}
    left_out = {"README.md", "AGENTS.md", "docs/x.md", "docs/history/y.md", "docs/history/z.txt",
                "tests/test_x.py"}
    for name in wanted | left_out | {".venv/z", "out/db", "untracked.toml"}:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    def read(tracked: set[str] | None) -> set[str]:
        return {p.relative_to(tmp_path).as_posix() for p in _scan_targets(tmp_path, tracked)}

    assert read(None) == wanted | {"untracked.toml"}
    assert read(wanted | left_out) == wanted


def test_a_home_path_is_found_by_line_in_text_bytes_symlinks_and_guidance(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "x.pin").write_text("a\nprefix\t/Users/someone/x\n", encoding="utf-8")
    (tmp_path / "tools" / "ok").write_text("ENGINE=$HOME/.substrate/engine\n# ~/.local/bin\n",
                                          encoding="utf-8")
    (tmp_path / "tools" / "bytes").write_bytes(b"\xff\xfe\n/Users/someone/\n")
    (tmp_path / "tools" / "link").symlink_to("/Users/someone/nowhere")
    home = tmp_path / "elsewhere" / "Users" / "someone"
    home.mkdir(parents=True)
    (tmp_path / "tools" / "to-a-folder").symlink_to(home, target_is_directory=True)
    (tmp_path / "tools" / "AGENTS.md").write_text("the vaults are in ~/vaults\n", encoding="utf-8")
    assert _host_paths(tmp_path, None) == ["tools/AGENTS.md:1", "tools/bytes:2", "tools/link:0",
                                          "tools/to-a-folder:0", "tools/x.pin:2"]


def test_the_host_pattern_allows_only_the_shared_folder_and_placeholders() -> None:
    for spelling in ("/Users/someone/notes", "/users/someone/notes", "/Users/Shared-x/",
                     "/Volumes/Mac/x", "/root/x", "/home/someone", "/var/folders/ab/T/tmp.x",
                     "C:\\Users\\someone", "C:\\\\Users\\\\someone", "c:\\users\\someone",
                     "file:///Users/someone", "HOME=/home/someone", "file://localhost/Users/someone",
                     "${X:-/Users/someone/x}", "-I/Users/someone/include", r"a\n/Users/someone",
                     "-L/home/someone/lib", "in /users/someone."):
        assert HOST_PATH.search(spelling), spelling
    for spelling in ("/Users/Shared/atlas-agent", "/Users/Shared/substrate-python/cpython-3.14.7", "`/Users/Shared`", "/Users/Shared", "/Users/<name>/",
                     "/Users/$USER/", "/Users/...", "C:\\Users\\<name>\\",
                     "C:\\\\Users\\\\<name>\\\\", "$HOME/.substrate/engine", "~/.local/bin/uv",
                     "/rootless", "/Volumes", "/Volumes/",
                     "/Users/{name}", "/Users/*", "https://api.github.com/users/x", "build/root/x",
                     "It is /Users/Shared.", "uv run"):
        assert not HOST_PATH.search(spelling), spelling
