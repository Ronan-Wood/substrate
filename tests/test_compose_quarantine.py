"""Issue #22: a note that fails on its own is QUARANTINED, and the rest of the scope is indexed.

Before this, any per-note failure made `compose` exit 3 having indexed nothing ("refusing to index
a partial scope"), so one bad note froze the scope. Now the note is left out entirely (never
indexed under a fallback class or spine, and no earlier version of it stays), and it is named with
its reason in compose's warning and `substrate status`, and by kind and key over MCP and in the
refresh record. The scope is still refused for a fault that is not one note's own, above the
quarantine ceiling, and when no note is left to index.

"Fails before" marks a test that fails on `03e09f2` the way the issue describes: the scope
refuses. "Guard" marks one that passes there too, and fails here only with its guard switched off.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from substrate import agent, checks, cli, private, quarantine, refresh, refresh_state, scopes, stack
from substrate.mcp import server
from substrate.store.index_store import IndexStore

_VAULTS = Path(__file__).resolve().parent.parent / "vaults"

TUNING = "02-areas/retrieval-tuning.md"
TUNING_QUERY = "embedders reranking strategies gold set"
CALL = "_sources/engine-boundary-call.md"
CALL_QUERY = "app owns retrieval engine library links"
CALL_REF = "demo/engine-boundary-call#"


class World:
    """A copy of the demo vault pair, with a registry, index and database of its own."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault = root / "demo-vault"
        self.core = root / "demo-core-vault"
        self.db = root / "demo.db"
        self.index_root = root / "demo-index"
        self.registry = root / "scopes.toml"
        shutil.copytree(_VAULTS / "demo-vault", self.vault)
        shutil.copytree(_VAULTS / "demo-core-vault", self.core)

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def compose(self) -> tuple[int, str, str]:
        return self.run("compose", str(self.vault), "--db", str(self.db),
                        "--index-root", str(self.index_root), "--registry", str(self.registry))

    def status(self) -> dict:
        rc, out, err = self.run("status", "--scope", "demo", "--registry", str(self.registry),
                                "--lexical-only", "--json")
        assert rc == 0, err
        return json.loads(out)

    def status_text(self) -> str:
        rc, out, err = self.run("status", "--scope", "demo", "--registry", str(self.registry),
                                "--lexical-only")
        assert rc == 0, err
        return out

    def query(self, text: str, *flags: str) -> list[dict]:
        rc, out, err = self.run("query", "--scope", "demo", "--registry", str(self.registry),
                                "--no-vector", "--json", *flags, text)
        assert rc == 0, err
        return json.loads(out)["passages"]

    def call_hits(self, *flags: str) -> list[dict]:
        return [p for p in self.query(CALL_QUERY, *flags) if p["expand_ref"].startswith(CALL_REF)]

    def indexed(self) -> set[str]:
        """The vault-relative paths of every document in the index."""
        with IndexStore(str(self.db), migrate=False) as store:
            rows = [r[0] for r in store.db.execute("SELECT source_path FROM documents")]
        return {self._rel(Path(p)) for p in rows}

    def _rel(self, path: Path) -> str:
        for v in (self.vault, self.core):
            if path.resolve().is_relative_to(v.resolve()):
                return f"{v.name}/{path.resolve().relative_to(v.resolve()).as_posix()}"
        return str(path)

    def write(self, rel: str, text: str) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return path

    def manifest(self, line: str) -> None:
        """`line` as the first line of the project vault's manifest, above every [table]."""
        m = self.vault / ".substrate.toml"
        m.write_text(line + "\n" + m.read_text("utf-8"), "utf-8")

    def ingest_dir(self, vault: Path, rel: str) -> Path:
        return self.index_root / private.ingest_dir_name(vault.name, (vault / rel).resolve())


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    return World(tmp_path)


def _bad(n: int) -> str:
    return f"---\nstatus: someday\n---\n\n# Bad note {n}\n\nBroken body zqbad{n}x.\n"


def _quarantined(world: World, err: str, *rels: str, says: str,
                 kind: str | None = None, key: str | None = None) -> list[dict]:
    """Each of `rels` (vault-relative, vault name first) is quarantined: named in compose's
    warning with a reason holding `says`, which `substrate status` in a terminal quotes too; listed
    in `status`'s payload by path with `kind` and `key` and no reason; and absent from the index."""
    assert "WARNING (quarantine)" in err, err
    for rel in rels:
        assert rel in err, err
    assert says in err, err
    held = world.status()["quarantined"]
    named = {f"{q['vault']}/{q['path']}": q for q in held}
    for rel in rels:
        assert rel in named, (rel, named)
        assert set(named[rel]) == {"vault", "path", "kind", "key"}, named[rel]
        assert named[rel]["kind"] in quarantine.KINDS
        if kind is not None:
            assert (named[rel]["kind"], named[rel]["key"]) == (kind, key), named[rel]
    assert says in world.status_text()
    assert not set(rels) & world.indexed(), "a quarantined note was indexed"
    return held


# ---------------------------------------------------------------- each kind is quarantined

def _not_yaml(w: World) -> str:
    w.write("02-areas/not-yaml.md", "---\nstatus: active\nWe met and agreed.\n---\n\n# N\n\nB.\n")
    return "demo-vault/02-areas/not-yaml.md"


def _unknown_status(w: World) -> str:
    w.write("02-areas/bad-status.md", _bad(0))
    return "demo-vault/02-areas/bad-status.md"


def _unknown_doc_type(w: World) -> str:
    w.write("02-areas/bad-type.md", "---\ndoc_type: tutorial\n---\n\n# T\n\nBody.\n")
    return "demo-vault/02-areas/bad-type.md"


def _unknown_confidence(w: World) -> str:
    w.write("02-areas/bad-confidence.md", "---\nconfidence: high\n---\n\n# C\n\nBody.\n")
    return "demo-vault/02-areas/bad-confidence.md"


def _near_miss_key(w: World) -> str:
    w.write("02-areas/near-miss.md", "---\nStatus: archived\n---\n\n# Near\n\nBody.\n")
    return "demo-vault/02-areas/near-miss.md"


def _superseded_no_link(w: World) -> str:
    w.write("02-areas/orphan.md", "---\nstatus: superseded\n---\n\n# Orphan\n\nBody.\n")
    return "demo-vault/02-areas/orphan.md"


def _class_policy(w: World) -> str:
    w.write("_sources/untitled-call.md", "---\nclass: conversation\n---\n\nNo heading, said.\n")
    return "demo-vault/_sources/untitled-call.md"


def _unknown_class(w: World) -> str:
    w.write("02-areas/odd-class.md", "---\nclass: memo\n---\n\n# Odd\n\nBody.\n")
    return "demo-vault/02-areas/odd-class.md"


def _not_utf8(w: World) -> str:
    (w.vault / "02-areas" / "latin1.md").write_bytes(b"# Caf\xe9\n\nNot UTF-8.\n")
    return "demo-vault/02-areas/latin1.md"


