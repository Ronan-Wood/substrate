"""Issue #71: notes with the same bytes no longer refuse the scope.

Before, two notes with the same bytes refused the whole scope (exit 2, "two notes are
byte-identical"), and so did one name with the same text in two folders ("doc_id … produced by two
notes"). An ordinary vault trips both without any sync conflict: unfilled daily notes made from one
template, `Untitled.md` and `Untitled 1.md` both empty, an empty `Untitled.md` in two folders. Now:

  * same bytes, different names: each is indexed as its own note, and `status` lists each group;
  * the same derived doc_id from the same bytes: the first by path is indexed, and the others are
    named in `status` as its duplicates;
  * a doc_id a note DECLARES that another note also has, and one derived doc_id from different
    bytes, still refuse the scope.

"Fails before" marks a test that fails on `f6b1699` the way the issue describes: the scope
refuses. "Guard" marks one that passes there too.

Every file these tests write has a name both APFS and ext4 store as given: ASCII, or a name in one
Unicode form in a folder of its own, so a case-folding or normalizing volume never merges two of
them. The order "first by path" is tested on strings.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unicodedata
from pathlib import Path

import pytest

from substrate import cli, compose_report, digests, introspect, notes, private, scopes
from substrate import vault as V
from substrate.extract.base import doc_id_for, doc_id_from
from substrate.store.index_store import IndexStore

# An unfilled daily note: frontmatter and headings, nothing typed under them.
DAILY = "---\ntags: [daily]\n---\n\n## Tasks\n\n## Notes\n"
PLAN = "The plan both machines edited: ship the parser first, then the index.\n"
TODO = "# Todo\n\n- milk\n- bread\n"
# Two bodies whose SHA-256 share their first 32 bits, so one name gives them one derived doc_id.
COLLIDING = ("# Todo\n\nItem 21779.\n", "# Todo\n\nItem 98006.\n")


class Plain:
    """A project vault of plain notes, with a registry, index and database of its own."""

    def __init__(self, root: Path, name: str = "plain") -> None:
        self.root = root
        self.vault = root / f"{name}-vault"
        self.name = name
        self.db = root / f"{name}.db"
        self.index_root = root / f"{name}-index"
        self.registry = root / "scopes.toml"
        self.vault.mkdir(parents=True)
        self.manifest()
        self.write("keep.md", "# Keep\n\nA note that is not a copy of anything.\n")

    def manifest(self, *lines: str) -> None:
        (self.vault / ".substrate.toml").write_text(
            "\n".join([f'name = "{self.name}"', *lines]) + "\n", "utf-8")

    def write(self, rel: str, text: str | bytes) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, "utf-8")
        return path

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def compose(self, *flags: str) -> tuple[int, str, str]:
        return self.run("compose", str(self.vault), "--db", str(self.db), "--index-root",
                        str(self.index_root), "--registry", str(self.registry), *flags)

    def status(self) -> dict:
        rc, out, err = self.run("status", "--scope", self.name, "--registry", str(self.registry),
                                "--lexical-only", "--json")
        assert rc == 0, err
        return json.loads(out)

    def status_text(self) -> str:
        rc, out, err = self.run("status", "--scope", self.name, "--registry", str(self.registry),
                                "--lexical-only")
        assert rc == 0, err
        return out

    def query(self, text: str) -> list[dict]:
        rc, out, err = self.run("search", "--scope", self.name, "--registry", str(self.registry),
                                "--no-vector", "--json", text)
        assert rc == 0, err
        return json.loads(out)["passages"]

    def documents(self) -> dict[str, tuple[str, str]]:
        """Each indexed document's vault-relative source path, with its doc_id and title."""
        with IndexStore(str(self.db), migrate=False) as store:
            rows = store.db.execute("SELECT doc_id, title, source_path FROM documents").fetchall()
        return {Path(r["source_path"]).resolve().relative_to(self.vault.resolve()).as_posix():
                (r["doc_id"], r["title"]) for r in rows}

    def folder(self, rel: str) -> Path:
        return self.index_root / private.ingest_dir_name(self.vault.name,
                                                         (self.vault / rel).resolve())


@pytest.fixture
def plain(tmp_path, monkeypatch) -> Plain:
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path / "engine")
    return Plain(tmp_path)


def _ref(vault: str, path: str) -> dict:
    return {"vault": vault, "path": path}


# ---------------------------------------------------------------- the vault the issue names

