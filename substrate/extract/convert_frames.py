"""`convert/1`: the frames the engine and its conversion worker exchange over stdio.

Engine-internal (D5 of #25): nothing under `contract/` names them. The worker reads
attacker-controlled documents, so the engine decodes what it sends as data and nothing more, and
its memory is bounded by the framing rather than by counting what a parser builds.

**Every JSON line is small**: at most `MAX_LINE` bytes with its `\\n`, an object with exact keys
and types, no duplicate key, no NaN, `"v": 1` and a `type`; a request and its reply carry the same
integer `id`. **Large data never travels as JSON.** A `converted` reply is a header line stating
`bytes`, then exactly that many bytes of UTF-8 markdown. An `items` reply is a header stating
`count` and `bytes`, then `count` item records filling exactly `bytes`: each a small JSON line
stating `text_bytes`, then that many bytes of UTF-8 text. Raw runs are decoded strictly, so a
lone surrogate or any invalid UTF-8 is refused, with no escape processing at all.

The worst a JSON line can cost while it is parsed, measured on CPython 3.14.4 and 3.14.7 with
tracemalloc for a line of `MAX_LINE` bytes: 2.8 MiB for 32,768 levels of nesting (44 times the
line), 1.5 MiB for a list of empty objects (24 times), 1.4 MiB for a list of empty lists, under
0.6 MiB for lists of numbers or one-character strings. So no line costs more than about 3 MiB.
A whole reply at its caps costs more than its bytes, since text is decoded into a string and items
into dicts: measured in the engine over a real pipe, about 200 MiB for 64 MiB of ASCII markdown,
330 MiB when one character in it needs four bytes, and under 400 MiB for 200,000 items.

| direction | header line | then |
|---|---|---|
| worker → engine | `hello`: `docling` (version or null) | — |
| engine → worker | `convert`: `id`, `format`, `path` | — |
| engine → worker | `extract_pdf`: `id`, `path`, `first`, `last` | — |
| worker → engine | `converted`: `id`, `bytes`, `pages`, `status`, `seconds` | `bytes` of markdown |
| worker → engine | `items`: `id`, `count`, `bytes` | `count` × (item line + its text) |
| worker → engine | `refused`: `id`, `reason` | — |
| worker → engine | `needs_extra` / `unsupported`: `id`, `error` | — |
| engine → worker | `bye` | — |

An item line holds `label`, `page`, `lang`, `height`, `left`, `layer` and `text_bytes`.
"""

from __future__ import annotations

import json

PROTOCOL = 1
# A JSON line, its newline included. 64 KiB holds the longest sentence a reply carries
# (`MAX_SENTENCE`) and an item's fields many times over, and costs at most about 3 MiB to parse.
MAX_LINE = 64 * 1024
# The markdown of one document, and the item records of one PDF batch.
MAX_MARKDOWN = 64 * 1024 * 1024
MAX_ITEMS_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 200_000
# Characters. Escaped as JSON, one can take six bytes, so a sentence always fits its line.
MAX_SENTENCE = 8 * 1024
MAX_SHORT = 256
_MAX_COUNT = 10_000_000


class FrameError(ValueError):
    """A frame that cannot be trusted: the whole reply is discarded."""