KINDS = [
    ("frontmatter that is not YAML", _not_yaml, "is not YAML", "frontmatter_unreadable", "status"),
    ("a status outside the vocabulary", _unknown_status, "status 'someday' is not one of",
     "outside_vocabulary", "status"),
    ("a doc_type outside the vocabulary", _unknown_doc_type, "doc_type 'tutorial' is not one of",
     "outside_vocabulary", "doc_type"),
    ("a confidence outside the vocabulary", _unknown_confidence, "confidence 'high'",
     "outside_vocabulary", "confidence"),
    ("an engine key spelled another way", _near_miss_key, "does not read as `status`",
     "engine_key_spelled_another_way", "status"),
    ("superseded with no superseded_by", _superseded_no_link, "no 'superseded_by'",
     "superseded_without_link", "superseded_by"),
    ("a class policy refusal", _class_policy, "missing required field(s) ['title']",
     "missing_required_field", "title"),
    ("a class outside the vocabulary", _unknown_class, "unknown document_class 'memo'",
     "outside_vocabulary", "class"),
    ("a note that is not UTF-8", _not_utf8, "unreadable: not UTF-8", "not_utf8", None),
]


@pytest.mark.parametrize("what, break_it, says, kind, key", KINDS, ids=[k[0] for k in KINDS])
def test_each_kind_of_per_note_failure_quarantines_the_note_and_indexes_the_rest(
        world, what, break_it, says, kind, key):
    """Fails before: exit 3, "refusing to index a partial scope", and no index. Each is named
    over MCP by its own kind, never `other`."""
    rel = break_it(world)
    rc, out, err = world.compose()
    assert rc == 0, err
    held = _quarantined(world, err, rel, says=says)
    assert (held[0]["kind"], held[0]["key"]) == (kind, key)
    assert f"{quarantine.MARK} {rel}" in out
    assert "demo-vault/02-areas/retrieval-tuning.md" in world.indexed()
    assert world.query(TUNING_QUERY), "the rest of the scope did not answer"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_note_the_filesystem_cannot_read_still_refuses_the_scope(world):
    """Guard (refuses on `03e09f2` too). A read the OS refuses may be a volume mid-sync, and
    quarantining the note would remove its last good version from the index, so it is not the
    note's own fault: the scope refuses, and the earlier index stays (the PR's open questions)."""
    assert world.compose()[0] == 0
    before = world.indexed()
    path = world.vault / TUNING
    path.chmod(0)
    try:
        rc, _, err = world.compose()
        assert rc == 2, err
        assert "cannot read" in err and "WARNING (quarantine)" not in err
        assert world.indexed() == before
    finally:
        path.chmod(0o644)


def test_a_note_over_the_size_cap_is_quarantined_and_never_read_whole(world, monkeypatch):
    """Fails before: the vault layer refused the scope ("cap"). Its checksum is never taken, by
    compose or by `status`'s drift, because the file may be a sparse one of any size; with none
    stored, it reads as still quarantined while it stays over the cap."""
    from substrate import freshness, vault

    monkeypatch.setattr(vault, "_MAX_BYTES", 4096)
    world.write("02-areas/huge.md", "# Huge\n\n" + "word " * 2000)
    reads: list[int] = []
    real = freshness.read_bounded

    def counted(path, cap, **kw):
        data = real(path, cap, **kw)
        if Path(path).name == "huge.md" and cap == 4096:   # the fingerprint's reads
            reads.append(len(data))
        return data

    monkeypatch.setattr(freshness, "read_bounded", counted)
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/02-areas/huge.md", says="larger than the")
    assert not reads, "the note over the cap was read: read_bounded returned its bytes"
    with IndexStore(str(world.db), migrate=False) as store:
        [row] = store.quarantined()
    assert row["fingerprint"] is None
    drift = world.status()["drift"]
    assert drift["quarantined"] == 1 and not drift["stale"]


def test_a_note_that_goes_away_mid_compose_refuses_the_scope_and_says_so(world, monkeypatch):
    """Fails before: the `OSError` from reading the note was a traceback out of compose. Discovery
    read the note; by the time ingest reads it, it is gone. Not quarantined, for the reason the
    OS-unreadable test gives: a scope fault, named."""
    from substrate.markdown import ingest

    real = ingest.read_markdown

    def gone(path, *args, **kwargs):
        if path.name == "retrieval-tuning.md":
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(ingest, "read_markdown", gone)
    rc, _, err = world.compose()
    assert rc == 3, err
    assert "fault(s) that are not one note's own" in err and "cannot be read" in err
    assert not world.db.exists()


def test_an_oserror_that_is_not_the_notes_still_ends_the_compose(world, monkeypatch):
    """Guard: a write into the index root that fails is not one note's fault."""
    from substrate.markdown import ingest

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device", "elsewhere")

    monkeypatch.setattr(ingest, "_write_at", full)
    with pytest.raises(OSError, match="No space left"):
        world.compose()


def test_a_coverage_failure_quarantines_the_note(world, monkeypatch):
    """Fails before. A18 is raised inside ingest, per note (`CoverageError`)."""
    from substrate.markdown import ingest

    real = ingest.content_coverage

    def losing(body_md, captured):
        cov, missing = real(body_md, captured)
        return (0.5, ["zqlost"]) if "zqlost" in body_md else (cov, missing)

    monkeypatch.setattr(ingest, "content_coverage", losing)
    world.write("02-areas/lossy.md", "# Lossy\n\nzqlost words.\n")
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/02-areas/lossy.md", says="A18 markdown coverage")


def test_an_a22_failure_quarantines_the_note_and_removes_what_this_run_wrote(world, monkeypatch):
    """Fails before: "FATAL (A22 per-note assertion)", exit 3. A22 runs after the note's ingest
    folder is written, so the folder holds this run's artifacts and must go too."""
    real = checks.document_checks

    def failing(out: Path):
        rows = real(out)
        if "retrieval-tuning" in out.name:
            rows.append(("A99-planted", "A99 planted", False, "planted for the test"))
        return rows

    monkeypatch.setattr(checks, "document_checks", failing)
    rc, out, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/" + TUNING, says="A22 per-note assertion: A99 planted")
    assert not world.ingest_dir(world.vault, TUNING).exists()
    assert not world.query(TUNING_QUERY)
    assert "pruned:" not in out, "this run's own artifacts were called an earlier version"


def test_a_note_in_an_inherited_vault_is_quarantined_under_its_own_vault(world):
    """Fails before."""
    (world.core / "00-operator" / "bad-core.md").write_text(_bad(1), "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    [held] = _quarantined(world, err, "demo-core-vault/00-operator/bad-core.md",
                          says="status 'someday'")
    assert (held["vault"], held["path"]) == ("demo-core-vault", "00-operator/bad-core.md")


