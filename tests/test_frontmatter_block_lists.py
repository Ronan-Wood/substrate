"""A YAML block list in frontmatter (`key:` then `  - item` lines, what Obsidian's Properties panel
writes) is read as the same list written in flow form, by the reader, `amend`'s pin and
`frontmatter check`. A frontmatter shape the reader cannot read is refused naming its key and line,
never as a note with no status.

Every note here is synthetic."""

from __future__ import annotations

import contextlib
import io
import shutil
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from ruamel.yaml import YAML

from substrate import cli, notes, scopes
from substrate import frontmatter as fm
from substrate import vault as V
from substrate.markdown import yaml_scalar
from substrate.markdown.ingest import ingest_markdown
from substrate.markdown.reader import (
    READ_KEYS,
    FrontmatterError,
    _parse_frontmatter,
    _parse_list,
    frontmatter_pairs,
    normalized_text,
    read_markdown,
)
from substrate.spine import SpineError

_VAULTS = Path(__file__).resolve().parent.parent / "vaults"
_R12 = YAML(typ="safe", pure=True)

# The shape that froze a scope: an Obsidian-edited note whose `related` became a block list.
_OBSIDIAN = """---
status: active
doc_type: reference
confidence: unstated
related:
  - "[[Alpha Note]]"
  - "[[Beta Note]]"
domains:
  - software-dev
  - databases
supersedes:
  - old-alpha
  - old-beta
---

# Synthetic note

Body text about the quokka index.
"""

_FLOW = """---
status: active
doc_type: reference
confidence: unstated
related: ["[[Alpha Note]]", "[[Beta Note]]"]
domains: [software-dev, databases]
supersedes: [old-alpha, old-beta]
---

# Synthetic note

Body text about the quokka index.
"""


def _note(tmp_path: Path, text: str, name: str = "n.md") -> Path:
    p = tmp_path / name
    p.write_text(text, "utf-8")
    return p


# --- the reader ----------------------------------------------------------------------------------

def test_a_block_list_keeps_the_rest_of_the_frontmatter(tmp_path):
    """Fails before: the `  - ` lines made the reader drop the whole block, so status was lost."""
    doc, body, _ = read_markdown(_note(tmp_path, _OBSIDIAN))
    assert (doc.status, doc.doc_type, doc.confidence) == ("active", "reference", "unstated")
    assert doc.domains == ["software-dev", "databases"]
    assert doc.supersedes == ["old-alpha", "old-beta"]
    assert "status:" not in body and "quokka" in body


def test_a_block_list_reads_as_the_same_list_in_flow_form(tmp_path):
    block = read_markdown(_note(tmp_path, _OBSIDIAN, "a.md"))[0]
    flow = read_markdown(_note(tmp_path, _FLOW, "b.md"))[0]
    for field in ("status", "doc_type", "confidence", "domains", "supersedes"):
        assert getattr(block, field) == getattr(flow, field)
    assert fm.list_items(_parse_frontmatter(_OBSIDIAN)[0]["related"]) == \
        fm.list_items(_parse_frontmatter(_FLOW)[0]["related"])


@pytest.mark.parametrize("items", [
    "- software-dev\n- databases",            # at the key's own indent, which YAML allows
    "  - software-dev\n  - databases",        # Obsidian's two spaces
    "    - software-dev\n\n    - databases",  # deeper, with a blank line between items
    "  -   software-dev   \n  - 'databases'",
])
def test_every_block_list_indent_yaml_reads_is_read(items):
    text = f"---\nstatus: active\ndomains:\n{items}\ndoc_type: reference\n---\nbody\n"
    front, body = _parse_frontmatter(text)
    assert front["status"] == "active" and front["doc_type"] == "reference"
    assert body == "body\n"
    assert _parse_list(front["domains"]) == ["software-dev", "databases"]
    assert yaml.safe_load(text.split("---\n")[1])["domains"] == ["software-dev", "databases"]


