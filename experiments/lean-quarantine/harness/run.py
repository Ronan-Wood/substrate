"""Differential testing: the real `substrate compose` against the Lean model of its quarantine.

    python run.py agree --cases 300             # the two must agree on every case
    python run.py agree --mutant keep-stale     # a guard broken on purpose: the harness must notice

Each case is a generated pair of vaults (a project vault and, sometimes, a vault it inherits) whose
notes each pass or fail in one of the ways compose quarantines, a project manifest that may declare
a `quarantine_ceiling`, sometimes a fault that is not one note's own, and sometimes an earlier
compose in which some of the notes were indexed in a good version. The real compose runs in this
process, against a scratch registry and engine home. The compiled model gets the abstract case as
JSON. They must agree:

  * the model refuses exactly when compose exits non-zero, and then the index is what the earlier
    compose left (or there is none);
  * otherwise the index holds exactly the notes the model indexes, and the quarantine table names
    exactly the notes it quarantines. A failed note whose earlier version is still indexed is a
    disagreement.

Runs are derandomized, so the same command gives the same counts. A script, not a module: it
refuses to be imported, because importing it points `HOME` at a scratch folder. Every check is an
explicit `raise`, never an `assert`, so `python -O` cannot turn it into one that passes without
testing anything.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __name__ != "__main__":
    raise ImportError("harness/run.py is a script: run it, do not import it")

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LEAN_BIN = HERE.parent / "lean" / ".lake" / "build" / "bin" / "quarantine"

_RUN_ROOT = Path(os.path.realpath(tempfile.mkdtemp(prefix="quarantine-diff-",
                                                   dir=os.environ.get("QUARANTINE_TMP"))))
os.environ["HOME"] = str(_RUN_ROOT / "home")
os.environ.pop("SUBSTRATE_REGISTRY", None)
os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] = str(HERE / ".hypothesis")
sys.path.insert(0, str(REPO))

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    from substrate import cli, private, prune, quarantine, scopes
except BaseException:
    with contextlib.suppress(OSError):
        os.rmdir(_RUN_ROOT)
    raise


class HarnessBug(Exception):
    """The harness, not the code or the model, is wrong. Never caught: a finding cannot hide it."""


class Finding(Exception):
    """The code and the model disagree. Hypothesis shrinks on it."""


# ---------------------------------------------------------------- the world

# Every way a note fails on its own that the harness writes, each one compose quarantines. `ok`
# passes. Each body holds the note's number, so no two notes are byte-identical.
KINDS = {
    "ok": lambda i: f"# Note {i}\n\nBody zq{i}x.\n",
    "status": lambda i: f"---\nstatus: someday\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "doc_type": lambda i: f"---\ndoc_type: tutorial\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "not_yaml": lambda i: f"---\nstatus: active\nWe agreed {i}.\n---\n\n# Note {i}\n\nBody.\n",
    "near_miss": lambda i: f"---\nStatus: archived\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "orphan": lambda i: f"---\nstatus: superseded\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "untitled_call": lambda i: f"---\nclass: conversation\n---\n\nSaid zq{i}x.\n",
    "class": lambda i: f"---\nclass: memo\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
}
NOT_UTF8 = "not_utf8"
FAILURES = [*(k for k in KINDS if k != "ok"), NOT_UTF8]
# Faults that are not one note's own. Each refuses the scope. The first four `vault.resolve_scope`
# refuses before any note is read; `planted_folder` (a link where a passing note's ingest folder
# goes) reaches `quarantine.decide` as a scope fault while compose ingests.
FAULTS = ["duplicate_doc_id", "missing_vault", "broken_manifest", "bad_ceiling", "planted_folder"]


@dataclass(frozen=True)
class Note:
    core: bool          # in the inherited vault, else the project vault
    kind: str           # "ok" or one of FAILURES
    prior: bool         # indexed, in a good version, by an earlier compose


@dataclass(frozen=True)
class World:
    notes: tuple[Note, ...]
    fault: str | None
    decl: str | int     # "absent", "none", or a count

    @property
    def inherits(self) -> bool:
        return any(n.core for n in self.notes) or self.fault == "missing_vault"

    def to_json(self) -> dict:
        return {"notes": [[i, n.kind != "ok"] for i, n in enumerate(self.notes)],
                "fault": self.fault is not None, "decl": self.decl}


@st.composite
def world(draw) -> World:
    fail_rate = draw(st.sampled_from([0.0, 0.2, 0.5, 0.9, 1.0]))
    size = draw(st.integers(1, 24))
    notes = []
    for _ in range(size):
        failed = draw(st.floats(0, 1)) < fail_rate
        notes.append(Note(core=draw(st.booleans()) and draw(st.booleans()),
                          kind=draw(st.sampled_from(FAILURES)) if failed else "ok",
                          prior=draw(st.booleans())))
    fault = draw(st.sampled_from([None] * 6 + FAULTS))
    if fault == "planted_folder" and all(n.kind != "ok" for n in notes):
        # Ingest opens a note's folder only once the note has passed, so a link at a failing
        # note's folder is never written through. With no note passing, there is nowhere to plant.
        fault = None
    decl = draw(st.sampled_from(["absent", "absent", "none"]) | st.integers(0, 12))
    return World(tuple(notes), fault, decl)


# ---------------------------------------------------------------- the two deciders

class Lean:
    def __init__(self) -> None:
        self.p = subprocess.Popen([str(LEAN_BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True)

    def verdict(self, w: World) -> dict:
        self.p.stdin.write(json.dumps(w.to_json()) + "\n")
        self.p.stdin.flush()
        out = json.loads(self.p.stdout.readline())
        if "error" in out:
            raise HarnessBug(f"the model could not read the world: {out['error']}")
        if "refuse" in out:
            return {"refuse": True}
        return {"indexed": sorted(out["indexed"]), "quarantined": sorted(out["quarantined"])}


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.project = root / "proj-vault"
        self.core = root / "core-vault"
        self.db = root / "proj.db"
        self.index = root / "proj-index"
        self.registry = root / "scopes.toml"

    def note(self, i: int, n: Note) -> Path:
        return (self.core if n.core else self.project) / "notes" / f"n{i:02d}.md"


def _write_note(path: Path, i: int, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == NOT_UTF8:
        path.write_bytes(f"# Note {i}\n\nCaf".encode() + b"\xe9 " + f"zq{i}x.\n".encode())
    else:
        path.write_text(KINDS[kind](i), "utf-8")


def _manifest(lay: Layout, w: World, *, final: bool) -> None:
    lines = ['name = "proj"']
    if w.inherits:
        lines.append('inherits = ["core-vault"]')
    if final and w.fault == "bad_ceiling":
        lines.append(f"{quarantine.CEILING_KEY} = -1")
    elif w.decl != "absent":
        lines.append(f"{quarantine.CEILING_KEY} = {json.dumps(w.decl)}")
    if final and w.fault == "broken_manifest":
        lines.append("reference_domains = ")
    lay.project.mkdir(parents=True, exist_ok=True)
    (lay.project / ".substrate.toml").write_text("\n".join(lines) + "\n", "utf-8")


def _compose(lay: Layout) -> tuple[int, list[dict]]:
    """Compose, and the records of its report to a refresh pass (`substrate.compose_report`)."""
    report = lay.root / "report.jsonl"
    report.write_text("", "utf-8")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(lay.project), "--db", str(lay.db), "--index-root",
                       str(lay.index), "--registry", str(lay.registry), "--report", str(report)])
    if rc not in (0, 2, 3):
        raise HarnessBug(f"compose exited {rc}:\n{err.getvalue()[-2000:]}")
    records = [json.loads(ln) for ln in report.read_text("utf-8").splitlines()]
    if not records or records[-1] != {"kind": "end", "exit": rc}:
        raise HarnessBug(f"compose's report does not end with its exit {rc}: {records[-2:]}")
    return rc, records[:-1]


def _held(lay: Layout) -> set[str]:
    """The ingest folders that hold an ingested version."""
    return {d.name for d in lay.index.iterdir() if (d / "run.json").is_file()} \
        if lay.index.is_dir() else set()


def _index_of(lay: Layout, w: World) -> tuple[list[int], list[int]] | None:
    """(indexed, quarantined) note numbers, from the index on disk; None when there is none."""
    if not lay.db.exists():
        return None
    by_path = {str(lay.note(i, n).resolve()): i for i, n in enumerate(w.notes)}
    by_rel = {(("core-vault" if n.core else "proj-vault"), f"notes/n{i:02d}.md"): i
              for i, n in enumerate(w.notes)}
    db = sqlite3.connect(f"file:{lay.db}?mode=ro", uri=True)
    try:
        docs = [r[0] for r in db.execute("SELECT source_path FROM documents")]
        held = [(r[0], r[1]) for r in db.execute("SELECT vault, path FROM quarantine")]
    finally:
        db.close()
    unknown = [p for p in docs if str(Path(p).resolve()) not in by_path]
    if unknown:
        raise HarnessBug(f"indexed documents the world does not hold: {unknown[:3]}")
    return (sorted(by_path[str(Path(p).resolve())] for p in docs),
            sorted(by_rel[h] for h in held))


def python_verdict(w: World, lay: Layout) -> dict:
    # An earlier compose, in which every note marked `prior` was indexed in a good version.
    before = None
    if any(n.prior for n in w.notes):
        _manifest(lay, w, final=False)
        lay.core.mkdir(parents=True, exist_ok=True)
        for i, n in enumerate(w.notes):
            if n.prior:
                _write_note(lay.note(i, n), i, "ok")
        if _compose(lay)[0] != 0:
            raise HarnessBug("the earlier compose, of good notes only, refused")
        before = _index_of(lay, w)
        expected = sorted(i for i, n in enumerate(w.notes) if n.prior)
        if before != (expected, []):
            raise HarnessBug(f"the earlier compose indexed {before}, not {expected}")

    _manifest(lay, w, final=True)
    lay.core.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(w.notes):
        _write_note(lay.note(i, n), i, n.kind)
    if w.fault == "duplicate_doc_id":
        (lay.project / "notes").mkdir(parents=True, exist_ok=True)
        (lay.project / "notes" / "twin-a.md").write_text(
            "---\ndoc_id: twin\n---\n\n# Twin A\n\nOne.\n", "utf-8")
        (lay.project / "notes" / "twin-b.md").write_text(
            "---\ndoc_id: twin\n---\n\n# Twin B\n\nTwo.\n", "utf-8")
    if w.fault == "missing_vault":
        shutil.rmtree(lay.core)
    if w.fault == "planted_folder":
        i, n = next((i, n) for i, n in enumerate(w.notes) if n.kind == "ok")
        vault = lay.core if n.core else lay.project
        folder = lay.index / private.ingest_dir_name(vault.name, lay.note(i, n).resolve())
        if folder.is_dir():
            shutil.rmtree(folder)
        lay.index.mkdir(parents=True, exist_ok=True)
        (lay.root / "elsewhere").mkdir()
        folder.symlink_to(lay.root / "elsewhere")
    lay.project.joinpath("notes").mkdir(parents=True, exist_ok=True)

    held_before = _held(lay)
    rc, records = _compose(lay)
    after = _index_of(lay, w)
    if rc != 0:
        # A REFUSAL LEAVES EVERYTHING AS IT WAS: the index, every ingest folder that held a
        # version, and the report, which says only why.
        if after != before:
            raise Finding(f"compose refused (exit {rc}) and the index changed: {before} -> "
                          f"{after}")
        if not held_before <= _held(lay):
            raise Finding(f"compose refused (exit {rc}) and removed ingest folders: "
                          f"{sorted(held_before - _held(lay))}")
        if [r["kind"] for r in records] != ["refused"]:
            raise Finding(f"compose refused (exit {rc}) and reported {records}")
        return {"refuse": True}
    if after is None:
        raise HarnessBug("compose exited 0 and wrote no index")
    by_rel = {f"{'core-vault' if n.core else 'proj-vault'}/notes/n{i:02d}.md": i
              for i, n in enumerate(w.notes)}
    reported = sorted(by_rel[r["path"]] for r in records if r["kind"] == "quarantined")
    if reported != after[1]:
        raise Finding(f"compose's report names quarantined {reported}, its index {after[1]}")
    return {"indexed": after[0], "quarantined": after[1]}


def run_case(w: World) -> tuple[dict, dict]:
    case = Path(tempfile.mkdtemp(dir=_RUN_ROOT))
    try:
        lay = Layout(case)
        scopes.engine_home = lambda: case / "engine"
        return LEAN.verdict(w), python_verdict(w, lay)
    finally:
        shutil.rmtree(case, ignore_errors=True)


# ---------------------------------------------------------------- planted regressions

def _keep_stale():
    """A quarantined note's ingest folder is kept, so its earlier version stays indexed."""
    prune.remove_quarantined = lambda folders: []


