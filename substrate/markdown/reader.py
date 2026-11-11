"""Markdown → canonical Document. The vault-ingestion arm.

The PDF path recovers structure from glyph geometry; markdown HANDS us the structure, so this
reader honours it directly — ``#`` levels, ``` fences, ``-`` lists, ``|`` tables — and never
re-runs detection. It produces the same Document/Block shape DoclingExtractor produces, so
emit(), chunk(), the store and retrieval all run UNCHANGED: the reader's only job is
markdown-text → list[Block]; the existing emitter fills char offsets and renders the body.

No Docling, no torch. That is the point — a project vault must ingest on a fresh download with
nothing installed but the engine's own small dependencies. The one it uses is PyYAML, and only for
a frontmatter block that is not flat `key: value` lines (`_yaml_scan`).

Supported markdown subset (the vault format + the engine's own emitted markdown): ATX headings
(``#``…``######`` with optional space-preceded closing hashes), fenced code (``` / ~~~, with an
info-string language), ``-``/``*``/``+`` and ordered list items (one block each), GitHub pipe
tables, paragraphs, and ``<!-- page:N -->`` anchors. NOT structurally recognised (they degrade to
paragraphs/flat items, content preserved but hierarchy lost): setext headings, 4-space indented
code, borderless tables, nested lists, wrapped list-item continuations. The engine's emit avoids
all of those, so round-trip is unaffected; hand-authored notes using them still ingest without
content loss (A18), they just lose the extra structure.

The one non-obvious guard is A18 (`content_coverage` + `uncovered_content_blocks`). Re-emit
coverage (chunk chars / body chars, A14) CANNOT see a stage that silently drops a source line:
the dropped text never enters the body either, so numerator and denominator shrink together and
coverage stays ~1.0. So A18 measures against the SOURCE file instead. Two complementary checks:
the aggregate token ratio catches large/systematic loss; the per-block survival check catches a
single dropped content block at ANY doc size (a small categorical drop a large doc's ratio would
hide). Heading/path STRUCTURE integrity is A17's job, not A18's.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from substrate.extract.base import doc_id_for  # stdlib-only; no Docling/torch pulled in
from substrate.markdown import yaml_scalar
from substrate.models import Block, Document, Kind

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_PAGE = re.compile(r"^<!--\s*page:(\d+)\s*-->\s*$")
_PAGE_ANCHOR = re.compile(r"<!--\s*page:\d+\s*-->")  # linear; strips anchors for coverage
# Loose ATX match — closing-hash trimming is done in Python (a regex that trims it backtracks
# super-quadratically on a line ending in a long whitespace run: a crafted heading hangs ingest).
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# Fence-language excludes control bytes so a crafted info-string can't land a C0 byte in
# document.md / blocks.jsonl (emit renders lang verbatim; the normalize invariant strips C0).
_FENCE = re.compile(r"^([`~]{3,})\s*([^\s`~\x00-\x1f\x7f]*)")
_LIST = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+\S")
_LIST_MARKER = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")
_TABLE = re.compile(r"^\s{0,3}\|.*\|\s*$")
# Content-tokens: Unicode word runs (letters in ANY script, and multi-digit numbers), length ≥ 2,
# underscore excluded. Letters keep a dropped CJK / Cyrillic / accented paragraph visible; digits
# keep a dropped numeric table / list ("| 12 | 34 |", "- 42") visible — a letters-only class was
# blind to both. Single characters are dropped, so a bare list-marker digit (1.–9.) is not a
# phantom token; page-anchor numbers are stripped before tokenising.
_TOKEN = re.compile(r"[^\W_]{2,}", re.UNICODE)
_INT = re.compile(r"-?[0-9]+")

_I64 = 2**63 - 1
_MAX_MD_BYTES = 64 * 1024 * 1024  # refuse a pathological file rather than OOM
_DOC_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # accepted frontmatter doc_id shape
_SHA256 = re.compile(r"[0-9a-f]{64}")               # accepted frontmatter source_sha256 shape
_DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")  # accepted domain-tag shape


def _parse_list(v: str | None, pattern: re.Pattern[str] = _DOMAIN) -> list[str]:
    """Parse a frontmatter flow list — `[a, b, c]` — into a clean, shape-checked list.

    The reader keeps frontmatter values as raw strings, so `domains: [software-dev, databases]`
    arrives here as the literal `"[software-dev, databases]"`. Only well-shaped values survive (a
    stray one is dropped, not carried as a phantom tag); a bare value with no brackets is treated
    as a single entry. Deliberately narrow — these are tag axes, not free text.

    `pattern` is the vocabulary, because two list-valued spine fields need the SAME splitting and
    DIFFERENT shapes: `domains` admits uppercase and `/`, a `supersedes` entry must be a legal
    doc_id. Parameterised rather than copied — a second splitter is a second place for the
    quote-stripping and dedup rules to drift, and only one of the two would get the next fix.
    """
    if not v:
        return []
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    out: list[str] = []
    for part in v.split(","):
        t = part.strip().strip('"').strip("'")
        if t and pattern.fullmatch(t) and t not in out:
            out.append(t)
    return out


def doc_id_list(value: object) -> list[str]:
    """A `supersedes` value, from any of the three shapes it legitimately arrives in, as doc_ids.

    v8 makes `supersedes` list-valued: one live note can replace SEVERAL dead ones, and the scalar
    field could name only one of them — "a field that states half a fact in the shape of a whole
    one", which is the Boundary Principle's own failure. (`superseded_by` stays scalar: a dead note
    has exactly one live replacement, and a list there would invent a case that does not exist.)

    Three shapes, one function, because the same field is read from three places:
      * a frontmatter flow list, `supersedes: [a, b]` — the v8 form;
      * a frontmatter scalar, `supersedes: a` — every note written before v8, which `_parse_list`
        already treats as a one-entry list, so those notes keep working untouched;
      * a JSON value out of a `run.json`, already a list (v8) or a string (v7 and earlier).

    Those three are the LEGITIMATE shapes. The parameter is `object` because a `run.json` is
    arbitrary JSON on disk that a person may have edited by hand, so it can also hold a dict, an
    int, or a nested list, and no union type would be honest about that — the two isinstance
    guards below are what actually decides. A bare `list(value)` over a v7 run.json would explode
    `"old-note"` into ['o','l','d',…] — eight phantom supersession links on a note that had one.

    Per-element validated: an entry that is not a legal doc_id is DROPPED, exactly as a malformed
    `source_sha256` is, because a link that cannot identify what it names reads as provenance while
    being unusable. Order preserved and duplicates removed.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return _parse_list(value, _DOC_ID)
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        # Cleaned the SAME way `_parse_list` cleans a split entry. Matching raw here made one shape
        # stricter than the other — `" a "` survived as a string and was dropped as a list element,
        # so a hand-edited run.json lost a real supersession link that the equivalent frontmatter
        # would have recovered. Two normalisation rules for one field is precisely the drift this
        # function's own docstring says a second splitter would cause.
        t = item.strip().strip('"').strip("'")
        if t and _DOC_ID.fullmatch(t) and t not in out:
            out.append(t)
    return out