def _ordinary(p: Plain) -> list[str]:
    """A month of unfilled daily notes, a note and its sync conflict copy, two empty `Untitled`
    notes, and an empty `Untitled.md` in a second folder. Returns the daily notes' paths."""
    days = [f"Daily/2026-10-{d:02d}.md" for d in range(1, 31)]
    for rel in days:
        p.write(rel, DAILY)
    p.write("Plan.md", PLAN)
    p.write("Plan (conflicted copy).md", PLAN)
    p.write("Untitled.md", "")
    p.write("Untitled 1.md", "")
    p.write("Inbox/Untitled.md", "")
    return days


def test_an_ordinary_vault_with_identical_notes_composes(plain):
    """Fails before: exit 2, "two notes are byte-identical". Nothing is quarantined, every note
    with its own name is indexed under its own title, the one copy left out is named in `status`
    and is not drift, and the conflict copy is listed."""
    days = _ordinary(plain)
    rc, out, err = plain.compose()
    assert rc == 0, err
    assert "WARNING (quarantine)" not in err
    assert "duplicate: plain-vault/Untitled.md  (the same doc_id and bytes as " \
           "plain-vault/Inbox/Untitled.md, which is indexed in its place)" in out

    docs = plain.documents()
    indexed = {*days, "keep.md", "Plan.md", "Plan (conflicted copy).md", "Untitled 1.md",
               "Inbox/Untitled.md"}
    assert set(docs) == indexed, set(docs) ^ indexed
    # Each by its own name: a daily note by its first heading, the rest by their file names.
    assert {docs[r][1] for r in days} == {"Tasks"}
    assert docs["Plan (conflicted copy).md"][1] == "Plan (conflicted copy)"
    assert docs["Untitled 1.md"][1] == "Untitled 1"
    assert docs["Inbox/Untitled.md"][1] == "Untitled"
    assert len({d for d, _ in docs.values()}) == len(docs), "two documents share a doc_id"

    s = plain.status()
    assert s["quarantined"] == []
    assert s["drift"]["stale"] is False and s["drift"]["added"] == [], s["drift"]
    assert s["identical"] == {
        "duplicates": 1,
        "duplicate_notes": [{**_ref("plain-vault", "Untitled.md"),
                           "copy_of": _ref("plain-vault", "Inbox/Untitled.md")}],
        "groups": 1,
        "notes": 2,
        "listed": [{"notes": 2, "first": [_ref("plain-vault", "Plan (conflicted copy).md"),
                                          _ref("plain-vault", "Plan.md")]}],
        # The thirty daily notes and the two empty notes that are indexed.
        "empty": 32,
    }
    text = plain.status_text()
    assert "plain-vault/Untitled.md: a copy of plain-vault/Inbox/Untitled.md" in text
    assert "plain-vault/Plan (conflicted copy).md, plain-vault/Plan.md" in text
    assert "32 note(s) with no body" in text

    # Recomposing changes nothing: the same copy is left out, and the index is current.
    rc, out2, err = plain.compose()
    assert rc == 0, err
    assert "unchanged 35" in out2, out2
    assert plain.documents() == docs


def test_a_note_and_its_conflict_copy_both_answer(plain):
    """What search returns today for a note and its conflict copy: two hits with the same text,
    one from each note. Folding them is Open."""
    plain.write("Plan.md", PLAN)
    plain.write("Plan (conflicted copy).md", PLAN)
    assert plain.compose()[0] == 0
    found = plain.query("parser first then the index")
    hits = [h for h in found if "parser" in h["snippet"]]
    assert len(hits) == 2, found
    assert {h["snippet"] for h in hits} == {hits[0]["snippet"]}
    assert {h["citation"] for h in hits} == {"Plan · @plain-vault",
                                             "Plan (conflicted copy) · @plain-vault"}, hits
    ranks = [i for i, h in enumerate(found) if "parser" in h["snippet"]]
    assert ranks == [0, 1], "the two copies are not adjacent"


# ---------------------------------------------------------------- one doc_id, the same bytes

def test_one_name_in_two_folders_indexes_the_first_by_path(plain):
    """Fails before: exit 2, "doc_id … produced by two notes"."""
    plain.write("B/todo.md", TODO)
    plain.write("A/todo.md", TODO)
    rc, out, err = plain.compose()
    assert rc == 0, err
    docs = plain.documents()
    assert "A/todo.md" in docs and "B/todo.md" not in docs
    assert docs["A/todo.md"][0] == doc_id_for(plain.vault / "B/todo.md")
    with IndexStore(str(plain.db), migrate=False) as store:
        assert store.quarantined() == [], "a copy left out is not a quarantined note"
    s = plain.status()
    assert s["identical"]["duplicate_notes"] == [
        {**_ref("plain-vault", "B/todo.md"), "copy_of": _ref("plain-vault", "A/todo.md")}]
    assert s["identical"]["groups"] == 0
    assert s["drift"]["added"] == [] and s["drift"]["stale"] is False
    # The choice is the same on the next compose.
    assert plain.compose()[0] == 0
    assert set(plain.documents()) == set(docs)