def test_a_quarantined_note_is_never_indexed_under_a_fallback_class(world):
    """Fails before (the scope refused). The call written `Class:` would have been an ordinary
    note to the reader; quarantine must not index it as one."""
    path = world.vault / CALL
    path.write_text(path.read_text("utf-8").replace("\nclass:", "\nClass:", 1), "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/" + CALL, says="does not read as `class`")
    assert not world.call_hits()
    assert not world.call_hits("--include-sources")


# ---------------------------------------------------------------- whole-scope faults still refuse

def _duplicate_doc_id(w: World) -> int:
    w.write("02-areas/twin.md", "---\ndoc_id: retrieval-tuning\n---\n\n# Twin\n\nBody.\n")
    return 2


def _byte_identical(w: World) -> int:
    w.write("02-areas/plain.md", "# Plain\n\nThe same words twice.\n")
    w.write("02-areas/plain (conflicted copy).md", "# Plain\n\nThe same words twice.\n")
    return 2


def _missing_vault(w: World) -> int:
    shutil.rmtree(w.core)
    return 2


def _broken_manifest(w: World) -> int:
    w.manifest("name = ")
    return 2


def _bad_ceiling(w: World) -> int:
    w.manifest("quarantine_ceiling = -1")
    return 2


def _planted_ingest_folder(w: World) -> int:
    target = w.root / "elsewhere"
    target.mkdir()
    w.index_root.mkdir()
    w.ingest_dir(w.vault, TUNING).symlink_to(target)
    return 3


WHOLE_SCOPE = [
    ("a doc_id two notes claim", _duplicate_doc_id, "retrieval-tuning"),
    ("two byte-identical notes", _byte_identical, "byte-identical"),
    ("a declared vault that is not there", _missing_vault, "demo-core-vault"),
    ("a manifest that does not parse", _broken_manifest, "malformed manifest"),
    ("a quarantine ceiling that is not one", _bad_ceiling, "quarantine_ceiling"),
    ("a link planted where a note's ingest folder goes", _planted_ingest_folder,
     "is not a folder of this user's"),
]


@pytest.mark.parametrize("what, break_it, says", WHOLE_SCOPE, ids=[c[0] for c in WHOLE_SCOPE])
def test_a_fault_that_is_not_one_notes_own_still_refuses_the_scope(world, what, break_it, says):
    """Guard. Beside a note that would be quarantined, so the quarantine cannot hide the fault.
    The planted link is the guard that `OutputDirError` stays a scope fault: quarantining it would
    index the rest and leave the link for the next compose to write a note through."""
    world.write("02-areas/bad-status.md", _bad(0))
    code = break_it(world)
    rc, _, err = world.compose()
    assert rc == code, (what, err)
    assert says in err, (what, err)
    assert "WARNING (quarantine)" not in err
    assert not world.db.exists(), f"compose refused over {what} and still wrote an index"


# ---------------------------------------------------------------- indexed, broken, fixed

def test_a_note_indexed_then_broken_then_fixed(world):
    """Fails before at the second compose (exit 3, and the old version kept answering from the
    frozen index). Quarantine removes the earlier good version's ingest folder, so its chunks are
    not indexed; fixing the note indexes it again and empties the quarantine."""
    assert world.compose()[0] == 0
    assert world.query(TUNING_QUERY)
    folder = world.ingest_dir(world.vault, TUNING)
    assert (folder / "run.json").is_file()
    good = (world.vault / TUNING).read_text("utf-8")

    (world.vault / TUNING).write_text(good.replace("\nstatus: active", "\nstatus: someday", 1),
                                      "utf-8")
    rc, out, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/" + TUNING, says="status 'someday'")
    assert not folder.exists(), "the earlier version's ingest folder stayed"
    assert (f"pruned: demo-vault/{TUNING}  (note quarantined; removed {folder.name})") in out
    assert not world.query(TUNING_QUERY), "the earlier version is still answering"
    assert not world.status()["drift"]["stale"]

    (world.vault / TUNING).write_text(good, "utf-8")
    assert world.status()["drift"]["added"], "a quarantined note edited since must read as stale"
    rc, _, err = world.compose()
    assert rc == 0, err
    assert "WARNING (quarantine)" not in err
    assert world.status()["quarantined"] == []
    assert world.query(TUNING_QUERY)


# ---------------------------------------------------------------- the ceiling

def _bad_notes(world: World, n: int) -> None:
    for i in range(n):
        world.write(f"02-areas/bad-{i:02d}.md", _bad(i))


@pytest.mark.parametrize("bad", [9, 10])
def test_the_default_ceiling_quarantines_up_to_ten_notes(world, bad):
    """Fails before. The demo scope's 10 notes are below 1,000, so the default is 10."""
    _bad_notes(world, bad)
    rc, _, err = world.compose()
    assert rc == 0, err
    assert len(world.status()["quarantined"]) == bad
    assert "(ceiling 10)" in err


def test_the_default_ceiling_refuses_eleven_notes(world):
    """Guard: refuses on `03e09f2` too, which named no ceiling. Fails here with the ceiling off."""
    _bad_notes(world, 11)
    rc, _, err = world.compose()
    assert rc == 3, err
    assert "11 of 21 note(s) failed to ingest, more than the quarantine ceiling of 10" in err
    assert "quarantine_ceiling" in err
    assert not world.db.exists()


def _big(tmp_path: Path, good: int, bad: int) -> tuple[int, str, Path]:
    vault = tmp_path / "big-vault"
    (vault / "notes").mkdir(parents=True)
    (vault / ".substrate.toml").write_text('name = "big"\n', "utf-8")
    for i in range(good):
        (vault / "notes" / f"n{i:04d}.md").write_text(f"# Note {i}\n\nBody zq{i}x.\n", "utf-8")
    for i in range(bad):
        (vault / "notes" / f"bad{i:02d}.md").write_text(_bad(i), "utf-8")
    out, err = io.StringIO(), io.StringIO()
    db = tmp_path / "big.db"
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(vault), "--db", str(db), "--index-root",
                       str(tmp_path / "big-index"), "--registry", str(tmp_path / "scopes.toml")])
    return rc, err.getvalue(), db


def test_the_default_ceiling_is_one_percent_once_that_is_more_than_ten(tmp_path, monkeypatch):
    """Fails before. 1,111 notes: 1% rounds down to 11, so 11 failures are quarantined."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    rc, err, db = _big(tmp_path, 1100, 11)
    assert rc == 0, err[-2000:]
    assert "11 of 1111 note(s) are QUARANTINED" in err and "(ceiling 11)" in err


def test_the_default_one_percent_refuses_one_note_over_it(tmp_path, monkeypatch):
    """Guard (refuses before too). 1,112 notes: 1% is still 11, and 12 failed."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    rc, err, db = _big(tmp_path, 1100, 12)
    assert rc == 3, err[-2000:]
    assert "12 of 1112 note(s) failed to ingest, more than the quarantine ceiling of 11" in err
    assert not db.exists()


