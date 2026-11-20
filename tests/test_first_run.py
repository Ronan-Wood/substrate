"""The first run: `substrate init`, `--version`, `help`, and `search` as the CLI's name for `query`.

Before this, a new user's first commands each failed: `compose` refused a vault with no manifest
and said nothing about writing one, `init` and `help` were not commands, `--version` was a missing
argument, and `--help` listed 25 commands, eight with no description, and no line saying what to
run first. Every test here marked "Fails before" fails on the engine from before.

ON EXT4 AS ON APFS. No test depends on how a volume stores or compares a name: the default-name
rules are tested on strings, NFC and NFD spellings included, and every folder a test makes has a
plain ASCII name, except one with a space, which both volumes store as given. Paths are under
`tmp_path`, far below either volume's limits. Symlinks, `O_EXCL` and a folder's write bit behave
alike on both.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from pathlib import Path

import pytest

from substrate import agent, cli, manifest_init, scopes
from substrate import vault as V

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = V.MANIFEST


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(list(argv))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue(), err.getvalue()


def _notes(folder: Path) -> Path:
    """A folder of plain notes: no frontmatter, most with no heading, one empty, nothing written
    for the engine."""
    folder.mkdir(parents=True)
    (folder / "garden.md").write_text("Tomatoes need full sun and deep watering.\n", "utf-8")
    (folder / "groceries.md").write_text("- milk\n- bread\n- lentils\n", "utf-8")
    (folder / "trips").mkdir()
    (folder / "trips" / "coast.md").write_text("# Coast\n\nThe path closes at dusk in winter.\n",
                                               "utf-8")
    (folder / "inbox.md").write_text("", "utf-8")  # an empty note composes, holding nothing
    return folder


def _tree(*roots: Path) -> dict:
    """Every entry under each root, by path: its kind and its bytes, link target or nothing."""
    out = {}
    for root in roots:
        if not os.path.lexists(root):
            out[str(root)] = None
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                st = os.lstat(p)
                if os.path.islink(p):
                    out[str(p)] = ("link", os.readlink(p))
                elif p.is_dir():
                    out[str(p)] = ("dir", st.st_mode)
                else:
                    out[str(p)] = ("file", st.st_mode, p.read_bytes())
    return out


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """The engine's folder and the registry, both the test's own."""
    h = tmp_path / "engine-home"
    monkeypatch.setattr(scopes, "engine_home", lambda: h)
    monkeypatch.setattr(scopes, "index_home", lambda: h / scopes.INDEX_DIR)
    monkeypatch.setenv(scopes.ENV_VAR, str(h / "scopes.toml"))
    return h


def init(vault: Path, *more: str) -> tuple[int, str, str]:
    return run("init", str(vault), *more)


# ---------------------------------------------------------------- the round trip the issue names

def test_init_compose_and_search_run_in_order_on_plain_notes(tmp_path, home):
    """Fails before: `init` is not a command, so the compose refuses the vault with no manifest."""
    vault = _notes(tmp_path / "Field Notes")
    rc, out, err = init(vault)
    assert (rc, err) == (0, "")
    assert "found 4 notes" in out
    assert "substrate compose" in out and "substrate search --scope field-notes" in out
    rc, out, err = run("compose", str(vault))
    assert rc == 0, err
    assert "quarantined" not in out + err
    assert "scope: 'field-notes' registered" in out
    rc, out, err = run("search", "--scope", "field-notes", "--no-vector", "--json", "tomatoes sun")
    assert rc == 0, err
    hits = json.loads(out)["passages"]
    assert hits and hits[0]["expand_ref"].startswith("field-notes/garden-")


def test_init_then_compose_a_folder_of_empty_untitled_notes(tmp_path, home):
    """Where #71 meets `init`: a stranger's folder whose only notes are empty `Untitled` notes, two
    in one folder and one in another. `init` counts them all, compose indexes the two with names
    of their own and leaves the third out as a copy, and nothing is quarantined. Failed before
    #71: compose refused the scope, the notes being byte-identical."""
    vault = tmp_path / "Inbox Folder"
    (vault / "later").mkdir(parents=True)
    for rel in ("Untitled.md", "Untitled 1.md", "later/Untitled.md"):
        (vault / rel).write_text("", "utf-8")
    rc, out, err = init(vault)
    assert (rc, err) == (0, "")
    assert "found 3 notes" in out
    rc, out, err = run("compose", str(vault))
    assert rc == 0, err
    assert "quarantined" not in out + err
    # First by path, by code point: `Untitled.md` (`U`) before the folder `later/` (`l`).
    assert ("duplicate: Inbox Folder/later/Untitled.md  (the same doc_id and bytes as "
            "Inbox Folder/Untitled.md, which is indexed in its place)") in out
    rc, out, err = run("status", "--scope", "inbox-folder", "--lexical-only", "--json")
    assert rc == 0, err
    ident = json.loads(out)["identical"]
    assert (ident["duplicates"], ident["groups"], ident["empty"]) == (1, 0, 2)