def _bounded_int(v: str, lo: int, hi: int) -> int | None:
    """ASCII-only, range-checked int, else None. `str.isdigit()` is NOT a sound int() guard: it
    is True for superscripts (int() then raises) and for a 40-digit bignum (int() succeeds, then
    OverflowError at SQLite-insert time aborts the whole reconcile). Both refuse cleanly here."""
    v = v.strip()
    if not _INT.fullmatch(v):
        return None
    n = int(v)
    return n if lo <= n <= hi else None


def _parse_frontmatter(text: str, *, strict: bool = False,
                       keys: frozenset[str] | None = None) -> tuple[dict[str, str], str]:
    """Split leading YAML frontmatter (the emit() format) from the body. Round-tripping the
    engine's own markdown recovers the ORIGINAL doc_id/sha/version, so re-ingesting it keeps
    the same chunk_ids and reconcile sees the same document rather than a new one.

    A leading ``---`` block is frontmatter when every non-blank line is a first-column ``key:
    value`` line (read line by line, as every engine and app writer emits it), or when YAML reads
    the block as a mapping of keys (`_raw_scan`). Otherwise it is an ordinary thematic break (a
    note opening with a divider), and treating it as frontmatter would silently swallow the prose
    between the two rules — a dropped-content bug the A18 check, which runs on the body after the
    frontmatter, could not see.

    A value the reader cannot read (`_raw_scan` says why) is left out, and every other key is read.
    When `strict`, as ingest is on the vault path, such a value under one of `keys` (`READ_KEYS` by
    default) raises FrontmatterError instead, because leaving it out would withhold a field that is
    read.
    """
    front, body, _ = _read_frontmatter(text, strict, keys)
    return front, body