def _ceiling_ge():
    """The scope is refused AT the ceiling, not above it."""
    real = quarantine.decide
    quarantine.decide = lambda outcomes, faults, ceiling: real(
        outcomes, faults, None if ceiling is None else max(ceiling - 1, 0))


def _no_default():
    """A manifest that declares no ceiling gets none, rather than the larger of 10 and 1%."""
    real = quarantine.ceiling_for
    quarantine.ceiling_for = lambda declared, notes: (None if declared is None
                                                      else real(declared, notes))


def _faults_ignored():
    """`decide` drops the scope faults compose finds while ingesting."""
    real = quarantine.decide
    quarantine.decide = lambda outcomes, faults, ceiling: real(outcomes, [], ceiling)


def _allow_empty():
    """`decide` indexes a scope whose every note failed: an empty index replaces the earlier one."""
    real = quarantine.decide

    def decide(outcomes, faults, ceiling):
        d = real(outcomes, faults, ceiling)
        return (quarantine.Decision(None, (), d.quarantined)
                if d.refusal == quarantine.NOTHING_LEFT else d)

    quarantine.decide = decide


MUTANTS = {"keep-stale": _keep_stale, "ceiling-ge": _ceiling_ge, "no-default": _no_default,
           "faults-ignored": _faults_ignored, "allow-empty": _allow_empty}