def test_an_item_flow_syntax_would_split_is_quoted_as_one_item():
    front, _ = _parse_frontmatter("---\nstatus: active\naliases:\n  - Last, First\n  - a]b\n---\n")
    assert fm.list_items(front["aliases"]) == [("Last, First", True), ("a]b", True)]


def test_a_consistently_indented_mapping_is_still_read():
    front, _ = _parse_frontmatter("---\n  status: active\n  domains:\n    - calc\n---\nbody\n")
    assert front["status"] == "active" and front["domains"] == "[calc]"


# Values the line reader cannot read, under keys the engine reads. Each was read before as a thematic
# break (the whole block dropped, so "no status") or, where an indented line held a colon, as a
# top-level key it is not.
UNREADABLE = [
    ("doc_type:\n  kind: reference", "doc_type", 4),            # a nested mapping
    ("title: |\n  line one\n  line two", "title", 3),          # a value over two lines
    ("domains:\n  - - nested\n  - x", "domains", 4),           # a list inside a list
    ("domains:\n  - a: 1\n    b: 2", "domains", 4),            # a mapping as an item
    ("supersedes:\n  - doc: old-id", "supersedes", 4),         # a one-line mapping as an item
    ("status:\n  x: y", "status", 3),                         # `status` again, after line 2
]

# Blocks YAML cannot read that name a key the engine reads.
NOT_YAML = ["domains:\n    - a\n  - b", 'title: He said "hi": ok\nrelated:\n  - x',
            "status: active\nWe met and agreed."]

# Shapes a line reader could not take that YAML reads, and the engine now reads as YAML does.
READS_AS_YAML = [
    ("title: a long\n  title on two lines", "title", "a long title on two lines"),
    ("domains: [calc,\n  algebra]", "domains", "[calc, algebra]"),
    ("domains:\n  - a\n    - b", "domains", "[a - b]"),
    ("title: |\n  # Heading only", "title", "# Heading only"),
    ("aliases:\n  - a # note", "aliases", "[a]"),
]


def _with(shape: str) -> str:
    return f"---\nstatus: active\n{shape}\nconfidence: unstated\n---\nbody text here\n"


@pytest.mark.parametrize("shape, key, line", UNREADABLE)
def test_a_value_the_reader_cannot_read_under_a_read_key_is_refused_by_key_and_line(
        tmp_path, shape, key, line):
    with pytest.raises(FrontmatterError) as e:
        read_markdown(_note(tmp_path, _with(shape)))
    assert f"`{key}`" in str(e.value) and f"line {line}" in str(e.value)
    assert "no status" not in str(e.value)


@pytest.mark.parametrize("shape, key, line", UNREADABLE)
def test_compose_s_ingest_names_the_shape_not_a_missing_status(tmp_path, shape, key, line):
    """Fails before: compose refused these notes with "no status", which named nothing."""
    with pytest.raises(ValueError) as e:
        ingest_markdown(_note(tmp_path, _with(shape)), tmp_path / "out", require_status=True)
    assert f"`{key}`" in str(e.value) and f"line {line}" in str(e.value)
    assert "no status" not in str(e.value)


@pytest.mark.parametrize("shape", NOT_YAML)
def test_a_block_yaml_cannot_read_that_names_a_read_key_is_refused_by_line(tmp_path, shape):
    with pytest.raises(FrontmatterError) as e:
        ingest_markdown(_note(tmp_path, _with(shape)), tmp_path / "out", require_status=True)
    assert "is not YAML" in str(e.value) and "line " in str(e.value)
    assert "no status" not in str(e.value)


@pytest.mark.parametrize("shape, key, value", READS_AS_YAML)
def test_a_shape_yaml_reads_is_read_as_yaml_reads_it(shape, key, value):
    front, _ = _parse_frontmatter(_with(shape), strict=True)
    assert front["status"] == "active" and front[key] == value