def _read_frontmatter(text: str, strict: bool = False, keys: frozenset[str] | None = None,
                      ) -> tuple[dict[str, str], str, str | None]:
    """`_parse_frontmatter`'s (front, body), and why a leading `---` block was read as body text,
    from one scan."""
    pairs, end, scan = _frontmatter_scan(text, strict, READ_KEYS if keys is None else keys)
    if pairs is None:
        return {}, text, scan.prose
    return dict(pairs), text[end:], None


def normalized_text(data: bytes) -> str:
    """`data` decoded as the reader reads a note: strict UTF-8 (raises UnicodeDecodeError), with
    CRLF and lone CR turned into LF so the LF-anchored frontmatter regex sees the same block on
    every platform. One definition, because a caller that decoded or normalised differently
    would be answering questions about a different text than the one the reader parses."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def frontmatter_pairs(text: str) -> list[tuple[str, str]] | None:
    """The frontmatter's (key, value) pairs IN ORDER, duplicates kept, or None when `text` has no
    frontmatter — exactly the lines `_parse_frontmatter` reads, by the same rules, because it is
    built from this. A caller that must see what the dict collapses (a repeated key: the dict keeps
    the last) reads this instead of re-splitting the block itself; a second splitter, one that broke
    lines on `\\n` while this breaks on everything `splitlines` does, let a key hidden behind U+2028
    go uncounted (crosscheck, 2026-11-04). In a block read through YAML a repeated key is a value
    the reader cannot read, so it is left out rather than kept twice."""
    return _frontmatter_scan(text)[0]


def raw_frontmatter(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """(the block between the fences, its (key, value) pairs with each value as written after the
    colon, stripped; for a block read through YAML, each value as the line readers would read it
    back), or None when `text` has no frontmatter. Raises FrontmatterError when a value cannot be
    read, whichever key. The one splitter: the readers above decode these values, and `substrate
    frontmatter` compares them with YAML."""
    scan = _raw_scan(text)
    if scan.problems:
        raise FrontmatterError(scan.problems[0][1])
    return None if scan.pairs is None else (scan.block, scan.pairs)


def unread_keys(text: str) -> list[tuple[str, str]]:
    """Each frontmatter key YAML reads whose value the reader cannot, and why, naming the line.
    Empty for a block YAML cannot read either: that declares nothing to anyone."""
    scan = _raw_scan(text)
    return list(scan.problems) if scan.pairs is not None else []


class FrontmatterError(ValueError):
    """A frontmatter value the reader cannot read, naming the key and the note's line. A ValueError,
    which every ingest caller already turns into a refusal."""


# Every frontmatter key the engine reads from a note (`read_markdown`) or a source's `_meta.md`
# (`vault._source_meta`). Strict reading refuses a value it cannot read under one of these, and
# leaves one under any other key out, since nothing reads it. A key added to either reader belongs
# here, which `tests/test_frontmatter_block_lists.py` checks.
READ_KEYS = frozenset({
    "doc_id", "title", "class", "document_class", "status", "doc_type", "confidence", "domains",
    "supersedes", "superseded_by", "source_sha256", "source_pages", "version", "version_date",
    "page_label_offset", "raw", "raw_sha256", "raw_location",
})

# Past this a `---` block is not read through YAML: no frontmatter needs it, and a divider can open
# a long note. The same bound `blobs gc` and `frontmatter check` hold.
_MAX_YAML_BLOCK = 1024 * 1024
_KEY_LINE = re.compile(r"[ \t]*([\"']?)([A-Za-z_][\w-]*)\1[ \t]*:")
_STR_TAG = "tag:yaml.org,2002:str"
# A node written with a tag (`!!null x`, `!t x`), behind its anchor if it has one.
_TAGGED = re.compile(r"(?:&[^\s,\[\]{}]+[ \t]+)?!")
# Line breaks to YAML 1.1 (PyYAML) that YAML 1.2 (Obsidian) reads as ordinary characters, so a block
# holding one is two different blocks to the two.
_ODD_BREAKS = "\x85\u2028\u2029"


@dataclass(frozen=True)
class _Scan:
    pairs: list[tuple[str, str]] | None  # None: no frontmatter the reader reads
    end: int = 0
    block: str = ""
    problems: tuple[tuple[str, str], ...] = ()  # (key, why) for each value left out of `pairs`
    prose: str | None = None  # a leading `---` block read as body text: why


def _shown(text: str, cap: int = 60) -> str:
    """Note text for a message: at most `cap` characters, anything unprintable (a terminal escape,
    a bidi override) spelled as its code point, so a crafted note cannot write to the terminal."""
    out = "".join(ch if ch.isprintable() else f"\\u{ord(ch):04x}" for ch in text[:cap])
    return out + ("…" if len(text) > cap else "")


def _raw_scan(text: str) -> _Scan:
    """The frontmatter's (key, value) pairs, two ways.

    A block whose every non-blank line is a first-column `key: value` is read line by line, each
    value as written after the colon. That is what every writer of the engine and the app emits,
    and the old unescaped dialect those writers left in the vaults is not always YAML, so this path
    is exactly the reader it always was.

    Any other block (a block list, which Obsidian writes; a nested mapping; a comment; an indented
    key) is YAML's to read, so it is read by YAML: `_yaml_scan`.
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return _Scan(None)
    block = m.group(1)
    pairs: list[tuple[str, str]] = []
    for line in block.splitlines():
        if not line.strip():
            continue
        if ":" not in line or line[:1] in " \t":
            return _yaml_scan(block, m.end())
        k, _, v = line.partition(":")
        pairs.append((k.strip(), v.strip()))
    return _Scan(pairs, m.end(), block)


