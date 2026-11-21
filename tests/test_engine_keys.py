"""Issue #53: a vault whose notes use `status`, `doc_type`, `confidence` or `class` for values of
their own names, in `[engine_keys]`, another property for the engine to read each field from.

Decided 2026-11-19: the manifest renames the keys the engine reads, each field on its own and none
by default; the vault's own property of the old name is then one the engine does not judge, except
that a value under it that would withhold or freeze the note under the engine's own name
quarantines the note (the guard); a value outside the vocabulary under the engine's key, renamed or
not, is quarantined as before; and the over-ceiling refusal names the setting.

"Fails before" marks a test that fails on `f6b1699`, where `[engine_keys]` is an open table nobody
reads: a foreign value is quarantined, and a note the guard holds is indexed. "Guard" marks one
that passes there too.

Every name in these tests is ASCII, so each holds as written on a case-sensitive Linux volume that
keeps NFC and NFD names apart: the engine compares property names by `reader.canonical_forms`, not
by the filesystem, and no test creates two files whose names differ only in case or form.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from substrate import freshness, notes, quarantine, refresh_state, scopes, vault
from substrate.store.index_store import IndexStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_compose_quarantine import CALL, World, _mcp, _quarantined  # noqa: E402

# Spelled here, not imported, so this file runs on an engine without `engine_keys` and each test
# fails there the way the issue describes rather than all of them at import.
GUARD = "withheld_value_under_renamed_key"
FIELDS = ("status", "doc_type", "confidence", "class")
RENAMED = ('[engine_keys]\nstatus = "substrate_status"\ndoc_type = "substrate_doc_type"\n'
           'confidence = "substrate_confidence"\nclass = "substrate_class"\n')


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    """The demo pair with the project vault's own notes replaced by one plain note: a vault of
    notes written before their author met the engine, inheriting a core vault written for it."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    w = World(tmp_path)
    for note in w.vault.rglob("*.md"):
        note.unlink()
    w.write("02-areas/plain.md", "# A plain note\n\nNothing declared, zqplainx.\n")
    return w


@pytest.fixture
def demo(tmp_path, monkeypatch) -> World:
    """The demo pair as it is, written for the engine."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    return World(tmp_path)


def _table(world: World, text: str = RENAMED, vault_dir: Path | None = None) -> None:
    """`text` at the end of a vault's manifest, below every other table."""
    m = (vault_dir or world.vault) / ".substrate.toml"
    m.write_text(m.read_text("utf-8") + "\n" + text, "utf-8")


def _note(world: World, rel: str, front: str, body: str = "") -> Path:
    stem = Path(rel).stem
    return world.write(rel, f"---\n{front}\n---\n\n# Note {stem}\n\nBody of zq{stem.replace('-', '')}x. {body}\n")


FOREIGN = {
    "02-areas/done.md": "status: done",
    "02-areas/in-progress.md": "status: in progress",
    "02-areas/sure.md": "confidence: high",
    "02-areas/meeting.md": "doc_type: meeting",
    "02-areas/maths.md": "class: Math 101",
    "02-areas/all-four.md": "status: done\ndoc_type: meeting\nconfidence: high\nclass: Math 101",
}


def _foreign(world: World) -> None:
    for rel, front in FOREIGN.items():
        _note(world, rel, front)


def _held(world: World) -> dict[str, tuple[str, str | None]]:
    return {f"{q['vault']}/{q['path']}": (q["kind"], q["key"])
            for q in world.status()["quarantined"]}


# ---------------------------------------------------------------- a vault of its own values

def test_with_no_setting_a_foreign_value_is_quarantined_as_before(world):
    """Guard: no `[engine_keys]`, and each foreign value is `outside_vocabulary` under its key."""
    _foreign(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    held = _held(world)
    assert held["demo-vault/02-areas/done.md"] == ("outside_vocabulary", "status")
    assert held["demo-vault/02-areas/sure.md"] == ("outside_vocabulary", "confidence")
    assert held["demo-vault/02-areas/meeting.md"] == ("outside_vocabulary", "doc_type")
    assert held["demo-vault/02-areas/maths.md"] == ("outside_vocabulary", "class")


def test_a_vault_of_foreign_values_composes_with_its_keys_renamed(world):
    """Fails before: every one of these notes is quarantined. With the four fields renamed they
    compose, nothing is quarantined, and each reads as a note that declared no spine."""
    _foreign(world)
    _table(world)
    rc, out, err = world.compose()
    assert rc == 0, err
    assert "WARNING (quarantine)" not in err, err
    assert world.status()["quarantined"] == []
    assert {f"demo-vault/{r}" for r in FOREIGN} <= world.indexed()
    with IndexStore(str(world.db), migrate=False) as store:
        row = store.db.execute(
            "SELECT status, doc_type, confidence, document_class FROM documents "
            "WHERE source_path LIKE '%all-four.md'").fetchone()
    assert tuple(row) == ("active", "reference", "unjudged", "unclassified")
    assert world.status()["engine_keys"]["renamed"] == [
        {"vault": "demo-vault", "field": f, "key": f"substrate_{f}"} for f in FIELDS]


def test_each_field_is_renamed_on_its_own(world):
    """Fails before. Renaming `status` alone leaves `class` read as the engine's key."""
    _note(world, "02-areas/done.md", "status: done")
    _note(world, "02-areas/maths.md", "class: Math 101")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world) == {"demo-vault/02-areas/maths.md": ("outside_vocabulary", "class")}
    assert "demo-vault/02-areas/done.md" in world.indexed()


