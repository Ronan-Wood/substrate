"""Frontmatter ends at a whole `---` line, by one rule the reader and `frontmatter fix` share.

The reader used to end the block at the first line that merely STARTS with `---`, so `---x: 1`
ended it for the engine while Obsidian and every YAML reader read on: what followed was body to one
and properties to the others. A stamp key written there was never checked. Now the block closes
only at `---` followed by nothing but spaces or tabs, so `--- ` still closes, as it always did.
"""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from substrate import frontmatter as fm  # noqa: E402
from substrate import notes  # noqa: E402
from substrate.markdown import reader  # noqa: E402

HEAD = "status: active\ndoc_type: decision\nconfidence: proposed\n"


@pytest.mark.parametrize("line", ["---x: 1", "----", "--- x", "---\tx", "-----"])
def test_a_line_that_only_starts_with_dashes_is_inside_the_block(line: str) -> None:
    text = f"---\ntitle: a\n{line}\nwritten_by: alice\n---\nbody\n"
    m = reader._FRONTMATTER.match(text)
    assert m and m.group(1) == f"title: a\n{line}\nwritten_by: alice", line
    assert text[m.end():] == "body\n"


def test_a_key_after_a_dash_line_is_read() -> None:
    text = "---\ntitle: a\n---x: 1\nwritten_by: alice\n---\nbody\n"
    assert reader.frontmatter_pairs(text) == [("title", "a"), ("---x", "1"),
                                              ("written_by", "alice")]


@pytest.mark.parametrize("fence", ["---", "--- ", "---\t", "---  \t ", "--- \t"])
@pytest.mark.parametrize("tail", ["\nbody\n", ""])
def test_a_fence_with_trailing_spaces_or_tabs_closes(fence: str, tail: str) -> None:
    text = f"---\ntitle: a\n{fence}{tail}"
    assert reader.frontmatter_pairs(text) == [("title", "a")], repr(text)
    assert reader._parse_frontmatter(text)[1].strip() == tail.strip()


def test_a_stamp_key_after_a_dash_line_is_checked() -> None:
    """The bypass on `main` after #8: `---x: 1` hid the `amended_by` line below it from the stamp
    check, and Obsidian showed it as a property."""
    forged = f"---\n{HEAD}---x: 1\namended_by: alice\n---\n\n# A note\n".encode()
    with pytest.raises(notes.NoteError, match="amended_by"):
        notes.stamp_new(forged, "claude-code")
    with pytest.raises(notes.NoteError, match="amended_by"):
        notes.stamp_amend(forged + b"\nedited\n", f"---\n{HEAD}---\n\n# A note\n".encode(),
                          "claude-code")


# --------------------------------------------------------------------------- flat blocks read alike

_OLD_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _old_scan(text: str):
    """The reader's flat line scan with the OLD fence: (pairs, end), or "yaml" for a block the
    reader hands to YAML, or (None, 0) for no frontmatter."""
    m = _OLD_FRONTMATTER.match(text)
    if not m:
        return None, 0
    pairs = []
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line or line[:1] in " \t":
            return "yaml", 0
        k, _, v = line.partition(":")
        pairs.append((k.strip(), v.strip()))
    return pairs, m.end()


