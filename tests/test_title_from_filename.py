"""Issue #55: a note with no `title:` and no heading takes its file name as its title.

Before this, such a note failed the class gate (`missing required field(s) ['title']`). Since #22
that quarantines it, so a vault of daily notes, quick captures, lists and clippings lost each one,
and past the ceiling (by default the larger of 10 notes and 1%) refused the whole scope. Now the
order is a declared `title:`, then the first level-1 heading, then the first heading of any level,
then the file name, and `title_source` in `run.json` records `filename`.

"Fails before" marks a test that fails on `5e389cf` the way the issue describes: the note is
quarantined (compose) or refused (the write path, standalone `ingest-md --md`). The tests of what a
file name becomes fail there only because `classes.title_from_filename` is new. "Guard" marks one
that passes there too: what must not move.

Every file name a test writes is ASCII, or a name that is legal and stored as given on both APFS
and ext4 (spaces, dots, a doubled `.md`). A test about a name's Unicode form, its control
characters or its length works on strings, never on the volume: APFS and ext4 disagree on those.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import unicodedata
from pathlib import Path

import pytest

from substrate import classes, cli, notes, scopes, stack, vault
from substrate.markdown.ingest import ingest_markdown
from substrate.mcp import server

_VAULTS = Path(__file__).resolve().parent.parent / "vaults"

SPINE = "---\nstatus: active\ndoc_type: reference\nconfidence: unstated\n---\n"
DAILY = "02-areas/Daily 2026-11-19.md"
DAILY_BODY = "\nMet about the zqgearbox rollout and the sprocket audit.\n"


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


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

    def write(self, rel: str, text: str) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return path

    def run(self, *argv: str) -> tuple[int, str, str]:
        return _run(*argv)

    def compose(self) -> str:
        rc, out, err = self.run("compose", str(self.vault), "--db", str(self.db),
                                "--index-root", str(self.index_root),
                                "--registry", str(self.registry))
        assert rc == 0, err
        assert "QUARANTINED" not in err, err
        return out

    def status(self) -> dict:
        rc, out, err = self.run("status", "--scope", "demo", "--registry", str(self.registry),
                                "--lexical-only", "--json")
        assert rc == 0, err
        return json.loads(out)

    def query(self, text: str) -> list[dict]:
        rc, out, err = self.run("query", "--scope", "demo", "--registry", str(self.registry),
                                "--no-vector", "--json", text)
        assert rc == 0, err
        return json.loads(out)["passages"]

    def tool(self, name: str, **arguments) -> dict:
        cfg = server.Config(str(self.registry), stack.build(lexical_only=True), read_only=True)
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name,
                                          "arguments": {"scope": "demo", **arguments}}}, cfg)
        body = reply["result"]
        assert not body.get("isError"), body
        return json.loads(body["content"][0]["text"])

    def documents(self) -> dict[str, dict]:
        """Every document the scope holds, by doc_id, as the MCP `documents` tool lists it."""
        return {d["doc_id"]: d for d in self.tool("documents", limit=200)["documents"]}

    def titled(self, title: str) -> list[dict]:
        return [d for d in self.documents().values() if d["title"] == title]

    def run_json(self, rel: str) -> dict:
        """The `run.json` compose wrote for the note at `rel`."""
        [run] = [r for p in self.index_root.glob("*/run.json")
                 if (r := json.loads(p.read_text("utf-8")))["source"].endswith(rel)]
        return run


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    monkeypatch.setattr(scopes, "engine_home", lambda: tmp_path)
    return World(tmp_path)


def _ingest(tmp_path: Path, name: str, text: str) -> dict:
    """`run.json` of one note ingested as compose ingests it: in place, with no origin."""
    src = tmp_path / name
    src.write_text(text, "utf-8")
    return ingest_markdown(src, tmp_path / "out").run


# ---------------------------------------------------------------- what the file name becomes

@pytest.mark.parametrize("name, title", [
    ("Daily 2026-11-19.md", "Daily 2026-11-19"),
    ("README notes.md", "README notes"),              # case as written
    ("a  b.md", "a  b"),                              # inner spaces as written
    ("notes.md.md", "notes.md"),                      # one `.md` comes off, not two
    ("notes.MD", "notes"),                            # a handed-in file: its last suffix
    ("deck.final.pptx", "deck.final"),
    ("v1.2 spec.md", "v1.2 spec"),
    ("...md", ".."),                                  # dots are visible: kept
    ("  Inbox \t.md", "Inbox"),                       # surrounding whitespace removed
    ("a\nb.md", "a b"),                               # a line break is one space
    ("a\r\n\tb.md", "a b"),                           # a run of them, one space
    ("x\x1b[31mred.md", "x [31mred"),                 # an escape cannot reach a terminal
    ("a\x85b\u2028c\u2029d.md", "a b c d"),           # C1 and Unicode line separators
    ("\U0001f468\u200d\U0001f469\u200d\U0001f467 family.md",   # a joiner keeps an emoji whole
     "\U0001f468\u200d\U0001f469\u200d\U0001f467 family"),
    ("\u05e9\u05dc\u05d5\u05dd\u200f.md", "\u05e9\u05dc\u05d5\u05dd\u200f"),  # a direction mark
])
def test_a_file_name_becomes_the_title_as_its_owner_wrote_it(name, title):
    """On strings, not the volume, so it holds on ext4 and APFS alike."""
    assert classes.title_from_filename(name) == title


@pytest.mark.parametrize("name", [
    ".md", "   .md", "\n.md", "\t\r\n .md", "\x00\x1f.md",
    "\u200b.md",                   # a zero-width space
    "\ufeff.md",                   # a byte-order mark
    "\u00ad.md",                   # a soft hyphen
    "\u202e.md",                   # a right-to-left override alone
    "\u3164\u115f.md",             # Hangul fillers
    "\u0301\u0302.md",             # combining marks with nothing to combine with
    "\u00a0\u3000.md",             # no-break and ideographic spaces
])
def test_a_name_with_nothing_visible_gives_no_title(name):
    """Visible as `reader.invisible` judges a key or a fence: white space, format characters,
    combining marks and fillers alone are nothing to show."""
    assert classes.title_from_filename(name) is None


def test_an_nfd_name_and_an_nfc_name_give_one_title():
    """ext4 stores the two forms as two names, APFS and some tools give back NFD, and a query is
    typed NFC. The title is NFC either way."""
    nfc = unicodedata.normalize("NFC", "Caf\u00e9 r\u00e9sum\u00e9.md")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert classes.title_from_filename(nfd) == classes.title_from_filename(nfc) == nfc[:-3]
    assert unicodedata.is_normalized("NFC", classes.title_from_filename(nfd))


def test_a_255_byte_name_is_kept_whole():
    """The longest name either volume stores, as bytes (ext4) and well inside APFS's 255 UTF-16
    units. Nothing cuts a title."""
    name = "\u00e9" * 126 + ".md"
    assert len(name.encode("utf-8")) == 255
    assert classes.title_from_filename(name) == "\u00e9" * 126


def test_compose_does_not_discover_an_upper_case_md(tmp_path):
    """Guard. `.MD` is not a note to compose, on either volume, so its title is never asked for
    there. Discovery matches `*.md` by case on macOS and Linux alike."""
    (tmp_path / "notes.MD").write_text("Body.\n", "utf-8")
    (tmp_path / "kept.md").write_text("Body two.\n", "utf-8")
    ref = vault.VaultRef(name="v", path=tmp_path, skip=vault.DEFAULT_SKIP, keys=vault.engine_keys.DEFAULT)
    assert [n.path.name for n in vault._discover_notes(ref)] == ["kept.md"]


# ---------------------------------------------------------------- the order, one note at a time

def test_a_note_with_no_title_and_no_heading_takes_its_file_name(tmp_path):
    """Fails before: `missing required field(s) ['title']`."""
    run = _ingest(tmp_path, "Quick capture.md", SPINE + "\nA thought about sprockets.\n")
    assert run["class"]["title"] == "Quick capture"
    assert run["extract"]["title_source"] == "filename"


@pytest.mark.parametrize("text, title, source", [
    (SPINE.replace("---\nstatus", "---\ntitle: Declared\nstatus") + "\n# Heading\n\nBody.\n",
     "Declared", "declared"),
    (SPINE.replace("---\nstatus", "---\ntitle: Declared\nstatus") + "\nBody.\n",
     "Declared", "declared"),
    (SPINE + "\n## Second\n\nBody.\n\n# First\n\nMore.\n", "First", "heading"),
    (SPINE + "\n## Second\n\nBody.\n", "Second", "heading"),
    (SPINE + "\n# Only a heading\n", "Only a heading", "heading"),
])
def test_a_declared_title_or_a_heading_keeps_the_title_it_has_today(tmp_path, text, title, source):
    """Guard: the file name is last."""
    run = _ingest(tmp_path, "the-file-name.md", text)
    assert (run["class"]["title"], run["extract"]["title_source"]) == (title, source)


def test_a_versioned_note_still_needs_its_version(tmp_path):
    """Fails before, on its message: the note lacked `['title', 'version']`. The file name stands
    in for a title, never for a version."""
    src = tmp_path / "Spec.md"
    src.write_text("---\nclass: reference-versioned\n---\n\nNo version is stated here.\n", "utf-8")
    with pytest.raises(classes.ClassPolicyError, match=r"missing required field\(s\) \['version'\]"):
        ingest_markdown(src, tmp_path / "out")


def test_a_name_with_nothing_visible_is_still_refused(tmp_path):
    """Guard. No title is better than an invisible one: the note fails the class gate as before,
    which compose quarantines. Legal on ext4 and APFS."""
    src = tmp_path / "   .md"
    src.write_text("Body text.\n", "utf-8")
    with pytest.raises(classes.ClassPolicyError, match=r"\['title'\]"):
        ingest_markdown(src, tmp_path / "out")


# ---------------------------------------------------------------- compose

def test_the_demo_vault_composes_a_heading_less_note_with_nothing_quarantined(world):
    """Fails before: the note is quarantined (`missing_required_field`, `title`)."""
    world.write(DAILY, SPINE + DAILY_BODY)
    world.compose()
    assert world.status()["quarantined"] == []
    [doc] = world.titled("Daily 2026-11-19")
    assert doc["passage_count"] == 1
    assert world.run_json(DAILY)["extract"]["title_source"] == "filename"


def test_a_heading_less_note_is_found_by_its_body_and_shown_by_its_file_name(world):
    """Fails before: nothing to find. The title a client sees is the file name, in the search
    hit's citation and in `documents`."""
    world.write(DAILY, SPINE + DAILY_BODY)
    world.compose()
    hits = [h for h in world.query("zqgearbox rollout sprocket audit")
            if h["expand_ref"].startswith("demo/daily-2026-11-19-")]
    assert hits, "the heading-less note was not found by its body"
    assert {h["citation"].split(" \u00b7 ")[0] for h in hits} == {"Daily 2026-11-19"}
    [mcp_hit] = [h for h in world.tool("search", query="zqgearbox rollout")["passages"]
                 if h["expand_ref"].startswith("demo/daily-2026-11-19-")]
    assert mcp_hit["citation"].startswith("Daily 2026-11-19")