def test_names_that_slug_alike_are_one_doc_id(plain):
    """Fails before. `Todo.md` and `todo.md` (in two folders, so no volume folds them together)
    and `a b.md` beside `a-b.md` derive one doc_id: the second rule's case, not the first's."""
    plain.write("X/Todo.md", TODO)
    plain.write("Y/todo.md", TODO)
    plain.write("a b.md", PLAN)
    plain.write("a-b.md", PLAN)
    scope = V.resolve_scope(plain.vault)
    assert [(scope.relative(n), scope.relative(k)) for n, k in scope.duplicates] == [
        ("Y/todo.md", "X/Todo.md"), ("a-b.md", "a b.md")]
    assert scope.identical == ()
    rc, _, err = plain.compose()
    assert rc == 0, err
    assert {"a b.md", "X/Todo.md"} <= set(plain.documents())


def test_names_in_two_unicode_forms_are_two_notes(plain):
    """The same name in NFC and NFD derives two doc_ids (a combining mark is not alphanumeric),
    so both are indexed and listed as one group: the first rule's case. In two folders, so a
    volume that normalizes names never sees the two as one file."""
    nfc, nfd = unicodedata.normalize("NFC", "Café"), unicodedata.normalize("NFD", "Café")
    assert nfc != nfd
    sha = hashlib.sha256(PLAN.encode()).hexdigest()
    assert doc_id_from(nfc, sha) != doc_id_from(nfd, sha)
    plain.write(f"A/{nfc}.md", PLAN)
    plain.write(f"B/{nfd}.md", PLAN)
    scope = V.resolve_scope(plain.vault)
    assert scope.duplicates == ()
    [group] = scope.identical
    assert len(group.notes) == 2


def test_the_ids_names_share_on_strings():
    """When two names give one doc_id, on strings: case, separators and the first 48 characters of
    the slug. Not the Unicode form."""
    sha = hashlib.sha256(b"x").hexdigest()
    same = [("Todo", "todo"), ("a b", "a-b"), ("a_b", "a.b"), ("x" * 48 + "1", "x" * 48 + "2")]
    for a, b in same:
        assert doc_id_from(a, sha) == doc_id_from(b, sha), (a, b)
    assert doc_id_from("todo", sha) != doc_id_from("todo2", sha)


def test_deleting_the_indexed_copy_hands_its_id_to_the_other(plain):
    """The indexed copy goes; the next one in order takes its place under the same doc_id, from
    its own ingest folder, and the old folder is pruned as a deleted note's."""
    plain.write("A/todo.md", TODO)
    plain.write("B/todo.md", TODO)
    assert plain.compose()[0] == 0
    did = plain.documents()["A/todo.md"][0]
    (plain.vault / "A/todo.md").unlink()
    rc, out, err = plain.compose()
    assert rc == 0, err
    assert plain.documents() == {"keep.md": plain.documents()["keep.md"],
                                 "B/todo.md": (did, "Todo")}
    assert not plain.folder("A/todo.md").exists() and plain.folder("B/todo.md").is_dir()
    assert "pruned: plain-vault/A/todo.md  (note deleted" in out
    s = plain.status()
    assert s["identical"]["duplicates"] == 0 and s["drift"]["stale"] is False


def test_a_copy_that_comes_first_takes_over_and_the_old_folder_goes(plain):
    """A copy that sorts before the indexed one appears. The new one is indexed, and the folder of
    the one indexed before is removed: it holds the same doc_id, and reconcile keeps whichever
    folder holding an id comes last."""
    plain.write("M/todo.md", TODO)
    assert plain.compose()[0] == 0
    assert plain.folder("M/todo.md").is_dir()
    plain.write("A/todo.md", TODO)
    rc, out, err = plain.compose()
    assert rc == 0, err
    docs = plain.documents()
    assert "A/todo.md" in docs and "M/todo.md" not in docs
    assert not plain.folder("M/todo.md").exists()
    assert "pruned: plain-vault/M/todo.md  (note left out as a copy of another; removed " in out
    assert plain.status()["drift"]["stale"] is False


def test_a_copys_folder_that_cannot_be_removed_is_refused_if_it_answers(plain):
    """Guard for `assert_composed`'s `sources`. A folder holding a file ingest did not write is
    never removed. When it is the copy's, and reconcile reads it last, the index would answer
    from the copy's earlier ingest under the indexed note's doc_id: refused."""
    plain.write("K/todo.md", TODO)
    for n in range(100):
        first = f"{n:02d}/todo.md"
        if plain.folder("K/todo.md").name > plain.folder(first).name:
            break
    else:
        raise AssertionError("no folder name sorts before the copy's")
    assert plain.compose()[0] == 0
    (plain.folder("K/todo.md") / "notes.txt").write_text("mine", "utf-8")
    plain.write(first, TODO)
    rc, _, err = plain.compose()
    assert rc == 3, err
    assert "read from another note than the one this compose ingested" in err