def test_the_manifest_it_writes_holds_the_name_and_nothing_else(tmp_path, home):
    """Fails before: nothing writes one. It passes every manifest check, unknown keys included.

    Decided: no other key is offered, not even as a comment. An `inherits` or `writable_folders`
    example names a folder this vault does not have, and uncommented it refuses the compose; a
    `skip` example replaces the default list. docs/configuration.md has them."""
    vault = _notes(tmp_path / "notes")
    assert init(vault)[0] == 0
    text = (vault / MANIFEST).read_text("utf-8")
    assert text == 'name = "notes"\n'
    parsed = tomllib.loads(text)
    assert parsed == {"name": "notes"}
    assert V.unknown_keys(parsed) == [] and V.misplaced_keys(parsed) == []
    assert V._read_manifest(vault) == parsed
    assert V.resolve_scope(vault).name == "notes"


def test_a_name_with_quotes_and_a_backslash_is_written_as_toml_and_read_back(tmp_path, home):
    """Fails before: no `init`. The name is escaped in the file and in what is printed."""
    vault = _notes(tmp_path / "notes")
    name = 'Ann\'s "work" \\ notes'
    rc, out, err = init(vault, "--name", name)
    assert (rc, err) == (0, "")
    assert V.resolve_scope(vault).name == name
    assert repr(name) in out
    assert "--scope 'Ann'\"'\"'s \"work\" \\ notes'" in out


# ---------------------------------------------------------------- 1. never overwrite, never follow

def _occupy(vault: Path, kind: str) -> None:
    target = vault / MANIFEST
    if kind == "a file":
        target.write_text('name = "theirs"\n', "utf-8")
    elif kind == "a symlink":
        (vault.parent / "elsewhere.toml").write_text('name = "theirs"\n', "utf-8")
        target.symlink_to(vault.parent / "elsewhere.toml")
    elif kind == "a symlink to nothing":
        target.symlink_to(vault.parent / "missing.toml")
    else:
        target.mkdir()
        (target / "inside").write_text("kept\n", "utf-8")


@pytest.mark.parametrize("kind", ["a file", "a symlink", "a symlink to nothing", "a folder"])
def test_an_existing_manifest_of_any_kind_refuses_and_changes_nothing(tmp_path, home, kind):
    """Fails before: no `init`. A dangling symlink is followed by an ordinary open: it must not be."""
    vault = _notes(tmp_path / "notes")
    _occupy(vault, kind)
    before = _tree(tmp_path)
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert err.startswith("FATAL (init): ") and f"already exists ({kind})" in err
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("kind", ["a file", "a symlink", "a symlink to nothing", "a folder"])
def test_the_write_itself_refuses_an_existing_entry(tmp_path, kind):
    """Fails before: no `init`. `O_EXCL | O_NOFOLLOW`, whatever the checks before it saw."""
    vault = _notes(tmp_path / "notes")
    _occupy(vault, kind)
    before = _tree(tmp_path)
    planned = manifest_init.Plan(vault=vault, name="notes", source="", notes=3,
                                 checked=os.stat(vault))
    with pytest.raises(manifest_init.InitError, match="already exists"):
        manifest_init.create(planned)
    assert _tree(tmp_path) == before


def test_a_manifest_that_appears_after_the_checks_is_not_clobbered(tmp_path, home, monkeypatch):
    """Fails before: no `init`. Another writer's manifest lands between the checks and the write."""
    vault = _notes(tmp_path / "notes")
    real_walk = manifest_init._walk

    def racing(v: Path) -> int:
        (v / MANIFEST).write_text('name = "theirs"\n', "utf-8")
        return real_walk(v)

    monkeypatch.setattr(manifest_init, "_walk", racing)
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "already exists (a file)" in err
    assert (vault / MANIFEST).read_text("utf-8") == 'name = "theirs"\n'


