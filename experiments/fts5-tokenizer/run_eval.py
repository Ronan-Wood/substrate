"""`substrate eval` with the tokenizer loaded into every sqlite3 connection.

    python run_eval.py --db SCRATCH.substrate.db --gold ../../substrate/eval/gold.json ...

Arguments are `substrate eval`'s. No engine code changes: the shim patches `sqlite3.connect`
before the engine imports, which is the only way an unmodified engine can open a table declared
`tokenize='substrate'`. Running it against a unicode61 index is harmless (the load is a no-op
for that table), so both arms go through the same entry point.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SUBSTRATE_ENGINE: the engine tree to import (the directory holding the `substrate` package).
sys.path.insert(0, os.environ.get("SUBSTRATE_ENGINE") or str(HERE.parent.parent / "substrate"))
sys.path.insert(0, str(HERE))

import substrate_tok  # noqa: E402

substrate_tok.install()

from substrate.cli import main  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:]
    # Never the engine's default `eval/.baseline.json`: a baseline from some other run would
    # relabel cases (`MISS (was rank N)`) and is not a property of either arm.
    if not any(a == "--baseline" or a.startswith("--baseline=") for a in args):
        args += ["--baseline", str(HERE / "scratch" / "no-baseline.json")]
    sys.exit(main(["eval", *args]))