def test_the_renamed_key_is_read_and_judged_as_the_engine_s_own(world):
    """Fails before: the renamed key is unread, so `archived` under it is indexed live, and a typo
    under it is indexed too. Under its new name the field is the engine's: a value is read, and one
    outside the vocabulary is quarantined. No value is mapped or defaulted."""
    _note(world, "02-areas/filed.md", "status: done\nsubstrate_status: archived")
    _note(world, "02-areas/typo.md", "status: done\nsubstrate_status: archvied")
    _note(world, "02-areas/typo-class.md", "substrate_class: conversaton")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    held = _held(world)
    assert held["demo-vault/02-areas/typo.md"] == ("outside_vocabulary", "status")
    assert held["demo-vault/02-areas/typo-class.md"] == ("outside_vocabulary", "class")
    assert "archvied" in err
    with IndexStore(str(world.db), migrate=False) as store:
        status = store.db.execute("SELECT status FROM documents WHERE source_path LIKE "
                                  "'%filed.md'").fetchone()[0]
    assert status == "archived"
    assert not [p for p in world.query("zqfiledx") if "filed" in p["expand_ref"]]
    assert [p for p in world.query("zqfiledx", "--include-archived") if "filed" in p["expand_ref"]]


# ---------------------------------------------------------------- the guard

GUARDED = {
    "02-areas/old-archived.md": ("status: archived", "status"),
    "02-areas/old-superseded.md": ("status: superseded\nsuperseded_by: x", "status"),
    "02-areas/old-call.md": ("class: conversation\ntitle: A call", "class"),
    "02-areas/old-frozen.md": ("class: reference-frozen", "class"),
    "02-areas/old-doc-class.md": ("document_class: conversation\ntitle: A call", "document_class"),
    "02-areas/spelled.md": ("Class: conversation\ntitle: A call", "class"),
    "02-areas/quoted.md": ('"class": conversation\ntitle: A call', "class"),
    "02-areas/decoy.md": ("substrate_status: active\nsubstrate_doc_type: reference\n"
                          "class: conversation\nclass: Math 101", "class"),
    "02-areas/both.md": ("class: Math 101\ndocument_class: conversation", "document_class"),
    "02-areas/block.md": ("tags:\n  - x\nstatus: archived", "status"),
    "02-areas/commented.md": ("class: conversation # a call\ntitle: A call", "class"),
    "02-areas/tagged.md": ("class: !!str conversation\ntitle: A call", "class"),
    "02-areas/anchored.md": ("status: &s archived", "status"),
    "02-areas/other-new.md": ("class: conversation\nsubstrate_class: reference-frozen", "class"),
}


def test_a_value_that_would_withhold_the_note_under_the_old_name_is_held(world):
    """Fails before: with `[engine_keys]` unread, each of these is read under the engine's own name
    and indexed: archived, superseded, a call, a frozen edition. With the field renamed, the old
    name's value would withhold or freeze the note were it the engine's key, so the note is
    quarantined under its own kind, naming the old key, and is absent from every search."""
    for rel, (front, _) in GUARDED.items():
        _note(world, rel, front)
    world.manifest('quarantine_ceiling = "none"')
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    held = _held(world)
    for rel, (_, key) in GUARDED.items():
        assert held.get(f"demo-vault/{rel}") == (GUARD, key), (rel, held.get(f"demo-vault/{rel}"))
    assert not {f"demo-vault/{r}" for r in GUARDED} & world.indexed()
    for flags in ((), ("--include-sources",), ("--include-archived",),
                  ("--include-sources", "--include-archived")):
        for rel in GUARDED:
            stem = Path(rel).stem.replace("-", "")
            assert not [p for p in world.query(f"zq{stem}x", *flags)
                        if Path(rel).stem in p["expand_ref"]], (rel, flags)
    assert world.status()["engine_keys"]["guarded"] == len(GUARDED)
    assert f"{len(GUARDED)} note(s) held by the guard" in world.status_text()


def test_the_same_value_under_the_new_name_lets_the_note_compose(world):
    """Guard: it passes before too, where the old name is the engine's key and both notes read as
    they do here. Here it shows the guard's advice works: a note that declares under the new name
    the value its old key holds composes, read under the new name, so a call stays a call."""
    _note(world, "02-areas/both-call.md",
          "class: conversation\nsubstrate_class: conversation\ntitle: A call")
    _note(world, "02-areas/both-archived.md", "status: archived\nsubstrate_status: archived")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["quarantined"] == []
    with IndexStore(str(world.db), migrate=False) as store:
        rows = dict(store.db.execute(
            "SELECT source_path, document_class || '/' || status FROM documents "
            "WHERE source_path LIKE '%both-%'").fetchall())
    assert sorted(rows.values()) == ["conversation/active", "unclassified/archived"]


def test_a_refusal_under_a_renamed_key_says_where_the_field_is_read_from(world):
    """Fails before. A typo, or an absence on the write path, under a renamed field names the
    property the engine reads it from, and says the old name is the vault's own."""
    _note(world, "02-areas/typo.md", "substrate_status: archvied")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert "this vault reads status from `substrate_status` ([engine_keys]), and `status:` is " \
        "the vault's own: " in err, err


def test_a_value_the_engine_merely_knows_under_the_old_name_is_ordinary(world):
    """Fails before (`Status` is refused there). A value of the engine's that withholds and freezes
    nothing (`status: active`, `class: reference-versioned`) is the vault's own once the field is
    renamed: the note reads as declaring none, and a `Status` key is the vault's too."""
    _note(world, "02-areas/was-active.md", "status: active")
    _note(world, "02-areas/was-versioned.md", "class: reference-versioned")
    _note(world, "02-areas/capital.md", "Status: Done")
    _note(world, "02-areas/old-type.md", "doc_type: digest\nconfidence: unjudged")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["quarantined"] == []
    assert world.status()["defaulted"]["status"] >= 4


def test_without_a_rename_the_old_name_is_the_engine_s_key(world):
    """Guard: no rename, no guard. `status: archived` is archived, `Status` refused as ever."""
    _note(world, "02-areas/old-archived.md", "status: archived")
    _note(world, "02-areas/capital.md", "Status: Done")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world) == {
        "demo-vault/02-areas/capital.md": ("engine_key_spelled_another_way", "status")}
    assert "demo-vault/02-areas/old-archived.md" in world.indexed()