def test_assert_composed_checks_each_doc_against_its_source(tmp_path):
    """On its own: a doc_id read from another note than the one compose ingested it from."""
    from substrate.models import Chunk, Document

    with IndexStore(str(tmp_path / "x.db")) as store:
        doc = Document(doc_id="todo-1", source_path=str(tmp_path / "B/todo.md"),
                       source_sha256="s", source_pages=1, document_class="unclassified",
                       title="Todo", status="active", vault="v", tier=3)
        store.upsert(doc, [Chunk(chunk_id="todo-1#c0", doc_id="todo-1", kind="passage",
                                 text="milk", path=["Todo"], level=1,
                                 document_class="unclassified")],
                     markdown_path="/todo.md", markdown_mtime=0.0, markdown_sha256="m")
        V.assert_composed(store, sources={"todo-1": tmp_path / "B/todo.md"})
        with pytest.raises(V.VaultError, match="read from another note"):
            V.assert_composed(store, sources={"todo-1": tmp_path / "A/todo.md"})


# ---------------------------------------------------------------- still refused

def test_a_declared_doc_id_on_two_identical_copies_still_refuses(plain):
    """Guard, with a better message: a conflict copy of a note that declares its doc_id carries
    the declaration, so the scope refuses and names both files. A known limit (Open)."""
    body = "---\ndoc_id: plan\n---\n\n# Plan\n\nThe plan.\n"
    plain.write("Plan.md", body)
    plain.write("Plan (conflicted copy).md", body)
    rc, _, err = plain.compose()
    assert rc == 2, err
    assert "doc_id 'plan' is claimed by two notes" in err and "both declare it" in err
    assert "Plan (conflicted copy).md" in err and "Plan.md;" in err
    assert not plain.db.exists()


def test_a_declared_doc_id_another_note_derives_still_refuses(plain):
    """Guard. One note declares the id another derives from its name and bytes."""
    plain.write("B/todo.md", TODO)
    did = doc_id_for(plain.vault / "B/todo.md")
    plain.write("A/todo.md", f"---\ndoc_id: {did}\n---\n\n# Todo\n\n- eggs\n")
    rc, _, err = plain.compose()
    assert rc == 2, err
    assert f"doc_id {did!r} is claimed by two notes" in err
    assert "A/todo.md declares it, and the other derives it" in err


def test_one_derived_doc_id_from_different_bytes_still_refuses(plain):
    """Guard. A 32-bit collision of the fingerprint `doc_id_from` keeps: one id, two notes that
    are not copies, so neither may stand for the other."""
    a, b = COLLIDING
    assert hashlib.sha256(a.encode()).hexdigest()[:8] == hashlib.sha256(b.encode()).hexdigest()[:8]
    plain.write("A/todo.md", a)
    plain.write("B/todo.md", b)
    rc, _, err = plain.compose()
    assert rc == 2, err
    assert "is derived by two notes whose bytes differ" in err


def test_copies_their_sources_compose_differently_still_refuse(plain):
    """Guard. Two versions of a source can hold one passage under one name with the same bytes.
    Their `_meta.md` give them different versions, so leaving one out would drop the passage from
    that version: refused as before."""
    for version in ("1.0", "2.0"):
        src = f"10-reference/versioned/spec/{version}"
        plain.write(f"{src}/_meta.md", f"---\nclass: reference-versioned\nversion: "
                                       f"{version}\nraw: spec-{version}.html\n---\n\n# Spec\n")
        plain.write(f"{src}/passages/00-scope.md", "# Scope\n\nThis section did not change.\n")
    rc, _, err = plain.compose()
    assert rc == 2, err
    assert "whose sources compose them differently" in err


