"""The manifest's `skip` list: what one vault's compose never indexes.

Compose used to skip one fixed set of names in every vault: `_meta.md`, `structure.md`,
`MEMORY.md`, `log.md`, `WRITING.md` and anything under `99-templates/`. Those are one layout's
housekeeping files, and in another vault `log.md` is an ordinary note, which was then either left
out of the index without a word or, where it carried frontmatter, refused the whole scope.

A manifest may now declare `skip = [...]`, and the fixed set is only the default. Everything that
knew the fixed set has to follow the list in effect, or two parts of the engine disagree about
which files belong to a scope: discovery, the refusal of a skipped file that declares a spine, the
write path, drift, and the prune a compose runs before it proves the index.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from substrate import freshness, notes, prune, refresh  # noqa: E402
from substrate import vault as V  # noqa: E402
from substrate.store.index_store import IndexStore  # noqa: E402

DEFAULT_NAMES = ("_meta.md", "structure.md", "MEMORY.md", "log.md", "WRITING.md", "99-templates")
SPINE = "---\nstatus: active\ndoc_type: reference\nconfidence: proposed\n---\n"


def _vault(root: Path, skip: str | None = None, *, name: str = "demo-vault",
           inherits: str = "[]", manifest: bool = True) -> Path:
    """A vault holding one ordinary note. `skip` is the TOML written after `skip = `; None declares
    nothing."""
    v = root / name
    v.mkdir(parents=True)
    if manifest:
        declared = "" if skip is None else f"skip = {skip}\n"
        (v / ".substrate.toml").write_text(
            f'name = "{name}"\ninherits = {inherits}\n{declared}', encoding="utf-8")
    _note(v, "02-areas/area.md")
    return v


def _note(v: Path, rel: str, *, spine: bool = True) -> Path:
    p = v / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # The path is in the body, so no two notes are byte-identical.
    p.write_text(f"{SPINE if spine else ''}\n# {rel}\n\nThe body of {v.name}/{rel}.\n",
                 encoding="utf-8")
    return p


def _composed(v: Path) -> set[str]:
    """Every note the scope composes, as `<vault>/<path inside it>`."""
    scope = V.resolve_scope(v)
    roots = {ref.name: ref.path for ref in scope.vaults}
    return {f"{n.vault}/{n.path.relative_to(roots[n.vault]).as_posix()}" for n in scope.notes}


def _refusal(fn) -> str:
    try:
        fn()
    except V.VaultError as e:
        return str(e)
    raise AssertionError("expected a VaultError")


@pytest.fixture
def root() -> Path:
    return Path(tempfile.mkdtemp()).resolve()


# ---------------------------------------------------------------- what a list skips

def test_an_empty_skip_list_indexes_everything(root: Path) -> None:
    """`skip = []` is a declaration. Every default name is an ordinary note there, `log.md` with
    its frontmatter included: under the fixed list that file refused the whole scope."""
    v = _vault(root, "[]")
    for rel in ("log.md", "00-index/MEMORY.md", "structure.md", "WRITING.md",
                "99-templates/template.md", "03-references/src/_meta.md"):
        _note(v, rel)
    assert _composed(v) == {
        "demo-vault/02-areas/area.md", "demo-vault/log.md", "demo-vault/00-index/MEMORY.md",
        "demo-vault/structure.md", "demo-vault/WRITING.md",
        "demo-vault/99-templates/template.md", "demo-vault/03-references/src/_meta.md"}
    assert V.skip_list(v) == V.SkipList(files=frozenset(), folders=frozenset())


def test_a_declared_list_skips_exactly_that_list(root: Path) -> None:
    """The declared names are skipped, a file in any folder and a folder at any depth, and the
    default names are not: a declared list replaces the default, it does not add to it."""
    v = _vault(root, '["scratch.md", "drafts"]')
    for rel in ("scratch.md", "02-areas/deep/scratch.md", "drafts/idea.md",
                "02-areas/drafts/deeper/idea.md"):
        _note(v, rel, spine=False)
    for rel in ("log.md", "99-templates/template.md", "02-areas/kept.md"):
        _note(v, rel)
    assert _composed(v) == {
        "demo-vault/02-areas/area.md", "demo-vault/log.md",
        "demo-vault/99-templates/template.md", "demo-vault/02-areas/kept.md"}
    assert V.skip_list(v) == V.SkipList(files=frozenset({"scratch.md"}),
                                        folders=frozenset({"drafts"}))


def test_no_declaration_skips_the_default_names(root: Path) -> None:
    """A pin, not a regression test: a vault that declares nothing composes as it always has."""
    v = _vault(root)
    for rel in ("log.md", "00-index/MEMORY.md", "02-areas/structure.md", "WRITING.md",
                "99-templates/template.md", "02-areas/99-templates/deep/template.md",
                "03-references/src/_meta.md"):
        _note(v, rel, spine=False)
    assert _composed(v) == {"demo-vault/02-areas/area.md"}
    assert V.skip_list(v) == V.DEFAULT_SKIP == V.SkipList.of(DEFAULT_NAMES)
    assert V.resolve_scope(v).project.skip == V.DEFAULT_SKIP
    # Carried from the manifest reading, never assumed: a VaultRef is not built without one.
    with pytest.raises(TypeError):
        V.VaultRef(name="demo-vault", path=v)  # type: ignore[call-arg]


def test_a_file_name_entry_does_not_skip_a_folder_of_that_name(root: Path) -> None:
    """A pin: an entry ending in `.md` is matched against file names only, as the fixed set of
    file names was, so the notes inside a folder that happens to be called `log.md` are composed.
    One list matched against every path part would drop them."""
    v = _vault(root)
    _note(v, "02-areas/log.md/inside.md")
    assert _composed(v) == {"demo-vault/02-areas/area.md", "demo-vault/02-areas/log.md/inside.md"}
    assert not V.DEFAULT_SKIP.skips(("02-areas", "log.md", "inside.md"))
    assert V.DEFAULT_SKIP.skips(("02-areas", "log.md"))
    # No path at all is no note: asked by the prune of a folder that names its vault's own root.
    assert not V.DEFAULT_SKIP.skips(())


def test_an_entry_matches_a_name_stored_in_another_unicode_form(root: Path) -> None:
    """An accented name typed into the manifest is NFC, and a file system may store it decomposed.
    The two look the same everywhere, so compared as bytes the entry skipped nothing."""
    import unicodedata
    folder, name = "brouillons-\u00e9t\u00e9", "r\u00e9sum\u00e9.md"
    v = _vault(root, f'["{folder}", "{name}"]')
    _note(v, f"{unicodedata.normalize('NFD', folder)}/idea.md", spine=False)
    _note(v, f"02-areas/{unicodedata.normalize('NFD', name)}", spine=False)
    on_disk = {unicodedata.normalize("NFC", p.name) for p in (v / "02-areas").iterdir()}
    assert name in on_disk, on_disk
    assert _composed(v) == {"demo-vault/02-areas/area.md"}


def test_a_declared_folder_is_matched_inside_the_vault_only(root: Path) -> None:
    """The vault-relative path is what is matched. A vault that lives under a folder with a
    declared name would otherwise skip every note it has."""
    v = _vault(root / "drafts", '["drafts"]')
    _note(v, "drafts/idea.md", spine=False)
    assert _composed(v) == {"demo-vault/02-areas/area.md"}


# ---------------------------------------------------------------- which vault's list governs

def test_each_vault_s_own_list_governs_its_own_files(root: Path) -> None:
    """A project vault that indexes everything does not un-skip the housekeeping files of a vault
    it inherits, and an inherited vault's own declaration does not reach the project's files."""
    core = _vault(root, name="core-vault", manifest=False)
    _note(core, "log.md", spine=False)
    _note(core, "99-templates/template.md", spine=False)
    shared = _vault(root, '["parked"]', name="shared-vault")
    _note(shared, "parked/later.md", spine=False)
    _note(shared, "log.md")
    v = _vault(root, "[]", inherits='["core-vault", "shared-vault"]')
    _note(v, "log.md")
    _note(v, "parked/later.md")

    assert _composed(v) == {
        "core-vault/02-areas/area.md",
        "shared-vault/02-areas/area.md", "shared-vault/log.md",
        "demo-vault/02-areas/area.md", "demo-vault/log.md", "demo-vault/parked/later.md"}
    by_name = {ref.name: ref.skip for ref in V.resolve_scope(v).vaults}
    assert by_name["core-vault"] == V.DEFAULT_SKIP
    assert by_name["shared-vault"] == V.SkipList.of(["parked"])
    assert by_name["demo-vault"] == V.SkipList.of([])


# ---------------------------------------------------------------- a skipped file with a spine

def test_the_spine_refusal_follows_a_declared_list(root: Path) -> None:
    """A file the declared list names, carrying a spine, refuses the scope by its path. A default
    name the declared list leaves out is an ordinary note, spine and all."""
    v = _vault(root, '["scratch.md", "drafts", "_meta.md"]')
    _note(v, "log.md")
    assert "demo-vault/log.md" in _composed(v)

    scratch = _note(v, "02-areas/scratch.md")
    said = _refusal(lambda: V.resolve_scope(v))
    assert str(scratch) in said and "status" in said and "skip" in said, said

    # Without a spine the same file is skipped quietly, as a default name is.
    _note(v, "02-areas/scratch.md", spine=False)
    assert "demo-vault/02-areas/scratch.md" not in _composed(v)


def test_what_the_spine_refusal_never_covered_it_still_does_not(root: Path) -> None:
    """A note under a skipped folder may carry a spine, as a template does, and `_meta.md` may:
    its spine is read for the passages beside it. Both hold for a declared list as for the default."""
    v = _vault(root, '["drafts", "_meta.md"]')
    _note(v, "drafts/idea.md")
    _note(v, "03-references/src/_meta.md")
    assert _composed(v) == {"demo-vault/02-areas/area.md"}


# ---------------------------------------------------------------- a `skip` that cannot be applied

MISPLACED = (
    ("reference_pins", 'name = "demo"\ninherits = []\n\n[reference_pins]\ngo = "1.21"\n'
                       'skip = ["drafts"]\n'),
    ("extra", 'name = "demo"\ninherits = []\n\n[extra]\nskip = []\n'),
    ("extra.deeper", 'name = "demo"\ninherits = []\n\n[extra.deeper]\nskip = ["drafts"]\n'),
    # A pin really named `skip` reads the same as a swallowed key, so the name is reserved.
    ("reference_pins", 'name = "demo"\ninherits = []\n\n[reference_pins]\nskip = "1.0"\n'),
)

# The value, then what the refusal names: the type, or the positions of the faulty entries.
MALFORMED = (
    ('"kept-out"', "it is a str"),
    ("true", "it is a bool"),
    ('["kept-out", 3]', "entries [2]"),
    ('[["kept-out"]]', "entries [1]"),
    ('["kept-out/drafts"]', "entries [1]"),
    ('["kept-out/"]', "entries [1]"),
    ('["kept-out\\\\old"]', "entries [1]"),
    ('["kept-out", ""]', "entries [2]"),
    ('["."]', "entries [1]"),
    ('["kept-out", ".."]', "entries [2]"),
    ('[" kept-out"]', "entries [1]"),
    ('["kept-out.md "]', "entries [1]"),
    ('["kept-out*"]', "entries [1]"),
    ('["*.md", "kept-out", "dra?ts"]', "entries [1, 3]"),
    ('["kept-out\\u0000"]', "entries [1]"),
)


def _manifest_only(root: Path, body: str) -> Path:
    v = root / "demo-vault"
    v.mkdir(parents=True, exist_ok=True)
    (v / ".substrate.toml").write_text(body, encoding="utf-8")
    _note(v, "02-areas/area.md")
    _note(v, "drafts/idea.md", spine=False)
    return v


@pytest.mark.parametrize("table,body", MISPLACED)
def test_a_skip_written_inside_a_table_is_refused_by_name(root: Path, table: str, body: str) -> None:
    """TOML puts a key written below a `[table]` header inside that table, where nothing reads it.
    Read as "no `skip` declared", the vault would index what its owner wrote down as skipped."""
    v = _manifest_only(root, body)
    for read in (lambda: V._read_manifest(v), lambda: V.resolve_scope(v), lambda: V.skip_list(v)):
        said = _refusal(read)
        assert "'skip'" in said and f"[{table}]" in said, said


@pytest.mark.parametrize("value,named", MALFORMED)
def test_a_skip_that_cannot_be_applied_is_refused(root: Path, value: str, named: str) -> None:
    """Not a list of strings, or an entry that is not one exact file or folder name: empty, a path,
    `.` or `..`, padded with space, or a pattern. Each would skip nothing, and the vault would be
    indexed as if the entry were not there.

    The refusal names the entries by position and quotes none of them, because it reaches every
    client of `status`."""
    v = _manifest_only(root, f'name = "demo"\ninherits = []\nskip = {value}\n')
    for read in (lambda: V._read_manifest(v), lambda: V.resolve_scope(v), lambda: V.skip_list(v)):
        said = _refusal(read)
        assert "'skip'" in said and named in said and str(v / ".substrate.toml") in said, said
        assert "kept-out" not in said, said


def test_an_inherited_vault_s_malformed_skip_refuses_the_scope(root: Path) -> None:
    shared = _vault(root, '"parked"', name="shared-vault")
    v = _vault(root, inherits='["shared-vault"]')
    said = _refusal(lambda: V.resolve_scope(v))
    assert str(shared / ".substrate.toml") in said and "'skip'" in said, said


def test_the_skip_list_is_read_from_a_manifest_refused_for_another_key(root: Path) -> None:
    """The write path asks what a vault skips without composing it. A manifest that fails for a
    key that has nothing to do with skipping still answers; one that cannot be parsed does not."""
    v = _manifest_only(root, 'inherits = []\nreference_domains = ["Not A Tag"]\nskip = ["drafts"]\n')
    _refusal(lambda: V._read_manifest(v))
    assert V.skip_list(v) == V.SkipList.of(["drafts"])

    (v / ".substrate.toml").write_text('name = "demo"\nskip = [\n', encoding="utf-8")
    assert str(v / ".substrate.toml") in _refusal(lambda: V.skip_list(v))


# ---------------------------------------------------------------- the write path

GOOD = (SPINE.replace("---\n", "---\ndomains: [retrieval]\n", 1)
        + "\n# A note\n\nWritten through the gate, so it has to land where compose reads.\n").encode()


def _writable(root: Path, skip: str | None) -> Path:
    v = _vault(root, skip)
    for folder in notes.WRITABLE_FOLDERS:
        (v / folder).mkdir(exist_ok=True)
    return v


def _note_refusal(fn) -> str:
    try:
        fn()
    except notes.NoteError as e:
        return str(e)
    raise AssertionError("expected a NoteError")


def _plan(v: Path, filename: str, folder: str = "02-areas") -> notes.Plan:
    return notes.plan(project_vault=v, content=GOOD, filename=filename, folder=folder,
                      book=notes.PlanBook())


def _amend(v: Path, rel: str, book: notes.PlanBook | None = None) -> notes.AmendPlan:
    return notes.plan_amend(project_vault=v, path=rel, content=GOOD + b"\nAmended.\n",
                            book=book or notes.PlanBook())


def _on_disk(v: Path, *rels: str) -> None:
    for rel in rels:
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_bytes(GOOD)


def test_ingest_refuses_what_the_vault_s_own_list_skips(root: Path) -> None:
    """A name or a folder the vault's declared list skips is refused before anything is on disk,
    because the note would be written and never indexed."""
    v = _writable(root, '["scratch.md", "04-synthesis"]')
    said = _note_refusal(lambda: _plan(v, "scratch.md"))
    assert "'scratch.md'" in said and "skip list" in said, said
    said = _note_refusal(lambda: _plan(v, "note.md", folder="04-synthesis"))
    assert "'04-synthesis'" in said and "skip list" in said, said
    assert _plan(v, "note.md", folder="03-references").target.name == "note.md"


def test_amend_refuses_what_the_vault_s_own_list_skips(root: Path) -> None:
    v = _writable(root, '["scratch.md", "drafts"]')
    _on_disk(v, "02-areas/scratch.md", "02-areas/drafts/idea.md", "02-areas/kept.md")
    for rel in ("02-areas/scratch.md", "02-areas/drafts/idea.md"):
        assert "skip list" in _note_refusal(lambda rel=rel: _amend(v, rel))
    assert _amend(v, "02-areas/kept.md").target == v / "02-areas" / "kept.md"


@pytest.mark.parametrize("skip", ["[]", '["drafts"]'])
def test_a_source_s_meta_is_never_written_whatever_the_list_says(root: Path, skip: str) -> None:
    """`_meta.md` supplies the class and spine of every passage beside it, so writing one re-spines
    notes the plan's diff never shows. Refused only while the list named it, it was writable in
    any vault whose declared list left it out, and that holds for `ingest` and `amend` alike."""
    v = _writable(root, skip)
    meta = v / "03-references" / "src" / "_meta.md"
    _on_disk(v, "03-references/src/_meta.md")
    book = notes.PlanBook()

    said = _note_refusal(lambda: _amend(v, "03-references/src/_meta.md", book))
    assert "'03-references/src/_meta.md'" in said and "whatever the vault's `skip`" in said, said
    for folder in notes.WRITABLE_FOLDERS:
        said = _note_refusal(lambda folder=folder: _plan(v, "_meta.md", folder=folder))
        assert "'_meta.md'" in said and "whatever the vault's `skip`" in said, said
    assert meta.read_bytes() == GOOD and not book._issued
    assert not any((v / f / "_meta.md").exists() for f in notes.WRITABLE_FOLDERS)


@pytest.mark.parametrize("skip", [None, "[]"])
def test_a_source_s_meta_is_refused_in_any_case(root: Path, skip: str | None) -> None:
    """On a volume that ignores case, `_source_meta`'s open of `_meta.md` reads `_META.md`, so a
    name compared exactly let a case variant be written and re-spine the passages beside it. The
    refusal folds case on every platform.

    `_open_note_dir` refuses an amend spelled otherwise than the file on disk, so the amend case
    that matters is a file whose own spelling is `_META.md`."""
    v = _writable(root, skip)
    meta = v / "03-references" / "src" / "_META.md"
    _on_disk(v, "03-references/src/_META.md")
    book = notes.PlanBook()

    said = _note_refusal(lambda: _amend(v, "03-references/src/_META.md", book))
    assert "'03-references/src/_META.md'" in said and "in any letter case" in said, said
    for name in ("_META.md", "_Meta.md"):
        said = _note_refusal(lambda name=name: _plan(v, name, folder="03-references"))
        assert repr(name) in said and "in any letter case" in said, said
    assert meta.read_bytes() == GOOD and not book._issued
    assert sorted(p.name for p in (v / "03-references").iterdir()) == ["src"]


def test_every_spelling_a_volume_serves_as_meta_md_is_a_source_meta() -> None:
    """Case folded, NFC, and without the code points HFS+ leaves out of a name. Nothing else."""
    for name in ("_meta.md", "_META.md", "_Meta.md", "_meta.MD", "_me\u200cta.md",
                 "\ufeff_meta.md", "_meta.m\u206ad"):
        assert V.is_source_meta(name), name
    for name in ("meta.md", "_meta.md.md", "_meta.md ", "_\uff4deta.md", "_meta\u2024md"):
        assert not V.is_source_meta(name), name


def test_a_default_name_the_list_leaves_out_is_an_ordinary_note_to_write(root: Path) -> None:
    """Under `skip = []`, `log.md` is an indexed note, and `amend` edits it as it would any other.
    `ingest` plans one, and the other default names, instead of refusing them."""
    v = _writable(root, "[]")
    log = v / "02-areas" / "log.md"
    _on_disk(v, "02-areas/log.md")
    assert "demo-vault/02-areas/log.md" in _composed(v)

    book = notes.PlanBook()
    plan = _amend(v, "02-areas/log.md", book)
    notes.commit_amend(project_vault=v, path="02-areas/log.md", content=GOOD + b"\nAmended.\n",
                       confirm_token=plan.confirm_token, book=book)
    # Amend pins the doc_id the note had been composed under; the edit is the rest.
    written = log.read_bytes()
    assert written.endswith(GOOD.split(b"---\n", 2)[2] + b"\nAmended.\n"), written
    assert written.startswith(b"---\ndoc_id: "), written
    assert "demo-vault/02-areas/log.md" in _composed(v)

    for name in ("structure.md", "MEMORY.md", "log.md", "WRITING.md"):
        assert _plan(v, name, folder="04-synthesis").target == v / "04-synthesis" / name
    _on_disk(v, "02-areas/99-templates/template.md")
    assert _amend(v, "02-areas/99-templates/template.md").diff


def test_a_write_is_refused_while_the_skip_list_cannot_be_read(root: Path) -> None:
    """Which names are indexed is unknown, so nothing is planned against a guess at them. The
    refusal names the manifest and not what is in it: the caller may be a granted client, and the
    fault is the owner's to read."""
    v = _writable(root, '["kept-out-of-the-index", 3]')
    _on_disk(v, "02-areas/note.md")
    for said in (_note_refusal(lambda: _plan(v, "other.md")),
                 _note_refusal(lambda: _amend(v, "02-areas/note.md"))):
        assert str(v / ".substrate.toml") in said and "`skip`" in said, said
        assert "kept-out-of-the-index" not in said, said


