"""The manifest's `writable_folders`: where `ingest` adds a note and `amend` changes one.

The write path took one layout's folders, `02-areas`, `03-references` and `04-synthesis`, in every
vault, so a vault laid out any other way could take no write. A manifest may now declare
`writable_folders = [...]`, paths relative to the project vault. With no declaration, any folder
compose does not skip is writable, the root included.

What the folder is checked against is the CALLER'S string, and the caller is a granted client. So
beside the list, each folder is walked on disk without following a link, every component spelled
as it is there. A client must never steer a write outside the project vault, into an inherited
vault, into a hidden or skipped folder, through a symlink, over an existing file, or into a folder
a declaration leaves out.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from substrate import notes  # noqa: E402
from substrate import vault as V  # noqa: E402

SPINE = "---\nstatus: active\ndoc_type: reference\nconfidence: proposed\ndomains: [retrieval]\n---\n"
GOOD = (SPINE + "\n# A note\n\nWritten through the gate, where compose reads it.\n").encode()


@pytest.fixture
def root() -> Path:
    return Path(tempfile.mkdtemp()).resolve()


def _vault(root: Path, declared: str | None = None, *, extra: str = "",
           name: str = "demo-vault") -> Path:
    """A project vault with the Doc 2 folders and one note in each. `declared` is the TOML
    written after `writable_folders = `; None declares nothing."""
    v = root / name
    v.mkdir(parents=True)
    line = "" if declared is None else f"writable_folders = {declared}\n"
    (v / ".substrate.toml").write_text(f'name = "{name}"\n{line}{extra}', encoding="utf-8")
    for folder in ("02-areas", "03-references", "04-synthesis"):
        _note(v, f"{folder}/existing.md")
    return v


def _note(v: Path, rel: str) -> Path:
    p = v / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(GOOD + f"\nThe body of {rel}.\n".encode())
    return p


def _write(v: Path, folder: str, filename: str = "new.md") -> Path:
    """Plan and commit one note, as a client does over two calls."""
    book = notes.PlanBook()
    # Its own bytes, so no two written notes are byte-identical and refuse the compose.
    tag = hashlib.sha256(f"{folder}\0{filename}".encode()).hexdigest()[:12]
    content = GOOD + f"\nWritten as {tag}.\n".encode()
    p = notes.plan(project_vault=v, content=content, filename=filename, folder=folder, book=book)
    return notes.commit(project_vault=v, content=content, filename=filename, folder=folder,
                        confirm_token=p.confirm_token, book=book)


def _amend(v: Path, rel: str) -> Path:
    book = notes.PlanBook()
    new = (v / rel).read_bytes() + b"\nAmended.\n"
    p = notes.plan_amend(project_vault=v, path=rel, content=new, book=book)
    target, _, _ = notes.commit_amend(project_vault=v, path=rel, content=new,
                                      confirm_token=p.confirm_token, book=book)
    return target


def _refusal(fn) -> str:
    try:
        fn()
    except notes.NoteError as e:
        return str(e)
    raise AssertionError("expected a NoteError")


def _files(v: Path) -> set[str]:
    return {p.relative_to(v).as_posix() for p in v.rglob("*") if p.is_file() or p.is_symlink()}


# ---------------------------------------------------------------- the issue's done-when

def test_a_declared_folder_takes_a_write(root: Path) -> None:
    v = _vault(root, '["Notes", "Projects/ideas"]')
    (v / "Notes").mkdir()
    (v / "Projects" / "ideas").mkdir(parents=True)
    assert _write(v, "Notes") == v / "Notes" / "new.md"
    assert _write(v, "Projects/ideas") == v / "Projects" / "ideas" / "new.md"
    # A folder below a declared one is inside it, for ingest as for amend, and is created.
    assert _write(v, "Notes/2026") == v / "Notes" / "2026" / "new.md"
    assert _amend(v, "Notes/2026/new.md") == v / "Notes" / "2026" / "new.md"


def test_a_vault_that_declares_nothing_takes_a_write_in_any_folder(root: Path) -> None:
    v = _vault(root)
    (v / "Inbox").mkdir()
    assert _write(v, "Inbox") == v / "Inbox" / "new.md"
    # A folder that is not there yet is created, as `ingest` created its one folder before.
    assert _write(v, "Projects/ideas", "idea.md") == v / "Projects" / "ideas" / "idea.md"
    # The vault root is `.`.
    assert _write(v, ".", "top.md") == v / "top.md"
    assert _write(v, "02-areas/frontend", "frontend-digest.md").is_file()
    # amend reaches the same folders, the root included.
    assert _amend(v, "Inbox/new.md") == v / "Inbox" / "new.md"
    assert _amend(v, "top.md") == v / "top.md"
    # Written where compose reads: every one of them is in the scope.
    composed = {n.path.relative_to(v).as_posix() for n in V.resolve_scope(v).notes}
    assert {"Inbox/new.md", "Projects/ideas/idea.md", "top.md",
            "02-areas/frontend/frontend-digest.md"} <= composed


@pytest.mark.parametrize("folder", ["..", "../demo-core-vault", "Notes/../..", "Notes/../Inbox",
                                    "./Notes", "Notes/.", "/tmp", "~/elsewhere/.."])
def test_a_dot_dot_or_absolute_folder_is_refused(root: Path, folder: str) -> None:
    v = _vault(root)
    (v / "Notes").mkdir()
    (v / "Inbox").mkdir()
    before = _files(v)
    said = _refusal(lambda: _write(v, folder))
    assert f"`folder` {folder!r}" in said, said
    assert _files(v) == before and not list(root.glob("*/new.md"))


def test_a_symlinked_folder_is_refused_wherever_it_points(root: Path) -> None:
    """Inside the vault too: a link from a writable folder to a skipped or hidden one passes every
    check made on the caller's string. On `0add66b` the link at `02-areas` was resolved, found
    inside the vault, and the note written into `99-templates/`, where compose never reads."""
    v = _vault(root)
    outside = root / "outside"
    outside.mkdir()
    (v / "99-templates").mkdir()
    (v / ".obsidian").mkdir()
    for f in (v / "02-areas").iterdir():
        f.unlink()
    (v / "02-areas").rmdir()
    (v / "02-areas").symlink_to(v / "99-templates")
    (v / "04-synthesis" / "hidden").symlink_to(v / ".obsidian")
    (v / "Out").symlink_to(outside)
    for folder in ("02-areas", "04-synthesis/hidden", "Out", "Out/deeper"):
        said = _refusal(lambda folder=folder: _write(v, folder))
        assert "is a symlink" in said, (folder, said)
    assert not list((v / "99-templates").iterdir())
    assert not list((v / ".obsidian").iterdir())
    assert not list(outside.iterdir())


def test_a_folder_swapped_for_a_link_after_the_plan_is_refused(root: Path) -> None:
    v = _vault(root)
    (v / "Inbox").mkdir()
    outside = root / "outside"
    outside.mkdir()
    book = notes.PlanBook()
    p = notes.plan(project_vault=v, content=GOOD, filename="new.md", folder="Inbox", book=book)
    (v / "Inbox").rmdir()
    (v / "Inbox").symlink_to(outside)
    said = _refusal(lambda: notes.commit(project_vault=v, content=GOOD, filename="new.md",
                                         folder="Inbox", confirm_token=p.confirm_token,
                                         book=book))
    assert "is a symlink" in said, said
    assert not list(outside.iterdir())


# ---------------------------------------------------------------- the declaration

def test_a_declared_folder_that_does_not_exist_refuses_by_name(root: Path) -> None:
    v = _vault(root, '["04-synthesis", "Notes"]')
    for fn in (lambda: _write(v, "04-synthesis"), lambda: _amend(v, "04-synthesis/existing.md")):
        said = _refusal(fn)
        assert "'writable_folders' entries [2]" in said and "not folders in the vault" in said, said
    assert not (v / "Notes").exists(), "a declared folder is not created"
    assert not (v / "04-synthesis" / "new.md").exists()


def test_a_declared_folder_that_is_a_symlink_or_a_file_refuses(root: Path) -> None:
    v = _vault(root, '["Linked", "afile", "04-synthesis"]')
    (v / "Linked").symlink_to(v / "02-areas")
    (v / "afile").write_bytes(GOOD)
    said = _refusal(lambda: _write(v, "04-synthesis"))
    assert "'writable_folders' entries [1, 2]" in said, said


def test_a_declared_folder_the_skip_list_skips_refuses(root: Path) -> None:
    v = _vault(root, '["04-synthesis", "99-templates/areas"]')
    (v / "99-templates" / "areas").mkdir(parents=True)
    said = _refusal(lambda: _write(v, "04-synthesis"))
    assert "'writable_folders' entries [2]" in said and "skip list" in said, said
    # And under a declared `skip` rather than the default.
    w = _vault(root, '["Drafts"]', extra='skip = ["Drafts"]\n', name="other-vault")
    (w / "Drafts").mkdir()
    said = _refusal(lambda: _write(w, "02-areas"))
    assert "'writable_folders' entries [1]" in said and "skip list" in said, said


def test_an_undeclared_folder_refuses_when_a_list_is_declared(root: Path) -> None:
    v = _vault(root, '["Notes"]')
    (v / "Notes").mkdir()
    for folder in ("04-synthesis", "Inbox", ".", "Notesx", "No"):
        said = _refusal(lambda folder=folder: _write(v, folder))
        assert "not in a writable folder" in said and "['Notes']" in said, (folder, said)
    _note(v, "top.md")
    for rel in ("04-synthesis/existing.md", "top.md"):
        said = _refusal(lambda rel=rel: _amend(v, rel))
        assert "not in a writable folder" in said, (rel, said)
    assert not (v / "Inbox").exists() and not (v / "04-synthesis" / "new.md").exists()
    assert not (v / "Notesx").exists() and not (v / "No").exists()


def test_the_root_is_writable_under_a_declaration_only_when_declared(root: Path) -> None:
    v = _vault(root, '["."]')
    assert _write(v, ".", "top.md") == v / "top.md"
    assert _write(v, "Anywhere/below") == v / "Anywhere" / "below" / "new.md"


@pytest.mark.parametrize("declared", [
    '"Notes"', '{ a = "Notes" }', '[1]', '["Notes", 2]', '[""]', '["/abs"]', '["../elsewhere"]',
    '["Notes/.."]', '["./Notes"]', '[".obsidian"]', '["Notes/.hidden"]', '["Notes/"]',
    '["Projects//ideas"]', '[" Notes"]', '["Notes "]', '["Notes\\\\x"]', '["Notes\\u0000"]',
])
def test_a_malformed_declaration_fails_closed(root: Path, declared: str) -> None:
    """A typo or a wrong type writes nothing, anywhere, rather than falling back to the default."""
    v = _vault(root, declared)
    (v / "Notes").mkdir()
    for fn in (lambda: _write(v, "04-synthesis"), lambda: _write(v, "Notes"),
               lambda: _amend(v, "04-synthesis/existing.md")):
        said = _refusal(fn)
        assert "'writable_folders'" in said, said
    assert not (v / "04-synthesis" / "new.md").exists() and not (v / "Notes" / "new.md").exists()


def test_a_misplaced_declaration_fails_closed_and_refuses_the_compose(root: Path) -> None:
    """Below a `[table]` header the key belongs to the table, where the engine would never read
    it. Reserved at the top level, as `skip` is, so a pin of that name is refused too."""
    v = _vault(root, extra='[reference_pins]\nwritable_folders = ["Notes"]\n')
    (v / "Notes").mkdir()
    said = _refusal(lambda: _write(v, "04-synthesis"))
    assert "'writable_folders' is inside the [reference_pins] table" in said, said
    with pytest.raises(V.VaultError, match="'writable_folders' is inside the"):
        V.resolve_scope(v)


def test_only_the_project_vault_s_own_declaration_counts(root: Path) -> None:
    """An inherited vault is never written, whatever either manifest says: its own declaration is
    never read for a write, and no folder of the project vault's reaches it."""
    core = root / "demo-core-vault"
    (core / "00-operator").mkdir(parents=True)
    (core / ".substrate.toml").write_text('name = "core"\nwritable_folders = ["00-operator"]\n')
    v = _vault(root, extra='inherits = ["demo-core-vault"]\n')
    (v / "core").symlink_to(core)
    for folder in ("../demo-core-vault/00-operator", str(core / "00-operator"), "core",
                   "core/00-operator"):
        _refusal(lambda folder=folder: _write(v, folder))
    w = _vault(root, '["../demo-core-vault/00-operator"]', name="other-vault")
    said = _refusal(lambda: _write(w, "04-synthesis"))
    assert "'writable_folders' entries [1]" in said, said
    for rel in ("../demo-core-vault/00-operator/x.md", str(core / "00-operator" / "x.md")):
        _note(core, "00-operator/x.md")
        _refusal(lambda rel=rel: _amend(v, rel))
    assert sorted(p.name for p in (core / "00-operator").iterdir()) == ["x.md"]