def _text(node) -> str | None:
    """A scalar's text as a line reader can hold it, or None when it holds a line break. The one
    break allowed is the one a one-line `|` or `>` block scalar ends with."""
    v = node.value
    if node.style in ("|", ">") and v.endswith("\n"):
        v = v[:-1]
    return None if any(ch in v for ch in "\n\r" + _ODD_BREAKS) else v


def _as_written(node, v: str, flow: bool) -> str:
    """`v`, the text of a scalar YAML read, as a line reader reads it back: a plain one as written,
    unless a flow list would split or end it there; any other through `yaml_scalar.encode`, or
    single-quoted with every character kept where `encode` would replace one."""
    if node.style is None and v == v.strip() and not (
            flow and any(ch in yaml_scalar._FLOW for ch in v)):
        return v  # (a line reader trims what YAML keeps, a no-break space, so that is quoted)
    if yaml_scalar.sanitize(v) == v:
        return yaml_scalar.encode(v, flow=flow)
    return "'" + v.replace("'", "''") + "'"


def _yaml_scan(block: str, end: int) -> _Scan:
    """`block` read by YAML, every scalar as the string written (`BaseLoader` resolves no types).

    Each value comes back as the line readers read it: a plain scalar as written, any other through
    `yaml_scalar.encode`, and a list of one-line scalars as the same list in flow form. Anything
    else is left out and named in `problems` with its line, because the line readers would read it
    differently from YAML's own loaders or not at all:
      * a nested mapping or list, or a value over more than one line;
      * an alias, a tag, or a merge key `<<` (each key a merge would set is named too);
      * a key written more than once, which YAML 1.2 refuses and 1.1 reads as its last.
    Each node is read at most once, so an alias cannot make the work grow past the block.

    A block YAML does not read as a mapping is body text. When it is not YAML at all (or is past
    `_MAX_YAML_BLOCK`, or holds a break only YAML 1.1 takes) and a line in it names one of
    `READ_KEYS`, each such key is a problem too, so strict reading refuses the note rather than
    read a field it declares as absent.
    """
    import yaml  # here, not at the top: a flat block, which every writer emits, never needs it

    def unreadable(why: str) -> _Scan:
        named = []
        for line in block.splitlines():
            km = _KEY_LINE.match(line)
            if km and km.group(2) in READ_KEYS and km.group(2) not in named:
                named.append(km.group(2))
        whole = f"the block between the two `---` lines {why}"
        if named:
            return _Scan(None, block=block, problems=tuple(
                (k, f"`{k}`: {whole}, so the engine reads none of it") for k in named))
        return _Scan(None, block=block, prose=(f"{whole}, so it is read as body text, not "
                                               f"frontmatter") if ":" in block else None)

    if len(block) > _MAX_YAML_BLOCK:
        return unreadable(f"is over {_MAX_YAML_BLOCK // 1024} KiB")
    if any(ch in block for ch in _ODD_BREAKS):
        return unreadable("holds a NEL, LS or PS, a line break to YAML 1.1 and not to YAML 1.2")
    try:
        root = yaml.compose(block, Loader=yaml.BaseLoader)
    except Exception as e:  # noqa: BLE001 — any failure is "not YAML": deep nesting, bad escapes
        mark = getattr(e, "problem_mark", None)
        problem = _shown(str(getattr(e, "problem", None) or type(e).__name__))
        return unreadable(f"is not YAML ({problem}"
                          + (f", line {mark.line + 2}" if mark is not None else "") + ")")
    if not isinstance(root, yaml.MappingNode) or not root.value:
        return unreadable("is not a mapping of keys")

    pairs: list[tuple[str, str]] = []
    problems: dict[str, str] = {}
    keys_seen: set[str] = set()
    nodes_seen: set[int] = set()

    def left_out(key: str, node, why: str) -> None:
        pairs[:] = [p for p in pairs if p[0] != key]
        problems.setdefault(key, f"`{_shown(key)}`, line {node.start_mark.line + 2}: {why}, which "
                                 f"the engine's reader does not read")

    def odd(node) -> str | None:
        """Why the line readers cannot take `node` as YAML's loaders do, if they cannot."""
        if id(node) in nodes_seen:
            return "an alias"
        nodes_seen.add(id(node))
        if _TAGGED.match(block, node.start_mark.index):
            return "a YAML tag"
        return None

    for knode, vnode in root.value:
        if not isinstance(knode, yaml.ScalarNode) or odd(knode):
            left_out("?", knode, "a key that is not plain text")
            continue
        key = knode.value
        if key in keys_seen:
            left_out(key, knode, "a key written more than once")
            continue
        keys_seen.add(key)
        if key == "<<" and knode.style is None:  # before the alias test: a merge is often `<<: *d`
            merged = vnode.value if isinstance(vnode, yaml.SequenceNode) else [vnode]
            for m in merged:
                for k, _ in m.value if isinstance(m, yaml.MappingNode) else ():
                    if isinstance(k, yaml.ScalarNode):
                        left_out(k.value, k, "set through a merge key `<<`")
            left_out(key, vnode, "a merge key")
        elif (why := odd(vnode)) is not None:
            left_out(key, vnode, why)
        elif isinstance(vnode, yaml.ScalarNode):
            if (v := _text(vnode)) is None:
                left_out(key, vnode, "a value over more than one line")
            else:
                pairs.append((key, _as_written(vnode, v, flow=False)))
        elif isinstance(vnode, yaml.SequenceNode):
            items = []
            for n in vnode.value:
                why = odd(n) or (None if isinstance(n, yaml.ScalarNode) and _text(n) is not None
                                 else "a list item that is not one line of text")
                if why is not None:
                    left_out(key, n, why)
                    break
                items.append(_as_written(n, _text(n), flow=True))
            else:
                pairs.append((key, "[" + ", ".join(items) + "]"))
        else:
            left_out(key, vnode, "a nested mapping")
    return _Scan(pairs, end, block, tuple(problems.items()))


