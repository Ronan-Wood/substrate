"""Differential testing: the real `substrate compose` against the Lean model of its quarantine.

    python run.py agree --cases 300             # the two must agree on every case
    python run.py agree --mutant keep-stale     # a guard broken on purpose: the harness must notice

Each case is a generated pair of vaults (a project vault and, sometimes, a vault it inherits) whose
notes each pass or fail in one of the ways compose quarantines, some of them byte copies of another
(under a name of their own, or under the same name in another folder or vault), a project manifest
that may declare a `quarantine_ceiling`, sometimes a fault that is not one note's own, and
sometimes an earlier compose in which some of the notes were indexed in a good version. The real compose runs in this
process, against a scratch registry and engine home. The compiled model gets the abstract case as
JSON. They must agree:

  * the model refuses exactly when compose exits non-zero, and then the index is what the earlier
    compose left (or there is none);
  * otherwise the index holds exactly the notes the model indexes, the quarantine table names
    exactly the notes it quarantines, and compose's report and `status` name exactly the copies it
    leaves out, each with the note indexed in its place. A failed note whose earlier version is
    still indexed is a disagreement.

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

    from substrate import cli, introspect, private, prune, quarantine, scopes
    from substrate import vault as V
except BaseException:
    with contextlib.suppress(OSError):
        os.rmdir(_RUN_ROOT)
    raise


class HarnessBug(Exception):
    """The harness, not the code or the model, is wrong. Never caught: a finding cannot hide it."""


class Finding(Exception):
    """The code and the model disagree. Hypothesis shrinks on it."""


# ---------------------------------------------------------------- the world

# The kinds of note the harness writes. Whether one fails is `OUTCOME`'s, by its vault; `ok` passes
# everywhere. Each body holds a note's number, so two notes are byte-identical only when one is a
# copy of the other (`Note.copy_of`).
KINDS = {
    "ok": lambda i: f"# Note {i}\n\nBody zq{i}x.\n",
    "status": lambda i: f"---\nstatus: someday\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "doc_type": lambda i: f"---\ndoc_type: tutorial\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "not_yaml": lambda i: f"---\nstatus: active\nWe agreed {i}.\n---\n\n# Note {i}\n\nBody.\n",
    "near_miss": lambda i: f"---\nStatus: archived\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "orphan": lambda i: f"---\nstatus: superseded\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "unversioned": lambda i: f"---\nclass: reference-versioned\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "class": lambda i: f"---\nclass: memo\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
}
NOT_UTF8 = "not_utf8"
FAILURES = [*(k for k in KINDS if k != "ok"), NOT_UTF8]
# Kinds whose outcome is the vault's: in half the cases the project vault renames `RENAMES` in
# `[engine_keys]`, and the vault it inherits never does. A note of the vault's own values
# (`foreign`) passes only where its vault renames; one whose old key still says archived and a
# call (`guarded`) fails only there; a typo or a misspelling under a new name fails only there.
RENAMED_KINDS = {
    "foreign": lambda i: f"---\nstatus: done\nclass: Math 101\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "guarded": lambda i: (f"---\nstatus: archived\nclass: conversation\n---\n\n# Note {i}\n\n"
                          f"Body zq{i}x.\n"),
    "renamed_typo": lambda i: f"---\ns_status: someday\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    "renamed_near": lambda i: f"---\nS_status: archived\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
    # A spine field no vault renames: two vaults that rename others read it alike.
    "confident": lambda i: f"---\nconfidence: stated\n---\n\n# Note {i}\n\nBody zq{i}x.\n",
}
ALL_KINDS = {**KINDS, **RENAMED_KINDS}
RENAMES = {"status": "s_status", "doc_type": "s_doc_type", "class": "s_class"}
# Whether a note of each kind fails: (in a vault that renames nothing, in one renaming `RENAMES`).
# Renamed, `status`, `doc_type` and `class` are the vault's own, so a value outside the vocabulary
# there passes, and one the engine withholds (`Status: archived`, `status: superseded`) is held
# by the guard; `status` stays a key the engine reads for the guard, so a block it cannot read
# still fails.
OUTCOME = {
    "ok": (False, False), "status": (True, False), "doc_type": (True, False),
    "not_yaml": (True, True), "near_miss": (True, True), "orphan": (True, True),
    "unversioned": (True, False), "class": (True, False), NOT_UTF8: (True, True),
    "foreign": (True, False), "guarded": (False, True), "renamed_typo": (False, True),
    "renamed_near": (False, True), "confident": (False, False),
}
# Faults that are not one note's own. Each refuses the scope. The first four `vault.resolve_scope`
# refuses before any note is read; `planted_folder` (a link where a passing note's ingest folder
# goes) reaches `quarantine.decide` as a scope fault while compose ingests.
# `declared_copies` (a conflict copy of a note that declares its doc_id) and `derived_collision`
# (one name in two folders, bytes that differ, and a doc_id fingerprint that matches anyway) are
# refused there too; they are the two kinds of shared doc_id that leaving out copies does not cover.
FAULTS = ["duplicate_doc_id", "missing_vault", "broken_manifest", "bad_ceiling", "planted_folder",
          "declared_copies", "derived_collision"]
# Two bodies whose SHA-256 share their first 32 bits, so one name gives them one derived doc_id.
COLLIDING = ("# Todo\n\nItem 21779.\n", "# Todo\n\nItem 98006.\n")
# The folders a copy is written to. `notes` holds every note that is not a copy elsewhere, so a
# copy in `a` comes before its note by path and one in `zz` after it.
COPY_FOLDERS = ["a", "notes", "zz"]


@dataclass(frozen=True)
class Note:
    core: bool          # in the inherited vault, else the project vault
    kind: str           # "ok" or one of FAILURES; a copy's is its note's
    prior: bool         # indexed, in a good version, by an earlier compose
    copy_of: int | None = None   # its bytes are this note's
    same_name: bool = False      # and its file name, so the two derive one doc_id
    folder: str = "notes"

    def rel(self, i: int) -> str:
        """Its path inside its vault."""
        own = self.copy_of if self.same_name else i
        return f"{self.folder}/n{own:02d}.md"

    def body_of(self, i: int) -> int:
        return i if self.copy_of is None else self.copy_of


@dataclass(frozen=True)
class World:
    notes: tuple[Note, ...]
    fault: str | None
    decl: str | int     # "absent", "none", or a count
    renamed: bool = False   # the project manifest renames `RENAMES` at the final compose
    late: bool = False      # ... and not at the earlier one, so the setting changes between them

    @property
    def inherits(self) -> bool:
        return any(n.core for n in self.notes) or self.fault == "missing_vault"

    def renames(self, i: int) -> bool:
        """Whether note `i`'s vault renames, at the final compose: the project's alone."""
        return self.renamed and not self.notes[i].core

    def failed(self, i: int) -> bool:
        return OUTCOME[self.notes[i].kind][self.renames(i)]

    def read_apart(self) -> bool:
        """Whether two notes of one doc_id are in a vault that renames and one that does not, and
        write a spine field the one renames (every kind with frontmatter but `confident`), so the
        two vaults read them differently: no copy can stand for the other, and compose refuses the
        scope (`vault._read_with`)."""
        groups: dict[int, set[bool]] = {}
        for i, n in enumerate(self.notes):
            if n.kind not in ("ok", NOT_UTF8, "confident"):
                groups.setdefault(self.ident(i), set()).add(self.renames(i))
        return any(len(v) == 2 for v in groups.values())

    def where(self, i: int) -> tuple[str, str]:
        n = self.notes[i]
        return ("core-vault" if n.core else "proj-vault"), n.rel(i)

    def ident(self, i: int) -> int:
        """The doc_id note `i` derives, as a number: one per file name and bytes. A note that is
        not UTF-8 is never read for one, so its own."""
        n = self.notes[i]
        if n.kind == NOT_UTF8:
            return 100_000 + i
        return n.body_of(i) * 1000 + (n.copy_of if n.same_name else i)

    def kept(self, i: int) -> bool:
        """Whether note `i` is composed: no note with its doc_id comes before it."""
        ranks = self.ranks()
        return all(self.ident(j) != self.ident(i) or ranks[i] <= ranks[j]
                   for j in range(len(self.notes)))

    def ranks(self) -> list[int]:
        """Each note's place in first-by-path order: the project vault before the one it
        inherits, then the path inside the vault, folder by folder."""
        keyed = sorted(range(len(self.notes)), key=lambda i: (
            self.notes[i].core, tuple(self.notes[i].rel(i).split("/"))))
        return [keyed.index(i) for i in range(len(self.notes))]

    def plantable(self) -> list[int]:
        """The notes a link at whose ingest folder compose writes through: composed, and passing."""
        return [i for i in range(len(self.notes)) if not self.failed(i) and self.kept(i)]

    def to_json(self) -> dict:
        ranks = self.ranks()
        return {"notes": [[i, self.failed(i), self.ident(i), ranks[i]]
                          for i in range(len(self.notes))],
                "fault": self.fault is not None or self.read_apart(), "decl": self.decl}


@st.composite
def world(draw) -> World:
    fail_rate = draw(st.sampled_from([0.0, 0.2, 0.5, 0.9, 1.0]))
    size = draw(st.integers(1, 24))
    renamed = draw(st.booleans())
    kinds = FAILURES + list(RENAMED_KINDS) if renamed else FAILURES
    notes = []
    for _ in range(size):
        failed = draw(st.floats(0, 1)) < fail_rate
        notes.append(Note(core=draw(st.booleans()) and draw(st.booleans()),
                          kind=draw(st.sampled_from(kinds)) if failed else "ok",
                          prior=draw(st.booleans())))
    # Copies: of a note under a name of their own (each is indexed), or under its name in another
    # folder or vault (one doc_id, so one of them is).
    taken = {(n.core, n.rel(i)) for i, n in enumerate(notes)}
    for _ in range(draw(st.sampled_from([0, 1, 3, 6]))):
        of = draw(st.integers(0, size - 1))
        c = Note(core=draw(st.booleans()) and draw(st.booleans()), kind=notes[of].kind,
                 prior=draw(st.booleans()), copy_of=of, same_name=draw(st.booleans()),
                 folder=draw(st.sampled_from(COPY_FOLDERS)))
        if (c.core, c.rel(len(notes))) not in taken:
            taken.add((c.core, c.rel(len(notes))))
            notes.append(c)
    fault = draw(st.sampled_from([None] * 6 + FAULTS))
    decl = draw(st.sampled_from(["absent", "absent", "none"]) | st.integers(0, 12))
    late = renamed and draw(st.booleans())
    w = World(tuple(notes), fault, decl, renamed, late)
    if fault == "planted_folder" and not w.plantable():
        # Ingest opens a note's folder only once the note has passed, so a link at a failing
        # note's folder, or at a copy's, is never written through. With no composed note
        # passing, there is nowhere to plant.
        w = World(w.notes, None, decl, renamed, late)
    return w


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
        return {"indexed": sorted(out["indexed"]), "quarantined": sorted(out["quarantined"]),
                "copies": sorted(out["copies"])}


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.project = root / "proj-vault"
        self.core = root / "core-vault"
        self.db = root / "proj.db"
        self.index = root / "proj-index"
        self.registry = root / "scopes.toml"

    def note(self, i: int, n: Note) -> Path:
        return (self.core if n.core else self.project) / n.rel(i)


def _write_note(path: Path, i: int, kind: str) -> None:
    """Note `i`'s body, as `kind` writes it, at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == NOT_UTF8:
        path.write_bytes(f"# Note {i}\n\nCaf".encode() + b"\xe9 " + f"zq{i}x.\n".encode())
    else:
        path.write_text(ALL_KINDS[kind](i), "utf-8")


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
    if w.renamed and (final or not w.late):
        lines.append("[engine_keys]")
        lines.extend(f'{k} = "{v}"' for k, v in RENAMES.items())
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
    by_rel = {w.where(i): i for i in range(len(w.notes))}
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
                # A guarded note is good in a vault that does not rename yet: with the rename
                # added after, the same bytes must leave the index.
                same = w.late and n.kind == "guarded" and n.copy_of is None
                _write_note(lay.note(i, n), i, n.kind if same else "ok")
        if _compose(lay)[0] != 0:
            raise HarnessBug("the earlier compose, of good notes only, refused")
        before = _index_of(lay, w)
        expected = sorted(i for i, n in enumerate(w.notes) if n.prior)
        if before != (expected, []):
            raise HarnessBug(f"the earlier compose indexed {before}, not {expected}")

    _manifest(lay, w, final=True)
    lay.core.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(w.notes):
        _write_note(lay.note(i, n), n.body_of(i), n.kind)
    if w.fault == "declared_copies":
        for name in ("twin.md", "twin (conflicted copy).md"):
            (lay.project / "notes" / name).parent.mkdir(parents=True, exist_ok=True)
            (lay.project / "notes" / name).write_text(
                "---\ndoc_id: twin\n---\n\n# Twin\n\nOne.\n", "utf-8")
    if w.fault == "derived_collision":
        for folder, body in zip(("x", "y"), COLLIDING, strict=True):
            (lay.project / folder).mkdir(parents=True, exist_ok=True)
            (lay.project / folder / "todo.md").write_text(body, "utf-8")
    if w.fault == "duplicate_doc_id":
        (lay.project / "notes").mkdir(parents=True, exist_ok=True)
        (lay.project / "notes" / "twin-a.md").write_text(
            "---\ndoc_id: twin\n---\n\n# Twin A\n\nOne.\n", "utf-8")
        (lay.project / "notes" / "twin-b.md").write_text(
            "---\ndoc_id: twin\n---\n\n# Twin B\n\nTwo.\n", "utf-8")
    if w.fault == "missing_vault":
        shutil.rmtree(lay.core)
    if w.fault == "planted_folder":
        i = w.plantable()[0]
        n = w.notes[i]
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
    by_rel = {"/".join(w.where(i)): i for i in range(len(w.notes))}
    reported = sorted(by_rel[r["path"]] for r in records if r["kind"] == "quarantined")
    if reported != after[1]:
        raise Finding(f"compose's report names quarantined {reported}, its index {after[1]}")
    # EACH COPY LEFT OUT IS NAMED, in compose's report and in `status`, with the note composed in
    # its place, and the report says whether that note was quarantined.
    dups = [r for r in records if r["kind"] == "duplicate"]
    copies = {by_rel[r["path"]]: by_rel[r["copy_of"]] for r in dups}
    named = introspect.identical_report(V.resolve_scope(lay.project))
    shown = {by_rel[f"{d['vault']}/{d['path']}"]: by_rel[f"{d['copy_of']['vault']}/"
                                                         f"{d['copy_of']['path']}"]
             for d in named["duplicate_notes"]}
    if shown != copies or named["duplicates"] != len(copies):
        raise Finding(f"compose's report names copies {copies}, `status` {shown}")
    composed = set(after[0]) | set(after[1])
    for r in dups:
        c, twin = by_rel[r["path"]], by_rel[r["copy_of"]]
        if twin not in composed or w.ident(twin) != w.ident(c):
            raise Finding(f"copy {c} is named as a copy of {twin}, which is not the note "
                          f"composed in its place")
        if r["quarantined"] != (twin in after[1]):
            raise Finding(f"copy {c}'s report says its note {twin} is "
                          f"{'' if r['quarantined'] else 'not '}quarantined")
    return {"indexed": after[0], "quarantined": after[1], "copies": sorted(copies)}


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
    prune.remove_unindexed = lambda folders: []


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