@pytest.mark.parametrize("shape", [
    "meta:\n  status: archived",                 # a nested mapping, holding a spine-named key
    "summary: |\n  line one\n  line two",
    "related:\n  - - x",
    "zotero:\n  - key: ABC\n    year: 2026",
])
def test_a_value_under_a_key_nothing_reads_is_left_out_and_the_rest_read(shape):
    """Fails before: `  status: archived` under `meta:` became the note's status, and the other
    shapes dropped the whole block, status with it."""
    front, body = _parse_frontmatter(_with(shape), strict=True)
    assert front == {"status": "active", "confidence": "unstated"}
    assert body == "body text here\n"


def test_a_reader_that_only_looks_reads_every_key_but_the_unreadable_one():
    text = _with("doc_type:\n  kind: reference")
    assert _parse_frontmatter(text)[0] == {"status": "active", "confidence": "unstated"}
    assert frontmatter_pairs(text) == [("status", "active"), ("confidence", "unstated")]


def test_an_indented_comment_is_a_comment():
    front, _ = _parse_frontmatter("---\nstatus: active\ntags:\n  # c\n  - a\n---\n", strict=True)
    assert front == {"status": "active", "tags": "[a]"}


def test_an_unreadable_key_s_message_is_capped_and_has_no_control_characters():
    """`unread_keys` reports keys nothing reads too, and `frontmatter check` prints them."""
    from substrate.markdown.reader import unread_keys
    key = "\u202e" + "k" * 300
    [(_, why)] = unread_keys(f"---\nstatus: active\n{key}:\n  a: b\n---\n")
    assert len(why) < 300 and "\\u202e" in why
    assert all(ch.isprintable() for ch in why)
    prose = fm.assess("---\nstatus: active\n\x1b[31mred line\n---\n")
    assert prose is not None and all(ch.isprintable() for ch in prose[1][0])


def test_a_flow_quoted_item_keeps_every_character():
    front, _ = _parse_frontmatter("---\nstatus: active\naliases:\n  - a\ufeff, b\n---\n")
    assert fm.list_items(front["aliases"]) == [("a\ufeff, b", True)]


def test_read_keys_names_every_key_the_engine_reads():
    src = Path(__file__).resolve().parent.parent / "substrate"
    import re
    read = set()
    for f in ("markdown/reader.py", "vault.py"):
        code = (src / f).read_text("utf-8")
        read |= set(re.findall(r'front\.get\("([a-z0-9_]+)"', code))
        read |= set(re.findall(r'front\["([a-z0-9_]+)"\]', code))
        read |= set(re.findall(r'"([a-z0-9_]+)" (?:not )?in front\b', code))
    assert "raw_sha256" in read and read <= READ_KEYS
    assert V._META_KEYS <= READ_KEYS


def test_a_divider_with_prose_is_still_body_text():
    for text in ("---\nAgenda: x\nWe met and agreed.\n---\nmore\n",
                 "---\n# A heading between two rules\n---\nmore\n",
                 "---\n\nMeeting notes. Discussed the quokka migration.\n\n---\n\nAction items.\n"):
        assert _parse_frontmatter(text, strict=True) == ({}, text)


def test_a_block_read_as_body_says_why_on_the_vault_path(tmp_path):
    """Fails before: refused as "no status" alone, naming nothing."""
    text = "---\nAgenda: weekly\nWe met and agreed.\n---\n\n# Weekly\n\nbody text\n"
    with pytest.raises(SpineError) as e:
        ingest_markdown(_note(tmp_path, text), tmp_path / "out", require_status=True)
    assert "is not YAML" in str(e.value) and "line 3" in str(e.value)


def test_a_template_comment_no_longer_hides_the_frontmatter(tmp_path):
    """Fails before: the comment line made the reader take the block for a divider."""
    text = "---\n# written by a template\nstatus: active\ndoc_type: reference\n---\nbody\n"
    doc, body, _ = read_markdown(_note(tmp_path, text))
    assert (doc.status, doc.doc_type, body) == ("active", "reference", "body\n")
    assert fm.assess(text) is None