@pytest.mark.parametrize("notes, ceiling", [(0, 10), (1, 10), (999, 10), (1099, 10),
                                            (1100, 11), (1199, 11), (1200, 12), (5000, 50)])
def test_one_percent_counts_every_note_and_rounds_down(notes, ceiling):
    assert quarantine.ceiling_for(None, notes) == ceiling


@pytest.mark.parametrize("declared", [None, '"none"', "25"])
def test_a_compose_that_would_index_nothing_refuses_and_the_earlier_index_stays(world, declared):
    """Fails before only on wording, as a guard would: `03e09f2` refused any failed note. DECIDED:
    when every note the scope discovered fails, the scope refuses whatever `quarantine_ceiling`
    says, `"none"` included, and the earlier index stays."""
    if declared is not None:
        world.manifest(f"{quarantine.CEILING_KEY} = {declared}")
    assert world.compose()[0] == 0
    before = world.indexed()
    notes = [p for v in (world.vault, world.core) for p in v.rglob("*.md")
             if "99-templates" not in p.parts and p.name not in {
                 "MEMORY.md", "log.md", "WRITING.md", "structure.md", "_meta.md"}]
    for i, path in enumerate(notes):
        path.write_text(_bad(100 + i), "utf-8")
    rc, _, err = world.compose()
    assert rc == 3, err
    assert f"all {len(notes)} note(s) failed to ingest, and none was left to index" in err
    assert world.indexed() == before


def test_a_compose_with_one_note_left_indexes_it(tmp_path, monkeypatch):
    """Fails before: one failed note refused the scope. All but one of 4 fail, under the default
    ceiling of 10: the scope composes with the one note."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    rc, err, db = _big(tmp_path, 1, 3)
    assert rc == 0, err
    assert "3 of 4 note(s) are QUARANTINED" in err
    with IndexStore(str(db), migrate=False) as store:
        assert store.stats()["documents"] == 1
        assert len(store.quarantined()) == 3


@pytest.mark.parametrize("declared, bad, refused", [
    ("2", 2, False), ("2", 3, True), ("0", 1, True), ("0", 0, False), ('"none"', 15, False),
    ("25", 15, False),
])
def test_a_declared_ceiling_replaces_the_default(world, declared, bad, refused):
    """Fails before for every case that indexes. `0` is the all-or-nothing compose of before."""
    world.manifest(f"{quarantine.CEILING_KEY} = {declared}")
    _bad_notes(world, bad)
    rc, _, err = world.compose()
    if refused:
        assert rc == 3, err
        assert (f"{bad} of {10 + bad} note(s) failed to ingest, more than the quarantine ceiling "
                f"of {declared}") in err
        assert "declared in" in err
    else:
        assert rc == 0, err
        assert len(world.status()["quarantined"]) == bad


@pytest.mark.parametrize("value", ["-1", "true", "2.5", '"10"', '"None"', "[3]"])
def test_a_ceiling_that_is_not_a_count_or_none_refuses(world, value):
    """Guard: a key `03e09f2` did not know, so it was ignored there. Read as the default it would
    let through, or refuse, a scope its owner meant otherwise."""
    world.manifest(f"{quarantine.CEILING_KEY} = {value}")
    rc, _, err = world.compose()
    assert rc == 2, err
    assert f"'{quarantine.CEILING_KEY}' must be a whole number of notes" in err


def test_a_ceiling_inside_a_table_refuses(world):
    """Guard, as above. TOML puts it inside `[reference_pins]`, where nothing reads it."""
    m = world.vault / ".substrate.toml"
    m.write_text(m.read_text("utf-8") + f'\n{quarantine.CEILING_KEY} = "none"\n', "utf-8")
    rc, _, err = world.compose()
    assert rc == 2, err
    assert f"'{quarantine.CEILING_KEY}' is inside the [reference_pins] table" in err


def test_an_inherited_vaults_ceiling_does_not_apply(world):
    """Fails before. The project vault's manifest sets the scope's ceiling; an inherited vault's is
    checked and then ignored."""
    (world.core / ".substrate.toml").write_text(
        f'name = "core"\n{quarantine.CEILING_KEY} = 0\n', "utf-8")
    world.write("02-areas/bad-status.md", _bad(0))
    rc, _, err = world.compose()
    assert rc == 0, err
    assert "(ceiling 10)" in err


# ---------------------------------------------------------------- status and the refresh record

def test_status_names_each_quarantined_note_loudly(world):
    """Fails before: no index, and `status` had no such field."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    payload = world.status()
    [held] = payload["quarantined"]
    assert set(held) == {"vault", "path", "kind", "key"}
    assert (held["vault"], held["path"]) == ("demo-vault", "02-areas/bad-status.md")
    assert (held["kind"], held["key"]) == ("outside_vocabulary", "status")
    assert payload["drift"]["quarantined"] == 1 and not payload["drift"]["stale"]
    text = world.status_text()
    assert "QUARANTINED: 1 note(s) are LEFT OUT of this index" in text
    assert "demo-vault/02-areas/bad-status.md: SpineError:" in text


def test_status_says_none_and_not_recorded_apart(world):
    """Guard: an index without the table reports the record as not kept, never as empty."""
    assert world.compose()[0] == 0
    assert world.status()["quarantined"] == []
    assert "quarantined: none" in world.status_text()
    with sqlite3.connect(world.db) as db:
        db.execute("DROP TABLE quarantine")
    assert world.status()["quarantined"] is None
    assert "quarantined: NOT RECORDED" in world.status_text()


def test_the_mcp_status_tool_lists_them(world):
    """Fails before. Through the server's own dispatcher, as a client calls it."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    cfg = server.Config(str(world.registry), stack.build(lexical_only=True), read_only=True)
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "status", "arguments": {"scope": "demo"}}}, cfg)
    body = json.loads(reply["result"]["content"][0]["text"])
    assert [(q["vault"], q["path"]) for q in body["quarantined"]] == [
        ("demo-vault", "02-areas/bad-status.md")]


def test_the_refresh_record_names_each_note_and_the_scope_is_not_frozen(world):
    """Fails before: compose refused, so the pass recorded `compose_failed` and froze the scope."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    refresh_state.record("demo", "refreshed", registry=world.registry)
    row = refresh_state.stored_row("demo", world.registry)
    assert row["quarantined"] == 1
    [note] = row["quarantined_notes"]
    assert note["path"] == "demo-vault/02-areas/bad-status.md"
    assert (note["kind"], note["key"]) == ("outside_vocabulary", "status")
    assert "reason" not in note and "someday" not in json.dumps(row)
    block = refresh_state.report("demo", world.registry)
    assert block["frozen"] is False and block["quarantined"] == 1
    assert "1 note(s) were QUARANTINED" in block["note"] and "`status` names each" in block["note"]
    assert "bad-status" not in json.dumps(block), "the block names a note; it carries a count"


