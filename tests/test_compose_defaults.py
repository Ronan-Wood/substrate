"""A note that declares no `status` or no `doc_type` composes with the defaults, and is counted.

Before this, `compose` required both on every note. One note without them failed its ingest, and
compose then refused the whole scope ("refusing to index a partial scope"), so a vault of plain
notes could not compose at all. Now such a note composes as `active` and `reference`, the defaults a
standalone `ingest-md` already used, and `substrate status` reports how many notes were defaulted
on each field. That count is what replaces the refusal: it is how an undeclared note in the live
retrieval set gets noticed, so it is tested through a refresh pass and not only a first compose.

Everything else compose refused, it still refuses. Those tests pass on the engine from before too.
Each test marked "Fails before" fails on that engine. A test marked "Guard" checks that the default
does not open a hole the old refusal closed; see the section that holds them.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from substrate import cli, scopes, spine, stack
from substrate.markdown.ingest import ingest_markdown
from substrate.mcp import server
from substrate.models import Chunk, Document, SourceOrigin
from substrate.store.index_store import IndexStore

_VAULTS = Path(__file__).resolve().parent.parent / "vaults"

# Two demo notes that each declare both fields, and words only the first one holds.
TUNING = "02-areas/retrieval-tuning.md"
INHERITANCE = "02-areas/manifest-inheritance.md"
TUNING_QUERY = "embedders reranking strategies gold set"


class World:
    """A copy of the demo vault pair, with a registry, index and database of its own."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault = root / "demo-vault"
        self.db = root / "demo.db"
        self.index_root = root / "demo-index"
        self.registry = root / "scopes.toml"
        shutil.copytree(_VAULTS / "demo-vault", self.vault)
        shutil.copytree(_VAULTS / "demo-core-vault", root / "demo-core-vault")

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

    def strip(self, note: str, *keys: str) -> Path:
        """Remove each `key: value` line from a note's frontmatter."""
        path = self.vault / note
        text = path.read_text("utf-8")
        for key in keys:
            text, n = re.subn(rf"(?m)^{key}:.*\n", "", text, count=1)
            assert n == 1, f"{note} declares no {key}"
        path.write_text(text, "utf-8")
        return path

    def declare(self, note: str, line: str) -> None:
        """Write `line` as the first line of a note's frontmatter."""
        path = self.vault / note
        text = path.read_text("utf-8")
        assert text.startswith("---\n")
        path.write_text("---\n" + line + "\n" + text[4:], "utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    return World(tmp_path)


def _note(tmp_path: Path, frontmatter: str, name: str = "n.md") -> Path:
    path = tmp_path / name
    block = f"---\n{frontmatter}\n---\n\n" if frontmatter else ""
    path.write_text(f"{block}# A note\n\nBody text of the note.\n", "utf-8")
    return path


# ---------------------------------------------------------------- the four things the issue names

def test_the_demo_vault_composes_with_status_and_doc_type_stripped_from_one_note(world):
    """Fails before: exit 3, "no status", and "refusing to index a partial scope"."""
    world.strip(TUNING, "status", "doc_type")
    rc, out, err = world.compose()
    assert rc == 0, err
    [line] = [ln for ln in out.splitlines() if "retrieval-tuning.md" in ln]
    assert "(active/reference/" in line, line


def test_the_stripped_note_is_retrieved_with_the_defaults(world):
    """Fails before: the scope did not compose, so there was nothing to retrieve."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    # No status flag: `active` is in the default retrieval set, so the note is found by default.
    rc, out, err = world.run("query", "--scope", "demo", "--registry", str(world.registry),
                             "--no-vector", "--json", TUNING_QUERY)
    assert rc == 0, err
    hits = [p for p in json.loads(out)["passages"]
            if p["expand_ref"].startswith("demo/retrieval-tuning#")]
    assert hits, "the note that declares no status was not retrieved"
    assert {(p["status"], p["doc_type"]) for p in hits} == {("active", "reference")}


def test_status_counts_the_notes_defaulted_on_each_field(world):
    """Fails before: the scope did not compose, and `status` had no such field."""
    world.strip(TUNING, "status", "doc_type")
    world.strip(INHERITANCE, "doc_type")
    assert world.compose()[0] == 0
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 2}
    assert ("defaulted: 1 note(s) declare no status (indexed as active) · "
            "2 declare no doc_type (indexed as reference)") in world.status_text()


def test_the_mcp_status_tool_reports_the_count(world):
    """Fails before. Through the server's own dispatcher, as a client calls it."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    cfg = server.Config(str(world.registry), stack.build(lexical_only=True), read_only=True)
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "status", "arguments": {"scope": "demo"}}}, cfg)
    body = reply["result"]
    assert not body.get("isError"), body
    assert json.loads(body["content"][0]["text"])["defaulted"] == {"status": 1, "doc_type": 1}


def test_a_note_with_an_unknown_status_still_refuses_compose(world):
    world.strip(TUNING, "status")
    world.declare(TUNING, "status: someday")
    rc, _, err = world.compose()
    assert rc == 3, err
    assert "retrieval-tuning.md" in err and "status 'someday' is not one of" in err
    assert "refusing to index a partial scope" in err
    assert not world.db.exists(), "a refused compose wrote an index"


# ---------------------------------------------------------------- the count is kept