def test_a_divider_with_no_key_shape_gets_no_frontmatter_reason(tmp_path):
    text = "---\n\nMeeting notes. Discussed it.\n\n---\n\nAction items.\n"
    assert read_markdown(_note(tmp_path, text))[2]["frontmatter_unread"] is None
    keyed = "---\nAgenda: x\nWe met.\n---\nbody\n"
    assert "line 3" in read_markdown(_note(tmp_path, keyed, "k.md"))[2]["frontmatter_unread"]


# --- the readers around ingest -------------------------------------------------------------------

def _passage(tmp_path: Path, meta: str) -> tuple[Path, Path]:
    root = tmp_path / "v"
    p = root / "03-references" / "src" / "passages" / "00-a.md"
    p.parent.mkdir(parents=True)
    (root / "03-references" / "src" / "_meta.md").write_text(meta, "utf-8")
    p.write_text("# A\n\nbody\n", "utf-8")
    return p, root


def test_a_meta_md_value_its_passages_inherit_is_refused_if_unreadable(tmp_path):
    p, root = _passage(tmp_path, "---\nstatus: active\nclass:\n  kind: conversation\n---\n")
    with pytest.raises(V.VaultError, match="`class`, line 4"):
        V._source_meta(p, root)


def test_a_meta_md_keeps_every_field_beside_a_value_nothing_reads(tmp_path):
    """Fails on the scan that dropped the whole block over one unreadable line."""
    p, root = _passage(tmp_path, "---\nclass: conversation\nstatus: active\nspeakers:\n  host: A\n---\n")
    meta = V._source_meta(p, root)
    assert (meta["doc_class"], meta["status"]) == ("conversation", "active")


def test_amend_reads_a_superseded_note_s_spine_beside_a_nested_value(tmp_path):
    """Fails on the scan that dropped the whole block: the note read as undeclared, so amend's
    superseded and identity gates had nothing to refuse on."""
    p = _note(tmp_path, "---\ndoc_id: real-old-id\nstatus: superseded\nsuperseded_by: new-note\n"
                        "doc_type: reference\nmeta:\n  author: x\n---\n\n# T\n\nbody\n")
    spine, fid = notes._spine_of(p)
    assert (spine["status"], spine["doc_id"], fid) == ("superseded", "real-old-id", "real-old-id")


@pytest.mark.parametrize("extra", ["extra:\n  a: b", "confidence:\n  level: high"])
def test_a_skipped_file_declares_a_spine_beside_or_under_a_nested_value(tmp_path, extra):
    root = tmp_path / "proj"
    (root / "02-areas").mkdir(parents=True)
    (root / ".substrate.toml").write_text('name = "proj"\ninherits = []\n', "utf-8")
    (root / "02-areas" / "real.md").write_text(
        "---\nstatus: active\ndoc_type: reference\n---\n\n# Real\n\nBody.\n", "utf-8")
    (root / "log.md").write_text(f"---\ndoc_type: digest\n{extra}\n---\n\n# Log\n", "utf-8")
    with pytest.raises(V.VaultError, match="declares spine fields"):
        V.resolve_scope(root)


# --- compose, end to end -------------------------------------------------------------------------

def test_a_block_list_note_no_longer_freezes_its_scope(tmp_path):
    """Fails before: compose exited 3 with "no status" for the one Obsidian-edited note."""
    root = tmp_path / "w"
    shutil.copytree(_VAULTS / "demo-vault", root / "demo-vault")
    shutil.copytree(_VAULTS / "demo-core-vault", root / "demo-core-vault")
    (root / "demo-vault" / "02-areas" / "obsidian-edited.md").write_text(_OBSIDIAN, "utf-8")
    (root / "demo-vault" / "02-areas" / "plugin-data.md").write_text(
        _FLOW.replace("related:", "plugin:\n  view: grid\nrelated:"), "utf-8")
    default = scopes.engine_home
    scopes.engine_home = lambda: root
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["compose", str(root / "demo-vault"), "--db", str(root / "d.db"),
                           "--index-root", str(root / "ir"), "--registry", str(root / "s.toml")])
    finally:
        scopes.engine_home = default
    assert rc == 0, err.getvalue()
    assert "obsidian-edited.md" in out.getvalue() and "plugin-data.md" in out.getvalue()