def _frontmatter_scan(text: str, strict: bool = False, keys: frozenset[str] = READ_KEYS,
                      ) -> tuple[list[tuple[str, str]] | None, int, _Scan]:
    """(pairs or None, offset where the body starts, the scan). One regex match serves every
    caller. A value the reader cannot read under one of `keys` raises FrontmatterError when
    `strict`."""
    scan = _raw_scan(text)
    if strict:
        for key, why in scan.problems:
            if key in keys:
                raise FrontmatterError(why)
    if scan.pairs is None:
        return None, 0, scan
    return [(k, yaml_scalar.decode(v)) for k, v in scan.pairs], scan.end, scan


def _is_close_fence(line: str, ch: str, n: int) -> bool:
    """A closing fence is a line of ONLY the same fence char (≥ opening length), nothing else —
    NOT any line that merely starts with it. `startswith` closed on an inner ```python and split
    a code block that contained a nested fenced example."""
    s = line.strip()
    return len(s) >= n and set(s) == {ch}


def _blocks(md: str) -> list[Block]:
    """Parse markdown into blocks by its explicit syntax. Every non-blank source line routes
    into exactly one block — paragraphs are the catch-all, so nothing falls through silently
    (A18 proves it)."""
    blocks: list[Block] = []
    n = 0
    page: int | None = None
    para: list[str] = []
    table: list[str] = []
    lines = md.splitlines()

    def bid() -> str:
        nonlocal n
        n += 1
        return f"b{n:06d}"

    def flush_para() -> None:
        nonlocal para
        if para:
            blocks.append(Block(id=bid(), kind=Kind.TEXT, text=" ".join(para).strip(), page=page))
            para = []

    def flush_table() -> None:
        nonlocal table
        if table:
            blocks.append(Block(id=bid(), kind=Kind.TABLE, text="\n".join(table), page=page))
            table = []

    i = 0
    while i < len(lines):
        line = lines[i]

        pm = _PAGE.match(line)
        if pm:
            flush_para(); flush_table()
            # Range-check like every other numeric field: an unbounded int() here would let a
            # crafted <!-- page:99…9 --> set source_pages (= max anchors) to a bignum that
            # OverflowErrors at SQLite-insert time. A malformed anchor is ignored (page unchanged);
            # the line is still consumed (it is a comment, never content).
            p = _bounded_int(pm.group(1), 1, _I64)
            if p is not None:
                page = p
            i += 1
            continue

        fm = _FENCE.match(line)
        if fm:
            flush_para(); flush_table()
            fence, lang = fm.group(1), (fm.group(2) or None)
            body: list[str] = []
            i += 1
            while i < len(lines) and not _is_close_fence(lines[i], fence[0], len(fence)):
                body.append(lines[i])
                i += 1
            i += 1  # consume the closing fence (or run off the end on an unterminated block)
            blocks.append(Block(id=bid(), kind=Kind.CODE, text="\n".join(body), page=page, lang=lang))
            continue

        hm = _HEADING.match(line)
        if hm:
            flush_para(); flush_table()
            blocks.append(Block(id=bid(), kind=Kind.HEADING, text=_atx_text(hm.group(2)),
                                page=page, level=len(hm.group(1))))
            i += 1
            continue

        if _TABLE.match(line):
            flush_para()
            table.append(line.strip())
            i += 1
            continue
        flush_table()

        if not line.strip():
            flush_para()
            i += 1
            continue

        if _LIST.match(line):
            flush_para()
            blocks.append(Block(id=bid(), kind=Kind.LIST_ITEM,
                                text=_LIST_MARKER.sub("", line).strip(), page=page))
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_para(); flush_table()
    return blocks