def test_a_write_that_fails_part_way_leaves_no_manifest(tmp_path, home, monkeypatch):
    """Fails before: no `init`. A partial manifest would refuse every compose after it."""
    vault = _notes(tmp_path / "notes")

    def full(fd: int, data: bytes) -> None:
        os.write(fd, data[:3])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manifest_init, "_write_all", full)
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "No space left on device; what was written is removed" in err
    assert not os.path.lexists(vault / MANIFEST)


def test_a_folder_swapped_after_the_checks_is_not_written(tmp_path, home, monkeypatch):
    """Fails before: no `init`. The folder written must be the folder checked."""
    vault = _notes(tmp_path / "notes")
    elsewhere = _notes(tmp_path / "elsewhere")

    def swapping(v: Path) -> int:
        v.rename(tmp_path / "moved")
        elsewhere.rename(v)
        return 3

    monkeypatch.setattr(manifest_init, "_walk", swapping)
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "changed while init was checking it" in err
    assert not os.path.lexists(vault / MANIFEST)
    assert not os.path.lexists(tmp_path / "moved" / MANIFEST)


def test_a_symlinked_vault_is_followed_and_the_manifest_written_where_it_points(tmp_path, home):
    """Fails before: no `init`. Compose resolves the vault the same way, so the two agree."""
    vault = _notes(tmp_path / "notes")
    link = tmp_path / "link-to-notes"
    link.symlink_to(vault)
    rc, out, err = init(link)
    assert rc == 0, err
    assert (vault / MANIFEST).is_file() and not (vault / MANIFEST).is_symlink()
    assert str(vault / MANIFEST) in out
    # Named after the folder it points at, which is the name compose gives the vault.
    assert tomllib.loads((vault / MANIFEST).read_text("utf-8")) == {"name": "notes"}


@pytest.mark.parametrize(("make", "says"), [
    ("empty", "the vault is empty, which names no folder."),
    ("missing", "does not exist. init writes into an existing folder of notes and creates no "
                "folder."),
    ("file", "is not a folder."),
    ("dangling", "does not exist."),
])
def test_a_vault_that_is_not_a_folder_refuses_and_creates_nothing(tmp_path, home, make, says):
    """Fails before: no `init`."""
    target = tmp_path / "notes"
    if make == "empty":
        target = Path(" ")
    elif make == "file":
        target.write_text("not a folder\n", "utf-8")
    elif make == "dangling":
        target.symlink_to(tmp_path / "gone")
    before = _tree(tmp_path)
    rc, out, err = init(target) if make != "empty" else run("init", "")
    assert (rc, out) == (2, "")
    assert err.startswith("FATAL (init): ") and says in err
    assert _tree(tmp_path) == before


def test_an_unwritable_vault_refuses_and_writes_nothing(tmp_path, home):
    """Fails before: no `init`. The folder's write bit, not a guess at it."""
    vault = _notes(tmp_path / "notes")
    vault.chmod(0o500)
    try:
        before = _tree(tmp_path)
        rc, out, err = init(vault)
        assert (rc, out) == (2, "")
        assert f"cannot create {vault / MANIFEST}: Permission denied. Nothing was written." in err
        assert _tree(tmp_path) == before
    finally:
        vault.chmod(0o700)


# ---------------------------------------------------------------- 2. only that one file

def test_init_writes_one_file_and_nothing_else(tmp_path, home):
    """Fails before: no `init`. No index, no registry row, no `.gitignore`, nothing in the engine's
    folder: the vault, the engine's folder and the registry are as they were, the manifest aside."""
    vault = _notes(tmp_path / "notes")
    registry = tmp_path / "elsewhere" / "scopes.toml"
    before = _tree(tmp_path)
    rc, _, err = init(vault, "--registry", str(registry))
    assert rc == 0, err
    after = _tree(tmp_path)
    added = set(after) - set(before)
    assert added == {str(vault / MANIFEST)}
    assert {k: v for k, v in after.items() if k not in added} == before
    assert not home.exists() and not registry.parent.exists()