def test_a_vault_that_declares_every_field_reports_zero(world):
    """Fails before: `status` had no `defaulted` field. A reference passage has no frontmatter of
    its own and takes its status and doc_type from its source's `_meta.md`, so it is not counted."""
    passage = (world.root / "demo-core-vault" / "10-reference" / "frozen" / "software-dev"
               / "ddia-2e" / "passages" / "00-partitioning.md")
    assert not passage.read_text("utf-8").startswith("---"), "the fixture now has frontmatter"
    assert world.compose()[0] == 0
    assert world.status()["defaulted"] == {"status": 0, "doc_type": 0}
    assert "defaulted: 0 note(s) declare no status" in world.status_text()


def test_the_count_survives_a_second_compose_that_changes_nothing(world):
    """Fails before. The second compose re-indexes no note, so the count must be one the index
    keeps, not one the compose that wrote it printed."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    rc, out, err = world.compose()
    assert rc == 0, err
    assert "added 0 · updated 0 ·" in out, out
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}


def test_the_count_survives_a_refresh_pass(world):
    """Fails before. The pass is the real one: `substrate refresh` composes each registered scope
    in a subprocess."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    before = world.db.stat().st_mtime_ns
    rc, _, err = world.run("refresh", "--registry", str(world.registry))
    # Embed needs a local model server, which CI does not have; either way the compose ran.
    assert rc in (0, 1), err
    assert "demo: compose_failed" not in err, err
    assert world.db.stat().st_mtime_ns > before, "the pass did not compose"
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}


def test_a_refresh_pass_counts_a_note_stripped_since_the_last_compose(world):
    """Fails before: the pass recorded `compose_failed` and the scope froze."""
    assert world.compose()[0] == 0
    world.strip(TUNING, "status")
    rc, _, err = world.run("refresh", "--registry", str(world.registry))
    assert rc in (0, 1), err
    assert "demo: compose_failed" not in err, err
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 0}


def test_declaring_the_field_takes_the_note_out_of_the_count(world):
    """Fails before."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    world.declare(TUNING, "status: active")
    assert world.compose()[0] == 0
    assert world.status()["defaulted"] == {"status": 0, "doc_type": 1}


def test_a_deleted_note_leaves_the_count(world):
    """Fails before."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    (world.vault / TUNING).unlink()
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["defaulted"] == {"status": 0, "doc_type": 0}