def test_an_unreadable_value_under_a_guarded_old_name_is_held(world):
    """Fails before (indexed). A `Class` key whose value the reader leaves out (a nested mapping)
    cannot be compared, so it may hold `conversation`: the note is not indexed."""
    world.write("02-areas/nested.md", "---\nClass:\n  kind: conversation\ntitle: A call\n---\n\n"
                                      "# A call\n\nzqnestedx.\n")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world)["demo-vault/02-areas/nested.md"] == ("frontmatter_unreadable", "class")


def test_a_rename_spelled_another_way_is_refused_as_any_engine_key_is(world):
    """Fails before (indexed). Exact spelling (#21) holds for a renamed key: `Substrate_Status` is
    `substrate_status` written another way, and is refused naming the engine field."""
    _note(world, "02-areas/near.md", "Substrate_Status: archived")
    _note(world, "02-areas/near-class.md", "substrate-class: conversation\ntitle: A call")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    held = _held(world)
    assert held["demo-vault/02-areas/near.md"] == ("engine_key_spelled_another_way", "status")
    assert held["demo-vault/02-areas/near-class.md"] == ("engine_key_spelled_another_way", "class")
    assert "reads only `substrate_status`" in err


def test_a_call_held_by_the_guard_leaves_its_text_nowhere_a_client_reads(world):
    """Fails before: the call is indexed and its text is served with `include_sources`. Held by the
    guard, it is named over MCP by path, kind and key only, and its text reaches no reply, no
    `refresh.json` and no committed frame."""
    said = "zqguardedcallsaidx"
    world.write("_sources/held-call.md",
                "---\ntitle: A call\nclass: conversation\nstatus: active\n---\n\n# A call\n\n"
                f"**Speaker A:** We {said} before the meeting.\n")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    _quarantined(world, err, "demo-vault/_sources/held-call.md", says="its `class` holds `conversation`",
                 kind=GUARD, key="class")
    refresh_state.record("demo", "refreshed", registry=world.registry)
    replies = [
        _mcp(world, "status", {"scope": "demo"}),
        _mcp(world, "search", {"scope": "demo", "query": "said before the meeting",
                               "include_sources": True}),
        _mcp(world, "documents", {"scope": "demo", "include_sources": True}),
    ]
    for reply in replies:
        assert said not in reply, reply[:400]
    assert GUARD in replies[0]
    assert said not in refresh_state.state_path(world.registry).read_text("utf-8")
    contract = Path(__file__).resolve().parent.parent / "contract"
    for f in contract.rglob("*.json"):
        assert said not in f.read_text("utf-8"), f


def test_the_demo_call_is_held_when_class_is_renamed(demo):
    """Fails before (the call stays indexed behind `include_sources`). A vault written for the
    engine that then renames `class` has its calls held, not served as ordinary notes."""
    world = demo
    assert world.compose()[0] == 0
    assert world.call_hits("--include-sources")
    _table(world, '[engine_keys]\nclass = "substrate_class"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world)["demo-vault/" + CALL] == (GUARD, "class")
    assert not world.call_hits("--include-sources")


# ---------------------------------------------------------------- the manifest setting

@pytest.mark.parametrize("text, says", [
    ('[engine_keys]\nstaus = "substrate_status"\n', "did you mean 'status'"),
    ('[engine_keys]\ntitle = "my_title"\n', "the fields a vault may rename"),
    ('[engine-keys]\nstatus = "substrate_status"\n', "did you mean [engine_keys]"),
    ('[Engine_Keys]\nstatus = "substrate_status"\n', "did you mean [engine_keys]"),
    ('[engine_key]\nstatus = "substrate_status"\n', "did you mean [engine_keys]"),
    ('[spine_keys]\nclass = "substrate_class"\n', "did you mean [engine_keys]"),
    ('[keys]\nstatus = "s_status"\nclass = "s_class"\n', "did you mean [engine_keys]"),
    ('[engine_keys]\nstatus = 3\n', "must be a property name"),
    ('[engine_keys]\nstatus = ""\n', "must be a property name"),
    ('[engine_keys]\nstatus = "my status"\n', "must be a property name"),
    ('[engine_keys]\nstatus = "1status"\n', "must be a property name"),
    ('[engine_keys]\nstatus = "status"\n', "its own name, which is no rename"),
    ('[engine_keys]\nstatus = "Status"\n', "its own name, which is no rename"),
    ('[engine_keys]\nclass = "document_class"\n', "its own name, which is no rename"),
    ('[engine_keys]\nstatus = "doc_type"\n', "the engine's own key `doc_type`"),
    ('[engine_keys]\nstatus = "Title"\n', "`title` spelled another way"),
    ('[engine_keys]\nstatus = "written_by"\n', "the engine's own key `written_by`"),
    ('[engine_keys]\nstatus = "s_x"\nclass = "s-x"\n', "renames status and class to 's-x'"),
    ('[engine_keys]\nstatus = "s_x"\nclass = "s_x"\n', "renames status and class to 's_x'"),
])
def test_a_setting_the_engine_cannot_apply_refuses_compose_by_name(world, text, says):
    """Fails before: `[engine_keys]` is an open table, so each of these composes as if it said
    nothing, and a typo drops the rename it holds without a word."""
    _table(world, text)
    rc, _, err = world.compose()
    assert rc == 2, err
    assert says in err, err


def test_a_setting_that_is_not_a_table_refuses(world):
    """Fails before (`engine_keys = …` is an unknown key there, refused by #52's rule, but as
    unknown: here it is a known key whose value is wrong)."""
    world.manifest('engine_keys = "substrate_status"')
    rc, _, err = world.compose()
    assert rc == 2 and "must be a table" in err, err


def test_a_top_level_key_swallowed_into_the_table_is_still_misplaced(world):
    """Guard: a key written below `[engine_keys]` belongs to it, and #52's check refuses it."""
    _table(world, '[engine_keys]\nstatus = "substrate_status"\nquarantine_ceiling = 20\n')
    rc, _, err = world.compose()
    assert rc == 2, err
    assert "'quarantine_ceiling' is inside the [engine_keys] table" in err


def test_another_table_is_still_open(world):
    """Guard: #52 leaves a table of another name unchecked, and this keeps that, for one that holds
    anything but spine fields alone."""
    _table(world, '[my_plugin]\nstatus = "whatever"\ntheme = "dark"\n')
    assert world.compose()[0] == 0


# ---------------------------------------------------------------- inherited vaults

def test_a_rename_holds_for_its_own_vault_and_no_other(world):
    """Fails before (the project's foreign note is quarantined). The project renames `status`; the
    core vault it inherits is read by its own manifest (it has none), so its `archived` note stays
    archived, out of default search, and its `done` note is quarantined as a foreign value."""
    _note(world, "02-areas/done.md", "status: done")
    core_archived = world.core / "02-areas" / "core-archived.md"
    core_archived.parent.mkdir(parents=True, exist_ok=True)
    core_archived.write_text("---\nstatus: archived\n---\n\n# Core archived\n\nzqcorearchx.\n",
                             "utf-8")
    (world.core / "02-areas" / "core-done.md").write_text(
        "---\nstatus: done\n---\n\n# Core done\n\nzqcoredonex.\n", "utf-8")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world) == {
        "demo-core-vault/02-areas/core-done.md": ("outside_vocabulary", "status")}
    assert {"demo-vault/02-areas/done.md", "demo-core-vault/02-areas/core-archived.md"} \
        <= world.indexed()
    with IndexStore(str(world.db), migrate=False) as store:
        status = store.db.execute("SELECT status FROM documents WHERE source_path LIKE "
                                  "'%core-archived.md'").fetchone()[0]
    assert status == "archived"
    assert not [p for p in world.query("zqcorearchx") if "core-archived" in p["expand_ref"]]