# ---------------------------------------------------------------- 4. the default name

@pytest.mark.parametrize(("folder", "name"), [
    ("notes", "notes"),
    ("My Notes", "my-notes"),
    ("Field  Notes (2024)", "field-notes-2024"),
    ("notes.v2", "notes-v2"),
    ("__drafts__", "drafts"),
    ("work_log", "work_log"),
    ("-leading-dash", "leading-dash"),
    (" spaced ", "spaced"),
    ("Café", "cafe"),
    ("Café", "cafe"),
    ("Straße", "strasse"),
    ("ＮＯＴＥＳ", "notes"),
    ("İstanbul", "istanbul"),
    ("a" * 128, "a" * 128),
])
def test_the_default_name_comes_from_the_folder_name(folder, name):
    """Fails before: no `default_name`. Decided on the string, so a volume's own form does not
    matter: ext4 stores `Café` as given, APFS may hand back either form."""
    assert manifest_init.default_name(folder) == name


@pytest.mark.parametrize("folder", ["笔记", "заметки", "---", "___", "!!!", "a" * 129, ""])
def test_a_folder_name_that_makes_no_name_gives_none(folder):
    """Fails before: no `default_name`. None rather than an invented name."""
    assert manifest_init.default_name(folder) is None


def test_both_unicode_forms_of_one_folder_name_give_one_name():
    for folder in ("Ångström Notes", "Crème brûlée", "Ňotes"):
        nfc, nfd = (unicodedata.normalize(f, folder) for f in ("NFC", "NFD"))
        assert nfc != nfd
        assert manifest_init.default_name(nfc) == manifest_init.default_name(nfd) is not None


@pytest.mark.parametrize("folder", ["My Notes", "Café", "notes.v2", "a" * 128, "work_log", "x"])
def test_a_derived_name_is_one_every_consumer_takes_as_it_is(folder):
    """Fails before: no `default_name`. Compose's checks, the registry's, an agent snapshot's, and
    the index folder, which keeps a plain name rather than hashing it."""
    name = manifest_init.default_name(folder)
    scopes.check_name(name)
    assert agent.valid_scope_name(name)
    assert scopes.index_folder_name(name) == name
    assert tomllib.loads(manifest_init.manifest_text(name)) == {"name": name}


def test_a_folder_name_that_makes_no_name_refuses_and_asks_for_name(tmp_path, home):
    """Fails before: no `init`."""
    vault = _notes(tmp_path / "笔记")
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "Pass --name NAME" in err and "'笔记'" in err
    assert not os.path.lexists(vault / MANIFEST)
    rc, _, err = init(vault, "--name", "biji")
    assert rc == 0, err
    assert V.resolve_scope(vault).name == "biji"


@pytest.mark.parametrize("name", ["", " notes", "notes ", "a/b", "tab\there", "esc\x1b[2J",
                                  "line\nbreak", " "])
def test_a_name_compose_would_refuse_or_that_does_not_print_is_refused(tmp_path, home, name):
    """Fails before: no `init`. The rules `scopes.check_name` applies, and printable."""
    vault = _notes(tmp_path / "notes")
    rc, out, err = init(vault, "--name", name)
    assert (rc, out) == (2, "")
    assert err.startswith(f"FATAL (init): --name {name!r} ")
    assert "\x1b" not in err
    assert not os.path.lexists(vault / MANIFEST)