# --- amend's pin ---------------------------------------------------------------------------------

def test_the_pin_writes_a_doc_id_into_a_block_list_note():
    """Fails before: the pin found no frontmatter to pin into and left the note as it was."""
    pinned = notes._pin(_OBSIDIAN.encode(), "synthetic-id")
    assert pinned.startswith(b"---\ndoc_id: synthetic-id\nstatus: active\n")
    pairs = frontmatter_pairs(normalized_text(pinned))
    assert pairs[0] == ("doc_id", "synthetic-id") and pairs[1:] == frontmatter_pairs(_OBSIDIAN)


def test_the_pin_goes_in_at_the_block_s_own_key_indent():
    content = b"---\n  status: active\n  doc_type: reference\n---\n\n# T\n\nbody\n"
    pinned = notes._pin(content, "synthetic-id")
    assert pinned.startswith(b"---\n  doc_id: synthetic-id\n  status: active\n")


# --- frontmatter check ---------------------------------------------------------------------------

def test_check_reads_a_block_list_note_and_finds_it_agrees():
    assert fm.assess(_OBSIDIAN) is None
    assert fm._engine_view(_OBSIDIAN)[0][0] == ("status", "active")


YAML_READS = [("meta:\n  status: archived", "meta", 4),
              ("title: |\n  line one\n  line two", "title", 3),
              ("supersedes:\n  - doc: old-id", "supersedes", 4)]


@pytest.mark.parametrize("shape, key, line", YAML_READS)
def test_check_names_yaml_the_engine_cannot_read(shape, key, line):
    """Fails before: the engine read no frontmatter, or misread it, so check called the note clean."""
    kind, reasons = fm.assess(f"---\nstatus: active\n{shape}\n---\nbody\n")
    assert kind == fm.READS_DIFFERENTLY
    assert any(f"`{key}`" in r and f"line {line}" in r for r in reasons)


def test_check_names_a_block_yaml_cannot_read_that_names_a_read_key():
    kind, reasons = fm.assess("---\nstatus: active\nWe met.\n---\nbody\n")
    assert kind == fm.NOT_YAML and "line 3" in reasons[0]


def test_check_still_leaves_a_divider_with_prose_alone():
    assert fm.assess("---\n\nMeeting notes. We met.\n\n---\n\nAction items.\n") is None


def test_check_names_a_block_list_item_the_engine_splits_and_yaml_does_not():
    kind, reasons = fm.assess("---\nstatus: active\ndomains:\n  - calc, algebra\n---\n")
    assert kind == fm.READS_DIFFERENTLY and reasons[0].startswith("domains:")


def test_fix_refuses_a_block_list_note_it_cannot_rewrite_line_by_line():
    with pytest.raises(ValueError, match="is not YAML"):
        fm.rewrite(b"---\ntitle: a: b\nrelated:\n  - x\n---\nbody\n")


# --- the property: every reader reads a block list alike -----------------------------------------