class _Last:
    """A sort key in reverse: what sorts first by `key` sorts last by this."""

    def __init__(self, key) -> None:
        self.key = key

    def __lt__(self, other: _Last) -> bool:
        return other.key < self.key


def _last_copy():
    """Of several notes with one doc_id, the LAST by path is indexed rather than the first."""
    real = V._first_by_path

    def first_by_path(where):
        key = real(where)
        return lambda n: _Last(key(n))

    V._first_by_path = first_by_path


def _read_alike():
    """Two vaults always read a copy alike, whatever they rename: a copy is left out for a note
    its own vault would read differently."""
    V._read_with = lambda raw, keys: ""


def _no_guard():
    """The guard is off: a note whose renamed field's old name still marks it archived or a call
    is indexed as a live note."""
    from substrate.markdown import ingest

    ingest.guard = lambda *a, **k: None


MUTANTS = {"keep-stale": _keep_stale, "ceiling-ge": _ceiling_ge, "no-default": _no_default,
           "faults-ignored": _faults_ignored, "allow-empty": _allow_empty,
           "last-copy": _last_copy, "no-guard": _no_guard, "read-alike": _read_alike}

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
    # Also counted, beside the one outcome each case has: the cases that hold copies.
    also: collections.Counter = collections.Counter()

    @settings(max_examples=a.cases, **SETTINGS)
    @given(world())
    def agree(w):
        model, code = run_case(w)
        failed = sum(w.failed(i) for i in range(len(w.notes)))
        if w.renamed:
            also["the project renames" + (", after an earlier compose" if w.late else "")] += 1
            if any(w.renames(i) and w.notes[i].kind == "guarded" for i in range(len(w.notes))):
                also["the project renames and holds a guarded note"] += 1
            if w.read_apart():
                also["copies two vaults read apart"] += 1
        if model.get("copies"):
            also["model leaves out copies"] += 1
        if "indexed" in model and any(w.notes[i].copy_of is not None and not w.notes[i].same_name
                                      for i in model["indexed"]):
            also["model indexes a copy under a name of its own"] += 1
        if "refuse" in model:
            n["model refuses: " + ("a scope fault" if w.fault or w.read_apart() else
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
    for k, v in sorted(also.items()):
        print(f"  {v:6d}  of which: {k}")
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