# ---------------------------------------------------------------- the prune of a skipped note

def test_a_missing_vault_prunes_nothing_it_skips(root: Path) -> None:
    """A skipped note's folder is held to the guard a deleted note's is: a vault whose root is
    missing or unreadable, or that composed nothing and carries no manifest, has none of its
    folders removed. The skip list is a rule about paths and needs no disk, so without this an
    unmounted vault's skipped notes would leave the index."""
    core = root / "core-vault"
    core.mkdir()
    vault = V.VaultRef(name="core-vault", path=core, skip=V.DEFAULT_SKIP)
    found = [prune.Stale("core-vault", Path("log.md"), root / "folder", skipped=True)]

    with pytest.raises(prune.PruneError, match="composed zero notes"):
        prune.refuse_missing(found, [vault], {})
    if os.geteuid() != 0:
        core.chmod(0)
        try:
            with pytest.raises(prune.PruneError, match="cannot be read"):
                prune.refuse_missing(found, [vault], {"core-vault": 3})
        finally:
            core.chmod(0o755)
    core.rmdir()
    with pytest.raises(prune.PruneError, match="is missing"):
        prune.refuse_missing(found, [vault], {"core-vault": 3})


# ---------------------------------------------------------------- a refresh after `skip` changes

DEMO = ROOT / "vaults"
INHERITS = 'inherits = ["demo-core-vault"]'


