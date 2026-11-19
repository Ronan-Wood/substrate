"""An unknown top-level key in `.substrate.toml` refuses, naming the key and the file.

A key the engine does not know used to be ignored, so a typo meant the default without a word: a
misspelt `skips` left the default skip list in effect, and a misspelt `writable_folders` left the
write path's default, any folder compose does not skip, which is wider than the owner wrote.

Tables stay open at any name, because the format defines none with a fixed schema. A table is what
`tomllib` returns as one: `[x]`, `x = {}` and a dotted `x.y = 1`. An array of tables is an array.

Every key here is in a file's CONTENTS, never in a file name, and no test's path varies in case,
Unicode form or length, so APFS and ext4 behave alike.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from substrate import introspect, notes, refresh, refresh_state  # noqa: E402
from substrate import vault as V  # noqa: E402

SPINE = ("---\nstatus: active\ndoc_type: reference\nconfidence: proposed\ndomains: [retrieval]\n"
         "---\n")
DEMO = ROOT / "vaults"


def _vault(root: Path, body: str, name: str = "demo-vault") -> Path:
    """A vault holding one ordinary note in `04-synthesis/`, with `body` as its manifest."""
    v = root / name
    (v / "04-synthesis").mkdir(parents=True)
    (v / "04-synthesis" / f"{name}-note.md").write_text(
        SPINE + f"\n# A note in {name}\n\nAn ordinary note.\n", encoding="utf-8")
    (v / ".substrate.toml").write_text(body, encoding="utf-8")
    return v


def _refusal(fn) -> str:
    try:
        fn()
    except (V.VaultError, notes.NoteError) as e:
        return str(e)
    raise AssertionError("expected a refusal; the manifest was accepted")


def _compose_refusal(root: Path, body: str) -> str:
    v = _vault(root, body)
    return _refusal(lambda: V.resolve_scope(v))


# ---------------------------------------------------------------- compose refuses, by name

@pytest.mark.parametrize(("line", "key", "near"), [
    ('skips = ["drafts"]', "skips", "skip"),
    ('writeable_folders = ["Notes"]', "writeable_folders", "writable_folders"),
    ('writable-folders = ["Notes"]', "writable-folders", "writable_folders"),
    ("inherit = []", "inherit", "inherits"),
    ('reference_domain = ["software-dev"]', "reference_domain", "reference_domains"),
    ('reference_pin = "go"', "reference_pin", "reference_pins"),
    ('identities = "/tmp/roster.json"', "identities", None),
    ("version = 2", "version", None),
])
def test_a_misspelt_or_unknown_key_refuses_compose_by_name(tmp_path: Path, line: str, key: str,
                                                          near: str | None) -> None:
    """On the old code the key was ignored and the scope composed with the default."""
    said = _compose_refusal(tmp_path, f'name = "demo"\n{line}\n')
    assert f"{key!r} is not a key the manifest defines" in said, said
    assert str(tmp_path / "demo-vault" / ".substrate.toml") in said, said
    if near is None:
        assert "did you mean" not in said, said
    else:
        assert f"(did you mean {near!r}?)" in said, said


@pytest.mark.parametrize("key", ["Skip", "SKIP", "sKiP", "\uff53\uff4b\uff49\uff50", "\u017fkip"])
def test_another_case_or_compatibility_form_of_a_key_is_unknown(tmp_path: Path, key: str) -> None:
    """Keys are read exactly as written, so `Skip` is not `skip`. The suggestion folds case and
    compatibility forms (full-width letters, the long s) to say which key it looks like."""
    said = _compose_refusal(tmp_path, f'name = "demo"\n"{key}" = ["drafts"]\n')
    assert f"{key!r} is not a key the manifest defines (did you mean 'skip'?)" in said, said
    assert "spelled exactly" in said, said


def test_two_unicode_forms_of_one_key_are_two_unknown_keys() -> None:
    """Without the filesystem: TOML keeps an NFC and an NFD spelling as two keys, and both are
    unknown. A known key is never matched in another form, since every known key is ASCII."""
    nfc, nfd = "caf\u00e9", "cafe\u0301"
    manifest = tomllib.loads(f'name = "demo"\n"{nfc}" = 1\n"{nfd}" = 2\n')
    assert V.unknown_keys(manifest) == [nfc, nfd]
    assert all(k.isascii() for k in V.TOP_LEVEL_KEYS)


def test_every_unknown_key_is_named_in_file_order(tmp_path: Path) -> None:
    said = _compose_refusal(tmp_path, 'zeta = 1\nname = "demo"\nalpha = 2\nskips = []\n')
    assert ("'zeta', 'alpha', 'skips' are not keys the manifest defines (did you mean 'skip' "
            "for 'skips'?)" in said), said


@pytest.mark.parametrize(("key", "shown"), [
    ("a\nb", r"'a\nb'"),
    ("a\tb\u0007", r"'a\tb\x07'"),
    ("skip\u200b", r"'skip\u200b'"),
    ("\u202eskip", r"'\u202eskip'"),
])
def test_a_key_holding_control_or_invisible_characters_is_printed_escaped(
        tmp_path: Path, key: str, shown: str) -> None:
    """A TOML key can hold a newline, a control character or one that renders as nothing. Printed
    raw, it would break the line it is reported on or hide what is wrong with it."""
    quoted = key.encode("unicode_escape").decode("ascii").replace("\\x", "\\u00")
    said = _compose_refusal(tmp_path, f'name = "demo"\n"{quoted}" = 1\n')
    assert f"{shown} is not a key the manifest defines" in said, said
    assert key not in said, said
    assert "\n" not in said and "\u202e" not in said and "\u200b" not in said, said


def test_an_inherited_vault_s_unknown_key_refuses_the_scope(tmp_path: Path) -> None:
    """Every manifest in the chain is read as the project's is."""
    _vault(tmp_path, 'name = "core"\nskips = ["drafts"]\n', name="core-vault")
    v = _vault(tmp_path, 'name = "demo"\ninherits = ["core-vault"]\n')
    said = _refusal(lambda: V.resolve_scope(v))
    assert str(tmp_path / "core-vault" / ".substrate.toml") in said and "'skips'" in said, said