def test_one_file_two_vaults_discover_still_refuses(tmp_path, monkeypatch):
    """Guard. An inherited vault inside the project vault: each note in it is discovered twice, by
    the same path. One file with two owners, not a copy, so neither is left out for the other."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path / "engine")
    p = Plain(tmp_path)
    p.manifest(f'inherits = ["{(p.vault / "inner").as_posix()}"]')
    p.write("inner/shared.md", TODO)
    with pytest.raises(V.VaultError, match="is discovered by two vaults"):
        V.resolve_scope(p.vault)


def test_an_inherited_vault_named_in_another_case_still_refuses(tmp_path, monkeypatch):
    """Guard for comparing files, not spellings. `inherits` names the inner folder as `INNER`. On a
    volume that folds case (APFS by default) that is the same folder, reached by a spelling
    `resolve` keeps, and each nested note is one file found twice: refused, not left out as a copy.
    On a case-sensitive volume (ext4) `INNER` does not exist and the scope refuses as a missing
    vault. Either way it refuses, and neither branch is skipped."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path / "engine")
    p = Plain(tmp_path)
    p.manifest(f'inherits = ["{(p.vault / "INNER").as_posix()}"]')
    p.write("inner/shared.md", TODO)
    folds = (p.vault / "INNER" / "shared.md").exists()
    with pytest.raises(V.VaultError) as refused:
        V.resolve_scope(p.vault)
    if folds:
        said = str(refused.value)
        assert "are one file, discovered twice, through two vaults" in said, said
        assert "/INNER/shared.md" in said and "/inner/shared.md" in said, said
    else:
        assert "INNER" in str(refused.value) and "discovered twice" not in str(refused.value)


def test_a_hard_link_is_one_file_and_refuses(plain):
    """Guard. One file under one name in two folders of a vault: not a copy of a note but the
    note twice, so neither is left out for the other. A hard link works the same on APFS and ext4.
    Under two different names it derives two doc_ids and composes as two notes, as a copy does."""
    import os

    plain.write("A/todo.md", TODO)
    (plain.vault / "B").mkdir()
    os.link(plain.vault / "A/todo.md", plain.vault / "B/todo.md")
    with pytest.raises(V.VaultError, match="are one file, discovered twice: it has two names"):
        V.resolve_scope(plain.vault)


# ---------------------------------------------------------------- first by path

def _key(vaults: list[tuple[str, int]], rel: str):
    """The sort key of a note at `rel` in the vault ranked `rank` (0: the project)."""
    where, refs = {}, {}
    for name, rank in vaults:
        root = Path("/v") / name
        where[root / rel] = (rank, root)
        refs[name] = V.NoteRef(path=root / rel, vault=name, tier=3)
    return V._first_by_path(where), refs


def test_first_by_path_on_strings():
    """The order, on strings, so it is the one both volumes give. Folder by folder by code point
    (`A` before `A b` before `a`), NFC first and then as stored, and the project vault before any
    it inherits."""
    rels = ["a/todo.md", "A b/todo.md", "A/todo.md", "B/x.md", unicodedata.normalize(
        "NFD", "é/x.md"), "f/x.md", unicodedata.normalize("NFC", "é/x.md")]
    where = {Path("/v/p") / r: (0, Path("/v/p")) for r in rels}
    key = V._first_by_path(where)
    ordered = sorted(rels, key=lambda r: key(V.NoteRef(path=Path("/v/p") / r, vault="p", tier=3)))
    assert ordered == ["A/todo.md", "A b/todo.md", "B/x.md", "a/todo.md", "f/x.md",
                       unicodedata.normalize("NFD", "é/x.md"),
                       unicodedata.normalize("NFC", "é/x.md")]
    core = {Path("/v/core/A/x.md"): (1, Path("/v/core")), Path("/v/p/Z/x.md"): (0, Path("/v/p"))}
    key = V._first_by_path(core)
    assert key(V.NoteRef(path=Path("/v/p/Z/x.md"), vault="p", tier=3)) < key(
        V.NoteRef(path=Path("/v/core/A/x.md"), vault="core", tier=1))


def test_the_nearest_vault_wins_in_a_chain(tmp_path):
    """project → mid → core: a copy in `mid` and one in `core`, and `mid`'s is indexed. Then the
    project's own copy beats both."""
    for name, inherits in (("core-vault", None), ("mid-vault", "core-vault"),
                           ("proj-vault", "mid-vault")):
        v = tmp_path / name
        v.mkdir()
        if inherits:
            (v / ".substrate.toml").write_text(f'name = "{name[:-6]}"\ninherits = ["{inherits}"]\n')
        (v / "own.md").write_text(f"# Own\n\n{name} alone.\n")
    for name in ("core-vault", "mid-vault"):
        (tmp_path / name / "x.md").write_text(TODO)
    scope = V.resolve_scope(tmp_path / "proj-vault")
    assert [(n.vault, k.vault) for n, k in scope.duplicates] == [("core-vault", "mid-vault")]
    (tmp_path / "proj-vault" / "zz").mkdir()
    (tmp_path / "proj-vault" / "zz" / "x.md").write_text(TODO)
    scope = V.resolve_scope(tmp_path / "proj-vault")
    assert sorted((n.vault, k.vault) for n, k in scope.duplicates) == [
        ("core-vault", "proj-vault"), ("mid-vault", "proj-vault")]


# ---------------------------------------------------------------- not a failure, not drift