def test_the_refresh_record_keeps_the_index_quarantine_through_a_refusal(world):
    """Fails before. A pass that refuses leaves the older index answering, so the record says what
    that index leaves out, not nothing."""
    world.write("02-areas/bad-status.md", _bad(99))
    assert world.compose()[0] == 0
    _bad_notes(world, 11)
    assert world.compose()[0] == 3
    refresh_state.record("demo", "compose_failed", registry=world.registry)
    block = refresh_state.report("demo", world.registry)
    assert block["frozen"] is True and block["quarantined"] == 1
    assert block["note"].startswith("FROZEN")


def test_the_refresh_record_caps_what_it_lists(world):
    """Guard: the record is parsed on every query, so it lists at most 50 notes and cuts each
    reason; the count stays whole and `status` lists every note."""
    world.manifest(f'{quarantine.CEILING_KEY} = "none"')
    _bad_notes(world, 60)
    assert world.compose()[0] == 0
    refresh_state.record("demo", "refreshed", registry=world.registry)
    row = refresh_state.stored_row("demo", world.registry)
    assert row["quarantined"] == 60
    assert len(row["quarantined_notes"]) == refresh_state.MAX_QUARANTINED_LISTED
    assert all(len(n["path"]) <= refresh_state.MAX_PATH_CHARS and "reason" not in n
               for n in row["quarantined_notes"])
    assert len(world.status()["quarantined"]) == 60


def test_a_refresh_pass_reports_each_quarantined_note_and_records_refreshed(world):
    """Fails before: the pass recorded `compose_failed`. Through `run_pass`, with compose run in
    process and embed answered, so the pass's own parsing of compose's output is what is tested."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0   # registers the scope

    def runner(argv, env):
        import subprocess

        cmd = argv[argv.index("substrate.cli") + 1:]
        if cmd[0] != "compose":
            return subprocess.CompletedProcess(argv, 0, "", "")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(cmd)
        return subprocess.CompletedProcess(argv, rc, out.getvalue(), err.getvalue())

    lines: list[str] = []
    [result] = refresh.run_pass(world.registry, runner=runner, log=lines.append)
    assert result.outcome == "refreshed" and result.recorded
    assert "quarantined: demo-vault/02-areas/bad-status.md  (outside_vocabulary: status)" \
        in result.detail
    assert refresh_state.report("demo", world.registry)["quarantined"] == 1


# ---------------------------------------------------------------- calls stay withheld

def test_a_call_that_breaks_is_left_out_and_never_surfaces(world):
    """Fails before at the second compose. The call is indexed (behind `include_sources`), then
    breaks: it is quarantined, its earlier version is gone even with `include_sources`, and the
    reply envelope a search without it gets names nothing about it."""
    assert world.compose()[0] == 0
    assert not world.call_hits()
    assert world.call_hits("--include-sources")
    path = world.vault / CALL
    path.write_text(path.read_text("utf-8").replace("\nstatus: active", "\nstatus: live", 1),
                    "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/" + CALL, says="status 'live'")
    assert not world.call_hits()
    assert not world.call_hits("--include-sources")
    refresh_state.record("demo", "refreshed", registry=world.registry)
    rc, out, _ = world.run("query", "--scope", "demo", "--registry", str(world.registry),
                           "--no-vector", "--json", TUNING_QUERY)
    assert "engine-boundary-call" not in out
    assert json.loads(out)["refresh"]["quarantined"] == 1
    cfg = server.Config(str(world.registry), stack.build(lexical_only=True), read_only=True)
    for args in ({"scope": "demo"}, {"scope": "demo", "include_sources": True}):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "documents", "arguments": args}}, cfg)
        text = reply["result"]["content"][0]["text"]
        assert not reply["result"].get("isError"), text
        assert "engine-boundary-call" not in text, args


def test_an_agent_snapshot_carries_no_quarantine(world, tmp_path):
    """Guard (the snapshot had nothing to carry before). A quarantined note may be a call whose
    class was never read, so neither its path nor a count that includes calls reaches the agent:
    the snapshot drops the table, and its manifest the record's quarantine."""
    path = world.vault / CALL
    path.write_text(path.read_text("utf-8").replace("\nstatus: active", "\nstatus: live", 1),
                    "utf-8")
    assert world.compose()[0] == 0
    refresh_state.record("demo", "refreshed", registry=world.registry)
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=world.registry)
    with sqlite3.connect(shared / "demo.snapshot.db") as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert "quarantine" not in tables
    manifest = (shared / "demo.snapshot.json").read_text("utf-8")
    assert "engine-boundary-call" not in manifest and "quarantined" not in manifest
    cfg = server.Config(None, stack.build(lexical_only=True),
                        agent=agent.AgentScopes(["demo"], shared))
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "status", "arguments": {"scope": "demo"}}}, cfg)
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["quarantined"] is None and body["refresh"]["quarantined"] is None


# ---------------------------------------------------------------- the decision, on its own

def test_decide_indexes_what_passed_and_quarantines_what_failed():
    d = quarantine.decide({"a": None, "b": "bad", "c": None}, [], 10)
    assert d.refusal is None and d.indexed == ("a", "c") and d.quarantined == (("b", "bad"),)


def test_decide_refuses_on_a_scope_fault_whatever_the_notes():
    d = quarantine.decide({"a": None}, ["duplicate doc_id"], None)
    assert d.refusal == quarantine.FAULT and d.indexed == ()


@pytest.mark.parametrize("failed, ceiling, refused", [(3, 3, False), (4, 3, True), (1, 0, True),
                                                     (0, 0, False), (99, None, False)])
def test_decide_refuses_only_above_the_ceiling(failed, ceiling, refused):
    outcomes = {i: ("bad" if i < failed else None) for i in range(100)}
    d = quarantine.decide(outcomes, [], ceiling)
    assert (d.refusal is not None) is refused
    assert len(d.quarantined) == failed
    assert d.indexed == (() if refused else tuple(range(failed, 100)))


# ---------------------------------------------------------------- what the review found