def test_an_inherited_vault_s_own_rename_holds_for_its_notes(tmp_path, monkeypatch):
    """Fails before. A core vault with a manifest of its own renames for its own notes; the
    project that inherits it renames nothing."""
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    world = World(tmp_path)
    (world.core / ".substrate.toml").write_text(
        'name = "core"\n\n[engine_keys]\nstatus = "substrate_status"\n', "utf-8")
    (world.core / "02-areas").mkdir(parents=True, exist_ok=True)
    (world.core / "02-areas" / "core-done.md").write_text(
        "---\nstatus: done\n---\n\n# Core done\n\nzqcoredonex.\n", "utf-8")
    _note(world, "02-areas/done.md", "status: done")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world) == {"demo-vault/02-areas/done.md": ("outside_vocabulary", "status")}
    assert "demo-core-vault/02-areas/core-done.md" in world.indexed()
    assert world.status()["engine_keys"]["renamed"] == [
        {"vault": "demo-core-vault", "field": "status", "key": "substrate_status"}]


def test_a_source_s_meta_md_is_read_under_the_engine_s_names(world):
    """Guard: `_meta.md` is the engine's own format, so a passage inherits its `class` and `status`
    whatever the vault renames."""
    src = world.vault / "03-references" / "src"
    (src / "passages").mkdir(parents=True)
    (src / "_meta.md").write_text("---\nclass: reference-frozen\nstatus: active\n"
                                  "doc_type: reference\n---\n", "utf-8")
    (src / "passages" / "p1.md").write_text("# Passage\n\nzqpassagex.\n", "utf-8")
    _table(world)
    rc, _, err = world.compose()
    assert rc == 0, err
    with IndexStore(str(world.db), migrate=False) as store:
        row = store.db.execute("SELECT document_class, status FROM documents "
                               "WHERE source_path LIKE '%p1.md'").fetchone()
    assert tuple(row) == ("reference-frozen", "active")


def test_a_skipped_file_declares_a_spine_by_the_vault_s_names(world):
    """Fails before: `log.md` with `status: done` declares a spine there, and refuses the scope.
    Renamed, it declares none; under the new name it declares one, and is refused."""
    log = world.vault / "log.md"
    log.write_text("---\nstatus: done\n---\n\n# Log\n", "utf-8")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    assert world.compose()[0] == 0
    log.write_text("---\nsubstrate_status: active\n---\n\n# Log\n", "utf-8")
    rc, _, err = world.compose()
    assert rc == 2 and "declares spine fields (substrate_status)" in err, err


# ---------------------------------------------------------------- the write path

def _amend_vault(tmp_path: Path, table: str = RENAMED) -> Path:
    v = tmp_path / "demo-vault"
    (v / "04-synthesis").mkdir(parents=True)
    (v / ".substrate.toml").write_text('name = "demo"\n\n' + table, "utf-8")
    return v


NEW = (b"---\nstatus: done\nsubstrate_status: active\nsubstrate_doc_type: decision\n"
       b"substrate_confidence: proposed\n---\n\n# A decision\n\nWe chose the hybrid stack.\n")


def test_ingest_reads_and_requires_the_renamed_keys(tmp_path):
    """Fails before: `status: done` refuses the note. Renamed, the write path requires the spine
    under the new names and leaves the old one alone."""
    v = _amend_vault(tmp_path)
    book = notes.PlanBook()
    plan = notes.plan(project_vault=v, content=NEW, filename="d.md", book=book)
    assert (plan.status, plan.doc_type, plan.confidence) == ("active", "decision", "proposed")
    with pytest.raises(notes.NoteError, match="reads status from `substrate_status`.*no status"):
        notes.plan(project_vault=v, content=NEW.replace(b"status: done\nsubstrate_status: active\n",
                                                        b"status: active\n"),
                   filename="d.md", book=book)


