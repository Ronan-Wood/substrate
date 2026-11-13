"""Build the parity inputs in SCRATCH from a copied index: rerank prompts and HyDE prompts made by
substrate's own code, so every runtime is fed byte-identical text.
  PYTHONPATH=substrate python make_inputs.py <index.db> <gold.json> <out_dir> [n_queries]
"""
import json, sys
from pathlib import Path

from substrate.retrieve import rerank_cross as rc
from substrate.retrieve.expand import PROMPTS
from substrate.store.index_store import IndexStore

db, gold, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
n = int(sys.argv[4]) if len(sys.argv) > 4 else 10
store = IndexStore(db, migrate=False)
if gold == "--headings":
    # Section headings from the copied index stand in as queries: realistic for this corpus, and
    # they never leave scratch. Deterministic: every k-th distinct deep heading.
    import sqlite3
    rows = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "SELECT DISTINCT path_str FROM chunks WHERE kind='passage' AND path_depth >= 2 "
        "ORDER BY path_str").fetchall()
    step = max(1, len(rows) // n)
    queries = [r[0].split(" > ")[-1] for r in rows[::step]][:n]
else:
    queries = [c["query"] for c in json.load(open(gold))["cases"]][:n]
pairs = []
for qi, q in enumerate(queries):
    for h in store.search(q, k=8, kind="passage"):
        prompt = rc.TEMPLATE.format(system=rc.SYSTEM, instruction=rc.INSTRUCTION,
                                    query=rc._defang(q),
                                    doc=rc._defang(" ".join(h.text.split()))[:rc.SNIPPET])
        pairs.append({"q": qi, "chunk": h.chunk_id, "prompt": prompt})
store.close()
(out / "pairs.json").write_text(json.dumps(pairs))
(out / "hyde_prompts.json").write_text(json.dumps([PROMPTS["canonical"].format(q=q) for q in queries]))
print(len(pairs), "pairs,", len(queries), "HyDE prompts →", out)