def _register(name: str, vault: Path, home: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    scopes.record(name, vault=vault, db=home / f"{name}.db", index_root=home / name)


def test_a_name_registered_to_another_vault_is_refused(tmp_path, home):
    """Fails before: no `init`. Compose would refuse the vault under that name, after the manifest
    was written; so init refuses first, naming the other vault."""
    _register("notes", tmp_path / "other" / "notes", home)
    vault = _notes(tmp_path / "notes")
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert f"scope 'notes' is already registered to {tmp_path / 'other' / 'notes'}" in err
    assert "Pass another --name" in err
    assert not os.path.lexists(vault / MANIFEST)
    assert init(vault, "--name", "notes-2")[0] == 0


def test_a_name_that_differs_from_a_registered_one_only_by_case_is_refused(tmp_path, home):
    """Fails before: no `init`. The two would share one index folder on a case-folding volume, so
    compose refuses the pair (`scopes.default_index`); decided on the names, not on the volume."""
    _register("Notes", tmp_path / "other" / "Notes", home)
    vault = _notes(tmp_path / "notes")
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "differs from the registered 'Notes' only by case" in err
    assert not os.path.lexists(vault / MANIFEST)


def test_a_twin_of_another_name_is_not_checked_for_a_vault_registered_already(tmp_path, home):
    """A registered vault keeps the index it has, so compose never asks the volume about a twin."""
    _register("Foo", tmp_path / "other" / "Foo", home)
    (tmp_path / "other" / "Foo" / MANIFEST).write_text('name = "Foo"\n', "utf-8")
    vault = _notes(tmp_path / "notes")
    _register("old", vault, home)
    rc, _, err = init(vault, "--name", "foo")
    assert rc == 0, err
    rc, _, err = run("compose", str(vault))
    assert rc == 0, err


def test_a_vault_registered_under_two_names_asks_for_name(tmp_path, home):
    """Fails before: no `init`. A manifest renamed twice leaves two rows; neither is its name."""
    vault = _notes(tmp_path / "notes")
    _register("alpha", vault, home)
    _register("zeta", vault, home)
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "under 2 names, 'alpha', 'zeta'" in err and "Pass --name NAME" in err
    assert not os.path.lexists(vault / MANIFEST)


def test_a_registered_name_init_will_not_write_asks_for_name(tmp_path, home):
    """Fails before: no `init`. A name compose took, with a line break in it, is not written back
    raw, which would leave a manifest that does not parse."""
    vault = _notes(tmp_path / "notes")
    _register("a\nb", vault, home)
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "registered as 'a\\nb', which init will not write" in err
    assert not os.path.lexists(vault / MANIFEST)


def test_the_manifest_is_valid_toml_for_any_name_it_is_handed():
    for name in ('q"uote', "back\\slash", "tab\there", "nl\nhere", "del\x7f", "é"):
        assert tomllib.loads(manifest_init.manifest_text(name)) == {"name": name}


def test_a_vault_the_registry_holds_keeps_the_name_it_is_registered_under(tmp_path, home):
    """Fails before: no `init`. A vault whose manifest was lost gets its scope back, rather than a
    second registry row for the same vault under a derived name."""
    vault = _notes(tmp_path / "notes")
    _register("garden-and-trips", vault, home)
    rc, out, err = init(vault)
    assert rc == 0, err
    assert "scope 'garden-and-trips', the name the registry has this vault under" in out
    assert V.resolve_scope(vault).name == "garden-and-trips"


def test_an_unreadable_registry_refuses(tmp_path, home):
    """Fails before: no `init`. Compose would refuse it too."""
    (tmp_path / "broken.toml").write_text("scopes = [", "utf-8")
    vault = _notes(tmp_path / "notes")
    rc, out, err = init(vault, "--registry", str(tmp_path / "broken.toml"))
    assert (rc, out) == (2, "")
    assert "cannot read scope registry" in err
    assert not os.path.lexists(vault / MANIFEST)


# ---------------------------------------------------------------- 5. inside or around another vault

def test_a_folder_inside_a_vault_is_refused(tmp_path, home):
    """Fails before: no `init`. Both scopes would index the nested notes."""
    outer = _notes(tmp_path / "outer")
    (outer / MANIFEST).write_text('name = "outer"\n', "utf-8")
    rc, out, err = init(outer / "trips")
    assert (rc, out) == (2, "")
    assert f"{outer / MANIFEST} is the manifest of a vault above {outer / 'trips'}" in err
    assert not os.path.lexists(outer / "trips" / MANIFEST)


def test_a_folder_below_that_cannot_be_read_refuses(tmp_path, home):
    """Fails before: no `init`. A manifest in it would go unseen, so the guard does not pass."""
    vault = _notes(tmp_path / "notes")
    (vault / "locked").mkdir()
    (vault / "locked" / MANIFEST).write_text('name = "inner"\n', "utf-8")
    (vault / "locked").chmod(0)
    try:
        rc, out, err = init(vault)
        assert (rc, out) == (2, "")
        assert f"cannot read {vault / 'locked'}: Permission denied" in err
        assert not os.path.lexists(vault / MANIFEST)
    finally:
        (vault / "locked").chmod(0o700)


def test_a_folder_that_can_be_listed_but_not_entered_refuses_cleanly(tmp_path, home):
    """Fails before: no `init`. And compose still refuses it with exit 2, not a traceback from the
    hint."""
    vault = _notes(tmp_path / "notes")
    vault.chmod(0o600)
    try:
        rc, out, err = init(vault)
        assert (rc, out) == (2, "")
        assert f"cannot read {vault / MANIFEST}: Permission denied" in err
        rc, _, err = run("compose", str(vault))
        assert rc == 2 and "FATAL (manifest)" in err and "Traceback" not in err
    finally:
        vault.chmod(0o700)


def test_a_manifest_below_spelled_in_another_case_is_refused(tmp_path, home):
    """Fails before: no `init`. Compared folded, so APFS and ext4 refuse alike."""
    vault = _notes(tmp_path / "notes")
    (vault / "trips" / ".SUBSTRATE.toml").write_text('name = "trips"\n', "utf-8")
    rc, _, err = init(vault)
    assert rc == 2 and "is the manifest of a vault inside" in err
    assert not os.path.lexists(vault / MANIFEST)


def test_a_name_whose_default_index_is_another_scopes_is_refused(tmp_path, home):
    """Fails before: no `init`. Compose would refuse it as that scope's index (`foreign_owner`)."""
    other = _notes(tmp_path / "other")
    (other / MANIFEST).write_text('name = "other"\n', "utf-8")
    scopes.record("other", vault=other, db=home / scopes.INDEX_DIR / "notes.db",
                  index_root=home / scopes.INDEX_DIR / "other")
    vault = _notes(tmp_path / "notes")
    rc, out, err = init(vault)
    assert (rc, out) == (2, "")
    assert "scope 'other' keeps its index at" in err and "Pass another --name" in err
    assert not os.path.lexists(vault / MANIFEST)


def test_a_nested_manifest_named_by_the_vault_is_printed_escaped(tmp_path, home):
    """A folder name is the vault's to choose, and a synced vault's may be anyone's."""
    vault = _notes(tmp_path / "notes")
    (vault / "x\x1b]0;title\x07y").mkdir()
    (vault / "x\x1b]0;title\x07y" / MANIFEST).write_text('name = "inner"\n', "utf-8")
    rc, _, err = init(vault)
    assert rc == 2 and "x\\x1b]0;title\\x07y" in err
    assert "\x1b" not in err and "\x07" not in err


def test_a_folder_at_or_above_the_engine_index_is_refused(tmp_path, home):
    """Fails before: no `init`. Compose refuses an index root with a manifest in it or above it, so
    a manifest at `~` made every compose into `~/.substrate/index` refuse."""
    rc, out, err = init(tmp_path)
    assert (rc, out) == (2, "")
    assert f"holds or is inside {home / scopes.INDEX_DIR}" in err
    assert not os.path.lexists(tmp_path / MANIFEST)


def test_a_folder_around_a_registered_ingest_tree_is_refused(tmp_path, home):
    """Fails before: no `init`. The same, for a scope composed with --index-root elsewhere."""
    folder = _notes(tmp_path / "work")
    (folder / "index").mkdir()
    scopes.record("work", vault=tmp_path / "work-vault", db=folder / "index" / "w.db",
                  index_root=folder / "index" / "w")
    rc, out, err = init(folder)
    assert (rc, out) == (2, "")
    assert f"{folder / 'index' / 'w'}, where the engine keeps an index" in err


def test_a_folder_that_holds_a_vault_is_refused(tmp_path, home):
    """Fails before: no `init`. The demo pair: a folder holding a project vault and its core."""
    shutil.copytree(ROOT / "vaults", tmp_path / "vaults")
    rc, out, err = init(tmp_path / "vaults")
    assert (rc, out) == (2, "")
    assert f"{tmp_path / 'vaults' / 'demo-vault' / MANIFEST} is the manifest of a vault inside" in err
    assert not os.path.lexists(tmp_path / "vaults" / MANIFEST)


# ---------------------------------------------------------------- 6. a folder with no notes

@pytest.mark.parametrize("contents", [{}, {"todo.txt": "not a note"}, {"log.md": "skipped\n"}])
def test_a_folder_with_no_note_gets_its_manifest_and_is_told_there_is_nothing_yet(
        tmp_path, home, contents):
    """Fails before: no `init`. Written, because a vault that will be filled through `ingest` needs
    its manifest first (with none, the write path takes no write). `log.md` is skipped by default,
    so it is no note either."""
    vault = tmp_path / "empty"
    vault.mkdir()
    for name, text in contents.items():
        (vault / name).write_text(text, "utf-8")
    rc, out, err = init(vault)
    assert (rc, err) == (0, "")
    assert "nothing to index yet" in out and "substrate search" not in out
    assert (vault / MANIFEST).is_file()
    rc, _, err = run("compose", str(vault))
    assert rc == 2 and "discovered zero notes" in err


# ---------------------------------------------------------------- the refusal that points at init

def test_compose_of_a_vault_with_no_manifest_names_init(tmp_path, home):
    """Fails before: the refusal said only that a manifest is required. Compose still refuses."""
    vault = _notes(tmp_path / "Field Notes")
    rc, out, err = run("compose", str(vault))
    assert rc == 2
    assert "refusing to guess a scope" in err
    assert f"`substrate init '{vault}'` writes one." in err
    assert not os.path.lexists(vault / MANIFEST)


def test_register_of_a_vault_with_no_manifest_names_init(tmp_path, home):
    """Fails before."""
    vault = _notes(tmp_path / "notes")
    rc, _, err = run("register", str(vault))
    assert rc == 2 and f"`substrate init {vault}` writes one." in err


def test_no_hint_for_a_folder_init_would_refuse(tmp_path, home):
    outer = _notes(tmp_path / "outer")
    (outer / MANIFEST).write_text('name = "outer"\n', "utf-8")
    rc, _, err = run("compose", str(outer / "trips"))
    assert rc == 2 and "substrate init" not in err


def test_the_hint_for_a_registered_vault_keeps_its_name(tmp_path, home):
    """Fails before. A registered vault whose manifest went missing is told the name to give back."""
    vault = _notes(tmp_path / "notes")
    _register("garden", vault, home)
    rc, _, err = run("compose", str(vault))
    assert rc == 2 and f"`substrate init {vault} --name garden` writes one." in err


def test_no_hint_where_init_could_not_help(tmp_path, home):
    """A vault that is not a folder, or one whose manifest is there and broken."""
    assert "substrate init" not in run("compose", str(tmp_path / "missing"))[2]
    vault = _notes(tmp_path / "notes")
    (vault / MANIFEST).write_text("names = 'x'\n", "utf-8")
    rc, _, err = run("compose", str(vault))
    assert rc == 2 and "substrate init" not in err


# ---------------------------------------------------------------- --version

def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_version_flag_prints_the_package_version_on_one_line():
    """Fails before: `error: the following arguments are required: cmd`, exit 2."""
    rc, out, err = run("--version")
    assert (rc, out, err) == (0, f"substrate {_pyproject_version()}\n", "")


def test_the_version_command_keeps_its_json():
    rc, out, _ = run("version")
    assert rc == 0 and set(json.loads(out)) == {"schema", "wire", "registry"}


def test_a_version_that_cannot_be_read_is_said_and_exits_2(monkeypatch):
    """Not guessed: a missing or unplain version is an error, not a placeholder."""
    def unreadable() -> str:
        raise ValueError("pyproject.toml has no plain [project] version")

    monkeypatch.setattr(cli, "package_version", unreadable)
    rc, out, err = run("--version")
    assert (rc, out) == (2, "")
    assert err == "substrate: the version is unknown: pyproject.toml has no plain [project] version\n"


def _imported(*argv: str) -> set[str]:
    r = subprocess.run([sys.executable, "-X", "importtime", "-m", "substrate.cli", *argv],
                       cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr[-2000:]
    return {line.rsplit("|", 1)[1].strip() for line in r.stderr.splitlines()
            if line.startswith("import time:") and "|" in line}


@pytest.mark.parametrize("argv", [("--version",), ("--help",), ("help",)])
def test_version_and_help_import_no_heavy_module(argv):
    """The model and document stacks stay out of the CLI's start, as they were before."""
    loaded = {m.split(".")[0] for m in _imported(*argv)}
    assert not loaded & {"docling", "torch", "numpy", "pypdfium2", "yaml", "cv2", "PIL"}


# ---------------------------------------------------------------- --help and help

def _commands():
    """The CLI's top-level subcommand action, caught as `main` builds it."""
    captured = {}
    real = cli.argparse.ArgumentParser.add_subparsers

    def spy(self, *a, **kw):
        action = real(self, *a, **kw)
        captured.setdefault("action", action)
        return action

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli.argparse.ArgumentParser, "add_subparsers", spy)
        run("help")
    return captured["action"]


def test_help_opens_with_what_substrate_does_and_the_three_commands_in_order():
    """Fails before: the usage line, then 25 command names."""
    rc, out, err = run("--help")
    assert (rc, err) == (0, "")
    head = out.split("options:")[0]
    assert "indexes folders of Markdown notes" in head
    lines = [ln.split() for ln in head.splitlines() if ln.startswith("  substrate ")]
    assert [ln[1] for ln in lines] == ["init", "compose", "search"]


def test_every_listed_command_has_a_description_and_the_rest_are_named_internal():
    """Fails before: eight commands had none. A command added without `help=` is hidden; this keeps
    that a decision, by holding the hidden set to `cli.INTERNAL`."""
    action = _commands()
    listed = {c.dest: c.help for c in action._choices_actions}
    assert all(h and h.strip() for h in listed.values()), listed
    primary = {name for name, p in action.choices.items()
               if p.prog == f"substrate {name}"}
    assert primary - set(listed) == set(cli.INTERNAL) == {
        "eval", "index", "ingest-md", "rechunk", "refresh-record", "review", "verify"}
    listing = run("--help")[1].split("\ncommands:\n")[1]
    shown = {ln.split()[0] for ln in listing.splitlines() if ln.startswith("    ") and ln[4] != " "}
    assert shown == set(listed) and not shown & set(cli.INTERNAL)


def test_an_internal_command_still_runs_and_has_help():
    for name in cli.INTERNAL:
        rc, out, err = run("help", name)
        assert (rc, err) == (0, "") and out.startswith(f"usage: substrate {name}")


@pytest.mark.parametrize("words", [(), ("compose",), ("init",), ("blobs",), ("blobs", "gc"),
                                   ("query",), ("help",)])
def test_help_prints_what_help_flag_prints(words):
    """Fails before: `help` is not a command."""
    rc, out, err = run("help", *words)
    flag = run(*words, "--help")
    assert (rc, out, err) == flag
    assert rc == 0 and out.startswith("usage: substrate")


@pytest.mark.parametrize(("typed", "near"), [("serch", "search"), ("comopse", "compose"),
                                             ("INIT", "init"), ("staus", "status"),
                                             ("frobnicate", None)])
def test_an_unknown_command_exits_2_with_the_near_miss(typed, near):
    """Fails before: argparse's "invalid choice", listing every command, internal ones too. The
    suggestion is the manifest's: `(did you mean 'x'?)`, or nothing below the cutoff."""
    guess = f" (did you mean {near!r}?)" if near else ""
    said = f"substrate: {typed!r} is not a command{guess}. `substrate help` lists them.\n"
    assert run(typed, "--scope", "x") == (2, "", said)
    assert run("help", typed) == (2, "", said)


# ---------------------------------------------------------------- 8. search and query are one command

def test_search_and_query_are_one_parser():
    """Fails before: `search` is not a command. One parser, so every flag is shared by construction."""
    action = _commands()
    assert action.choices["search"] is action.choices["query"]


def test_search_and_query_give_the_same_output_and_exit(tmp_path, home):
    """Fails before: `search` is not a command."""
    shutil.copytree(ROOT / "vaults", tmp_path / "vaults")
    assert run("compose", str(tmp_path / "vaults" / "demo-vault"))[0] == 0
    for flags in (("--scope", "demo", "--no-vector", "--json", "engine boundary"),
                  ("--scope", "demo", "--no-vector", "--expand", "--k", "2", "engine boundary"),
                  ("--scope", "demo", "--json", "--kind", "outline", "engine boundary"),
                  ("--scope", "nope", "--no-vector", "engine boundary")):
        searched = run("search", *flags)
        assert searched == run("query", *flags)
        assert searched[0] == (2 if "nope" in flags or "outline" in flags else 0), searched