# ---------------------------------------------------------------- tables stay open

@pytest.mark.parametrize("body", [
    '[app]\nwhatever = 1\nnested = { deep = [1, 2] }\n',
    'app = {}\n',
    'app = { theme = "dark" }\n',
    'app.theme = "dark"\n',
    '["Skip"]\nx = 1\n',
    '[""]\nx = 1\n',
    '[reference_pins]\ngo = "1.21"\n"any pin name at all" = "3"\nSkip = "1.0"\n',
])
def test_a_table_of_any_name_still_composes(tmp_path: Path, body: str) -> None:
    """A pin, not a regression test: the old code composed these too, and still must."""
    v = _vault(tmp_path, f'name = "demo"\n{body}')
    assert V.resolve_scope(v).name == "demo"


def test_an_array_of_tables_is_an_array_and_refuses(tmp_path: Path) -> None:
    """`[[app]]` and `app = [{}]` parse to one value, a list, so the rule cannot tell them apart,
    and neither is a table."""
    for i, body in enumerate(('[[app]]\nx = 1\n', 'app = [{ x = 1 }]\n')):
        said = _compose_refusal(tmp_path / str(i), f'name = "demo"\n{body}')
        assert "'app' is not a key the manifest defines" in said, said


@pytest.mark.parametrize(("body", "expected"), [
    ('[[app]]\nskip = ["drafts"]\n', "'skip' is inside the [app] table"),
    ('[app]\nskip = ["drafts"]\n', "'skip' is inside the [app] table"),
    ('skips = 1\n[reference_pins]\nskip = "1.0"\n', "'skip' is inside the [reference_pins] table"),
])
def test_a_misplaced_known_key_is_reported_before_an_unknown_one(tmp_path: Path, body: str,
                                                                 expected: str) -> None:
    """The more specific message wins: a known key below a header has one fix, moving it."""
    said = _compose_refusal(tmp_path, f'name = "demo"\n{body}')
    assert expected in said and "not a key the manifest defines" not in said, said