def test_the_file_name_is_not_indexed_text_as_a_declared_title_is_not(world):
    """Fails before: the heading-less note was quarantined, so there was one passage set, not two.
    A title that is not a heading is not in any passage's text, today's rule for `title:`, so
    a file-name title changes no passage and no ranking: the two notes below index the same
    passage text. Querying by a word only the file name holds finds neither (Open in the PR)."""
    world.write("02-areas/zqonlyinname.md", SPINE + "\nBody of the first.\n")
    world.write("02-areas/declared.md",
                SPINE.replace("---\nstatus", "---\ntitle: zqonlyinname\nstatus")
                + "\nBody of the first.\n")
    world.compose()
    passages = {}
    for p in world.index_root.glob("*/chunks.jsonl"):
        rows = [json.loads(line) for line in p.read_text("utf-8").splitlines()]
        if rows and rows[0]["text"] == "Body of the first.":
            passages[p.parent.name] = [r["text_with_path"] for r in rows]
    assert len(passages) == 2 and len({tuple(v) for v in passages.values()}) == 1, passages
    assert not world.query("zqonlyinname")


def test_a_vault_of_heading_less_notes_past_the_ceiling_composes(world):
    """Fails before: 12 of 22 notes quarantined is more than the default ceiling of 10, so the
    whole scope refused. Each note's bytes differ, so none is a byte-identical copy."""
    for i in range(12):
        world.write(f"02-areas/daily/2026-11-{i + 1:02d}.md", f"Captured zqday{i} today.\n")
    world.compose()
    assert world.status()["quarantined"] == []
    for i in range(12):
        assert world.titled(f"2026-11-{i + 1:02d}"), i