def _atx_text(text: str) -> str:
    """Heading text with an ATX closing-hash run removed — but only when preceded by whitespace,
    per CommonMark, so a ``## C#`` / ``## F#`` heading keeps its trailing ``#``."""
    text = text.rstrip()
    if text.endswith("#"):
        stripped = text.rstrip("#")
        if not stripped or stripped[-1].isspace():
            text = stripped.rstrip()
    return text


def content_coverage(body_md: str, captured: list[str]) -> tuple[float, list[str]]:
    """Fraction of the SOURCE's word-tokens that survive into `captured`.

    Against the source file, not the re-emitted body — a stage that dropped a line loses its
    tokens HERE even though re-emit coverage (A14) would stay ~1.0 (the drop shrinks numerator
    and denominator together). Multiset diff, so a dropped paragraph is caught even when its
    words recur elsewhere: the recurrence keeps them present but the COUNT falls short.

    `captured` is every text that should hold the source's content — the caller passes chunk
    `text_with_path`, each fence-language, and heading block texts (see cmd_ingest_md). Page
    anchors are stripped (a linear, bounded strip — never an unterminated-comment scan).
    """
    # Strip ordered/bulleted list MARKERS from the source before tokenising, because the block
    # extractor strips them from what it stores (`_LIST_MARKER.sub` where list items are built).
    # Counting them on one side only makes a marker look like dropped content. This bit exactly
    # where the token class and the marker syntax disagree: `_TOKEN` needs ≥2 characters, so
    # single-digit markers `1.`–`9.` were never counted and the asymmetry stayed invisible — until
    # a list ran to item 10. A real migrated note (a
    # 17-item numbered list) reported coverage 0.9554 against the 0.99 gate with `missing`
    # naming exactly ['10','11',…,'17'], and was refused for content it had not lost.
    src = Counter(_TOKEN.findall(
        "\n".join(_LIST_MARKER.sub("", ln) for ln in _PAGE_ANCHOR.sub("", body_md).split("\n"))
    ))
    got: Counter[str] = Counter()
    for t in captured:
        got.update(_TOKEN.findall(t))
    missing = src - got
    total = sum(src.values())
    cov = 1.0 - sum(missing.values()) / max(total, 1)
    return round(cov, 4), [t for t, _ in missing.most_common(8)]