# ---------------------------------------------------------------- the default's two exceptions

UNFROZEN = (SPINE + "\n# A source note\n\nNo class declared, so nothing but its folder protects it.\n")


@pytest.mark.parametrize("top", ["_sources", "_archive"])
def test_a_vault_that_declares_nothing_keeps_sources_and_archive_unwritable(
        root: Path, top: str) -> None:
    """The three-folder list was all that kept a client out of these two, so the default leaves
    them out. A note there that is not class-frozen would otherwise be amendable."""
    v = _vault(root)
    note = v / top / "old.md"
    note.parent.mkdir()
    note.write_text(UNFROZEN + f"\n{top}\n", encoding="utf-8")
    before = _files(v)
    for folder in (top, f"{top}/deeper"):
        said = _refusal(lambda folder=folder: _write(v, folder))
        assert f"'{top}/'" in said and "A declaration opens it" in said, said
    said = _refusal(lambda: _amend(v, f"{top}/old.md"))
    assert f"'{top}/'" in said and "A declaration opens it" in said, said
    assert _files(v) == before and note.read_text(encoding="utf-8").endswith(f"{top}\n")


@pytest.mark.parametrize("spelled", ["_SOURCES", "_Archive", "_sour\u200cces"])
def test_another_spelling_of_sources_or_archive_is_refused_even_before_it_exists(
        root: Path, spelled: str) -> None:
    """On a volume that ignores case, a `_SOURCES/` a client created would be the folder every
    later `_sources/` write lands in. Refused whether or not either spelling exists yet."""
    v = _vault(root)
    said = _refusal(lambda: _write(v, spelled))
    # The invisible-character spelling is refused earlier, as any such name is.
    assert "A declaration opens it" in said or "a viewer would hide" in said, said
    assert not (v / spelled).exists()