def test_a_copy_left_out_is_not_a_failure_for_the_ceiling(plain):
    """`quarantine_ceiling = 0` refuses any failed note. A copy left out is not one."""
    plain.manifest("quarantine_ceiling = 0")
    plain.write("A/todo.md", TODO)
    plain.write("B/todo.md", TODO)
    rc, _, err = plain.compose()
    assert rc == 0, err
    assert plain.status()["quarantined"] == []


def test_a_vault_whose_notes_are_all_copies_is_not_taken_for_an_unmounted_one(tmp_path,
                                                                             monkeypatch):
    """An inherited vault (no manifest) whose remaining notes are all copies of the project's
    composes none of them; it was still discovered, so a deleted note's folder in it is pruned
    rather than read as a missing vault."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path / "engine")
    p = Plain(tmp_path)
    core = tmp_path / "core-vault"
    core.mkdir()
    (core / "gone.md").write_text("# Gone\n\nSoon deleted.\n")
    (core / "todo.md").write_text(TODO)
    p.manifest('inherits = ["core-vault"]')
    p.write("todo.md", TODO)
    assert p.compose()[0] == 0
    (core / "gone.md").unlink()
    rc, out, err = p.compose()
    assert rc == 0, err
    assert "pruned: core-vault/gone.md  (note deleted" in out


def test_thirty_empty_notes_are_one_group_counted_not_listed(plain):
    """Thirty empty notes under thirty names: all indexed, each holding nothing, one group with no
    body, counted under `empty` and never listed. A month of daily notes is cheap to report."""
    for i in range(30):
        plain.write(f"Untitled {i}.md" if i else "Untitled.md", "")
    rc, _, err = plain.compose()
    assert rc == 0, err
    assert len(plain.documents()) == 31
    ident = plain.status()["identical"]
    assert (ident["groups"], ident["listed"], ident["empty"], ident["duplicates"]) == (0, [], 30, 0)


def test_the_report_is_bounded(plain):
    """A count of each, and the first ones: 50 copies left out, 20 groups, 10 notes a group."""
    for i in range(55):
        plain.write(f"A/n{i:02d}.md", f"# N\n\nCopy {i}.\n")
        plain.write(f"B/n{i:02d}.md", f"# N\n\nCopy {i}.\n")
    for g in range(22):
        for j in range(12):
            plain.write(f"G/g{g:02d}-{j:02d}.md", f"# G\n\nGroup {g}.\n")
    ident = introspect.identical_report(V.resolve_scope(plain.vault))
    assert ident["duplicates"] == 55 and len(ident["duplicate_notes"]) == introspect.MAX_DUPLICATES_LISTED
    assert ident["duplicate_notes"][0] == {**_ref("plain-vault", "B/n00.md"),
                                         "copy_of": _ref("plain-vault", "A/n00.md")}
    assert ident["groups"] == 22 and ident["notes"] == 22 * 12
    assert len(ident["listed"]) == introspect.MAX_GROUPS_LISTED
    assert ident["listed"][0] == {"notes": 12, "first": [
        _ref("plain-vault", f"G/g00-{j:02d}.md") for j in range(introspect.MAX_GROUP_NOTES_LISTED)]}


def test_a_name_with_a_line_break_is_escaped_where_printed(plain):
    """A copy's name is its author's: a line break in it is written as `\\n` in compose's output,
    `status` in a terminal and a pass's report, so it cannot start a line of its own."""
    plain.write("A/x\nduplicate: forged.md", TODO)
    plain.write("B/x\nduplicate: forged.md", TODO)
    report = plain.root / "report.jsonl"
    report.write_text("", "utf-8")
    rc, out, err = plain.compose("--report", str(report))
    assert rc == 0, err
    assert not any(line.startswith("duplicate: forged") for line in out.splitlines())
    assert "plain-vault/B/x\\nduplicate: forged.md" in out
    lines, broken = compose_report.read(report, 0)
    assert broken is None
    assert lines == ["duplicate: plain-vault/B/x\\nduplicate: forged.md  (a copy of "
                     "plain-vault/A/x\\nduplicate: forged.md, which is indexed)"]
    assert not any(line.lstrip().startswith("duplicate: forged")
                   for line in plain.status_text().splitlines())


def test_copies_whose_meta_differs_by_any_byte_still_refuse(plain):
    """Guard. Ingest judges a passage on its source's `_meta.md` as a whole, not only on the fields
    compose reads from it: `Status: active` there quarantines the passage. A copy must not wait on
    a note that fails for a file the copy does not share, so `_meta.md` files that are not the same
    bytes refuse, whatever fields they agree on."""
    for name, meta in (("s1", "---\nStatus: active\n---\n"), ("s2", "---\nnote: hi\n---\n")):
        plain.write(f"_sources/{name}/_meta.md", meta)
        plain.write(f"_sources/{name}/passages/00-intro.md", "# Intro\n\nThe same passage.\n")
    rc, _, err = plain.compose()
    assert rc == 2, err
    assert "whose sources compose them differently" in err