def test_ingest_refuses_a_note_the_guard_would_hold(tmp_path):
    """Fails before (the call is accepted as a conversation). A new note in a renamed vault cannot
    arrive marked a call under the old name."""
    v = _amend_vault(tmp_path)
    with pytest.raises(notes.NoteError, match="Refusing to index it as a live note"):
        notes.plan(project_vault=v, content=NEW.replace(b"status: done\n",
                                                        b"class: conversation\n"),
                   filename="d.md", book=notes.PlanBook())


def test_amend_refuses_a_note_the_guard_holds(tmp_path):
    """Fails before: with the rename unread, the note's `status: done` refuses the amend as a gate
    failure on the result, not as a note held. A note compose leaves out because its old `class`
    says it is a call is never amended as a live note, whatever the edit."""
    v = _amend_vault(tmp_path)
    held = NEW.replace(b"status: done\n", b"class: conversation\n")
    (v / "04-synthesis" / "call.md").write_bytes(held)
    with pytest.raises(notes.NoteError, match="never amended as a live note"):
        notes.plan_amend(project_vault=v, path="04-synthesis/call.md",
                         content=held.replace(b"class: conversation\n", b""),
                         book=notes.PlanBook())


def test_amend_s_refusal_of_a_held_note_carries_no_text_from_it(tmp_path):
    """Fails before (the note is a call there, refused as frozen). The guard's refusal reaches an
    MCP client through `amend`, so it names the engine key and the engine's value, never the line
    as written: a comment beside `conversation` is the note's text."""
    said = "zqwithsomeonex"
    v = _amend_vault(tmp_path)
    held = NEW.replace(b"status: done\n", f"class: conversation # {said}\n".encode())
    (v / "04-synthesis" / "call.md").write_bytes(held)
    with pytest.raises(notes.NoteError) as e:
        notes.plan_amend(project_vault=v, path="04-synthesis/call.md", content=held + b"\nMore.\n",
                         book=notes.PlanBook())
    assert "its `class` holds `conversation`" in str(e.value)
    assert said not in str(e.value)


def test_amend_reads_the_frozen_class_under_the_renamed_key(tmp_path):
    """Fails before: `substrate_class` is unread, so a frozen edition is amendable."""
    v = _amend_vault(tmp_path)
    frozen = NEW.replace(b"status: done\n", b"substrate_class: reference-frozen\n")
    (v / "04-synthesis" / "book.md").write_bytes(frozen)
    with pytest.raises(notes.NoteError, match="a frozen class"):
        notes.plan_amend(project_vault=v, path="04-synthesis/book.md",
                         content=frozen + b"\nMore.\n", book=notes.PlanBook())


def test_amend_edits_a_note_of_foreign_values_in_a_renamed_vault(tmp_path):
    """Fails before: `status: done` fails the gate on both sides."""
    v = _amend_vault(tmp_path)
    (v / "04-synthesis" / "d.md").write_bytes(NEW)
    book = notes.PlanBook()
    plan = notes.plan_amend(project_vault=v, path="04-synthesis/d.md", content=NEW + b"\nMore.\n",
                            book=book)
    assert plan.spine_after["status"] == "active"
    notes.commit_amend(project_vault=v, path="04-synthesis/d.md", content=NEW + b"\nMore.\n",
                       confirm_token=plan.confirm_token, book=book)


def test_the_write_path_refuses_when_the_renames_cannot_be_known(tmp_path):
    """Fails before. A misnamed table means the renames are unknown, so nothing is written."""
    v = _amend_vault(tmp_path, '[engine-keys]\nstatus = "substrate_status"\n')
    with pytest.raises(notes.NoteError, match=r"did you mean \[engine_keys\]"):
        notes.plan(project_vault=v, content=NEW, filename="d.md", book=notes.PlanBook())


# ---------------------------------------------------------------- changing the setting

def test_adding_and_removing_a_rename_is_drift_and_the_next_compose_follows(world):
    """Fails before: drift compares bytes, so a rename added under an unchanged vault reads clean
    and the archived note stays indexed. Here every note of the vault reads as changed, a note
    quarantined before is `added` (its fingerprint covers the renames), the next compose holds the
    archived note and removes its earlier version, and removing the rename undoes it all."""
    _note(world, "02-areas/old-archived.md", "status: archived")
    _note(world, "02-areas/done.md", "status: done")
    assert world.compose()[0] == 0
    assert world.status()["drift"]["stale"] is False
    assert "demo-vault/02-areas/old-archived.md" in world.indexed()
    folder = world.ingest_dir(world.vault, "02-areas/old-archived.md")
    assert folder.is_dir()

    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    drift = world.status()["drift"]
    assert drift["stale"] is True
    assert any(p.endswith("old-archived.md") for p in drift["changed"]), drift
    assert any(p.endswith("02-areas/done.md") for p in drift["added"]), drift
    assert any(p.endswith("plain.md") for p in drift["changed"]), drift
    assert not any("demo-core-vault" in p for p in drift["changed"]), drift

    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world) == {"demo-vault/02-areas/old-archived.md": (GUARD, "status")}
    assert not folder.exists()
    assert "demo-vault/02-areas/done.md" in world.indexed()
    assert world.status()["drift"]["stale"] is False

    m = world.vault / ".substrate.toml"
    m.write_text(m.read_text("utf-8").split("[engine_keys]")[0], "utf-8")
    drift = world.status()["drift"]
    assert drift["stale"] is True
    assert any(p.endswith("old-archived.md") for p in drift["added"]), drift
    assert world.compose()[0] == 0
    assert "demo-vault/02-areas/old-archived.md" in world.indexed()
    assert world.status()["drift"]["stale"] is False