def test_sources_and_archive_below_the_top_are_ordinary_folders(root: Path) -> None:
    v = _vault(root)
    assert _write(v, "Notes/_sources") == v / "Notes" / "_sources" / "new.md"
    assert _write(v, "02-areas/_archive") == v / "02-areas" / "_archive" / "new.md"


def test_a_declaration_naming_sources_and_archive_opens_them(root: Path) -> None:
    v = _vault(root, '["_sources", "_archive", "04-synthesis"]')
    for top in ("_sources", "_archive"):
        (v / top).mkdir()
        (v / top / "old.md").write_text(UNFROZEN + f"\n{top}\n", encoding="utf-8")
    for top in ("_sources", "_archive"):
        note = v / top / "old.md"
        assert _write(v, top) == v / top / "new.md"
        assert _amend(v, f"{top}/old.md") == note
    # A frozen call is still never amended, wherever it is.
    call = v / "_sources" / "call.md"
    call.write_text(SPINE.replace("---\n", "---\nclass: conversation\ntitle: A call\n", 1)
                    + "\n# A call\n\nWhat was said.\n", encoding="utf-8")
    assert "frozen class" in _refusal(lambda: _amend(v, "_sources/call.md"))


# ---------------------------------------------------------------- what stays refused anywhere

@pytest.mark.parametrize("name", ["_meta.md", "_META.md", "_Meta.md", "_me\u200cta.md"])
@pytest.mark.parametrize("skip", [None, "[]"])
def test_a_source_meta_is_refused_in_a_newly_writable_folder(root: Path, name: str,
                                                             skip: str | None) -> None:
    """Under the default skip list `_meta.md` is a skipped name as well; under `skip = []` only
    the write path's own refusal stands between a client and the file."""
    v = _vault(root, extra="" if skip is None else f"skip = {skip}\n")
    (v / "Inbox").mkdir()
    for folder in ("Inbox", ".", "Projects/new"):
        said = _refusal(lambda folder=folder: _write(v, folder, name))
        assert "whatever the vault's `skip`" in said or "skip list" in said, said
        if skip is not None:
            assert "whatever the vault's `skip`" in said, said
    assert not (v / "Inbox" / name).exists() and not (v / name).exists()
    assert not (v / "Projects").exists()