def test_two_notes_with_one_file_name_in_two_folders_are_two_documents(world):
    """Fails before: both notes were quarantined. Titles need not be unique, and `doc_id` does not
    come from the title: it is the declared one, else the file name's slug and a hash of the
    note's bytes (`extract/base.doc_id_for`)."""
    world.write("02-areas/Inbox.md", "First inbox, zqalpha.\n")
    world.write("04-synthesis/Inbox.md", "Second inbox, zqbeta.\n")
    world.compose()
    docs = world.titled("Inbox")
    assert len(docs) == 2
    assert len({d["doc_id"] for d in docs}) == 2
    assert all(d["doc_id"].startswith("inbox-") for d in docs)


def test_a_doubled_md_name_composes_titled_with_one_md_off(world):
    """Fails before: quarantined. Legal on ext4 and APFS."""
    world.write("02-areas/notes.md.md", "Doubled zqdoubled suffix.\n")
    world.compose()
    assert world.titled("notes.md")


# ---------------------------------------------------------------- an empty note

@pytest.mark.parametrize("rel, text", [
    ("02-areas/Untitled.md", ""),
    ("02-areas/Placeholder.md", SPINE),
], ids=["an empty file", "frontmatter only"])
def test_an_empty_note_composes_holding_nothing(world, rel, text):
    """Fails before: quarantined for a missing title. Decided: it composes as a heading-only note
    does today, as a document with no passage. `documents` lists it by its file name with
    `passage_count` 0 and no `expand_ref`, `search` never returns it, and nothing is quarantined,
    so it cannot count toward the ceiling or refuse the scope."""
    world.write(rel, text)
    world.compose()
    assert world.status()["quarantined"] == []
    title = Path(rel).stem
    [doc] = world.titled(title)
    assert (doc["passage_count"], doc["expand_ref"]) == (0, None)