def test_a_quarantined_note_s_fingerprint_covers_its_vault_s_renames(tmp_path):
    """Fails before (the third argument does not exist). With no renames the fingerprint is the one
    an earlier engine stored, so upgrading moves no quarantined note into `added`."""
    note = tmp_path / "n.md"
    note.write_text("---\nstatus: done\n---\n", "utf-8")
    plain = freshness.quarantine_fingerprint(note, None)
    assert freshness.quarantine_fingerprint(note, None, "") == plain
    from substrate import engine_keys

    sig = engine_keys.KeyMap((("status", "substrate_status"),)).signature
    assert freshness.quarantine_fingerprint(note, None, sig) != plain


# ---------------------------------------------------------------- where a new user meets it

def _many_foreign(world: World, n: int = 12) -> None:
    for i in range(n):
        _note(world, f"02-areas/task-{i}.md", "status: done" if i % 2 else "class: Math 101")


def test_the_over_ceiling_refusal_names_the_setting(world):
    """Fails before: twelve foreign values pass the default ceiling of ten and the scope refuses,
    saying nothing of a way to keep them. The refusal now names `[engine_keys]` with the fields."""
    _many_foreign(world)
    rc, _, err = world.compose()
    assert rc == 3, err
    assert "12 of them hold a value of the vault's own under" in err, err
    assert "`status` (6)" in err and "`class` (6)" in err, err
    assert ', under [engine_keys], for example status = "substrate_status", ' \
           'class = "substrate_class"' in err, err
    assert str(world.vault / ".substrate.toml") in err


def test_the_refusal_does_not_suggest_renaming_a_field_already_renamed(world):
    """Fails before: the renamed key is unread, so the twelve typos are indexed and nothing
    refuses. A typo under a renamed key is a typo: the refusal does not suggest renaming it
    again."""
    for i in range(12):
        _note(world, f"02-areas/typo-{i}.md", "substrate_status: archvied")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 3, err
    assert "under [engine_keys], for example" not in err, err


def test_foreign_values_says_which_fields_carry_them_for_init(world):
    """Fails before (no such function). For `substrate init`: per field, how many of a vault's
    notes give it a value outside the vocabulary, read as the vault reads them now."""
    _foreign(world)
    assert vault.foreign_values(world.vault) == {"status": 3, "doc_type": 2, "confidence": 2,
                                                 "class": 2}
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    assert vault.foreign_values(world.vault) == {"doc_type": 2, "confidence": 2, "class": 2}
    assert vault.foreign_values(world.core) == {}
    src = world.vault / "03-references" / "src"
    (src / "passages").mkdir(parents=True)
    (src / "_meta.md").write_text("---\nclass: reference-frozen\n---\n", "utf-8")
    (src / "passages" / "p1.md").write_text("---\nclass: Chapter 1\n---\n\n# P\n\nzq.\n", "utf-8")
    assert vault.foreign_values(world.vault)["class"] == 2, "a passage's own class is not read"


def test_the_guard_s_vocabulary_is_the_engine_s_own():
    """Derived from the engine's sets, so a value they gain is guarded the day it is added."""
    from substrate import classes, engine_keys, spine

    assert engine_keys.GUARDED["status"] == spine.EXCLUDED_STATUSES
    assert engine_keys.GUARDED["class"] == classes.EXCLUDED_CLASSES | {
        n for n, p in classes.POLICIES.items() if p.frozen}
    assert engine_keys.GUARDED["class"] == {"conversation", "reference-frozen"}
    assert GUARD == engine_keys.GUARD_KIND and GUARD in quarantine.KINDS
    assert FIELDS == engine_keys.FIELDS


def test_mcp_status_names_the_renames_and_the_guard_count(world):
    """Fails before (no `engine_keys` in the reply). With no rename it says so, and counts none."""
    assert world.compose()[0] == 0
    assert world.status()["engine_keys"] == {"renamed": [], "guarded": 0}
    _note(world, "02-areas/old-archived.md", "status: archived")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    assert world.compose()[0] == 0
    body = json.loads(json.loads(_mcp(world, "status", {"scope": "demo"}))
                      ["result"]["content"][0]["text"])
    assert set(body["engine_keys"]) == {"renamed", "guarded"}
    assert body["engine_keys"] == {
        "renamed": [{"vault": "demo-vault", "field": "status", "key": "substrate_status"}],
        "guarded": 1}


# ---------------------------------------------------------------- `substrate init` names the setting

def _init(vault_dir: Path, home: Path, monkeypatch) -> tuple[int, str, str]:
    import contextlib
    import io

    from substrate import cli

    monkeypatch.setattr(scopes, "engine_home", lambda: home)
    monkeypatch.setattr(scopes, "index_home", lambda: home / scopes.INDEX_DIR)
    monkeypatch.setenv(scopes.ENV_VAR, str(home / "scopes.toml"))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["init", str(vault_dir)])
    return rc, out.getvalue(), err.getvalue()


def _plain_vault(root: Path) -> Path:
    v = root / "tasks"
    (v / "projects").mkdir(parents=True)
    for i, front in enumerate(["status: done", "status: in progress", "class: Math 101",
                               "status: active"]):
        (v / "projects" / f"p{i}.md").write_text(f"---\n{front}\n---\n\n# P {i}\n\nzq{i}x.\n",
                                                 "utf-8")
    (v / "plain.md").write_text("Nothing declared.\n", "utf-8")
    return v