def test_a_passage_whose_meta_md_is_fixed_reads_as_stale(world):
    """Fails before (the scope refused). A passage is judged with its source's `_meta.md`, so a
    fix there must make `drift` say the next compose may index it, though the passage's own bytes
    never changed."""
    meta = world.core / "10-reference/frozen/software-dev/ddia-2e/_meta.md"
    good = meta.read_text("utf-8")
    meta.write_text(good.replace("\nclass:", "\nClass:", 1), "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["drift"]["quarantined"] == 1
    meta.write_text(good, "utf-8")
    drift = world.status()["drift"]
    assert drift["stale"] and drift["quarantined"] == 0
    assert any(p.endswith("00-partitioning.md") for p in drift["added"])


def test_the_record_describes_the_index_even_when_an_assertion_refuses(world, monkeypatch):
    """Fails before. The record is written right after reconcile, so a refusal after it leaves no
    note listed as left out that the index now holds."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    world.write("02-areas/bad-status.md", "# Fixed\n\nNow a plain note zqfixedx.\n")
    from substrate import vault

    def refuse(store, **kw):
        raise vault.VaultError("planted")

    monkeypatch.setattr(vault, "assert_composed", refuse)
    assert world.compose()[0] == 3
    assert world.status()["quarantined"] == []


def test_a_record_written_against_another_index_is_not_kept(world):
    """Guard. An engine from before the table recomposes the documents and leaves the rows; they no
    longer describe the index, so `status` says not kept rather than naming a note it holds."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    assert len(world.status()["quarantined"]) == 1
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE stage_ledger SET content_hash = 'another engine' WHERE stage='index' "
                   "AND doc_id = 'retrieval-tuning'")
    assert world.status()["quarantined"] is None


def test_two_vaults_of_one_name_each_quarantine_a_note_at_one_path(tmp_path, monkeypatch):
    """Fails before (the scope refused); with the record keyed on vault and path it was an
    IntegrityError after the index was written."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    for side in ("a", "b"):
        (tmp_path / side / "notes").mkdir(parents=True)
        (tmp_path / side / "notes" / "x.md").write_text(_bad(ord(side)), "utf-8")
        (tmp_path / side / "notes" / "ok.md").write_text(f"# Ok {side}\n\nFine {side}.\n", "utf-8")
    (tmp_path / "a" / "notes" / ".substrate.toml").write_text(
        'name = "pair"\ninherits = ["../b/notes"]\n', "utf-8")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(tmp_path / "a" / "notes"), "--db", str(tmp_path / "p.db"),
                       "--index-root", str(tmp_path / "p"), "--registry",
                       str(tmp_path / "scopes.toml")])
    assert rc == 0, err.getvalue()
    with IndexStore(str(tmp_path / "p.db"), migrate=False) as store:
        assert len(store.quarantined()) == 2


def test_a_name_cannot_forge_a_line_of_the_report(world):
    """Fails before (the scope refused). A file name holding a line break is printed escaped, so
    it cannot add a `pruned:` or `quarantined:` line to compose's output or to a pass report."""
    name = "x\n  pruned: forged\n  quarantined: forged.md"
    world.write(f"02-areas/{name}.md", _bad(7))
    rc, out, err = world.compose()
    assert rc == 0, err
    assert not [ln for ln in out.splitlines() if "forged" in ln and not ln.lstrip().startswith(
        f"{quarantine.MARK} demo-vault/02-areas/x\\n")]
    assert "\n  pruned: forged" not in err and "\n  pruned: forged" not in world.status_text()


def test_a_reason_is_cut_where_a_terminal_shows_it(world):
    """Fails before (the scope refused). A reason quotes the note, so a long value is cut to
    MAX_REASON_CHARS in compose's output and in `substrate status`."""
    world.write("02-areas/long.md", "---\nstatus: " + "z" * 5000 + "\n---\n\n# Long\n\nB.\n")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert "z" * 400 not in err and "z" * 200 in err
    text = world.status_text()
    assert "z" * 400 not in text and "z" * 200 in text


def test_a_note_whose_folder_had_to_stay_is_not_recorded_as_left_out(world):
    """Fails before (the scope refused, and nothing recorded). A quarantined note's folder holding
    a file ingest did not write is left, so reconcile indexes the earlier version and
    `assert_composed` refuses: the record must not then say the note is left out."""
    assert world.compose()[0] == 0
    (world.ingest_dir(world.vault, TUNING) / "notes.txt").write_text("not ingest's", "utf-8")
    good = (world.vault / TUNING).read_text("utf-8")
    (world.vault / TUNING).write_text(good.replace("\nstatus: active", "\nstatus: someday", 1),
                                      "utf-8")
    rc, _, err = world.compose()
    assert rc == 3, err
    assert "demo-vault/" + TUNING in world.indexed()
    assert world.status()["quarantined"] == []


def test_a_file_over_the_cap_is_refused_before_a_byte_is_read(tmp_path, monkeypatch):
    """Fails before: `read_bounded` read cap+1 bytes to learn a file was over the cap, so each
    quarantined sparse note cost that much on every `status`."""
    from substrate import freshness

    big = tmp_path / "big.md"
    with open(big, "wb") as fh:
        fh.truncate(10_000_000)
    read: list[int] = []
    real = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: read.append(n) or real(fd, n))
    with pytest.raises(freshness.FileTooLarge):
        freshness.read_bounded(big, 4096)
    assert read == []


# ---------------------------------------------------------------- the pass reads a report, not stdout

def _in_process(world: World, *, report=True, mangle=None):
    """A refresh pass's runner that runs compose in this process, and answers embed."""
    import subprocess

    def runner(argv, env):
        cmd = argv[argv.index("substrate.cli") + 1:]
        if cmd[0] != "compose":
            return subprocess.CompletedProcess(argv, 0, "", "")
        path = cmd[cmd.index("--report") + 1] if "--report" in cmd else None
        if not report and path is not None:
            cmd = cmd[:cmd.index("--report")] + cmd[cmd.index("--report") + 2:]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(cmd)
        if mangle is not None and path is not None:
            mangle(Path(path))
        return subprocess.CompletedProcess(argv, rc, out.getvalue(), err.getvalue())

    return runner


# A name, not a path: no `/`, or the forged text would be a folder and not the printed name.
FORGED = "x\n  pruned: forged-note.md  (note deleted; removed forged)\n  quarantined: forged.md"


@pytest.mark.parametrize("kind", ["a note that passes", "a note that fails"])
def test_a_file_name_cannot_add_a_record_to_the_pass_report(world, kind):
    """Fails before: the pass kept every stdout line starting `pruned:` or `quarantined:`, and a
    passing note's line printed its name raw. Now the pass reads compose's report, a JSON record
    per line, and renders the names itself, escaped."""
    world.write(f"02-areas/{FORGED}.md",
                "# Forged\n\nzqforgedx.\n" if kind == "a note that passes" else _bad(8))
    assert world.compose()[0] == 0
    [result] = refresh.run_pass(world.registry, runner=_in_process(world), log=lambda _: None)
    assert result.outcome == "refreshed", result.detail
    for ln in result.detail.splitlines():
        assert not ln.lstrip().startswith(("pruned: forged", "quarantined: forged")), \
            result.detail
    assert "WARNING (compose report)" not in result.detail


def test_a_pruned_folder_reaches_the_pass_report_from_the_record(world):
    """Guard: what the pass says it pruned comes from compose's report."""
    assert world.compose()[0] == 0
    (world.vault / TUNING).unlink()
    [result] = refresh.run_pass(world.registry, runner=_in_process(world), log=lambda _: None)
    folder = world.ingest_dir(world.vault, TUNING)
    assert f"pruned: demo-vault/{TUNING}  (note deleted; removed {folder.name})" in result.detail