def uncovered_content_blocks(
    blocks: list[Block], captured: list[str], min_survival: float = 0.5
) -> list[str]:
    """Per-block backstop the aggregate ratio misses: a single dropped content block (paragraph,
    list item, code, table) is flagged at ANY doc size, where a large doc's global ratio would
    hide a small categorical drop. Returns the ids of blocks that mostly vanished.

    HEADING blocks are excluded — a heading with content survives as a path, an empty-section
    heading is intentionally dropped by the chunker, and heading STRUCTURE is A17's job. INDEX
    blocks are excluded (never chunked). Presence-based (not multiset): a block flagged only if
    it has a token found NOWHERE in the output. The residual blind spot — dropping a block whose
    every token verbatim recurs elsewhere — is not a content loss (that text stays retrievable
    via the surviving copy), so presence is the right test, not a weaker one.
    """
    cap: Counter[str] = Counter()
    for t in captured:
        cap.update(_TOKEN.findall(t))
    bad: list[str] = []
    for b in blocks:
        if b.kind in (Kind.HEADING, Kind.INDEX):
            continue
        toks = _TOKEN.findall(b.text)
        if not toks:
            continue
        survived = sum(1 for t in toks if cap[t] > 0)
        if survived < len(toks) * min_survival:
            bad.append(b.id)
    return bad