def test_init_names_the_setting_for_foreign_values_and_writes_only_name(tmp_path, monkeypatch):
    """Fails before: `init` says nothing of the notes compose will quarantine. It names the fields
    and counts and prints the `[engine_keys]` lines, and still writes `name` alone, mode 0600."""
    v = _plain_vault(tmp_path)
    rc, out, err = _init(v, tmp_path / "home", monkeypatch)
    assert (rc, err) == (0, ""), err
    assert "`status` in 2 notes, `class` in 1 note" in out, out
    assert '[engine_keys]\n  status = "substrate_status"\n  class = "substrate_class"\n' in out
    assert out.index("[engine_keys]") < out.index("substrate compose"), "said before compose"
    manifest = v / ".substrate.toml"
    assert manifest.read_text("utf-8") == 'name = "tasks"\n'
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert "done" not in out and "Math" not in out, "no value from a note"


def test_init_says_nothing_of_the_setting_for_a_vault_written_for_the_engine(tmp_path,
                                                                             monkeypatch):
    """Guard: notes whose spine values are the engine's (or that declare none) need no rename,
    and `init` prints nothing about it."""
    v = tmp_path / "engine-notes"
    v.mkdir()
    (v / "a.md").write_text("---\nstatus: archived\nclass: conversation\ntitle: T\n---\n\n# T\n",
                            "utf-8")
    (v / "b.md").write_text("Plain.\n", "utf-8")
    rc, out, err = _init(v, tmp_path / "home", monkeypatch)
    assert rc == 0, err
    assert "engine_keys" not in out and "values of the vault's own" not in out


def test_init_then_the_printed_lines_compose_the_vault(tmp_path, monkeypatch):
    """Fails before. The lines `init` prints, added as printed, make the scope compose with nothing
    quarantined."""
    import contextlib
    import io

    from substrate import cli

    v = _plain_vault(tmp_path)
    rc, out, _ = _init(v, tmp_path / "home", monkeypatch)
    assert rc == 0
    lines = out[out.index("  [engine_keys]"):].split("  (docs")[0]
    m = v / ".substrate.toml"
    m.write_text(m.read_text("utf-8") + "\n" + "\n".join(ln.strip() for ln in
                                                       lines.splitlines()) + "\n", "utf-8")
    o, e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        rc = cli.main(["compose", str(v)])
    assert rc == 0, e.getvalue()
    assert "quarantine" not in (o.getvalue() + e.getvalue()).lower()


def test_init_still_writes_when_the_check_cannot_read_the_vault(tmp_path, monkeypatch):
    """Guard on the manifest being written: a skipped file that declares a spine makes compose
    refuse, and `init` says the check could not run rather than failing having written."""
    v = tmp_path / "notes"
    v.mkdir()
    (v / "a.md").write_text("Plain.\n", "utf-8")
    (v / "log.md").write_text("---\nstatus: done\n---\n", "utf-8")
    rc, out, err = _init(v, tmp_path / "home", monkeypatch)
    assert rc == 0, err
    assert "could not check the notes' spine values" in out
    assert (v / ".substrate.toml").read_text("utf-8") == 'name = "notes"\n'


# ---------------------------------------------------------------- where it meets identical notes (#71)

def _core_copy(world: World, rel: str, text: str) -> None:
    for root in (world.vault, world.core):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")


def test_a_copy_across_vaults_that_rename_alike_in_effect_is_left_out(world):
    """Guard on #71's rule with renames in play: a note with no spine key reads alike in a vault
    that renames and one that does not, so its copy under the same name is left out as a copy."""
    _core_copy(world, "02-areas/same.md", "# Same\n\nBody zqsamex.\n")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, out, err = world.compose()
    assert rc == 0, err
    ident = world.status()["identical"]
    assert ident["duplicates"] == 1
    assert ident["duplicate_notes"][0]["path"] == "02-areas/same.md"