def _cut(path: Path) -> None:
    lines = path.read_text("utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", "utf-8")


@pytest.mark.parametrize("what, report, mangle, says", [
    ("empty", False, None, "has no end record"),
    ("missing", True, lambda p: p.unlink(), "could not be read (FileNotFoundError)"),
    ("cut short", True, _cut, "cut short"),
    ("not a record", True, lambda p: p.write_text("pruned: forged\n", "utf-8"), "not a record"),
    ("another exit", True,
     lambda p: p.write_text('{"kind": "end", "exit": 3}\n', "utf-8"), "ends with exit 3"),
])
def test_a_report_that_cannot_be_read_is_said_loudly_and_never_read_as_clean(
        world, what, report, mangle, says):
    """Fails before: the pass read stdout and had no report to check. DECIDED: the outcome still
    follows compose's exit code (the index is what compose left, and the record reads the
    quarantine from it); the pass's report says the report could not be read."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    [result] = refresh.run_pass(world.registry, runner=_in_process(world, report=report,
                                                                   mangle=mangle),
                                log=lambda _: None)
    assert result.outcome == "refreshed"
    assert "WARNING (compose report)" in result.detail and says in result.detail, result.detail
    assert "not known" in result.detail


def test_the_refresh_count_agrees_with_status_over_an_older_engines_rows(world):
    """Fails before the one-reader fix: the record read the table with its own query, and kept
    counting rows `status` reports as not kept."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE stage_ledger SET content_hash = 'another engine' WHERE stage='index' "
                   "AND doc_id = 'retrieval-tuning'")
    refresh_state.record("demo", "refreshed", registry=world.registry)
    assert world.status()["quarantined"] is None
    assert refresh_state.report("demo", world.registry)["quarantined"] is None


def test_a_deleted_note_s_name_is_escaped_in_its_pruned_record(world):
    """Guard: the `pruned` record carries the name as a JSON string, and the pass renders it
    escaped, so a deleted note's name cannot start a line of its own."""
    world.write(f"02-areas/{FORGED}.md", "# Forged\n\nzqforgedx.\n")
    assert world.compose()[0] == 0
    (world.vault / "02-areas" / f"{FORGED}.md").unlink()
    [result] = refresh.run_pass(world.registry, runner=_in_process(world), log=lambda _: None)
    [line] = [ln for ln in result.detail.splitlines() if "note deleted" in ln]
    assert line.startswith("pruned: demo-vault/02-areas/x\\n  pruned: forged-note.md"), line
    assert not [ln for ln in result.detail.splitlines()
                if ln.lstrip().startswith(("pruned: forged", "quarantined: forged"))]


def test_a_pass_with_no_temp_folder_still_records(world, monkeypatch):
    """Guard: without a file for compose's report, compose still runs and the outcome is recorded,
    with the report said to be unknown."""
    import types

    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0

    def no_tmp(*a, **k):
        raise PermissionError(13, "Permission denied")

    # Only the pass's own temp file: the record it writes after uses `tempfile` too.
    monkeypatch.setattr(refresh, "tempfile", types.SimpleNamespace(mkstemp=no_tmp))
    [result] = refresh.run_pass(world.registry, runner=_in_process(world), log=lambda _: None)
    assert result.outcome == "refreshed" and result.recorded
    assert "could not be made (PermissionError)" in result.detail


def test_a_record_row_the_engine_did_not_write_never_stops_the_record(world):
    """Guard: `record` never raises over the quarantine it copies, so one hand-edited row cannot
    cost a pass the rest of its records."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE quarantine SET reason = X'41'")
    refresh_state.record("demo", "refreshed", registry=world.registry)
    assert refresh_state.report("demo", world.registry)["outcome"] == "refreshed"


# ---------------------------------------------------------------- no note text over MCP

SAID = "zqsaidonthecallx"
CALL_SHAPED = ("---\ntitle: A call\nclass: conversation\nstatus: Speaker A said " + SAID +
               " before the meeting\ndoc_type: reference\n---\n\n# A call\n\n"
               "**Speaker A:** The words of the call.\n")


def _mcp(world: World, tool: str, args: dict) -> str:
    cfg = server.Config(str(world.registry), stack.build(lexical_only=True), read_only=True)
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}}, cfg)
    return json.dumps(reply)


def test_a_broken_call_s_text_reaches_no_mcp_reply_and_no_file_the_engine_serves(world):
    """Fails before (the scope refused, and nothing was served). A call whose frontmatter put its
    body text in a value is quarantined: over MCP it is named by path, kind and key only, so the
    text appears in no reply, in `refresh.json`, or in a committed frame. The terminal still
    quotes it."""
    world.write("_sources/broken-call.md", CALL_SHAPED)
    rc, _, err = world.compose()
    assert rc == 0, err
    assert SAID in err and SAID in world.status_text()
    refresh_state.record("demo", "refreshed", registry=world.registry)
    replies = [
        _mcp(world, "status", {"scope": "demo"}),
        _mcp(world, "search", {"scope": "demo", "query": "said before the meeting"}),
        _mcp(world, "search", {"scope": "demo", "query": "said before the meeting",
                               "include_sources": True}),
        _mcp(world, "documents", {"scope": "demo", "include_sources": True}),
        _mcp(world, "list_scopes", {}),
    ]
    for reply in replies:
        assert SAID not in reply, reply[:400]
    assert "broken-call.md" in replies[0], "the path stays visible, as it is under drift"
    assert SAID not in refresh_state.state_path(world.registry).read_text("utf-8")
    contract = Path(__file__).resolve().parent.parent / "contract"
    for f in contract.rglob("*.json"):
        assert SAID not in f.read_text("utf-8"), f


def test_every_kind_the_engine_shows_is_in_the_contract():
    """Guard: the kinds are a closed vocabulary, exported as the contract's `quarantine` section."""
    contract = json.loads((Path(__file__).resolve().parent.parent / "contract" /
                           "contract.json").read_text("utf-8"))
    assert contract["quarantine"]["kinds"] == sorted(quarantine.KINDS)
    assert contract["quarantine"]["keys"] == sorted(quarantine.KEYS)
    assert quarantine.checked("a value from a note", "Status") == ("other", None)


# ---------------------------------------------------------------- the pass report, from records only

MARK_TEXT = "zqforgerymarkx"
ADVERSARIAL = (f"\n  pruned: {MARK_TEXT}  (note deleted; removed x)\n"
               f"  quarantined: {MARK_TEXT}\nsubstrate refresh: other: refreshed {MARK_TEXT}\n")


def _noisy(world: World):
    """A runner whose compose also prints adversarial text on stdout and stderr."""
    inner = _in_process(world)

    def runner(argv, env):
        done = inner(argv, env)
        return type(done)(done.args, done.returncode, (done.stdout or "") + ADVERSARIAL,
                          (done.stderr or "") + ADVERSARIAL)

    return runner