def test_a_note_with_no_frontmatter_at_all_composes_and_is_counted(world):
    """Fails before. The plain note a vault outside this project holds."""
    (world.vault / "02-areas" / "plain.md").write_text(
        "# A plain note\n\nNothing above the heading, as most notes are written.\n", "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}


def test_an_undeclared_note_under_sources_is_a_note_like_any_other(world, tmp_path):
    """Fails before: it froze the scope. DECIDED, so pinned: withholding follows the class a note
    declares, never its folder. A file with no frontmatter under `_sources/` is `unclassified` and
    `active`, a search with no flags returns it, and an agent snapshot carries its text. A change
    to any of that should be made on purpose (docs/security.md, "Calls are withheld by default")."""
    from substrate import agent

    (world.vault / "_sources" / "undeclared.md").write_text(
        "# Undeclared\n\nZqxjundeclared was said here, in a file nobody classified.\n", "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}
    rc, out, err = world.run("query", "--scope", "demo", "--registry", str(world.registry),
                             "--no-vector", "--json", "Zqxjundeclared")
    assert rc == 0, err
    [hit] = json.loads(out)["passages"]
    assert (hit["document_class"], hit["status"]) == ("unclassified", "active")
    shared = tmp_path / "shared"
    shared.mkdir()
    agent.publish("demo", shared, registry=world.registry)
    assert b"Zqxjundeclared" in (shared / "demo.snapshot.db").read_bytes()
    # The declared call beside it is still left out.
    assert b"engine-boundary-call" not in (shared / "demo.snapshot.db").read_bytes()


# ---------------------------------------------------------------- an empty value

@pytest.mark.parametrize("line", ["status:", 'status: ""', "status:   "])
def test_an_empty_status_is_read_as_absent_so_it_is_defaulted_and_counted(world, line):
    """Fails before: refused as "no status". The reader has always read an empty value as no
    value, so the note is the same as one with no `status` line: it was refused as one before,
    and is defaulted as one now."""
    world.strip(TUNING, "status")
    world.declare(TUNING, line)
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 0}


@pytest.mark.parametrize("line, shown", [('status: "  "', "'  '"), ("status: ~", "'~'"),
                                         ("status: null", "'null'")])
def test_a_quoted_blank_or_a_yaml_null_is_a_declared_value_and_still_refuses(world, line, shown):
    """The reader keeps these as the text written, so each is a value outside the vocabulary."""
    world.strip(TUNING, "status")
    world.declare(TUNING, line)
    rc, _, err = world.compose()
    assert rc == 3, err
    assert f"status {shown} is not one of" in err


# ---------------------------------------------------------------- what still refuses

def _unknown_doc_type(world: World) -> None:
    world.strip(INHERITANCE, "doc_type")
    world.declare(INHERITANCE, "doc_type: tutorial")


def _superseded_with_no_link(world: World) -> None:
    world.strip(INHERITANCE, "status")
    world.declare(INHERITANCE, "status: superseded")


def _not_yaml(world: World) -> None:
    world.declare(INHERITANCE, "We met and agreed.")


def _not_utf8(world: World) -> None:
    (world.vault / "02-areas" / "latin1.md").write_bytes(b"# Caf\xe9\n\nNot UTF-8.\n")


def _duplicate_doc_id(world: World) -> None:
    world.strip(INHERITANCE, "doc_id")
    world.declare(INHERITANCE, "doc_id: retrieval-tuning")


def _missing_vault(world: World) -> None:
    shutil.rmtree(world.root / "demo-core-vault")


STILL_REFUSED = [
    ("an unknown doc_type", _unknown_doc_type, 3, "doc_type 'tutorial' is not one of"),
    ("superseded with no superseded_by", _superseded_with_no_link, 3,
     "status 'superseded' but no 'superseded_by'"),
    ("frontmatter that is not YAML and names a key the engine reads", _not_yaml, 3, "is not YAML"),
    ("a note that is not UTF-8", _not_utf8, 2, "latin1.md: 'utf-8' codec can't decode"),
    ("a doc_id two notes claim", _duplicate_doc_id, 2, "retrieval-tuning"),
    ("a declared vault that is not there", _missing_vault, 2, "demo-core-vault"),
]


@pytest.mark.parametrize("what, break_it, code, says", STILL_REFUSED, ids=[c[0] for c in STILL_REFUSED])
def test_compose_still_refuses(world, what, break_it, code, says):
    """Beside a note that declares no status, which now composes: the leniency for an absent field
    hides none of these."""
    world.strip(TUNING, "status", "doc_type")
    break_it(world)
    rc, _, err = world.compose()
    assert rc == code, (what, err)
    assert says in err, (what, err)
    assert not world.db.exists(), f"compose refused over {what} and still wrote an index"


# ---------------------------------------------------------------- where a default must not stand in
#
# The rule (docs/configuration.md, "When a default doesn't apply"):
#   * the engine reads `status`, `doc_type`, `class`, `document_class` and `superseded_by` under that
#     exact spelling only. A key with the same `reader.canonical_forms` spelled otherwise refuses the
#     note, whatever else it declares (`spine.EXACT_SPELLING`);
#   * a note takes a default only when it has no opening `---` block, or the reader took that block
#     whole: every key a plain key (`reader.plain_key`). Otherwise it refuses, whatever the block
#     names.
# THE SCOPE (docs/security.md): mistakes people and editors make. Not a key disguised on purpose.
#
# "Guard" below marks a test that `09d1744` also refuses, as "no status", so it fails there only on
# the wording. Each guard test fails on this change with its guard taken out.

CALL = "_sources/engine-boundary-call.md"
ARCHIVED = "_archive/phase0-spike-notes.md"
SUPERSEDED = "04-synthesis/engine-boundary-old.md"
META = Path("demo-core-vault/10-reference/frozen/software-dev/ddia-2e/_meta.md")
PASSAGE = Path("demo-core-vault/10-reference/frozen/software-dev/ddia-2e/passages/00-partitioning.md")
NOT_FRONTMATTER = "opens with a `---` line the engine does not read as frontmatter"


def _not_read_as(key: str) -> str:
    return f"which the engine does not read as `{key}`: it reads only `{key}`, spelled exactly so"


def _block(text: str, change) -> str:
    """`text` with `change` applied to the lines of its frontmatter block."""
    head, block, rest = text.split("---\n", 2)
    return head + "---\n" + change(block.splitlines()) + "\n---\n" + rest


def _respell(old: str, new: str):
    def change(text: str) -> str:
        assert f"\n{old}" in text, f"no line starts {old!r}"
        return text.replace(f"\n{old}", f"\n{new}", 1)
    return change


def _refused(world: World, *names: str, says: tuple[str, ...]) -> str:
    rc, _, err = world.compose()
    assert rc == 3, err
    assert all(n in err for n in names) and any(s in err for s in says), err
    assert "refusing to index a partial scope" in err
    assert not world.db.exists(), "a refused compose wrote an index"
    return err


UNREAD_BLOCKS = [
    ("a byte-order mark before it", lambda t: "\ufeff" + t),
    ("a blank line before it", lambda t: "\n" + t),
    ("a no-break space on a line before it", lambda t: "\u00a0\n" + t),
    ("a zero-width space before it", lambda t: "\u200b" + t),
    ("a left-to-right mark before it", lambda t: "\u200e" + t),
    ("a soft hyphen before it", lambda t: "\u00ad" + t),
    ("an unassigned invisible code point before it", lambda t: "\u2065" + t),
    ("a left-to-right mark after the dashes", lambda t: t.replace("---\n", "---\u200e\n", 1)),
    ("spaces after the opening line", lambda t: t.replace("---\n", "--- \n", 1)),
    ("a `...` line closing it", lambda t: t.replace("\n---\n", "\n...\n", 1)),
    ("no closing line", lambda t: t.replace("\n---\n", "\n", 1)),
    ("no closing line, and a blank line inside it",
     lambda t: t.replace("\n---\n", "\n", 1).replace("\ntitle:", "\n\ntitle:", 1)),
    ("its keys written as a list", lambda t: _block(t, lambda ls: "\n".join("- " + ln for ln in ls))),
    ("its keys inside one `{...}`", lambda t: _block(t, lambda ls: "{" + ", ".join(ls) + "}")),
    ("a YAML tag on each key", lambda t: _block(t, lambda ls: "\n".join("!!str " + ln for ln in ls))),
    ("an anchor on each key", lambda t: _block(t, lambda ls: "\n".join(
        f"&a{i} {ln}" for i, ln in enumerate(ls)))),
    ("smart dashes for its fences", lambda t: t.replace("---\n", "\u2014-\n", 2)),
    ("an em dash for its fences", lambda t: t.replace("---\n", "\u2014\n", 2)),
    ("an en dash and a hyphen for its fences", lambda t: t.replace("---\n", "\u2013-\n", 2)),
]
_IDS = [c[0] for c in UNREAD_BLOCKS]


@pytest.mark.parametrize("note", [CALL, ARCHIVED])
@pytest.mark.parametrize("what, damage", UNREAD_BLOCKS, ids=_IDS)
def test_a_note_whose_opening_block_was_not_read_refuses(world, note, what, damage):
    """Guard. On the default alone the note composed as `active`, the call as `unclassified`, and
    a search with no flags returned it."""
    path = world.vault / note
    path.write_text(damage(path.read_text("utf-8")), "utf-8")
    _refused(world, Path(note).name, says=(NOT_FRONTMATTER,))


@pytest.mark.parametrize("what, damage", UNREAD_BLOCKS, ids=_IDS)
def test_a_meta_md_whose_opening_block_was_not_read_refuses_its_passages(world, what, damage):
    """Guard. A passage has no frontmatter of its own, so with its source's `_meta.md` unread it
    composed as `active` with no class, whatever the source declared. The vault layer already
    refuses some of these shapes in `_meta.md`; the rest refuse here."""
    meta = world.root / META
    meta.write_text(damage(meta.read_text("utf-8")), "utf-8")
    rc, _, err = world.compose()
    assert rc in (2, 3), err
    assert "_meta.md" in err, err
    assert not world.db.exists(), "a refused compose wrote an index"


# Every spelling a review found, and the mistakes people and editors make: case, `-` for `_`,
# quotes, a space, a character that renders as nothing.
SPELLINGS = [
    ("Class", "class"), ("CLASS", "class"), ('"class"', "class"), ("'class'", "class"),
    ("\u200bclass", "class"), ("cl\u200bass", "class"), ("cl\u034fass", "class"),
    ("class\ufe0f", "class"), ("\u3164class", "class"), ("cla\u2065ss", "class"),
    ("document-class", "document_class"), ("Document_Class", "document_class"),
    ("DocumentClass", "document_class"),
    # `Status: archived` is the owner's case: never indexed as live.
    ("Status", "status"), ("STATUS", "status"), ("'status'", "status"), ("\u200bstatus", "status"),
    ("Doc_Type", "doc_type"), ("doc-type", "doc_type"), ("docType", "doc_type"),
    ("DOC TYPE", "doc_type"), ("DocType", "doc_type"), ("doc__type", "doc_type"),
    # Caps lock left on, or one letter capitalised by mistake.
    ("cLASS", "class"), ("sTATUS", "status"), ("StaTus", "status"), ("DoC_type", "doc_type"),
    # A character that renders as nothing, outside a space.
    ("\u200b status", "status"),
]
_ENGINE_KEY_LINE = {"class": "class:", "document_class": "class:", "status": "status:",
                    "doc_type": "doc_type:"}


@pytest.mark.parametrize("spelling, key", SPELLINGS, ids=[s for s, _ in SPELLINGS])
def test_an_engine_key_spelled_another_way_refuses_naming_it(world, spelling, key):
    """Fails before for the class spellings: the demo call written `Class:` composed as
    `unclassified` and a search with no flags returned it. Fails before for the others too,
    refused as "no status" / "no doc_type", which named no key. DECIDED: exact spelling only;
    the message names the note, the key as written and the spelling to use."""
    note = CALL if key in ("class", "document_class") else ARCHIVED
    path = world.vault / note
    path.write_text(_respell(_ENGINE_KEY_LINE[key], f"{spelling}:")(path.read_text("utf-8")),
                    "utf-8")
    err = _refused(world, Path(note).name, says=(_not_read_as(key),))
    from substrate.markdown import reader

    assert f"writes `{reader._shown(spelling)}`" in err and f"Write `{key}:`" in err, err


def test_a_property_of_the_vault_s_own_named_status_refuses_beside_the_real_key(world):
    """DECIDED: a vault that keeps a `Status` property of its own has those notes refused, even
    when the note also declares `status` (engine issue #53 decides how such a vault is served)."""
    world.declare(TUNING, "Status: in progress")
    _refused(world, "retrieval-tuning.md", says=(_not_read_as("status"),))


def test_a_class_written_twice_refuses_a_note_that_takes_a_default(world):
    """Guard (09d1744 refused it as "no status"). The line reader keeps the last value, YAML and
    Obsidian refuse the block: neither is what the note says. Seen after a template is inserted
    into a note that already has frontmatter."""
    (world.vault / "_sources" / "twice.md").write_text(
        "---\nclass: conversation\nclass: reference-frozen\n---\n\n# Twice\n\nSaid.\n", "utf-8")
    _refused(world, "twice.md", says=("`class` is written more than once",))


def test_a_class_written_only_as_an_inline_field_is_body_text(world):
    """Fails before ("no status"). DECIDED, so pinned: the engine reads a note's spine and class
    only from its YAML frontmatter. `class:: conversation` in the body is body text, whatever
    Dataview or Logseq make of it, so the note is `unclassified` and takes the defaults, as any
    note that declares no class does (docs/security.md)."""
    (world.vault / "_sources" / "logseq.md").write_text(
        "class:: conversation\nstatus:: archived\n\n# Logseq\n\nZqxjlogseq was said.\n", "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}
    rc, out, err = world.run("query", "--scope", "demo", "--registry", str(world.registry),
                             "--no-vector", "--json", "Zqxjlogseq")
    assert rc == 0, err
    [hit] = json.loads(out)["passages"]
    assert (hit["document_class"], hit["status"]) == ("unclassified", "active")


def test_a_superseded_by_spelled_another_way_refuses_naming_it(world):
    """Fails before only on the message: 09d1744 refused it as a superseded note with no link,
    which did not name the key it had not read."""
    path = world.vault / SUPERSEDED
    path.write_text(_respell("superseded_by:", "superseded-by:")(path.read_text("utf-8")), "utf-8")
    err = _refused(world, "engine-boundary-old.md", says=(_not_read_as("superseded_by"),))
    assert "writes `superseded-by`" in err


def test_an_engine_key_spelled_another_way_in_a_meta_md_refuses_its_passages(world):
    """Fails before: the passage composed with no class."""
    meta = world.root / META
    meta.write_text(_respell("class:", "Class:")(meta.read_text("utf-8")), "utf-8")
    _refused(world, "_meta.md", says=(_not_read_as("class"),))


def test_a_passage_s_own_spelling_refuses_when_its_source_gives_the_value(world):
    """Fails before: the passage composed as `active` and nothing said so."""
    passage = world.root / PASSAGE
    passage.write_text("---\nStatus: archived\n---\n\n" + passage.read_text("utf-8"), "utf-8")
    _refused(world, "00-partitioning.md", says=(_not_read_as("status"),))


YAML_KEYS = [
    ("a YAML tag on the value", "Class: !!str conversation", "class"),
    ("an alias", "Class: *c", "class"),
    ("written twice", "Class: conversation\nClass: conversation", "class"),
    ("a merge key", "<<: {Class: conversation}", "class"),
]


@pytest.mark.parametrize("what, line, key", YAML_KEYS, ids=[c[0] for c in YAML_KEYS])
def test_a_key_yaml_leaves_out_is_still_compared(world, what, line, key):
    """Guard (09d1744 refused these as "no status"). In a block read by YAML, a value the reader
    cannot read is left out of what it reads, and its key is still compared."""
    (world.vault / "_sources" / "tagged.md").write_text(
        f"---\nanchor: &c conversation\ntags:\n  - call\n{line}\n---\n\n# Tagged\n\nSaid.\n",
        "utf-8")
    _refused(world, "tagged.md", says=(_not_read_as(key),))


def test_a_tagged_engine_key_in_a_yaml_block_refuses(world):
    """Guard (09d1744 refused it as "no status"). YAML reads `!!str class:` as `class`; the reader
    now names it by its text, so the strict read refuses it as a key it cannot take."""
    (world.vault / "_sources" / "tagged.md").write_text(
        "---\ntags:\n  - call\n!!str class: conversation\n---\n\n# Tagged\n\nSaid.\n", "utf-8")
    _refused(world, "tagged.md", says=("`class`, line 4: a key written with a YAML tag",))


def test_keys_behind_an_empty_looking_opening_are_still_read_and_compared(world):
    """Guard (09d1744 refused it as "no status"). `---\\n---\\n` followed by keys is frontmatter to
    the reader, so its keys are compared, not taken for an empty block."""
    (world.vault / "_sources" / "double.md").write_text(
        "---\n---\nClass: conversation\n---\n\n# Double\n\nSaid.\n", "utf-8")
    _refused(world, "double.md", says=(_not_read_as("class"),))


ORDINARY_KEYS = ["автор", "серия", "дата_изменения", "είδος", "ステータス", "创建时间戳",
                 "Date created", "related-notes", "Rating (1-5)", "aliases", "cssclasses", "tags",
                 "Größe", "Título", "Klaße", "statuses", "classes", "doc", "state", "stage",
                 # Pandoc and Quarto's document class, Marp's spot class, a run-together doctype.
                 "documentclass", "_class", "doctype", "-1", "-x", "2024",
                 # App keys that extend an engine key in camelCase or with a word of their own.
                 "statusCode", "className", "classList", "docTypeId", "subStatus", "reClass",
                 "Status Code", "DocumentType"]


@pytest.mark.parametrize("key", ORDINARY_KEYS)
def test_an_ordinary_property_composes_beside_a_full_spine_and_on_defaults(world, key):
    """Fails before only for the defaults half ("no status"). Earlier rounds of this design
    refused the non-Latin ones as look-alikes of `class`, and `documentclass` and `_class` as
    spellings of the class keys; neither rule is left. Beside a full spine the note composes as
    declared, as on 09d1744; with no spine it takes the defaults."""
    world.declare(TUNING, f"{key}: value")
    (world.vault / "02-areas" / "plain.md").write_text(
        f"---\n{key}: value\n---\n\n# Plain\n\nBody text.\n", "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, (key, err)
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}


PLAIN_NOTES = [
    # Composes: a key under another key is that key's, not the note's.
    ("a key the engine reads under a key it does not",
     "---\ntags: [a]\nplugin:\n  version: 2\n  status: on\n---\n\n# Plugin\n\nBody text.\n", None),
    # Composes: an empty block, which can hide nothing.
    ("an empty `---`/`---` block", "---\n---\n\n# Empty\n\nBody text.\n", None),
    # Composes: a key YAML reads that starts with `-`, which is a key there.
    ("a YAML key that starts with a dash", "---\ntags:\n  - a\n-x: 1\n---\n\n# Dash\n\nBody.\n",
     None),
    # Composes: a list of mappings written as YAML writes it, read by YAML.
    ("a list of mappings with an indented line",
     "---\ntasks:\n- title: Draft\n  done: false\n---\n\n# Tasks\n\nBody text.\n", None),
    # Composes: lists written without indenting them, as `yaml.safe_dump` writes them.
    ("a list of mappings with no indented line",
     "---\nmilestones:\n- status: done\n---\n\n# Milestones\n\nBody text.\n", None),
    ("a list of URLs with no indented line",
     "---\nsource:\n- https://example.com/a\n---\n\n# Sources\n\nBody text.\n", None),
    ("a list of times with no indented line",
     "---\ntimes:\n- 09:30\n- 14:00\n---\n\n# Times\n\nBody text.\n", None),
    ("a list of quoted values holding a colon",
     "---\naliases:\n- 'Foo: Bar'\n---\n\n# Aliases\n\nBody text.\n", None),
    ("a quoted key beside a list with no indented line",
     "---\n\"date created\": 2024\nsource:\n- https://a.example\n---\n\n# Q\n\nBody text.\n", None),
    # Composes: keys outside the spine may read differently to YAML (a `#` in a title, a list
    # spelled with quotes) without the note being taken for something it is not.
    ("a title holding a `#` beside a list",
     "---\ntitle: Bug #42 in parser\nsource:\n- https://x.org/a\n---\n\n# Bug\n\nBody.\n", None),
    ("domains spelled with quotes beside a list",
     '---\ndomains: ["retrieval", "search"]\nsource:\n- https://x.org/a\n---\n\n# D\n\nBody.\n',
     None),
    ("domains with no spaces beside a list",
     "---\ndomains: [retrieval,search]\nsource:\n- https://x.org/a\n---\n\n# D\n\nBody.\n", None),
    # Refuses, said: the line reader reads `status` as empty, YAML as a list. Neither the default
    # nor the list may stand in for what the note meant.
    ("a spine key whose value YAML reads as a list",
     "---\nstatus:\n- archived # was: x\n---\n\n# Was\n\nBody text.\n",
     "`status` reads as `` to the engine and as `[archived]` to YAML"),
    ("a class whose value YAML reads as a list",
     "---\nclass:\n- conversation # see: x\n---\n\n# See\n\nBody text.\n",
     "`class` reads as `` to the engine and as `[conversation]` to YAML"),
    # Refuses, said: YAML reads these as a key the line reader did not take, or not at all.
    ("a flow mapping", "---\n{status: archived}\n---\n\n# Flow\n\nBody text.\n",
     "YAML reads `status` as a key, and the engine's reader does not"),
    ("an anchored key", "---\n&a status: archived\n---\n\n# Anchor\n\nBody text.\n",
     "YAML reads `status` as a key, and the engine's reader does not"),
    # Refuses, as on `09d1744`: a `---` that opens a note and is not frontmatter.
    ("a rule, a line and a rule", "---\nAgenda for the week\n---\n\n# Ruled\n\nBody text.\n",
     NOT_FRONTMATTER),
    ("a byte-order mark and a rule", "\ufeff---\nAgenda for the week\n---\n\n# Ruled\n\nBody.\n",
     NOT_FRONTMATTER),
    ("an opening rule with a later rule and a list in the body",
     "---\nNotes from the week.\n\n# Week\n\n- status: on track\n\n---\n\nMore.\n", NOT_FRONTMATTER),
    ("an opening rule that never closes",
     "---\nNotes from the week.\n\n# Week\n\n- status: on track\n", NOT_FRONTMATTER),
    ("a longer rule", "-----\n\n# Ruled\n\nBody text.\n", NOT_FRONTMATTER),
]


@pytest.mark.parametrize("what, text, refuses", PLAIN_NOTES, ids=[c[0] for c in PLAIN_NOTES])
def test_a_note_that_declares_no_spine_composes_or_refuses_for_a_stated_reason(world, what, text,
                                                                               refuses):
    """The ones that compose fail before ("no status"). The ones that refuse are guards: they
    refused before too, as "no status", and now say what is wrong."""
    (world.vault / "02-areas" / "plain.md").write_text(text, "utf-8")
    if refuses is None:
        rc, _, err = world.compose()
        assert rc == 0, (what, err)
        assert world.status()["defaulted"] == {"status": 1, "doc_type": 1}
    else:
        _refused(world, "plain.md", says=(refuses,))


@pytest.mark.parametrize("text", [
    "# Ruled\n\nAbove the rule.\n\n---\n\nBelow it.\n",
    "---\ntags: [a]\n---\n\n# Ruled\n\nAbove the rule.\n\n---\n\nBelow it.\n",
])
def test_a_rule_below_the_first_line_is_not_an_opening_block(world, text):
    """Fails before ("no status"). Only the file's first text decides: a rule further down, below
    frontmatter or without any, is body text."""
    (world.vault / "02-areas" / "plain.md").write_text(text, "utf-8")
    rc, _, err = world.compose()
    assert rc == 0, err


def test_a_meta_md_outside_the_vault_is_never_read(world, tmp_path):
    """A vault that sits under a folder named `passages` must not be judged by an `_meta.md`
    above it. The confirming review made compose refuse an in-vault note that way."""
    nest = tmp_path / "passages"
    nest.mkdir()
    for name in ("demo-vault", "demo-core-vault"):
        shutil.move(str(world.root / name), str(nest / name))
    world.vault = nest / "demo-vault"
    (tmp_path / "_meta.md").write_text("\ufeff---\ntitle: outside\n---\n", "utf-8")
    world.strip(TUNING, "status", "doc_type")
    rc, _, err = world.compose()
    assert rc == 0, err


def test_the_write_path_still_requires_both_fields(tmp_path):
    """`notes.py` passes `require_status` for a note being written into a vault, and that gate is
    not this change's to move."""
    for frontmatter, says in (("doc_type: reference", "no status"), ("status: active", "no doc_type")):
        with pytest.raises(spine.SpineError, match=says):
            ingest_markdown(_note(tmp_path, frontmatter), tmp_path / "out", require_status=True)


# ---------------------------------------------------------------- what ingest records

def test_ingest_names_the_fields_it_defaulted_in_run_json(tmp_path):
    """Fails before: nothing said which notes were defaulted."""
    r = ingest_markdown(_note(tmp_path, ""), tmp_path / "out")
    assert (r.status, r.doc_type) == ("active", "reference")
    assert r.run["spine"]["defaulted"] == ["status", "doc_type"]
    run = json.loads((tmp_path / "out" / "run.json").read_text("utf-8"))
    assert run["spine"]["defaulted"] == ["status", "doc_type"]

    r = ingest_markdown(_note(tmp_path, "status: complete", "s.md"), tmp_path / "out-s")
    assert (r.status, r.run["spine"]["defaulted"]) == ("complete", ["doc_type"])


def test_a_note_that_declares_both_writes_the_spine_block_it_always_did(tmp_path):
    """`reconcile` hashes the spine block into its diff key. A key added for every note would
    re-index every note already indexed, and re-indexing a note drops its vectors."""
    r = ingest_markdown(_note(tmp_path, "status: active\ndoc_type: decision"), tmp_path / "out")
    assert "defaulted" not in r.run["spine"]


def test_a_value_inherited_from_meta_md_is_not_a_default(tmp_path):
    r = ingest_markdown(_note(tmp_path, ""), tmp_path / "out", override_status="complete",
                        override_doc_type="reference")
    assert r.status == "complete" and "defaulted" not in r.run["spine"]


def test_a_file_handed_to_ingest_is_not_recorded_or_judged(tmp_path):
    """Its spine is cleared because it is not the file's to declare, so there is no declaration
    to be missing, and its `run.json` is the one it always wrote. A file handed to `ingest` that
    opens with a byte-order mark or writes `Class:` is not refused for it."""
    for i, text in enumerate(("---\nstatus: archived\n---\n", "---\nClass: conversation\n---\n",
                              "\ufeff---\nstatus: archived\n---\n")):
        path = tmp_path / f"f{i}.md"
        path.write_text(text + "\n# A file\n\nBody text of the file.\n", "utf-8")
        r = ingest_markdown(path, tmp_path / f"out{i}", origin=SourceOrigin(source_format="markdown"))
        assert (r.status, r.doc_type) == ("active", "reference")
        assert "defaulted" not in r.run["spine"]


def test_a_note_that_declares_both_is_not_refused_for_its_opening(tmp_path):
    """The structural refusal is for a note that would take a default. A note that declares its
    status and doc_type composes as it always did, whatever else its block holds."""
    for extra in ("plugin:\n  version: 2", "- class: a list item, not a key"):
        r = ingest_markdown(_note(tmp_path, f"status: active\ndoc_type: decision\n{extra}"),
                            tmp_path / "out")
        assert (r.status, r.doc_type) == ("active", "decision")


def test_only_the_known_fields_are_read_back_from_a_run_json():
    assert spine.defaulted_fields(["doc_type", "status", "confidence", 7]) == ["status", "doc_type"]
    for junk in (None, "status", {"status": True}, 3):
        assert spine.defaulted_fields(junk) == []


def test_the_key_form_ignores_case_quotes_separators_and_invisible_characters():
    from substrate.markdown import reader

    for k in ("doc_type", "Doc-Type", "DOC TYPE", "docType", "DocType", "doc__type", '"doc_type"',
              "'Doc_Type'", "doc_\u200btype", "doc\u034f_type", "doc_\u3164type", "doc_\u2065type",
              "doc_type\ufe0f", "Doc\u00a0Type", "DoC_type", "\u200b doc_type", "doc_type \u200b"):
        assert "doc_type" in reader.canonical_forms(k), k
    assert "class" in reader.canonical_forms("cLASS") and "status" in reader.canonical_forms("StaTus")
    # Separators are normalised, not deleted, so a run-together word keeps a form of its own.
    for other in ("doctype", "DOCTYPE", "documentclass", "_class", "class_", "statusCode",
                  "className", "classList", "docTypeId", "subStatus", "reClass", "Status Code",
                  "DocumentType"):
        assert not reader.canonical_forms(other) & {"doc_type", "document_class", "class"}, other
    # No look-alike matching: a key in another script is itself.
    assert "class" not in reader.canonical_forms("\u0441lass")
    assert reader.canonical_forms("автор") == {"автор"}


def test_plain_keys_are_what_yaml_would_read_as_keys():
    from substrate.markdown import reader

    for k in ("status", "Date created", "related-notes", "Rating (1-5)", "автор", "创建时间戳",
              "\u200bclass", '"class"', "a#b", "x-", "-x", "-1", "?x", ":x"):
        assert reader.plain_key(k), k
    for k in ("- status", "{status", "[a", "? key", "!!str status", "&a status", "*a", "| a",
              "> a", "%TAG", "@a", "`a", "#c", "<<", "a #b", '"a"b"', "-", ": x"):
        assert not reader.plain_key(k), k


# ---------------------------------------------------------------- the index

def _seed(store: IndexStore, doc_id: str, defaulted: list[str] | None = None) -> None:
    doc = Document(doc_id=doc_id, source_path=f"/{doc_id}.md", source_sha256="s" * 8,
                   source_pages=1, document_class="unclassified", title=doc_id, status="active",
                   doc_type="reference")
    chunk = Chunk(chunk_id=f"{doc_id}#c0", doc_id=doc_id, kind="passage", text="body",
                  path=[doc_id], level=1, document_class="unclassified")
    store.upsert(doc, [chunk], markdown_path=f"/{doc_id}.md", markdown_mtime=0.0,
                 markdown_sha256="m" * 8, defaulted=defaulted)


def test_the_index_counts_per_field_and_forgets_a_note_it_no_longer_holds(tmp_path):
    """Fails before: the index kept no record."""
    with IndexStore(tmp_path / "i.db") as store:
        assert store.defaulted_counts() == {"status": 0, "doc_type": 0}
        _seed(store, "a", ["status", "doc_type"])
        _seed(store, "b", ["doc_type"])
        _seed(store, "c")
        assert store.defaulted_counts() == {"status": 1, "doc_type": 2}
        _seed(store, "a", ["doc_type"])           # re-indexed, now declaring a status
        assert store.defaulted_counts() == {"status": 0, "doc_type": 2}
        store.remove("b")
        assert store.defaulted_counts() == {"status": 0, "doc_type": 1}
        store.clear()
        assert store.defaulted_counts() == {"status": 0, "doc_type": 0}


def test_a_compose_puts_back_a_record_an_older_engine_dropped(world):
    """An engine from before the count re-indexes a note without writing its rows, and leaves the
    diff key this engine computes, so every later compose finds the note unchanged."""
    world.strip(TUNING, "status", "doc_type")
    assert world.compose()[0] == 0
    con = sqlite3.connect(world.db)
    con.execute("DELETE FROM spine_defaults")
    con.execute("INSERT INTO spine_defaults(doc_id, field) VALUES ('manifest-inheritance', 'status')")
    con.commit()
    con.close()
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 0}
    rc, out, err = world.compose()
    assert rc == 0, err
    assert "added 0 · updated 0 ·" in out, out
    with IndexStore(str(world.db), migrate=False) as store:
        assert store.defaulted_by_doc() == {"retrieval-tuning": ["status", "doc_type"]}


def _drop_the_table(db: Path) -> None:
    """Make `db` the file an engine from before the count wrote: same schema version, no table."""
    con = sqlite3.connect(db)
    con.execute("DROP TABLE spine_defaults")
    con.commit()
    con.close()


def test_an_index_from_before_the_count_reports_it_as_not_kept_rather_than_zero(world):
    """Fails before. The table arrived without a schema bump, so a read meets files without it."""
    assert world.compose()[0] == 0
    _drop_the_table(world.db)
    with IndexStore(str(world.db), migrate=False) as store:
        assert store.defaulted_counts() is None
    assert world.status()["defaulted"] is None
    assert "defaulted: NOT COUNTED" in world.status_text()
    # The next compose adds the table and counts, defaulting nothing or something.
    assert world.compose()[0] == 0
    assert world.status()["defaulted"] == {"status": 0, "doc_type": 0}
    _drop_the_table(world.db)
    world.strip(TUNING, "status")
    assert world.compose()[0] == 0
    assert world.status()["defaulted"] == {"status": 1, "doc_type": 0}


def test_a_write_to_an_index_from_before_the_count_makes_the_table(tmp_path):
    """`index` opens a current-version file without running the DDL, and may meet a `run.json`
    that names a defaulted field."""
    db = tmp_path / "i.db"
    IndexStore(db).close()
    _drop_the_table(db)
    with IndexStore(db, migrate=False) as store:
        _seed(store, "a", ["status"])
        assert store.defaulted_counts() == {"status": 1, "doc_type": 0}


def test_the_table_is_dropped_with_the_rest_when_the_schema_moves(tmp_path):
    db = tmp_path / "i.db"
    with IndexStore(db) as store:
        _seed(store, "a", ["status"])
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    with IndexStore(db) as store:
        assert store.rebuilt and store.defaulted_counts() == {"status": 0, "doc_type": 0}
