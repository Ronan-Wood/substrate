"""Load the `substrate` FTS5 tokenizer into sqlite3 connections.

    import substrate_tok
    substrate_tok.load(conn)            # one connection
    substrate_tok.install()             # every sqlite3.connect() from here on

FTS5 tokenizers are registered PER CONNECTION, so any connection that reads or writes a table
declared `tokenize='substrate'` must load it first, or SQLite raises "no such tokenizer". That
includes writers: the chunks_ai / chunks_ad triggers insert into the FTS table.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

LIB = Path(os.environ.get("SUBSTRATE_TOK_LIB")
           or Path(__file__).parent / "target" / "release" / "libsubstrate_tok.dylib")
ENTRY = "sqlite3_substratetok_init"

_orig_connect = sqlite3.connect


def load(conn: sqlite3.Connection) -> sqlite3.Connection:
    if not LIB.exists():
        raise FileNotFoundError(f"{LIB} is not built; run `cargo build --release`")
    # The C API only: SQL's load_extension() stays "not authorized" throughout, unlike under
    # enable_load_extension(True).
    flag = sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION
    conn.setconfig(flag, True)
    try:
        conn.load_extension(str(LIB), entrypoint=ENTRY)
    finally:
        conn.setconfig(flag, False)
    return conn


def _connect(*args, **kwargs):
    return load(_orig_connect(*args, **kwargs))


def install() -> None:
    """Patch sqlite3.connect so every new connection has the tokenizer. Experiment use only."""
    sqlite3.connect = _connect


def tokens(text: str, tokenize: str = "substrate") -> list[tuple[int, str]]:
    """(position, term) pairs the tokenizer emits for `text` at DOCUMENT time, read back through
    fts5vocab; colocated terms share a position. Query-time tokens are not observable this way."""
    db = load(_orig_connect(":memory:"))
    db.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize={_quote(tokenize)})")
    db.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, instance)")
    db.execute("INSERT INTO t(x) VALUES (?)", (text,))
    rows = db.execute("SELECT offset, term FROM v ORDER BY offset, term").fetchall()
    db.close()
    return [(int(o), t) for o, t in rows]


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
