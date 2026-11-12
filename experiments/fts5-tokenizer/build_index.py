"""Make the two arms of the experiment from one real index, which is only read.

    python build_index.py SRC.db OUTDIR NAME

Copies SRC to OUTDIR/NAME.base.db, upgrades that copy to the engine's schema, then writes
OUTDIR/NAME.unicode61.db and OUTDIR/NAME.substrate.db from it. Both arms hold the same corpus
(same chunks, chunk_ids and vectors); only `chunks_fts` differs, rebuilt under each tokenizer.
The unicode61 arm uses the engine's own `chunks_fts` DDL verbatim, so it is the production table.

The upgrade adds `documents.note_sha256` (v11) and the v10 identity tables. A v8 source is stamped
v11 WITHOUT v9's `document_class` relabel (`reference-frozen` -> `unclassified`); retrieval and the
eval do not branch on that value, and both arms share it.

Refuses ~/.substrate, any `out-vault/` and any `substrate/out/` as a destination, a destination that
is a symlink, one with WAL sidecars left by an earlier aborted run (delete them by hand), a source
whose -wal holds content, and a source older than v8 or newer than the engine's schema.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
# SUBSTRATE_ENGINE: the engine tree to import (the directory holding the `substrate` package).
sys.path.insert(0, os.environ.get("SUBSTRATE_ENGINE") or str(HERE.parent.parent / "substrate"))
sys.path.insert(0, str(HERE))

import substrate_tok  # noqa: E402
from substrate.store import schema  # noqa: E402

_FTS_DDL = re.search(r"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5\((.*?)\n\);",
                     schema.DDL, re.S)
if _FTS_DDL is None:
    sys.exit("REFUSING: could not find chunks_fts in schema.DDL; the engine's DDL moved")
if "tokenize" in _FTS_DDL[1]:
    sys.exit("REFUSING: the engine's chunks_fts already names a tokenizer; the arms would differ twice")
FTS = {"unicode61": _FTS_DDL[1], "substrate": _FTS_DDL[1] + ",\n    tokenize='substrate'"}


def refuse_real(p: Path) -> None:
    # Casefolded: APFS is case-insensitive, so `~/.Substrate` or `Out-Vault` is the live location too.
    rp = Path(str(p.resolve()).casefold())
    home = Path(str((Path.home() / ".substrate").resolve()).casefold())
    parts = rp.parts
    if (rp == home or home in rp.parents or "out-vault" in parts
            or ("substrate", "out") in zip(parts, parts[1:])):
        sys.exit(f"REFUSING: {p} is a live index location")


def fresh_destination(dst: Path) -> None:
    refuse_real(dst)
    if dst.is_symlink():
        sys.exit(f"REFUSING: {dst} is a symlink")
    left = [s for s in ("-wal", "-shm") if Path(f"{dst}{s}").exists()]
    if left:
        sys.exit(f"REFUSING: {dst} has {', '.join(left)} from an earlier run; SQLite would replay it "
                 "onto the new copy. Delete them and re-run.")


def read_only(src: Path) -> sqlite3.Connection:
    """A read of SRC that writes nothing beside it. `immutable=1` reads only the main file, so a
    source whose -wal holds content is refused rather than opened `mode=ro`, which can write -shm."""
    wal = Path(f"{src}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        sys.exit(f"REFUSING: {src} has uncheckpointed WAL content; copy it while nothing writes it, "
                 "or checkpoint it first")
    return sqlite3.connect(f"file:{quote(str(src.resolve()))}?immutable=1", uri=True)


def upgrade(path: Path) -> None:
    db = sqlite3.connect(path, isolation_level=None)
    v = db.execute("PRAGMA user_version").fetchone()[0]
    if v < 8:
        sys.exit(f"REFUSING: {path} is schema v{v}; only v8 and later upgrade additively to v11")
    if v > schema.SCHEMA_VERSION:
        sys.exit(f"REFUSING: {path} is schema v{v}, newer than this engine's v{schema.SCHEMA_VERSION}")
    if v != schema.SCHEMA_VERSION:
        if "note_sha256" not in [r[1] for r in db.execute("PRAGMA table_info(documents)")]:
            db.execute("ALTER TABLE documents ADD COLUMN note_sha256 TEXT")
        db.executescript(schema.DDL)
        db.execute(f"PRAGMA user_version={schema.SCHEMA_VERSION}")
    db.close()


def arm(base: Path, dst: Path, tokenizer: str) -> None:
    fresh_destination(dst)
    shutil.copyfile(base, dst)
    db = substrate_tok.load(sqlite3.connect(dst, isolation_level=None))
    db.execute("DROP TABLE chunks_fts")
    db.execute(f"CREATE VIRTUAL TABLE chunks_fts USING fts5({FTS[tokenizer]}\n)")
    t = time.perf_counter()
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    took = time.perf_counter() - t
    # rank=1: re-tokenize the external content and compare checksums, not only the index's structure.
    db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)")
    n = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    db.execute("CREATE VIRTUAL TABLE temp.v USING fts5vocab(main, chunks_fts, row)")
    terms = db.execute("SELECT count(*) FROM temp.v").fetchone()[0]
    db.execute("VACUUM")
    db.close()
    print(f"  {dst.name}: {n} chunks, {terms} distinct terms, rebuild {took:.3f}s, "
          f"{dst.stat().st_size/1e6:.1f} MB")


def main() -> None:
    src, out, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        sys.exit(f"REFUSING: NAME {name!r} must be [A-Za-z0-9_-]+")
    refuse_real(out)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"{name}.base.db"
    fresh_destination(base)
    s, d = read_only(src), sqlite3.connect(base)
    s.backup(d)
    s.close()
    d.close()
    upgrade(base)
    for tok in FTS:
        arm(base, out / f"{name}.{tok}.db", tok)


if __name__ == "__main__":
    main()