def test_a_copy_writing_only_fields_neither_vault_renames_is_left_out(world):
    """Guard on #71's rule: the project renames `class`, the core vault nothing, and a note under
    one name in both writes `status`, `doc_type` and `confidence` only, which both read alike, so
    the copy is left out rather than refusing the scope."""
    _core_copy(world, "02-areas/same.md", "---\nstatus: active\ndoc_type: decision\n"
                                          "confidence: stated\n---\n\n# Same\n\nzqsamex.\n")
    _table(world, '[engine_keys]\nclass = "substrate_class"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["identical"]["duplicates"] == 1


def test_a_copy_across_vaults_that_read_it_differently_refuses_and_says_why(world):
    """Fails before: renames are not read, so the two compose alike and one is left out, though
    the project would read `status: done` as its own and the core vault as a foreign status. Here
    neither can stand for the other: the scope refuses, and the message names the renames, not
    the `_meta.md` files (the sources have none)."""
    _core_copy(world, "02-areas/same.md", "---\nstatus: done\n---\n\n# Same\n\nzqsamex.\n")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 2, err
    assert "rename the spine keys differently ([engine_keys])" in err, err
    assert "`_meta.md` files" not in err


def test_a_copy_of_a_note_the_guard_holds_is_named_and_neither_is_indexed(world):
    """Fails before (the archived note is indexed there). A same-name copy, in the same vault's
    other folder, of a note the guard holds is a copy of a quarantined note: `status` says
    neither is indexed, in text and JSON."""
    text = "---\nstatus: archived\n---\n\n# Old\n\nzqoldx.\n"
    for folder in ("02-areas", "04-synthesis"):
        world.write(f"{folder}/old.md", text)
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    status = world.status()
    assert {(q["path"], q["kind"]) for q in status["quarantined"]} == {("02-areas/old.md", GUARD)}
    [dup] = status["identical"]["duplicate_notes"]
    assert dup["path"] == "04-synthesis/old.md" and dup["copy_of"]["path"] == "02-areas/old.md"
    assert not {"demo-vault/02-areas/old.md", "demo-vault/04-synthesis/old.md"} & world.indexed()
    text_out = world.status_text()
    assert "a copy of demo-vault/02-areas/old.md, which is QUARANTINED, so neither is indexed" \
        in text_out


@pytest.mark.parametrize("front", ["status: archived", "status: done"])
def test_same_text_notes_the_engine_quarantines_are_in_no_identical_group(world, front):
    """Fails before for the guard (the archived pair is indexed and grouped there). Two notes of
    one text under different names, both held by the guard, or both with a foreign value under a
    key the vault does not rename, are in no group: neither is indexed."""
    text = f"---\n{front}\n---\n\n# Twin\n\nzqtwinx.\n"
    world.write("02-areas/twin-a.md", text)
    world.write("02-areas/twin-b.md", text)
    _table(world, '[engine_keys]\nclass = "substrate_class"\n' if front == "status: done"
           else '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    status = world.status()
    assert len(status["quarantined"]) == 2
    assert status["identical"]["groups"] == 0, status["identical"]


def test_status_reads_every_block_together_in_text_and_json(world):
    """Fails before (no `engine_keys`). Renames, the guard's count, a quarantined note, a copy,
    a group of identical notes and the defaulted counts, side by side."""
    _note(world, "02-areas/held.md", "status: archived")
    _note(world, "02-areas/typo.md", "substrate_status: archvied")
    world.write("02-areas/dup-a.md", "# Dup\n\nzqdupx.\n")
    world.write("02-areas/dup-b.md", "# Dup\n\nzqdupx.\n")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    assert world.compose()[0] == 0
    s = world.status()
    assert s["engine_keys"] == {"renamed": [{"vault": "demo-vault", "field": "status",
                                             "key": "substrate_status"}], "guarded": 1}
    assert {(q["path"], q["kind"]) for q in s["quarantined"]} == {
        ("02-areas/held.md", GUARD), ("02-areas/typo.md", "outside_vocabulary")}
    assert s["identical"]["groups"] == 1 and s["identical"]["notes"] == 2
    assert s["defaulted"]["status"] >= 3
    text = world.status_text()
    for said in ("engine keys renamed: demo-vault: status from `substrate_status`",
                 "1 note(s) held by the guard", "QUARANTINED: 2 note(s)",
                 "identical: 1 group(s)", "defaulted:"):
        assert said in text, (said, text)


def test_amend_pins_the_derived_id_in_a_renamed_vault(tmp_path):
    """Fails before (`status: done` refuses the amend). With keys renamed, `amend` still writes the
    doc_id a note derives into its frontmatter (#71's pin), and reads the spine under the new
    names."""
    v = _amend_vault(tmp_path)
    (v / "04-synthesis" / "d.md").write_bytes(NEW)
    book = notes.PlanBook()
    plan = notes.plan_amend(project_vault=v, path="04-synthesis/d.md", content=NEW + b"\nMore.\n",
                            book=book)
    assert plan.pinned_doc_id is not None
    notes.commit_amend(project_vault=v, path="04-synthesis/d.md", content=NEW + b"\nMore.\n",
                       confirm_token=plan.confirm_token, book=book)
    written = (v / "04-synthesis" / "d.md").read_text("utf-8")
    assert written.startswith(f"---\ndoc_id: {plan.pinned_doc_id}\n")


# ---------------------------------------------------------------- the adversary round on the move

def test_a_quoted_value_with_a_hash_is_not_read_as_a_comment(world):
    """Fails before: `[engine_keys]` is unread there, so both notes are refused for a status
    outside the vocabulary. Here the guard reads a value as YAML reads the LINE: `"archived #2"`
    quoted is all text and is the vault's own, while `archived # 2` unquoted is `archived`."""
    _note(world, "02-areas/quoted-hash.md", 'status: "archived #2"')
    _note(world, "02-areas/commented.md", "status: archived # 2")
    _table(world, '[engine_keys]\nstatus = "substrate_status"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert _held(world) == {"demo-vault/02-areas/commented.md": (GUARD, "status")}
    assert "demo-vault/02-areas/quoted-hash.md" in world.indexed()


def test_a_passage_s_own_class_is_not_judged_where_its_meta_supplies_one(world):
    """Guard: it passes before too, where the rename is unread. A passage's class is its source's
    `_meta.md` class whatever the passage says, before a rename and after, so its own `class:
    conversation` neither holds it nor changes what it is."""
    src = world.vault / "03-references" / "src"
    (src / "passages").mkdir(parents=True)
    (src / "_meta.md").write_text("---\nclass: reference-frozen\n---\n", "utf-8")
    (src / "passages" / "p1.md").write_text(
        "---\nclass: conversation\n---\n\n# Passage\n\nzqpassagex.\n", "utf-8")
    _table(world, '[engine_keys]\nclass = "substrate_class"\n')
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["quarantined"] == []
    with IndexStore(str(world.db), migrate=False) as store:
        cls = store.db.execute("SELECT document_class FROM documents WHERE source_path LIKE "
                               "'%p1.md'").fetchone()[0]
    assert cls == "reference-frozen"


def test_commit_amend_says_the_note_moved_before_it_reads_the_manifest(tmp_path):
    """Fails before (the write path does not read `[engine_keys]`, so the broken table is no
    refusal there). The token and the compare-and-swap come before every gate: a note edited
    since the plan is reported as that, even when the manifest has since gone bad too."""
    v = _amend_vault(tmp_path)
    p = v / "04-synthesis" / "d.md"
    p.write_bytes(NEW)
    book = notes.PlanBook()
    plan = notes.plan_amend(project_vault=v, path="04-synthesis/d.md", content=NEW + b"\nMore.\n",
                            book=book)
    p.write_bytes(NEW + b"\nSaved elsewhere.\n")
    (v / ".substrate.toml").write_text('name = "demo"\n\n[engine-keys]\nstatus = "x"\n', "utf-8")
    with pytest.raises(notes.NoteError, match="changed after the plan"):
        notes.commit_amend(project_vault=v, path="04-synthesis/d.md", content=NEW + b"\nMore.\n",
                           confirm_token=plan.confirm_token, book=book)