def test_a_vault_of_empty_notes_past_the_ceiling_composes(world):
    """Fails before: 12 quarantined notes refused the scope. Their bytes differ (a daily note
    template's date); identical empty notes are `test_identical_notes.py`'s."""
    for i in range(12):
        world.write(f"02-areas/daily/2026-12-{i + 1:02d}.md", f"---\ndate: 2026-12-{i + 1:02d}\n---\n")
    world.compose()
    assert world.status()["quarantined"] == []
    assert all(world.titled(f"2026-12-{i + 1:02d}") for i in range(12))


# ---------------------------------------------------------------- drift and recompose

def test_renaming_a_heading_less_note_moves_its_title(world):
    """Fails before: the note was quarantined. The id is declared, so the rename keeps the
    document and must still move its title: the title is in the class block and the path in the
    source field, both in reconcile's diff key."""
    world.write("02-areas/Old name.md",
                SPINE.replace("---\nstatus", "---\ndoc_id: renamed-note\nstatus") + "\nBody zqr.\n")
    world.compose()
    assert world.documents()["renamed-note"]["title"] == "Old name"
    (world.vault / "02-areas" / "Old name.md").rename(world.vault / "02-areas" / "New name.md")
    drift = world.status()["drift"]
    assert drift["stale"], drift
    world.compose()
    assert world.documents()["renamed-note"]["title"] == "New name"
    assert not world.status()["drift"]["stale"]