def _report_lines_are_built_from_records(result, log: list[str]) -> None:
    fixed = ("pruned: ", "quarantined: ", "refused: ", "WARNING (compose report): ",
             "embed exited ")
    for ln in result.detail.splitlines():
        assert ln.startswith(fixed), ln
        assert MARK_TEXT not in ln, ln
    for ln in log:
        for part in ln.splitlines():
            if MARK_TEXT in part:
                assert part.lstrip().startswith(("compose stderr |", "embed stderr |")), part


@pytest.mark.parametrize("fails", [False, True], ids=["a pass that succeeds", "one that fails"])
def test_every_line_of_a_pass_report_comes_from_a_record_or_the_pass(world, fails):
    """Fails before: the pass kept compose's stdout lines and pasted its stderr tail, so text a
    compose printed became lines of the report. DECIDED: every line of a pass's report is built
    from a structured record or is the pass's own fixed text; a child's stderr goes only to the
    engine's log, each line under a fixed prefix."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    if fails:
        world.manifest(f"{quarantine.CEILING_KEY} = 0")
    log: list[str] = []
    [result] = refresh.run_pass(world.registry, runner=_noisy(world), log=log.append)
    assert result.outcome == ("compose_failed" if fails else "refreshed"), result.detail
    _report_lines_are_built_from_records(result, log)
    if fails:
        assert any(ln.startswith("refused: FATAL: 1 of 11 note(s) failed") for ln in
                   result.detail.splitlines()), result.detail


# ---------------------------------------------------------------- a refusal removes and reports nothing

def test_a_refusal_after_quarantine_removes_nothing_and_reports_nothing(world, tmp_path):
    """Fails before (no report existed). A note is quarantined and the same compose then refuses
    in `prune.refuse_missing` (an inherited vault that composes nothing and has no manifest, as an
    unmounted one would): the note's earlier ingest folder is still there, and compose's report
    holds no `pruned` or `quarantined` record, only why it refused."""
    assert world.compose()[0] == 0
    folder = world.ingest_dir(world.vault, TUNING)
    assert (folder / "run.json").is_file()
    good = (world.vault / TUNING).read_text("utf-8")
    (world.vault / TUNING).write_text(good.replace("\nstatus: active", "\nstatus: someday", 1),
                                      "utf-8")
    for p in world.core.rglob("*.md"):
        p.unlink()
    report = tmp_path / "report.jsonl"
    report.write_text("", "utf-8")
    rc, _, err = world.run("compose", str(world.vault), "--db", str(world.db), "--index-root",
                           str(world.index_root), "--registry", str(world.registry),
                           "--report", str(report))
    assert rc == 3 and "FATAL (prune)" in err, err
    assert (folder / "run.json").is_file(), "a refused compose removed an ingest folder"
    kinds = [json.loads(ln)["kind"] for ln in report.read_text("utf-8").splitlines()]
    assert kinds == ["refused", "end"], kinds


def test_a_hand_edited_row_s_kind_and_key_never_leave_the_engine(world):
    """Guard: a stored kind and key are held to the contract's vocabularies wherever a row leaves
    the engine, so a row edited by hand cannot put text where a client reads a kind: the MCP
    `status` reply, `refresh.json`, and a pass's report."""
    world.write("02-areas/bad-status.md", _bad(0))
    assert world.compose()[0] == 0
    with sqlite3.connect(world.db) as db:
        db.execute(f"UPDATE quarantine SET kind = '{SAID}', key = '{SAID}'")
    reply = _mcp(world, "status", {"scope": "demo"})
    assert SAID not in reply
    held = json.loads(json.loads(reply)["result"]["content"][0]["text"])["quarantined"]
    assert [(q["kind"], q["key"]) for q in held] == [("other", None)]
    refresh_state.record("demo", "refreshed", registry=world.registry)
    assert SAID not in refresh_state.state_path(world.registry).read_text("utf-8")
    from substrate import compose_report

    report = world.root / "forged-report.jsonl"
    report.write_text(json.dumps({"kind": "quarantined", "path": "demo-vault/x.md",
                                  "fault": SAID, "key": SAID}) + "\n"
                      + json.dumps({"kind": "end", "exit": 0}) + "\n", "utf-8")
    lines, problem = compose_report.read(report, 0)
    assert problem is None and lines == ["quarantined: demo-vault/x.md  (other)"]


# ---------------------------------------------------------------- notes the write path wrote (#23)

_WRITTEN = (b"---\ntitle: Written by a client\nstatus: active\ndoc_type: decision\n"
            b"confidence: stated\n---\n\n# Written by a client\n\nzqwrittenx in a nested folder.\n")


def test_a_note_ingest_wrote_then_broken_is_quarantined_like_any_other(world):
    """Fails on `22da5ae`, the base this lands on: the broken note refuses the scope. (On `03e09f2`
    it fails earlier: `ingest` took no nested folder before #23.) A note `ingest` wrote into a folder
    #23's default opens, then edited by hand into a value outside the vocabulary, is quarantined,
    and the rest of the scope composes."""
    from substrate import notes

    book = notes.PlanBook()
    plan = notes.plan(project_vault=world.vault, content=_WRITTEN, filename="client.md",
                      folder="02-areas/new-area", book=book)
    written = notes.commit(project_vault=world.vault, content=_WRITTEN, filename="client.md",
                           folder="02-areas/new-area", confirm_token=plan.confirm_token, book=book)
    assert world.compose()[0] == 0
    assert "demo-vault/02-areas/new-area/client.md" in world.indexed()
    written.write_bytes(written.read_bytes().replace(b"status: active", b"status: pending"))
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/02-areas/new-area/client.md",
                 says="status 'pending'", kind="outside_vocabulary", key="status")
    assert world.query(TUNING_QUERY)


def test_a_note_amend_changed_then_broken_is_quarantined_like_any_other(world):
    """Fails on `22da5ae`, the base this lands on: the broken note refuses the scope. As above,
    through `amend`."""
    from substrate import notes

    path = world.vault / TUNING
    book = notes.PlanBook()
    # The write path requires a confidence, which the demo note leaves to the default.
    new = path.read_bytes().replace(b"---\n", b"---\nconfidence: stated\n", 1) + \
        b"\nAmended by a client.\n"
    plan = notes.plan_amend(project_vault=world.vault, path=TUNING, content=new, book=book)
    notes.commit_amend(project_vault=world.vault, path=TUNING, content=new,
                       confirm_token=plan.confirm_token, book=book)
    assert world.compose()[0] == 0
    path.write_bytes(path.read_bytes().replace(b"\nstatus: active", b"\nStatus: active", 1))
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/" + TUNING, says="does not read as `status`",
                 kind="engine_key_spelled_another_way", key="status")