@pytest.mark.parametrize(("body", "expected"), [
    ('[skip]\nx = 1\n', "'skip' must be a top-level list of strings"),
    ('[inherits]\nx = 1\n', "'inherits' must be a list of strings"),
    ('[reference_domains]\nx = 1\n', "'reference_domains' must be a top-level list of strings"),
])
def test_a_known_key_written_as_a_table_gets_its_own_type_refusal(tmp_path: Path, body: str,
                                                                  expected: str) -> None:
    said = _compose_refusal(tmp_path, f'name = "demo"\n{body}')
    assert expected in said and "not a key the manifest defines" not in said, said


# ---------------------------------------------------------------- one known set

def test_the_check_reads_the_one_known_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key added to `TOP_LEVEL_KEYS` is known everywhere at once, with nothing else to change:
    refused before, composing after, listed in the refusal, and refused inside a table."""
    body = 'name = "demo"\nadded_later = 3\n'
    assert "'added_later' is not a key" in _compose_refusal(tmp_path / "before", body)
    monkeypatch.setattr(V, "TOP_LEVEL_KEYS", V.TOP_LEVEL_KEYS | {"added_later"})
    assert V.resolve_scope(_vault(tmp_path / "after", body)).name == "demo"
    said = _compose_refusal(tmp_path / "listed", 'name = "demo"\nadded_latr = 3\n')
    assert "(did you mean 'added_later'?)" in said and "keys are added_later," in said, said
    said = _compose_refusal(tmp_path / "inside", 'name = "demo"\n[app]\nadded_later = 3\n')
    assert "'added_later' is inside the [app] table" in said, said


def test_the_demo_vaults_compose_unchanged() -> None:
    """A pin: the shipped manifest carries no unknown key, and the demo scope composes."""
    manifest = DEMO / "demo-vault" / ".substrate.toml"
    assert V.unknown_keys(tomllib.loads(manifest.read_text(encoding="utf-8"))) == []
    assert V.resolve_scope(DEMO / "demo-vault").name == "demo"


def test_the_keys_accepted_unread_compose_and_a_misspelling_of_one_does_not(tmp_path: Path) -> None:
    """0.2.1 retired `guard_state` saying a manifest that still declares it composes. Scripta's
    manifest, as it writes one, carries two keys of its own at top level."""
    v = _vault(tmp_path / "kept", 'name = "demo"\nscripta_workspace_vault = true\n'
                                  'scripta_workspace = "Work"\ninherits = []\n'
                                  'guard_state = "/tmp/state.json"\n')
    assert V.resolve_scope(v).name == "demo"
    assert V.skip_list(v) == V.DEFAULT_SKIP and V.chain_best_effort(v) == (v.resolve(),)
    said = _compose_refusal(tmp_path / "typo", 'name = "demo"\nguard_states = "/tmp/s.json"\n')
    assert "'guard_states' is not a key the manifest defines" in said, said


def test_a_huge_key_or_many_keys_make_a_short_refusal_quickly(tmp_path: Path) -> None:
    """A manifest under the size cap can hold a key of millions of characters, and the refusal
    reaches every `status`. U+FDFA is the character NFKC expands most, eighteen-fold."""
    import time
    began = time.monotonic()
    said = _compose_refusal(tmp_path / "long", f'name = "demo"\n"{"\ufdfa" * 1_000_000}" = 1\n')
    assert time.monotonic() - began < 10 and len(said) < 1000, len(said)
    assert "... (1000000 characters) is not a key" in said and "did you mean" not in said, said
    keys = "".join(f"k{i} = 1\n" for i in range(5000))
    said = _compose_refusal(tmp_path / "many", f'name = "demo"\n{keys}')
    assert "'k9' and 4990 more are not keys" in said and len(said) < 1000, said
    # A character that does not print is written as up to ten; the cut is of what is printed.
    keys = "".join(f'"{i}{"\\U0010FFFF" * 100}" = 1\n' for i in range(12))
    said = _compose_refusal(tmp_path / "unprintable", f'name = "demo"\n{keys}')
    said = said.replace(str(tmp_path / "unprintable" / "demo-vault" / ".substrate.toml"), "")
    assert "(101 characters)" in said and "and 2 more" in said and len(said) < 1000, len(said)


# ---------------------------------------------------------------- the readers of one key

def _write(v: Path, folder: str = "04-synthesis") -> Path:
    book = notes.PlanBook()
    content = (SPINE + "\n# A new note\n\nWritten through the gate.\n").encode()
    p = notes.plan(project_vault=v, content=content, filename="new.md", folder=folder, book=book)
    return notes.commit(project_vault=v, content=content, filename="new.md", folder=folder,
                        confirm_token=p.confirm_token, book=book)


def _amend(v: Path, rel: str) -> Path:
    book = notes.PlanBook()
    new = (v / rel).read_bytes() + b"\nAmended.\n"
    p = notes.plan_amend(project_vault=v, path=rel, content=new, book=book)
    return notes.commit_amend(project_vault=v, path=rel, content=new,
                              confirm_token=p.confirm_token, book=book)[0]


@pytest.mark.parametrize("line", ['writeable_folders = ["Notes"]', 'skips = ["Notes"]',
                                  "version = 2"])
def test_the_write_path_refuses_while_an_unknown_key_may_be_a_key_it_reads(
        tmp_path: Path, line: str) -> None:
    """On the old code a misspelt `writable_folders` left the default, so `ingest` wrote outside
    the one folder its owner declared. Nothing is written now."""
    v = _vault(tmp_path, f'name = "demo"\n{line}\n')
    (v / "Notes").mkdir()
    for fn in (lambda: _write(v), lambda: _write(v, "Notes"),
               lambda: _amend(v, "04-synthesis/demo-vault-note.md")):
        said = _refusal(fn)
        assert "skips: the file, or the `skip` in it, does not read" in said, said
    assert not (v / "04-synthesis" / "new.md").exists() and not (v / "Notes" / "new.md").exists()
    assert b"Amended." not in (v / "04-synthesis" / "demo-vault-note.md").read_bytes()
    with pytest.raises(V.VaultError, match="An unknown key may be 'skip' spelled another way"):
        V.skip_list(v)


def test_the_write_path_names_writable_folders_when_skip_is_declared(tmp_path: Path) -> None:
    v = _vault(tmp_path, 'name = "demo"\ninherits = []\nskip = []\nwriteable_folders = ["Notes"]\n')
    said = _refusal(lambda: _write(v))
    assert ("'writeable_folders' is not a key the manifest defines (did you mean "
            "'writable_folders'?)" in said), said
    assert "An unknown key may be 'writable_folders' spelled another way" in said, said


def test_with_every_key_it_reads_declared_the_write_path_reads_them_and_compose_refuses(
        tmp_path: Path) -> None:
    """No unknown key can change what a declared key says, so the write path, which reads only
    `inherits`, `skip` and `writable_folders`, writes as they say. Compose refuses the manifest."""
    v = _vault(tmp_path, 'name = "demo"\ninherits = []\nskip = []\n'
                         'writable_folders = ["04-synthesis"]\nversion = 2\n')
    assert _write(v).is_file()
    assert "'version' is not a key" in _refusal(lambda: V.resolve_scope(v))


def test_an_inherited_vault_s_unknown_key_stops_a_write_where_it_declares_no_inherits(
        tmp_path: Path) -> None:
    """The project declares every key the write path reads, so only the inherited vault's manifest
    can refuse: its unknown key may be `inherits`, and the vaults below it are then unknown."""
    _vault(tmp_path, 'name = "core"\nversion = 2\n', name="core-vault")
    v = _vault(tmp_path, 'name = "demo"\ninherits = ["core-vault"]\nskip = []\n'
                         'writable_folders = ["04-synthesis"]\n')
    said = _refusal(lambda: _write(v))
    assert "a .substrate.toml in its chain does not read" in said, said
    assert not (v / "04-synthesis" / "new.md").exists()


def test_the_protection_chain_refuses_an_unknown_key_that_may_be_inherits(tmp_path: Path) -> None:
    """For protection only, and lenient about every other key. On the old code a misspelt
    `inherits` read as "inherits nothing", and protection covered the project vault alone."""
    _vault(tmp_path, 'name = "core"\n', name="core-vault")
    v = _vault(tmp_path, 'name = "demo"\ninherit = ["core-vault"]\n')
    said = _refusal(lambda: V.chain_best_effort(v))
    assert "(did you mean 'inherits'?)" in said and "vaults it inherits are unknown" in said, said
    w = _vault(tmp_path, 'name = "other"\ninherits = ["core-vault"]\nversion = 2\n',
               name="other-vault")
    assert V.chain_best_effort(w) == (w.resolve(), (tmp_path / "core-vault").resolve())


# ---------------------------------------------------------------- status and a refresh pass

def test_a_refresh_pass_freezes_the_scope_and_status_says_why(tmp_path: Path) -> None:
    """Composed once, then the manifest gains a misspelt key. On the old code the pass refreshed
    with the default skip list. It now fails the compose, the index is left as it was, the scope
    reads frozen, and both `status` surfaces carry the refusal."""
    vaults = tmp_path / "vaults"
    shutil.copytree(DEMO / "demo-vault", vaults / "demo-vault")
    shutil.copytree(DEMO / "demo-core-vault", vaults / "demo-core-vault")
    vault, registry = vaults / "demo-vault", tmp_path / "scopes.toml"
    db, index_root = tmp_path / "index" / "demo.db", tmp_path / "index" / "demo"
    reg = subprocess.run([sys.executable, "-m", "substrate.cli", "register", str(vault),
                          "--index-root", str(index_root), "--db", str(db),
                          "--registry", str(registry)],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert reg.returncode == 0, reg.stderr
    [first] = refresh.run_pass(registry, log=lambda _: None)
    assert first.outcome in ("refreshed", "embed_failed"), first
    built = db.read_bytes()

    manifest = vault / ".substrate.toml"
    manifest.write_text('skips = ["drafts-VALUEMARK"]\n' + manifest.read_text(encoding="utf-8"),
                        encoding="utf-8")
    logged: list[str] = []
    [result] = refresh.run_pass(registry, log=logged.append)

    # The pass reads why from compose's `refused` record, and compose's stderr goes to the log.
    assert result.outcome == "compose_failed", result
    assert (f"refused: FATAL (manifest): {manifest.resolve()}: 'skips' is not a key the "
            f"manifest defines (did you mean 'skip'?)") in result.detail, result.detail
    assert any("'skips' is not a key" in line for line in logged), logged
    # A key's name is what leaves the file, never its value.
    assert "VALUEMARK" not in result.detail and not any("VALUEMARK" in ln for ln in logged)
    assert db.read_bytes() == built
    assert refresh_state.report("demo", registry)["frozen"] is True

    [row] = introspect.scopes_payload(str(registry))["scopes"]
    assert row["sources"] is None and "'skips'" in row["error"], row
    from substrate import scopes, stack
    from substrate.store.index_store import IndexStore
    entry = scopes.load(str(registry))["demo"]
    with IndexStore(str(db)) as store:
        payload = introspect.status_payload(store, entry, stack=stack.Stack(),
                                            registry=str(registry))
    assert "'skips'" in payload["drift"]["error"], payload["drift"]
    assert payload["refresh"]["frozen"] is True, payload["refresh"]
    assert "VALUEMARK" not in payload["drift"]["error"] and "VALUEMARK" not in row["error"]


# ---------------------------------------------------------------- with quarantine (#22)

TUNING = "02-areas/retrieval-tuning.md"
BAD = "---\nstatus: someday\n---\n\n# A note that fails on its own\n\nBroken body.\n"


def _demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    from substrate import scopes
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    shutil.copytree(DEMO / "demo-vault", tmp_path / "demo-vault")
    shutil.copytree(DEMO / "demo-core-vault", tmp_path / "demo-core-vault")
    return {"vault": tmp_path / "demo-vault", "db": tmp_path / "demo.db",
            "index_root": tmp_path / "demo-index", "registry": tmp_path / "scopes.toml"}


def _compose(w: dict) -> tuple[int, str]:
    import contextlib
    import io
    from substrate import cli
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cli.main(["compose", str(w["vault"]), "--db", str(w["db"]),
                       "--index-root", str(w["index_root"]), "--registry", str(w["registry"])])
    return rc, err.getvalue()


def _first_line(w: dict, line: str) -> None:
    m = w["vault"] / ".substrate.toml"
    m.write_text(line + "\n" + m.read_text(encoding="utf-8"), encoding="utf-8")


@pytest.mark.parametrize("ceiling", ['"none"', "0", "25"])
def test_an_unknown_key_refuses_the_scope_whatever_the_ceiling_and_removes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str) -> None:
    """A whole-scope fault, never a quarantine. An indexed note is broken beside it, so on the old
    code, for a ceiling above 0, the note was quarantined, its ingest folder removed, and the rest
    indexed; for 0 the scope was refused for the ceiling, the key unnamed. The refusal
    now comes before any removal, and the index and every ingest folder are as they were."""
    w = _demo(tmp_path, monkeypatch)
    rc, err = _compose(w)
    assert rc == 0, err
    built, folders = w["db"].read_bytes(), sorted(p.name for p in w["index_root"].iterdir())
    (w["vault"] / TUNING).write_text(BAD, encoding="utf-8")
    _first_line(w, f"quarantine_ceiling = {ceiling}\nversion = 2")
    rc, err = _compose(w)
    assert rc == 2 and "'version' is not a key the manifest defines" in err, (rc, err)
    assert "WARNING (quarantine)" not in err, err
    assert w["db"].read_bytes() == built
    assert sorted(p.name for p in w["index_root"].iterdir()) == folders


def test_a_misspelt_quarantine_ceiling_refuses_and_names_the_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On the old code the default ceiling stood in for `0`: the bad note was quarantined and the
    scope indexed, where the owner had asked for any failure to refuse."""
    w = _demo(tmp_path, monkeypatch)
    (w["vault"] / "02-areas" / "bad.md").write_text(BAD, encoding="utf-8")
    _first_line(w, "quarantine_cieling = 0")
    rc, err = _compose(w)
    assert rc == 2, (rc, err)
    assert ("'quarantine_cieling' is not a key the manifest defines (did you mean "
            "'quarantine_ceiling'?)") in err, err
    assert "WARNING (quarantine)" not in err and not w["db"].exists(), err