def test_passages_hidden_and_skipped_folders_are_refused_by_default(root: Path) -> None:
    v = _vault(root)
    (v / "03-references" / "book" / "passages").mkdir(parents=True)
    (v / "03-references" / "book" / "_meta.md").write_bytes(
        b"---\nclass: reference-frozen\nstatus: active\n---\n")
    for folder, why in (("03-references/book/passages", "reference passages"),
                        ("Anything/passages/deeper", "reference passages"),
                        (".obsidian", "hidden"), ("Notes/.trash", "hidden"),
                        ("99-templates", "skip list"), ("Notes/99-templates", "skip list")):
        said = _refusal(lambda folder=folder: _write(v, folder))
        assert why in said, (folder, said)
    assert not (v / "Anything").exists() and not (v / "Notes").exists()
    assert not list((v / "03-references" / "book" / "passages").iterdir())


@pytest.mark.parametrize("folder", ["", "Notes/", "Notes//x", "Notes\\x", " Notes", "Notes\0x"])
def test_a_malformed_folder_from_the_caller_is_refused(root: Path, folder: str) -> None:
    v = _vault(root)
    (v / "Notes").mkdir()
    before = _files(v)
    said = _refusal(lambda: _write(v, folder))
    assert "`folder`" in said, said
    assert _files(v) == before


@pytest.mark.parametrize("bad", [["Notes"], 3])
def test_a_folder_that_is_not_a_string_is_refused(root: Path, bad: object) -> None:
    v = _vault(root)
    said = _refusal(lambda: notes.plan(project_vault=v, content=GOOD, filename="n.md",
                                       folder=bad, book=notes.PlanBook()))
    assert "must be strings" in said, said