def read_markdown(path: Path, doc_class: str | None = None, *,
                  strict: bool = True) -> tuple[Document, str, dict]:
    """Read a markdown file into a canonical Document. Returns (doc, body_md, reader_stats).

    body_md (source minus frontmatter) is handed back so the ingest pipeline can run the A18
    end-to-end coverage checks against it after chunking — the reader itself is a pure parser.
    Raises ValueError on an over-size file or non-UTF-8 bytes, and its FrontmatterError on a value
    it cannot read under one of `READ_KEYS` unless `strict` is off (cmd_ingest_md turns these into
    a clean FATAL rather than an unhandled traceback).
    """
    data = path.read_bytes()
    if len(data) > _MAX_MD_BYTES:
        raise ValueError(f"{path} is {len(data)} bytes (> {_MAX_MD_BYTES} cap)")
    # Line endings are normalised (`normalized_text`) so the LF-anchored frontmatter regex (and
    # everything after) works on CRLF / CR files; otherwise a Windows-authored note's frontmatter
    # is silently unrecognised and its doc_id/version/sha never recovered. Not content — coverage
    # tokens are unaffected.
    try:
        raw = normalized_text(data)
    except UnicodeDecodeError as e:
        raise ValueError(f"{path} is not valid UTF-8: {e}") from e
    sha = hashlib.sha256(data).hexdigest()

    # `unread`: why a leading `---` block was read as body text, for ingest to name if the note is
    # then refused for a spine it shows written; popped there, never reported.
    front, body_md, unread = _read_frontmatter(raw, strict=strict)
    blocks = _blocks(body_md)
    anchors = [b.page for b in blocks if b.page is not None]

    # Frontmatter doc_id / source_sha256 are trusted only if they have the expected shape;
    # otherwise derived. reconcile keys the whole index on doc_id (delete-then-insert) and records
    # source_sha256 as provenance, so an arbitrary crafted value is a spoof / collision surface.
    fid = front.get("doc_id", "")
    fsha = front.get("source_sha256", "")
    # Supersession links are doc_ids; a value that is not doc_id-shaped is dropped to None rather
    # than carried, so a crafted link cannot smuggle in a value the reconcile/index keys on. The
    # reader stays a pure PARSER — it does not enforce "superseded requires a link" or "status is
    # one of the four"; that is spine.validate_status, called by the ingest paths (which decide how
    # strict to be: the vault path requires status, a standalone ingest defaults it to active).
    fraw = front.get("raw_sha256", "")
    fsup_by = front.get("superseded_by", "")
    fsup = front.get("supersedes", "")
    doc = Document(
        doc_id=fid if _DOC_ID.fullmatch(fid) else doc_id_for(path),
        source_path=str(path),
        source_sha256=fsha if _SHA256.fullmatch(fsha) else sha,
        note_sha256=sha,
        source_pages=_bounded_int(front.get("source_pages", ""), 1, _I64)
        or (max(anchors) if anchors else 1),
        # Doc 2 §3 writes the document class as `class:` in a source's `_meta.md`, and vault.py
        # already maps that spelling. A note's OWN frontmatter must accept the same word, or the
        # vault format has two names for one field depending on which file it appears in — and the
        # unread spelling then falls through silently. That is how six migrated conversations were
        # relabelled `reference-frozen` under a fully green compose.
        #
        # THERE IS NO DEFAULT HERE ANY MORE, and removing it is the whole fix. The comment above
        # recorded that incident while the line beneath it kept the cause: an undeclared class
        # became `reference-frozen`, so 91% of the corpus claimed to be a published edition that
        # will not change. Absence now stays empty and `classes.apply` resolves it to
        # `unclassified` — the reader invents nothing, exactly as it invents no confidence.
        document_class=(doc_class or front.get("document_class") or front.get("class") or ""),
        blocks=blocks,
        title=front.get("title"),
        version=front.get("version"),
        version_date=front.get("version_date"),
        page_label_offset=_bounded_int(front.get("page_label_offset", ""), -_I64, _I64),
        # §3b raw provenance. `raw_sha256` is shape-checked like source_sha256: a malformed checksum
        # is dropped rather than carried, because a pointer that cannot identify the file it names
        # is worse than an absent one — it reads as provenance while being unusable.
        raw=(front.get("raw") or None),
        raw_sha256=fraw if _SHA256.fullmatch(fraw) else None,
        raw_location=(front.get("raw_location") or None),
        status=(front.get("status") or None),
        superseded_by=fsup_by if _DOC_ID.fullmatch(fsup_by) else None,
        # List-valued as of v8. `superseded_by` one line up stays scalar, and the asymmetry is the
        # fact rather than an inconsistency: a dead note has exactly one live replacement, a live
        # note can have replaced several.
        supersedes=doc_id_list(fsup),
        domains=_parse_list(front.get("domains")),
        # doc_type (§6a) is carried raw like status; spine.validate_doc_type is the gate that
        # refuses an unknown value or an absent one on the strict vault path. The reader stays a
        # pure parser and does not decide strictness.
        doc_type=(front.get("doc_type") or None),
        # confidence likewise: parsed raw, gated by spine.validate_confidence. Absent stays None
        # here and becomes `unjudged` at the gate — the reader never invents a value, so "the note
        # said nothing" and "the note said unstated" arrive at the gate distinguishable.
        #
        # They arrive distinguishable and, until 2026-07-28, LEFT indistinguishable: the gate mapped
        # None to `unstated` too. This comment described the property correctly while the code one
        # layer down discarded it — the reader was never the defect, which is why no reader test
        # could have caught it.
        confidence=(front.get("confidence") or None),
        extractor="markdown-reader/0.1.0",
        extractor_arm="markdown",
        layout_model="",
    )

    stats = {
        "blocks": len(blocks),
        "headings": sum(1 for b in blocks if b.kind is Kind.HEADING),
        "code": sum(1 for b in blocks if b.kind is Kind.CODE),
        "tables": sum(1 for b in blocks if b.kind is Kind.TABLE),
        "list_items": sum(1 for b in blocks if b.kind is Kind.LIST_ITEM),
        "status": doc.status,
        "domains": list(doc.domains),
        "doc_type": doc.doc_type,
        "frontmatter_unread": unread,
    }
    return doc, body_md, stats