def _declare(vault: Path, *extra: str) -> None:
    """Declare the default names plus `extra` in the demo manifest, above its `[reference_pins]`."""
    path = vault / ".substrate.toml"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if not ln.startswith("skip = ")]
    at = next(i for i, ln in enumerate(lines) if ln.startswith(INHERITS))
    names = ", ".join(f'"{n}"' for n in (*DEFAULT_NAMES, *extra))
    lines.insert(at + 1, f"skip = [{names}]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _indexed(demo: dict) -> set[str]:
    with sqlite3.connect(demo["db"]) as db:
        return {r[0] for r in db.execute("SELECT doc_id FROM documents")}


def _drift(demo: dict) -> dict:
    with IndexStore(demo["db"]) as store:
        return freshness.drift(store, [n.path for n in V.resolve_scope(demo["vault"]).notes])


@pytest.fixture
def composed(tmp_path: Path) -> dict:
    """The demo vaults copied out, with a note in `drafts/` and one in `parked/`, the manifest
    skipping `parked`, registered and composed once by a refresh pass."""
    vaults = tmp_path / "vaults"
    shutil.copytree(DEMO / "demo-vault", vaults / "demo-vault")
    shutil.copytree(DEMO / "demo-core-vault", vaults / "demo-core-vault")
    demo = {"vault": vaults / "demo-vault", "registry": tmp_path / "scopes.toml",
            "db": tmp_path / "index" / "demo.db", "index_root": tmp_path / "index" / "demo"}
    for folder, doc_id in (("drafts", "skip-test-idea"), ("parked", "skip-test-later")):
        note = demo["vault"] / folder / f"{doc_id}.md"
        note.parent.mkdir()
        note.write_text(SPINE.replace("---\n", f"---\ndoc_id: {doc_id}\n", 1)
                        + f"\n# {doc_id}\n\nA note the manifest's skip list moves in and out.\n",
                        encoding="utf-8")
    _declare(demo["vault"], "parked")
    reg = subprocess.run([sys.executable, "-m", "substrate.cli", "register", str(demo["vault"]),
                          "--index-root", str(demo["index_root"]), "--db", str(demo["db"]),
                          "--registry", str(demo["registry"])],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert reg.returncode == 0, reg.stderr
    [first] = refresh.run_pass(demo["registry"], log=lambda _: None)
    assert first.outcome in ("refreshed", "embed_failed"), first
    return demo


def test_a_refresh_after_skip_changes_picks_the_change_up(composed: dict) -> None:
    """A note newly skipped leaves the index and a note newly unskipped joins it, in one pass and
    without `--clean`. The skipped note is still on disk, so its ingest folder is not a deleted
    note's: before the prune knew the skip list, the pass refused the scope for it."""
    demo = composed
    before = _indexed(demo)
    assert "skip-test-idea" in before and "skip-test-later" not in before, sorted(before)
    idea = demo["vault"] / "drafts" / "skip-test-idea.md"
    later = demo["vault"] / "parked" / "skip-test-later.md"
    [folder] = [p for p in demo["index_root"].iterdir() if "__skip-test-idea__" in p.name]
    kept = idea.read_bytes()

    _declare(demo["vault"], "drafts")

    # Drift reads the same list compose will, so `status` already says what the pass will do.
    drift = _drift(demo)
    assert drift["removed"] == [str(idea.resolve())], drift
    assert drift["added"] == [str(later.resolve())], drift

    said: list[str] = []
    [result] = refresh.run_pass(demo["registry"], log=said.append)

    assert result.outcome in ("refreshed", "embed_failed"), (result, said)
    assert _indexed(demo) == (before - {"skip-test-idea"}) | {"skip-test-later"}
    assert not folder.exists()
    assert (f"pruned: demo-vault/drafts/skip-test-idea.md  (note skipped by its vault's skip "
            f"list; removed {folder.name})") in result.detail, result.detail
    # Derived data only: the note the vault now skips is untouched.
    assert idea.read_bytes() == kept
    drift = _drift(demo)
    assert not drift["stale"] and not drift["added"] and not drift["removed"], drift


def test_a_file_name_entry_for_a_note_with_a_spine_refuses_the_pass(composed: dict) -> None:
    """The two rules meet here, and the refusal wins: a file the list names that declares a spine
    is refused, not dropped, and every indexed note declares one. So naming an indexed note's file
    does not take it out of the index. The pass fails, saying why, and the index is as it was."""
    demo = composed
    before = _indexed(demo)
    idea = demo["vault"] / "drafts" / "skip-test-idea.md"

    _declare(demo["vault"], "parked", "skip-test-idea.md")
    [result] = refresh.run_pass(demo["registry"], log=lambda _: None)

    assert result.outcome == "compose_failed", result
    assert str(idea.resolve()) in result.detail and "declares spine fields" in result.detail, \
        result.detail
    assert "pruned:" not in result.detail and _indexed(demo) == before