@pytest.mark.parametrize("exists", [True, False])
@pytest.mark.parametrize("spelled", ["99-TEMPLATES", "Notes/PASSAGES", "PRIVATE"])
def test_another_case_of_a_skipped_or_passages_folder_is_refused(
        root: Path, spelled: str, exists: bool) -> None:
    """The skip list and `passages` compare case exactly, and APFS ignores it: `99-TEMPLATES`
    passed the lexical check and reached `99-templates/`. Where the real folder does not exist
    yet, the client's spelling would be created, and on such a volume it is the folder every later
    write to the real name lands in, which compose then reads as neither skipped nor passages."""
    v = _vault(root, extra='skip = ["99-templates", "Private"]\n')
    real = v / spelled.replace("TEMPLATES", "templates").replace("PASSAGES", "passages") \
        .replace("PRIVATE", "Private")
    if exists:
        real.mkdir(parents=True)
    said = _refusal(lambda: _write(v, spelled))
    assert "skip list" in said or "reference passages" in said, said
    assert not (v / spelled / "new.md").exists()


def test_an_existing_folder_named_in_another_case_is_refused(root: Path) -> None:
    v = _vault(root)
    (v / "Notes").mkdir()
    if not (v / "NOTES").exists():
        pytest.skip("this volume is case-sensitive")
    said = _refusal(lambda: _write(v, "NOTES"))
    assert "not spelled the way it is on disk" in said, said
    assert not list((v / "Notes").iterdir())


def test_ingest_still_never_overwrites_in_a_newly_writable_folder(root: Path) -> None:
    v = _vault(root)
    note = _note(v, "Inbox/new.md")
    before = note.read_bytes()
    assert "additive-only" in _refusal(lambda: _write(v, "Inbox"))
    assert note.read_bytes() == before


# ---------------------------------------------------------------- the MCP surface

def test_the_tool_schema_offers_any_folder_and_a_refusal_names_the_writable_ones(
        root: Path) -> None:
    from substrate.mcp import server

    tool = next(t for t in server.TOOLS if t["name"] == "ingest")
    folder = tool["inputSchema"]["properties"]["folder"]
    assert "enum" not in folder and folder["type"] == "string"
    assert "writable_folders" in tool["description"]
    v = _vault(root, '["Notes", "Projects"]')
    (v / "Projects").mkdir()
    (v / "Notes").mkdir()
    said = _refusal(lambda: _write(v, "04-synthesis"))
    assert "['Notes', 'Projects']" in said, said


# ---------------------------------------------------------------- crosscheck, 2026-11-13

def test_a_folder_ending_in_md_is_refused(root: Path) -> None:
    """Compose's walk takes `*.md` for notes, a folder included, and fails reading it, so one
    approved write would refuse the scope on every later compose, with no way back over MCP."""
    v = _vault(root)
    for folder in ("trap.md", "04-synthesis/trap.md", "Notes/X.MD/deeper"):
        said = _refusal(lambda folder=folder: _write(v, folder, "n.md"))
        assert "ending in `.md`" in said, said
    V.resolve_scope(v)
    w = _vault(root, '["trap.md"]', name="other-vault")
    (w / "trap.md").mkdir()
    assert "'writable_folders' entries [1]" in _refusal(lambda: _write(w, "04-synthesis"))


@pytest.mark.parametrize("declared", [None, '["04-synthesis"]'])
@pytest.mark.parametrize("folder", ["00-operator", "04-synthesis/00-operator-notes",
                                    "04-synthesis/10-reference"])
def test_ingest_never_writes_a_tier_1_or_2_note(root: Path, declared: str | None,
                                                folder: str) -> None:
    """A note's tier is its folder's name, so a client-created `00-operator/` would label its note
    with the operator tier, which is promoted by hand. `ingest` wrote tier 3 only, and still does."""
    v = _vault(root, declared)
    said = _refusal(lambda: _write(v, folder))
    assert "operator or reference tier" in said, said
    assert not (v / folder).exists()


@pytest.mark.parametrize("declared", [None, '["00-operator", "02-areas"]'])
@pytest.mark.parametrize("rel", ["00-operator/mine.md", "02-areas/00-operator/mine.md",
                                 "02-areas/10-reference-x/mine.md", "00-OPERATOR/mine.md",
                                 "02-areas/00-operator-notes.md"])
def test_amend_never_edits_a_tier_1_or_2_note(root: Path, declared: str | None, rel: str) -> None:
    """Owner decision: operator and reference notes are promoted by hand, so a client never edits
    one either, whatever `writable_folders` says. `main` amended `02-areas/00-operator/`."""
    v = _vault(root, declared)
    (v / "00-operator").mkdir(exist_ok=True)
    note = _note(v, rel)
    before = note.read_bytes()
    assert "operator or reference tier" in _refusal(lambda: _amend(v, rel))
    assert note.read_bytes() == before


def test_ingest_refuses_a_tier_name_in_any_case_even_when_declared(root: Path) -> None:
    v = _vault(root, '["00-operator", "02-areas"]')
    (v / "00-operator").mkdir()
    for folder, name in (("00-operator", "n.md"), ("00-OPERATOR", "n.md"),
                         ("02-areas", "00-Operator-x.md")):
        said = _refusal(lambda folder=folder, name=name: _write(v, folder, name))
        assert "operator or reference tier" in said, said
    assert not list((v / "00-operator").iterdir())