def test_a_declared_quarantine_ceiling_is_a_known_key(tmp_path: Path) -> None:
    assert V.resolve_scope(_vault(tmp_path, 'name = "demo"\nquarantine_ceiling = 5\n')).name \
        == "demo"


def test_an_unknown_key_that_may_be_inherits_holds_every_new_index_root_but_the_engine_s(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered scope whose manifest declares no `inherits` and carries an unknown key has
    vaults no one can know, so another scope's compose creates no new index root anywhere but
    directly in the engine's index folder, as with a manifest that does not parse."""
    from substrate import scopes
    w = _demo(tmp_path, monkeypatch)
    rc, err = _compose(w)
    assert rc == 0, err
    m = w["vault"] / ".substrate.toml"
    m.write_text("version = 2\n" + "".join(
        ln for ln in m.read_text(encoding="utf-8").splitlines(keepends=True)
        if not ln.startswith("inherits")), encoding="utf-8")
    other = _vault(tmp_path, 'name = "other"\ninherits = []\n', name="other-vault")

    def compose_other(root: Path) -> tuple[int, str]:
        return _compose({"vault": other, "db": root.parent / "other.db", "index_root": root,
                         "registry": w["registry"]})

    rc, err = compose_other(tmp_path / "elsewhere" / "other-index")
    assert rc == 2 and "FATAL (--index-root)" in err and "'version' is not a key" in err, err
    rc, err = compose_other(scopes.index_home() / "other")
    assert rc == 0, err