SETTINGS = dict(deadline=None, database=None, derandomize=True,
                suppress_health_check=list(HealthCheck), print_blob=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["agree"])
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--mutant", choices=sorted(MUTANTS), default=None,
                    help="break the real compose on purpose, to show the harness notices")
    a = ap.parse_args()
    if a.mutant:
        MUTANTS[a.mutant]()
    n: collections.Counter = collections.Counter()

    @settings(max_examples=a.cases, **SETTINGS)
    @given(world())
    def agree(w):
        model, code = run_case(w)
        failed = sum(nt.kind != "ok" for nt in w.notes)
        if "refuse" in model:
            n["model refuses: " + ("a scope fault" if w.fault else
                                   "nothing left to index" if failed == len(w.notes) else
                                   "over the ceiling")] += 1
        else:
            n["model indexes, quarantining " + ("none" if not failed else
                                                "some" if failed < len(w.notes) else "all")] += 1
        if model != code:
            raise Finding(f"model {model}\ncode  {code}\nworld {w}")

    label = f"agree{' --mutant ' + a.mutant if a.mutant else ''}"
    found: list[Exception] = []
    try:
        agree()
    except* Finding as group:
        # Hypothesis reports each distinct disagreement it found; a HarnessBug is never caught.
        found.extend(group.exceptions)
    if found:
        for e in found:
            print(f"{label}: minimal disagreement\n{e}")
        return 1
    print(f"{label}: {sum(n.values())} cases, no disagreement")
    for k, v in sorted(n.items()):
        print(f"  {v:6d}  {k}")
    return 0


if __name__ == "__main__":
    LEAN = None
    try:
        LEAN = Lean()
        sys.exit(main())
    finally:
        if LEAN is not None:
            LEAN.p.stdin.close()
            LEAN.p.wait()
        shutil.rmtree(_RUN_ROOT, ignore_errors=True)