def _omitted(v: Path) -> Path:
    book = notes.PlanBook()
    p = notes.plan(project_vault=v, content=GOOD, filename="n.md", book=book)
    return notes.commit(project_vault=v, content=GOOD, filename="n.md",
                        confirm_token=p.confirm_token, book=book)


def test_an_omitted_folder_writes_to_an_existing_04_synthesis(root: Path) -> None:
    v = _vault(root)
    assert _omitted(v) == v / "04-synthesis" / "n.md"


def test_an_omitted_folder_refuses_where_04_synthesis_is_missing(root: Path) -> None:
    v = _vault(root)
    shutil.rmtree(v / "04-synthesis")
    said = _refusal(lambda: _omitted(v))
    assert "Name a `folder`" in said and "'.' is the" in said, said
    assert not (v / "04-synthesis").exists()


def test_an_omitted_folder_refuses_where_a_declaration_leaves_it_out(root: Path) -> None:
    v = _vault(root, '["02-areas"]')
    said = _refusal(lambda: _omitted(v))
    assert "Name a `folder`" in said and "['02-areas']" in said, said
    assert not (v / "04-synthesis" / "n.md").exists()


@pytest.mark.parametrize("folder,filename", [("Notes\u202eevil", "n.md"), ("Notes/a\nb", "n.md"),
                                             ("Notes", "n\u200b.md"), ("Notes", "a\u202eb.md")])
def test_a_name_holding_a_hidden_character_is_refused(root: Path, folder: str,
                                                       filename: str) -> None:
    """The plan's target is what a person approves; a bidi override or a line break in it can
    make it read as another place."""
    v = _vault(root)
    (v / "Notes").mkdir()
    before = _files(v)
    said = _refusal(lambda: _write(v, folder, filename))
    assert "a viewer would hide" in said and "U+" in said, said
    assert _files(v) == before and sorted(p.name for p in v.iterdir() if p.is_dir()) == [
        "02-areas", "03-references", "04-synthesis", "Notes"]


def test_a_new_folder_named_decomposed_is_refused(root: Path) -> None:
    """A new name is made only in NFC, so `covers` (which compares NFC) and the folder on disk
    agree on what was written, and an NFD twin of a declared folder cannot be created beside it."""
    import unicodedata

    v = _vault(root, '["Caf\u00e9"]')
    (v / "Caf\u00e9").mkdir()
    nfd = unicodedata.normalize("NFD", "Caf\u00e9/new")
    said = _refusal(lambda: _write(v, nfd))
    assert ("decomposed" in said or "not spelled" in said
            or "not in a writable folder" in said), said
    assert _write(v, "Caf\u00e9/new") == v / "Caf\u00e9" / "new" / "new.md"
    # With no twin on disk the spelling check has nothing to compare, and only this refuses.
    w = _vault(root, name="other-vault")
    said = _refusal(lambda: _write(w, unicodedata.normalize("NFD", "R\u00e9sum\u00e9s")))
    assert "decomposed" in said, said
    assert sorted(p.name for p in w.iterdir() if p.is_dir()) == [
        "02-areas", "03-references", "04-synthesis"]