def test_a_flat_block_reads_exactly_as_it_did() -> None:
    """Only a block with a line that starts with `---` and is not a fence moves. Every other flat
    block, `--- ` fences and bodies with later `---` rules included, reads the same pairs, and its
    body starts at the same place."""
    rng = random.Random(20261111)
    keys = ["title", "status", "doc_type", "x", "a-b", "date", "domains"]
    values = ["", "plain", "'quoted: yes'", '"double"', "[a, b]", "a: b", "--", "- x", "#c"]
    compared = 0
    for _ in range(3000):
        lines = [f"{rng.choice(keys)}: {rng.choice(values)}".rstrip()
                 for _ in range(rng.randint(1, 6))]
        if rng.random() < 0.2:
            lines.insert(rng.randrange(len(lines) + 1), "")
        fence = rng.choice(["---", "---", "--- ", "---\t", "---  "])
        tail = rng.choice(["\n", "\nbody\n", "", "\n\n# H\n---\nmore\n", "\nx\n--- \ny\n"])
        text = "---\n" + "\n".join(lines) + "\n" + fence + tail
        old, old_end = _old_scan(text)
        if old == "yaml":
            continue
        compared += 1
        scan = reader._raw_scan(text)
        assert scan.pairs == old, text
        # The old fence stopped after `---`, leaving a `--- ` fence's spaces and line break to the
        # body; the new one takes them, so the body starts at the same text.
        moved = 0 if fence == "---" else len(fence) - 3 + tail.startswith("\n")
        assert scan.end == old_end + moved, text
    assert compared > 1500, compared


# --------------------------------------------------------------------------- frontmatter fix and check

def _vault(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    v = tmp_path / "v"
    for rel, body in files.items():
        p = v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
    return v


_UNREADABLE = 'title: "a: "b""'  # the old unescaped dialect: the engine reads it, YAML does not


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("fence", ["---", "--- ", "---\t"])
def test_fix_closes_where_the_reader_closes(tmp_path: Path, eol: str, fence: str) -> None:
    """`frontmatter fix` refused a `--- ` fence, which the reader closes on, as "not on a line of
    its own"."""
    note = eol.join(["---", _UNREADABLE, "status: active", fence, "", "body", ""])
    v = _vault(tmp_path, {"n.md": note})
    rep = fm.check([v], fix=True, write=True)
    assert [p.name for p in rep.written] == ["n.md"], rep.findings
    out = (v / "n.md").read_bytes().decode()
    assert out == eol.join(["---", "title: 'a: \"b\"'", "status: active", fence, "", "body", ""])
    assert fm.check([v]).findings == []


def test_check_and_fix_read_a_dash_line_as_inside_the_block(tmp_path: Path) -> None:
    """`check` reads past `---x: y` as the reader does, and `fix` refuses the line as outside the
    engine's dialect; it used to say the block's closing `---` was not on a line of its own."""
    v = _vault(tmp_path, {"n.md": f"---\n{_UNREADABLE}\n---x: y\n---\nbody\n"})
    rep = fm.check([v], fix=True, write=True)
    assert rep.written == []
    assert "not a first-column" in (rep.findings[0].refused or "")


def test_fix_refuses_mixed_line_endings_inside_the_block() -> None:
    with pytest.raises(ValueError, match="mixes line endings"):
        fm.rewrite(f"---\r\n{_UNREADABLE}\nstatus: active\r\n---\r\nbody\r\n".encode())


# --------------------------------------------------------------------------- a block that never closes

_UNCLOSED = "---\ndoc_id: keep-me\nstatus: superseded\ndoc_type: decision\n----\n\n# Body\n"


def test_a_block_that_used_to_close_at_a_dash_line_is_refused_not_dropped() -> None:
    """With no whole `---` line after `----`, the block never closes. Read as no frontmatter, the
    note silently lost its doc_id and a superseded note went live (crosscheck, 2026-11-11)."""
    with pytest.raises(reader.FrontmatterError, match=r"never closes: '----' only starts"):
        reader._parse_frontmatter(_UNCLOSED, strict=True)
    kind, why = fm.assess(_UNCLOSED)
    assert any("`status`" in w for w in why), why


def test_check_lists_a_block_that_never_closes(tmp_path: Path) -> None:
    v = _vault(tmp_path, {"n.md": _UNCLOSED})
    assert [f.path.name for f in fm.check([v]).findings] == ["n.md"]


@pytest.mark.parametrize("text", ["---\nsome prose: here\n----\nmore\n",
                                  "---\na divider, then prose\n---x\n"])
def test_a_divider_that_never_closes_is_still_body_text(text: str) -> None:
    assert reader._parse_frontmatter(text, strict=True) == ({}, text)