def _shown(value) -> str:
    """A value from a frame, as an error message may quote it: never more than a line."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise FrameError(f"the key {_shown(key)} appears twice")
        out[key] = value
    return out


def _no_constant(name):
    raise FrameError(f"{name} is not a number a frame may carry")


def _count(value, lo=0, hi=_MAX_COUNT):
    return type(value) is int and lo <= value <= hi


def _text(value, limit):
    return type(value) is str and len(value) <= limit


def _optional(check):
    return lambda v: v is None or check(v)


def _number(value):
    # Not `math.isfinite`, which raises OverflowError on an int too large for a float: JSON
    # allows 4300 digits. The comparison is exact for any int, and false for NaN and infinity.
    return type(value) in (int, float) and abs(value) < 1e9


_ID = {"id": lambda v: _count(v, 1, 2**31)}

SCHEMAS = {
    "hello": {"docling": _optional(lambda v: _text(v, MAX_SHORT))},
    "convert": {**_ID, "format": lambda v: _text(v, MAX_SHORT), "path": lambda v: _text(v, 4096)},
    "extract_pdf": {**_ID, "path": lambda v: _text(v, 4096), "first": lambda v: _count(v, 1),
                    "last": lambda v: _count(v, 1)},
    "converted": {**_ID, "bytes": lambda v: _count(v, 0, MAX_MARKDOWN), "pages": _optional(_count),
                  "status": lambda v: _text(v, MAX_SHORT), "seconds": _number},
    "items": {**_ID, "count": lambda v: _count(v, 0, MAX_ITEMS),
              "bytes": lambda v: _count(v, 0, MAX_ITEMS_BYTES)},
    "refused": {**_ID, "reason": lambda v: _text(v, MAX_SENTENCE)},
    "needs_extra": {**_ID, "error": lambda v: _text(v, MAX_SENTENCE)},
    "unsupported": {**_ID, "error": lambda v: _text(v, MAX_SENTENCE)},
    "bye": {},
}

ITEM = {
    "label": lambda v: _text(v, MAX_SHORT),
    "page": _optional(_count),
    "lang": _optional(lambda v: _text(v, MAX_SHORT)),
    "height": _optional(_number),
    "left": _optional(_number),
    "layer": lambda v: _text(v, MAX_SHORT),
    "text_bytes": lambda v: _count(v, 0, MAX_ITEMS_BYTES),
}


def _line(obj: dict) -> bytes:
    try:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
    except (ValueError, UnicodeError) as e:
        raise FrameError(f"not a line JSON can carry ({type(e).__name__})") from None
    if len(data) > MAX_LINE:
        raise FrameError(f"a line of {len(data)} bytes is over the {MAX_LINE}-byte bound")
    return data


def _object(line: bytes) -> dict:
    """One JSON line, parsed: an object, with no duplicate key and no NaN. Raises FrameError."""
    if len(line) >= MAX_LINE:
        raise FrameError(f"a line of {len(line)} bytes is over the {MAX_LINE}-byte bound")
    try:
        obj = json.loads(line.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=_no_constant)
    except FrameError:
        raise
    except (UnicodeError, ValueError, RecursionError) as e:
        raise FrameError(f"not a JSON line ({type(e).__name__})") from None
    if type(obj) is not dict:
        raise FrameError("a line must be a JSON object")
    return obj


def _check(obj: dict, schema: dict, what: str) -> dict:
    keys = set(obj)
    if keys != set(schema):
        extra, missing = sorted(keys - set(schema)), sorted(set(schema) - keys)
        raise FrameError(f"a {what} with {len(extra)} unknown key(s) "
                         f"{_shown(extra[:3]) if extra else ''} and missing {missing}")
    bad = [key for key, check in schema.items() if not check(obj[key])]
    if bad:
        raise FrameError(f"a {what} whose {', '.join(bad)} is out of bounds")
    # A lone surrogate (`"\ud800"`) is valid JSON and not text: it would fail the moment the engine
    # wrote it out, outside every refusal. Every field is a scalar by now.
    for value in obj.values():
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise FrameError(f"a {what} whose text is not UTF-8") from None
    return obj


def encode(frame: dict) -> bytes:
    """One header or request line. Raises FrameError."""
    return _line(frame)


def decode(line: bytes) -> dict:
    """One header or request line, without its newline, checked against its schema. A header's
    `bytes` and `count` say what follows it; they are within the bounds above or this refuses."""
    frame = _object(line)
    if frame.get("v") != PROTOCOL or type(frame.get("v")) is not int:
        raise FrameError(f"protocol version {_shown(frame.get('v'))}, not {PROTOCOL}")
    kind = frame.get("type")
    schema = SCHEMAS.get(kind) if type(kind) is str else None
    if schema is None:
        raise FrameError(f"an unknown frame type {_shown(kind)}")
    body = {k: v for k, v in frame.items() if k not in ("v", "type")}
    _check(body, schema, f"{kind} frame")
    return frame


def text(payload: bytes) -> str:
    """A raw run, decoded strictly. Raises FrameError."""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise FrameError("a raw run that is not UTF-8") from None


def encode_converted(rid: int, markdown: str, *, pages, status: str, seconds) -> bytes:
    """A `converted` reply: its header and the markdown. Raises FrameError."""
    try:
        payload = markdown.encode("utf-8")
    except UnicodeError:
        raise FrameError("markdown that is not UTF-8") from None
    if len(payload) > MAX_MARKDOWN:
        raise FrameError(f"markdown of {len(payload)} bytes is over the {MAX_MARKDOWN}-byte bound")
    header = {"v": PROTOCOL, "type": "converted", "id": rid, "bytes": len(payload),
              "pages": pages, "status": status, "seconds": seconds}
    data = _line(header)
    decode(data[:-1])
    return data + payload


def encode_items(rid: int, items: list[dict]) -> bytes:
    """An `items` reply: its header and each item as a line and its text. Raises FrameError."""
    if len(items) > MAX_ITEMS:
        raise FrameError(f"{len(items)} items, over the {MAX_ITEMS} bound")
    records = []
    for item in items:
        try:
            raw = item["text"].encode("utf-8")
        except UnicodeError:
            raise FrameError("an item whose text is not UTF-8") from None
        fields = {k: v for k, v in item.items() if k != "text"}
        line = _line({**fields, "text_bytes": len(raw)})
        _check(_object(line[:-1]), ITEM, "item")
        records += (line, raw)
    payload = b"".join(records)
    if len(payload) > MAX_ITEMS_BYTES:
        raise FrameError(f"items of {len(payload)} bytes are over the {MAX_ITEMS_BYTES}-byte bound")
    data = _line({"v": PROTOCOL, "type": "items", "id": rid, "count": len(items),
                  "bytes": len(payload)})
    return data + payload


def decode_items(count: int, payload: bytes) -> list[dict]:
    """The `count` item records an `items` header announced, which must fill `payload` exactly.
    Raises FrameError."""
    items, at = [], 0
    for _ in range(count):
        end = payload.find(b"\n", at, at + MAX_LINE)
        if end < 0:
            raise FrameError("an item line that does not end within its bound or its payload")
        item = _check(_object(payload[at:end]), ITEM, "item")
        start, at = end + 1, end + 1 + item.pop("text_bytes")
        if at > len(payload):
            raise FrameError("an item whose text runs past its payload")
        item["text"] = text(payload[start:at])
        items.append(item)
    if at != len(payload):
        raise FrameError(f"{len(payload) - at} bytes after the {count} items the header announced")
    return items