_TEXT = st.text(st.characters(codec="utf-8"), max_size=24)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(st.lists(_TEXT, min_size=1, max_size=5))
def test_any_block_list_reads_alike_to_the_engine_the_app_and_yaml(values):
    """Items written as each writer would write one (`yaml_scalar.encode`), one per `- ` line: the
    engine's list (the app's `parseList` reading, which `list_items` mirrors) is YAML 1.1's and
    YAML 1.2's list, check finds nothing, and the engine reads the block form as the flow form."""
    want = [yaml_scalar.sanitize(v) for v in values]
    items = "\n".join(f"  - {yaml_scalar.encode(v)}" for v in values)
    text = f"---\nstatus: active\naliases:\n{items}\n---\nbody\n"
    block = text.split("---\n")[1]
    for read in (yaml.safe_load(block), _R12.load(io.StringIO(block))):
        assert read["aliases"] == want
    front, _ = _parse_frontmatter(text, strict=True)
    assert front["status"] == "active"
    if all(w.strip() for w in want):  # a blank item is no item to the line readers, as in flow form
        assert [t for t, _ in fm.list_items(front["aliases"])] == want
        assert fm.assess(text) is None
    flow = "[" + ", ".join(yaml_scalar.encode(v, flow=True) for v in values) + "]"
    flow_text = f"---\nstatus: active\naliases: {flow}\n---\nbody\n"
    assert fm.list_items(front["aliases"]) == fm.list_items(_parse_frontmatter(flow_text)[0]["aliases"])


# --- the two readings, each against its reference -----------------------------------------------

def _old_line_reader(text: str):
    """The line reader before this change, which every flat block must still be read by."""
    import re as _re
    m = _re.compile(r"\A---\n(.*?)\n---\n?", _re.DOTALL).match(text)
    if not m:
        return None
    pairs = []
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            return None
        k, _, v = line.partition(":")
        pairs.append((k.strip(), yaml_scalar.decode(v.strip())))
    return pairs


_FLAT_LINE = st.builds(lambda k, v: f"{k}:{v}", st.from_regex(r"[A-Za-z#\-\"'][^:\n\r]{0,12}", fullmatch=True),
                       st.text(st.characters(codec="utf-8", exclude_characters="\n\r"), max_size=20))


@settings(max_examples=400, deadline=None)
@given(st.lists(st.one_of(_FLAT_LINE, st.just("")), min_size=1, max_size=6))
def test_a_flat_block_reads_exactly_as_the_line_reader_always_read_it(lines):
    lines = [ln for ln in lines if not ln[:1].isspace()]
    text = "---\n" + "\n".join(lines) + "\n---\nbody\n"
    if any(ln.strip() and (":" not in ln or ln[:1].isspace()) for ln in "\n".join(lines).splitlines()):
        return  # not flat once split as the reader splits it
    assert frontmatter_pairs(text) == _old_line_reader(text)


_WORD = st.text(st.characters(codec="utf-8", exclude_categories=["Cc", "Cs", "Zl", "Zp"]), max_size=16)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.dictionaries(st.from_regex(r"[a-z][a-z_]{0,8}", fullmatch=True),
                       st.one_of(_WORD, st.lists(_WORD, max_size=4)), min_size=1, max_size=5))
def test_a_block_read_through_yaml_reads_as_pyyaml_reads_it(data):
    """Written as a YAML tool writes it (block lists, quotes where needed), read by the engine, and
    by PyYAML resolving no types: the same strings, a blank list item aside."""
    block = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, width=10**6)
    text = f"---\n# written by a YAML tool\n{block.rstrip(chr(10))}\n---\nbody\n"
    want = yaml.load(block, Loader=yaml.BaseLoader)
    try:
        front, _ = _parse_frontmatter(text, strict=True)
    except FrontmatterError:
        assert any(isinstance(v, str) and any(c in v for c in "\n\r\x85\u2028\u2029")
                   or isinstance(v, list) and any(any(c in i for c in "\n\r\x85\u2028\u2029") for i in v)
                   for v in want.values())
        return
    for k, v in want.items():
        if isinstance(v, list):
            assert [t for t, _ in fm.list_items(front[k])] == [i for i in v if i.strip()]
        elif "\n" not in v.rstrip("\n"):
            assert front[k] == v.rstrip("\n")