def _folder_reaching(v: Path, limit: int) -> str:
    """A folder of 200-byte names, enough of them that `<v>/<folder>/new.md` is `limit` bytes or
    more. Each name is under every volume's NAME_MAX, so only the path's length is at fault."""
    base = len(os.fsencode(str(v))) + len("/new.md")
    return "/".join(["d" * 200] * ((limit - base) // 201 + 1))


def test_a_path_too_long_for_compose_to_read_is_refused(root: Path) -> None:
    """Written relative to a descriptor, a path past PATH_MAX is created, and compose's walk then
    never finds the note, without an error. The limit is the volume's: 1024 on APFS, 4096 on ext4,
    so a fixed length tested only one of them."""
    v = _vault(root)
    before = _files(v)
    said = _refusal(lambda: _write(v, _folder_reaching(v, os.pathconf(v, "PC_PATH_MAX"))))
    assert "compose cannot read a path" in said, said
    assert _files(v) == before and not (v / ("d" * 200)).exists()


@pytest.mark.parametrize("limit", [1024, 4096])
def test_a_path_too_long_is_refused_at_the_plan_under_either_limit(
        root: Path, limit: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both platforms' limits on either machine. Refused at the plan, before anything opens the
    path: past the volume's real limit, an open would fail with ENAMETOOLONG instead."""
    v = _vault(root)
    before = _files(v)
    real = os.pathconf
    monkeypatch.setattr(notes.os, "pathconf",
                        lambda p, name: limit if name == "PC_PATH_MAX" else real(p, name))
    folder = _folder_reaching(v, limit)
    said = _refusal(lambda: notes.plan(project_vault=v, content=GOOD, filename="new.md",
                                       folder=folder, book=notes.PlanBook()))
    assert "compose cannot read a path" in said and f"of {limit} bytes" in said, said
    assert _files(v) == before and not (v / ("d" * 200)).exists()


def test_another_case_of_a_skipped_file_name_is_refused(root: Path) -> None:
    v = _vault(root)
    for name in ("LOG.md", "Memory.md"):
        assert "skip list" in _refusal(lambda name=name: _write(v, "Notes", name))


# ---------------------------------------------------------------- confirming round, 2026-11-13

def test_a_project_vault_without_its_manifest_writes_nothing(root: Path) -> None:
    """With the manifest gone (moved, or mid-save) the default would widen a declared list to
    almost every folder, so nothing is written until it is back."""
    v = _vault(root, '["Notes"]')
    (v / "Notes").mkdir()
    (v / ".substrate.toml").unlink()
    for fn in (lambda: _write(v, "Other"), lambda: _write(v, "Notes"),
               lambda: _amend(v, "04-synthesis/existing.md")):
        assert "has no .substrate.toml" in _refusal(fn)
    assert not (v / "Other").exists() and not list((v / "Notes").iterdir())


def test_a_name_too_long_for_the_volume_is_refused_at_the_plan(root: Path) -> None:
    v = _vault(root)
    for folder, filename in (("a/" + "b" * 300, "n.md"), ("Notes", "n" * 300 + ".md")):
        said = _refusal(lambda folder=folder, filename=filename: notes.plan(
            project_vault=v, content=GOOD, filename=filename, folder=folder,
            book=notes.PlanBook()))
        assert "over the" in said and "a name may be" in said, said
    assert not (v / "a").exists() and not (v / "Notes").exists()


@pytest.mark.parametrize("folder,filename", [("a\tb", "n.md"), ("Notes", "t\tab.md")])
def test_a_tab_in_a_name_is_refused(root: Path, folder: str, filename: str) -> None:
    v = _vault(root)
    said = _refusal(lambda: _write(v, folder, filename))
    assert "holds a tab" in said, said
    assert not (v / folder).exists() or not list((v / folder).iterdir())


def test_amend_compares_passages_and_skipped_names_as_compose_does(root: Path) -> None:
    """Folded matching keeps `ingest` from creating a case twin. `amend` creates nothing, so an
    indexed note under `Passages/` (compose reads it as an ordinary note) stays amendable."""
    v = _vault(root)
    _note(v, "Notes/Passages/n.md")
    assert _amend(v, "Notes/Passages/n.md") == v / "Notes" / "Passages" / "n.md"


@pytest.mark.parametrize("listing", ["volume", "faked"])
def test_an_existing_decomposed_twin_of_a_declared_folder_is_not_declared(
        root: Path, listing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a volume that keeps NFC and NFD names apart, `Cafe\u0301/` beside a declared `Caf\u00e9/` is
    another folder, and compared in NFC it passed as the declared one (adversary, 2026-11-13).

    `covers` compares the caller's folder exactly, before the walk, so the rule holds on any
    volume. Where the volume can't hold both names (APFS), `faked` lists the twin to the walk,
    which is all the walk can see of one; `volume` there asserts the declaration refuses the
    NFD spelling before the walk's spelling check is reached. Neither branch skips: the docling
    job on macOS fails on any skip."""
    import unicodedata

    v = _vault(root, '["Caf\u00e9"]')
    (v / "Caf\u00e9").mkdir()
    nfd = unicodedata.normalize("NFD", "Caf\u00e9")
    try:
        (v / nfd).mkdir()
        twin = True
    except FileExistsError:
        twin = False
    if listing == "faked" and not twin:
        real_listdir = os.listdir

        def listdir(path=".", /):
            names = real_listdir(path)
            return [*names, nfd] if "Caf\u00e9" in names and nfd not in names else names
        monkeypatch.setattr(notes.os, "listdir", listdir)
    walked: list[tuple[str, ...]] = []
    real_walk = notes._walk_folder

    def walk(r, parts, **kw):
        walked.append(parts)
        return real_walk(r, parts, **kw)
    monkeypatch.setattr(notes, "_walk_folder", walk)

    said = _refusal(lambda: _write(v, nfd))
    assert "not in a writable folder" in said, said
    assert (nfd,) not in walked, walked
    assert not list((v / nfd).iterdir()) and not list((v / "Caf\u00e9").iterdir())


# ---------------------------------------------------------------- an inherited vault inside the project vault

NESTED = "this scope's project vault holds a vault it inherits"


def _all_writes_refused(v: Path, said_in: str = NESTED) -> None:
    _note(v, "02-areas/mine.md")
    before = _files(v)
    for fn in (lambda: _write(v, "04-synthesis"), lambda: _write(v, "Elsewhere/new"),
               lambda: _write(v, "."), lambda: _amend(v, "02-areas/mine.md")):
        said = _refusal(fn)
        assert said_in in said and "move the inherited vault out" in said, said
    assert _files(v) == before


@pytest.mark.parametrize("spelled,on_disk", [
    ("core", "core"),           # by path
    ("Core", "core"),           # in another letter case than the disk
    ("core", None),             # a folder that does not exist yet, which ingest could create
    ("deep/er/core", None),     # nor do the folders above it
])
def test_a_project_vault_holding_an_inherited_vault_takes_no_write(
        root: Path, spelled: str, on_disk: str | None) -> None:
    """A layout rule, not a match on each write's folder: that match was got past by path, by
    spelling, and by a vault named before it exists. Wherever the inherited vault is, every
    write to this vault is refused, and the refusal names the entry and the fix."""
    v = _vault(root, extra=f'inherits = ["demo-vault/{spelled}"]\n')
    if on_disk is not None:
        (v / on_disk).mkdir()
        if spelled != on_disk and not (v / spelled).exists():
            pytest.skip("this volume is case-sensitive")
    _all_writes_refused(v)
    assert repr(f"demo-vault/{spelled}") in _refusal(lambda: _write(v, "04-synthesis"))
    if on_disk is None:
        assert not (v / spelled.split("/")[0]).exists()


def test_a_nested_vault_named_by_an_inherited_vault_counts_too(root: Path) -> None:
    core = root / "demo-core-vault"
    core.mkdir()
    (core / ".substrate.toml").write_text('name = "core"\ninherits = ["demo-vault/shared"]\n')
    v = _vault(root, extra='inherits = ["demo-core-vault"]\n')
    _all_writes_refused(v)


def test_a_skipped_nested_inherited_vault_composes_and_still_takes_no_write(root: Path) -> None:
    """The rule's known cost. Skipping the nested folder makes the layout compose, since the
    project vault no longer indexes its notes too; every write to the project vault is still
    refused until the inherited vault is moved out."""
    v = _vault(root, extra='inherits = ["demo-vault/core"]\nskip = ["core"]\n')
    _note(v, "core/02-areas/shared.md")
    composed = {(n.vault, n.path.relative_to(n.path.parents[1]).as_posix())
                for n in V.resolve_scope(v).notes}
    assert ("core", "02-areas/shared.md") in composed
    _all_writes_refused(v)


def test_a_sibling_inherited_vault_is_unaffected(root: Path) -> None:
    core = root / "demo-core-vault"
    (core / "00-operator").mkdir(parents=True)
    v = _vault(root, extra='inherits = ["demo-core-vault", "demo-missing-vault"]\n')
    assert _write(v, "04-synthesis") == v / "04-synthesis" / "new.md"
    assert _amend(v, "02-areas/existing.md") == v / "02-areas" / "existing.md"



def test_an_omitted_folder_still_reports_a_refused_filename_as_itself(root: Path) -> None:
    v = _vault(root)
    for name, why in (("_meta.md", "whatever the vault's `skip`"), ("n.pdf", "not markdown"),
                      ("existing.md", "additive-only")):
        said = _refusal(lambda name=name: notes.plan(project_vault=v, content=GOOD,
                                                     filename=name, book=notes.PlanBook()))
        assert why in said and "no `folder` was given" not in said, (name, said)


# ---------------------------------------------------------------- final confirming round, 2026-11-14

@pytest.mark.parametrize("folder,name", [("02-areas", "00-operato\u0155-x.md"), (".", "00-operato\u0155.md"),
                                         ("02-areas", "10-referenc\u00e9.md"),
                                         ("02-areas", "00-operator\u0301.md")])
def test_a_tier_name_in_another_unicode_form_is_refused(root: Path, folder: str, name: str) -> None:
    """`00-operatoŕ` folds to one composed `ŕ`, so a folded prefix test missed it, while a volume
    that stores names decomposed keeps `00-operator` + U+0301, which compose reads as tier 1."""
    v = _vault(root)
    assert "operator or reference tier" in _refusal(lambda: _write(v, folder, name))
    _note(v, f"02-areas/{name}")
    assert "operator or reference tier" in _refusal(lambda: _amend(v, f"02-areas/{name}"))


@pytest.mark.parametrize("entry", ["~nosuchuser-sbstr/core", "core\\u0000x"])
def test_an_inherits_entry_that_does_not_resolve_refuses_by_name(root: Path, entry: str) -> None:
    v = _vault(root, extra=f'inherits = ["{entry}"]\n')
    said = _refusal(lambda: _write(v, "04-synthesis"))
    assert "inherits" in said, said


def test_an_omitted_folder_asks_for_one_when_04_synthesis_is_spelled_otherwise(root: Path) -> None:
    v = _vault(root)
    shutil.rmtree(v / "04-synthesis")
    (v / "04-Synthesis").mkdir()
    said = _refusal(lambda: _omitted(v))
    assert "Name a `folder`" in said, said
    assert not list((v / "04-Synthesis").iterdir())