def test_a_copy_of_a_quarantined_note_says_neither_is_indexed(plain):
    """A copy of a note that fails fails the same way, and is left out; the note is quarantined.
    Compose, its report and `status` say that neither is indexed, never that the copy's note
    answers in its place."""
    bad = "---\nstatus: someday\n---\n\n# Aa\n\nBody.\n"
    plain.write("aa.md", bad)
    plain.write("z/aa.md", bad)
    report = plain.root / "report.jsonl"
    report.write_text("", "utf-8")
    rc, out, err = plain.compose("--report", str(report))
    assert rc == 0, err
    assert ("duplicate: plain-vault/z/aa.md  (the same doc_id and bytes as plain-vault/aa.md, "
            "which is quarantined, so neither is indexed)") in out
    lines, broken = compose_report.read(report, 0)
    assert broken is None
    assert ("duplicate: plain-vault/z/aa.md  (a copy of plain-vault/aa.md, which is quarantined, "
            "so neither is indexed)") in lines
    s = plain.status()
    assert [q["path"] for q in s["quarantined"]] == ["aa.md"]
    assert s["identical"]["duplicate_notes"] == [
        {**_ref("plain-vault", "z/aa.md"), "copy_of": _ref("plain-vault", "aa.md")}]
    assert ("plain-vault/z/aa.md: a copy of plain-vault/aa.md, which is QUARANTINED, so neither "
            "is indexed") in plain.status_text()


def test_long_names_that_differ_after_48_characters_are_one_doc_id(plain):
    """A slug keeps 48 characters, so two unfilled notes from one template whose names differ only
    in a date at the end derive one doc_id, and one of them is left out with its title."""
    stem = "Weekly sync with the platform and infrastructure team"
    plain.write(f"{stem} 2026-01-05.md", "## Agenda\n\n## Actions\n")
    plain.write(f"{stem} 2026-01-12.md", "## Agenda\n\n## Actions\n")
    scope = V.resolve_scope(plain.vault)
    assert [(scope.relative(n), scope.relative(k)) for n, k in scope.duplicates] == [
        (f"{stem} 2026-01-12.md", f"{stem} 2026-01-05.md")]


def test_identical_notes_that_are_both_quarantined_are_no_group(plain):
    """A note with a status outside the vocabulary and its conflict copy both fail and are both
    quarantined. Neither is indexed, so neither answers twice: no group in compose's line or in
    `status`, as JSON or as text. They are named where they are, under `quarantined`."""
    bad = "---\nstatus: done\n---\n\n# Report\n\nThe quarterly figures.\n"
    plain.write("Report.md", bad)
    plain.write("Report (conflicted copy).md", bad)
    rc, out, err = plain.compose()
    assert rc == 0, err
    assert "WARNING (quarantine)" in err
    assert "identical:" not in out
    s = plain.status()
    assert sorted(q["path"] for q in s["quarantined"]) == ["Report (conflicted copy).md",
                                                            "Report.md"]
    assert (s["identical"]["groups"], s["identical"]["notes"], s["identical"]["listed"],
            s["identical"]["empty"]) == (0, 0, [], 0)
    assert "identical:" not in plain.status_text()


def test_three_identical_notes_with_one_quarantined_are_a_group_of_two(plain):
    """Three notes with one text, one of them under a name with nothing visible, so it has no
    title and is quarantined. The other two are indexed and are the group, of two."""
    body = "The plan both machines edited.\n"
    for name in ("Plan.md", "Plan 1.md", "   .md"):
        plain.write(name, body)
    rc, out, err = plain.compose()
    assert rc == 0, err
    assert [q["path"] for q in plain.status()["quarantined"]] == ["   .md"]
    assert "identical: 1 group(s) of notes with the same text, 2 notes, each indexed" in out
    ident = plain.status()["identical"]
    assert (ident["groups"], ident["notes"]) == (1, 2)
    assert ident["listed"] == [{"notes": 2, "first": [_ref("plain-vault", "Plan 1.md"),
                                                      _ref("plain-vault", "Plan.md")]}]
    assert "plain-vault/Plan 1.md, plain-vault/Plan.md" in plain.status_text()