def test_a_divider_whose_block_yaml_reads_as_keys_is_frontmatter_as_in_obsidian():
    """The one way a note opening with a divider loses body text: its block is a YAML mapping, so
    Obsidian shows it as properties too. Nothing the engine reads is in it, and check agrees."""
    text = "---\nAttendees: Alice, Bob\n  - Alice to send the report\n---\n\nRest\n"
    front, body = _parse_frontmatter(text)
    assert front == {"Attendees": "Alice, Bob - Alice to send the report"} and body == "\nRest\n"
    assert fm.assess(text) is None


# --- YAML the line readers would read otherwise is left out, named ------------------------------

@pytest.mark.parametrize("shape, key", [
    ("base: &d\n  status: superseded\n<<: *d", "status"),               # a merge key
    ("<<: {superseded_by: newer, doc_type: decision}", "superseded_by"),
    ("status: !!null superseded", "status"),                             # a tag
    ("status: &s !!str active", "status"),
    ('doc_id: "victim-id\\n"', "doc_id"),                                # a quoted trailing break
    ("doc_id:\n  k: v\ndoc_id: real-id", "doc_id"),                      # a key written twice
    ("domains: &a [calc]\nsupersedes: *a", "supersedes"),               # an alias
])
def test_yaml_the_line_readers_read_otherwise_is_refused_under_a_read_key(shape, key):
    with pytest.raises(FrontmatterError, match=f"`{key}`"):
        _parse_frontmatter(f"---\n# c\nconfidence: unstated\n{shape}\n---\nbody\n", strict=True)


def test_a_break_only_yaml_1_1_takes_makes_a_non_flat_block_unreadable():
    with pytest.raises(FrontmatterError, match="NEL, LS or PS"):
        _parse_frontmatter("---\ntags:\n  - a\n# hi\u2028status: superseded\n---\n", strict=True)


def test_an_over_size_block_that_names_a_read_key_is_refused():
    pad = "".join(f"k{i}: v\n" for i in range(110_000))
    with pytest.raises(FrontmatterError, match="KiB"):
        _parse_frontmatter(f"---\n# c\nstatus: superseded\n{pad}---\nbody\n", strict=True)


def test_aliases_cannot_make_the_work_outgrow_the_block():
    import time
    big = "x" * 50_000
    block = f"# c\nstatus: active\na: &a {big}\nb: [{', '.join(['*a'] * 4000)}]\n"
    t0 = time.monotonic()
    front, _ = _parse_frontmatter(f"---\n{block}---\nbody\n")
    assert time.monotonic() - t0 < 2 and "b" not in front and len(front["a"]) == 50_000


def test_a_flat_line_led_by_a_no_break_space_is_still_flat():
    front, _ = _parse_frontmatter("---\nstatus: active\n\u3000doc_type: reference\n---\nb\n")
    assert front == {"status": "active", "doc_type": "reference"}  # as the line reader always read it


def test_amend_repairs_a_note_whose_unread_value_no_gate_rests_on(tmp_path):
    p = _note(tmp_path, "---\nstatus: active\ndoc_type: reference\ntitle: |\n  line one\n  line two\n"
                        "---\n\n# T\n\nbody\n")
    spine, _ = notes._spine_of(p)
    assert spine["status"] == "active"


def test_amend_refuses_a_note_whose_unread_value_a_gate_rests_on(tmp_path):
    p = _note(tmp_path, "---\nstatus:\n  was: superseded\ndoc_type: reference\n---\n\n# T\n\nbody\n")
    with pytest.raises(FrontmatterError, match="`status`"):
        notes._spine_of(p)


def test_the_pin_takes_its_indent_from_a_key_not_a_comment():
    content = b"---\n  # template\nstatus: active\ndoc_type: reference\n---\n\n# T\n\nbody\n"
    assert notes._pin(content, "synthetic-id").startswith(b"---\ndoc_id: synthetic-id\n  # template\n")