def test_the_title_follows_a_note_that_gains_and_loses_a_heading(world):
    """Fails before: without its heading the note was quarantined."""
    rel = "02-areas/Weekly.md"
    head = SPINE.replace("---\nstatus", "---\ndoc_id: weekly-note\nstatus")
    world.write(rel, head + "\nBody zqw.\n")
    world.compose()
    assert world.documents()["weekly-note"]["title"] == "Weekly"
    assert world.run_json(rel)["extract"]["title_source"] == "filename"

    world.write(rel, head + "\n# Week of planning\n\nBody zqw.\n")
    world.compose()
    assert world.documents()["weekly-note"]["title"] == "Week of planning"
    assert world.run_json(rel)["extract"]["title_source"] == "heading"

    world.write(rel, head + "\nBody zqw.\n")
    world.compose()
    assert world.documents()["weekly-note"]["title"] == "Weekly"
    assert world.run_json(rel)["extract"]["title_source"] == "filename"


# ---------------------------------------------------------------- the write path

def test_ingest_accepts_a_heading_less_note_and_titles_it_by_the_name_it_writes(world):
    """Fails before: the plan refused it with `missing required field(s) ['title']`, which compose
    now accepts. The write path judges the note under the file name the plan will write."""
    book = notes.PlanBook()
    content = SPINE.encode() + b"\nA capture written through the tool, zqtool.\n"
    plan = notes.plan(project_vault=world.vault, content=content, filename="Tool capture.md",
                      folder="02-areas", book=book)
    assert plan.passages == 1
    notes.commit(project_vault=world.vault, content=content, filename="Tool capture.md",
                 folder="02-areas", confirm_token=plan.confirm_token, book=book)
    world.compose()
    assert world.titled("Tool capture")


def test_ingest_still_refuses_what_compose_quarantines(world):
    """Guard. The same gate on both sides: a name with nothing visible is refused by the write
    path, as compose quarantines it."""
    with pytest.raises(notes.NoteError, match=r"\['title'\]"):
        notes.plan(project_vault=world.vault, content=SPINE.encode() + b"\nBody.\n",
                   filename="   .md", folder="02-areas", book=notes.PlanBook())


def test_amend_removing_the_only_heading_titles_the_note_by_its_file_name(world):
    """Fails before: the plan refused the edit for a missing title. Now the note's title changes
    from its heading to its file name, which is what removing the heading means."""
    rel = "02-areas/Meeting notes.md"
    before = SPINE + "\n# Kickoff\n\nWe agreed on zqkickoff.\n"
    world.write(rel, before)
    world.compose()
    assert world.titled("Kickoff")
    after = (SPINE + "\nWe agreed on zqkickoff.\n").encode()
    book = notes.PlanBook()
    plan = notes.plan_amend(project_vault=world.vault, path=rel, content=after, book=book)
    notes.commit_amend(project_vault=world.vault, path=rel, content=after,
                       confirm_token=plan.confirm_token, book=book)
    world.compose()
    assert not world.titled("Kickoff")
    [doc] = world.titled("Meeting notes")
    assert world.run_json(rel)["extract"]["title_source"] == "filename"
    assert doc["passage_count"] == 1


# ---------------------------------------------------------------- standalone ingest-md --md

def test_ingest_md_titles_a_heading_less_md_by_its_file_name(tmp_path):
    """Fails before: exit 3, `missing required field(s) ['title']`. A `.md` handed in takes its
    file name as a `.txt`, a converted file and the same note in a vault do."""
    src = tmp_path / "Clipping.md"
    src.write_text("Text clipped from a page, zqclip.\n", "utf-8")
    out = tmp_path / "out"
    rc, stdout, err = _run("ingest-md", "--md", str(src), "--out", str(out))
    assert rc == 0, err
    run = json.loads((out / "run.json").read_text("utf-8"))
    assert (run["class"]["title"], run["extract"]["title_source"]) == ("Clipping", "filename")


def test_ingest_md_still_refuses_an_empty_md_as_unretrievable(tmp_path):
    """Fails before, on its message: the class gate refused it for a missing title first. A
    handed-in file keeps the zero-passage gate the vault path does not have."""
    src = tmp_path / "Empty.md"
    src.write_text("", "utf-8")
    rc, _, err = _run("ingest-md", "--md", str(src), "--out", str(tmp_path / "out"))
    assert rc == 3
    assert "FATAL (unretrievable)" in err, err