def test_empty_counts_only_indexed_notes_with_no_body(plain):
    """`empty` counts indexed notes. Three empty notes with one quarantined (a name with nothing
    visible) count two; two frontmatter-only notes with a status outside the vocabulary, both
    quarantined, count none."""
    for name in ("Untitled.md", "Untitled 1.md", "   .md"):
        plain.write(name, "")
    for name in ("Draft.md", "Draft 1.md"):
        plain.write(name, "---\nstatus: done\n---\n")
    rc, _, err = plain.compose()
    assert rc == 0, err
    s = plain.status()
    assert sorted(q["path"] for q in s["quarantined"]) == ["   .md", "Draft 1.md", "Draft.md"]
    assert (s["identical"]["empty"], s["identical"]["groups"]) == (2, 0)
    assert "2 note(s) with no body share their bytes with another" in plain.status_text()


def test_compose_names_at_most_fifty_copies(plain):
    """Compose's output and a pass's report name the first 50 copies, as `status` lists them, and
    count the rest: a vault copied into a folder of itself is a copy per note on every pass."""
    for i in range(53):
        for folder in ("A", "B"):
            plain.write(f"{folder}/n{i:02d}.md", f"# N\n\nCopy {i}.\n")
    report = plain.root / "report.jsonl"
    report.write_text("", "utf-8")
    rc, out, err = plain.compose("--report", str(report))
    assert rc == 0, err
    named = [ln for ln in out.splitlines() if ln.lstrip().startswith("duplicate:")]
    assert len(named) == 51 and named[-1].strip() == (
        "duplicate: … and 3 more (`substrate status` counts them)")
    lines, broken = compose_report.read(report, 0)
    assert broken is None
    dups = [ln for ln in lines if ln.startswith("duplicate:")]
    assert len(dups) == 51 and dups[-1] == "duplicate: … and 3 more"


def test_a_copy_is_still_a_member_of_the_area_it_sits_in(plain):
    """Digest membership is folder position (`substrate digests`), not what is indexed: a copy left
    out for a note in another area still makes its own area's digest behind when it is newer."""
    import os

    plain.write("02-areas/a/todo.md", TODO)
    plain.write("02-areas/b/b-digest.md", "---\ndoc_type: digest\n---\n\n# B\n\nB holds todo.\n")
    copy = plain.write("02-areas/b/todo.md", TODO)
    digest = plain.vault / "02-areas/b/b-digest.md"
    os.utime(digest, (1_000_000, 1_000_000))
    os.utime(copy, (2_000_000, 2_000_000))
    scope = V.resolve_scope(plain.vault)
    assert [scope.relative(n) for n, _ in scope.duplicates] == ["02-areas/b/todo.md"]
    [rec] = [r for r in digests.audit(scope)["digests"] if r["path"].endswith("b-digest.md")]
    assert rec["stale"] is True, rec


# ---------------------------------------------------------------- the write path

AMENDED = b"\n- eggs\n"
# The write path requires a whole spine.
SPINED = "---\nstatus: active\ndoc_type: reference\nconfidence: proposed\n---\n\n" + TODO


def _amend(p: Plain, rel: str) -> None:
    book = notes.PlanBook()
    content = (p.vault / rel).read_bytes() + AMENDED
    plan = notes.plan_amend(project_vault=p.vault, path=rel, content=content, book=book)
    notes.commit_amend(project_vault=p.vault, path=rel, content=content,
                       confirm_token=plan.confirm_token, book=book)


@pytest.mark.parametrize("rel", ["A/todo.md", "B/todo.md"], ids=["the indexed copy", "the copy"])
def test_amending_either_copy_pins_the_id_the_other_derives(plain, rel):
    """`amend` pins the doc_id a note derives now into its frontmatter, and the other copy still
    derives that id from its unchanged bytes. So after amending either one, a note DECLARES the id
    another derives, and the next compose refuses, naming both. Nothing is overwritten. That is
    the write path's to check before it writes (#64), and is Open here."""
    plain.write("A/todo.md", SPINED)
    plain.write("B/todo.md", SPINED)
    assert plain.compose()[0] == 0
    _amend(plain, rel)
    rc, _, err = plain.compose()
    assert rc == 2, err
    assert "is claimed by two notes" in err and f"{rel} declares it" in err
    assert set(plain.documents()) == {"keep.md", "A/todo.md"}, "the refusal changed the index"


def test_editing_a_copy_by_hand_makes_two_notes(plain):
    """An edit that writes no doc_id (by hand, in the editor): the copies differ, each derives its
    own id, and both are indexed."""
    plain.write("A/todo.md", TODO)
    plain.write("B/todo.md", TODO)
    assert plain.compose()[0] == 0
    plain.write("B/todo.md", TODO + "- eggs\n")
    rc, _, err = plain.compose()
    assert rc == 0, err
    assert {"A/todo.md", "B/todo.md"} <= set(plain.documents())
    assert plain.status()["identical"]["duplicates"] == 0
