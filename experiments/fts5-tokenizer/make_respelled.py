"""Derive RESPELLED identifier cases from the large private eval set.

    python make_respelled.py [snake]

The set's identifier cases spell each identifier as the note does, and name it in `answer`. This
rewrites only those identifiers in the query, one token at a time (a token as TOKEN finds it), and
keeps the rest of the query, the answer and the attribution unchanged:
  - a token containing `/` `.` `\\` or `:` (a path or dotted name) becomes its words;
  - a token joined by `_` or `-` becomes camelCase, unless its parts are all digits (a date);
  - a camelCase or PascalCase token becomes its words.
With `snake`, only camelCase tokens are rewritten, to snake_case (gold-respelled-snake-*.json,
stratum `respelled-camel2snake`). Camel boundaries are the tokenizer's (split.rs): `URLs` and `IPv6`
stay whole. A case is kept only if the rewrite changes the engine's own query terms (`fts.terms`).
Cases graded by `targets` name no identifier and are skipped. Reads and writes only under
$PRIVATE_EVAL; output: $PRIVATE_EVAL/fts5-tokenizer/gold-respelled[-snake]-<scope>.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("SUBSTRATE_ENGINE") or str(HERE.parent.parent / "substrate"))
from substrate.store import fts  # noqa: E402

PRIVATE = Path(os.environ.get("PRIVATE_EVAL") or Path.home() / "CodeHome/substrate-eval-private")
SCOPES = tuple(json.loads((PRIVATE / "fts5-tokenizer" / "labels.json").read_text()))
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./\\:-]*[A-Za-z0-9]")


def camel(w: str) -> list[str]:
    """split.rs's camel boundaries: lower→Upper, and Upper→Upper-lower with at least two capitals on
    the left unless the lowercase is a lone plural `s`. ASCII only; digits never form a boundary."""
    n, out, frm = len(w), [], 0
    up = lambda k: w[k].isascii() and w[k].isupper()  # noqa: E731
    lo = lambda k: k < n and w[k].isascii() and w[k].islower()  # noqa: E731
    for j in range(1, n + 1):
        if j == n or (up(j) and (lo(j - 1) or (up(j - 1) and j - frm >= 2 and lo(j + 1)
                                                and (lo(j + 2) or w[j + 1] != "s")))):
            out.append(w[frm:j])
            frm = j
    return out


def words(tok: str) -> list[str]:
    return [w for part in re.split(r"[_./\\:-]+", tok) if part for w in camel(part)]


def respell(tok: str, snake: bool) -> tuple[str, str] | None:
    if snake:
        if re.search(r"[_./\\:-]", tok) or len(camel(tok)) < 2:
            return None
        return "_".join(w.lower() for w in camel(tok)), "camel2snake"
    if re.search(r"[./\\:]", tok) and re.search(r"[A-Za-z]", tok):
        return " ".join(w.lower() for w in words(tok)), "path"
    if re.search(r"[_-]", tok):
        ws = [w for w in re.split(r"[_-]+", tok) if w]
        if len(ws) > 1 and not all(w.isdigit() for w in ws):
            return ws[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in ws[1:]), "joined"
    if len(camel(tok)) > 1:
        return " ".join(w.lower() for w in camel(tok)), "camel"
    return None


def main() -> None:
    if sys.argv[1:] not in ([], ["snake"]):
        sys.exit("usage: make_respelled.py [snake]")
    snake = sys.argv[1:] == ["snake"]
    out_dir = PRIVATE / "fts5-tokenizer"
    for scope in SCOPES:
        src = json.loads((PRIVATE / "gold" / f"gold-{scope}.json").read_text())
        cases, skipped = [], 0
        for c in src["cases"]:
            if c.get("stratum", "").removeprefix("core-") != "identifier":
                continue
            idents = {t for a in c.get("answer") or [] for t in TOKEN.findall(a)}
            shapes = []

            def sub(m: re.Match) -> str:
                r = respell(m.group(), snake) if m.group() in idents else None
                if r is None:
                    return m.group()
                shapes.append(r[1])
                return r[0]

            q = TOKEN.sub(sub, c["query"])
            if not shapes or fts.terms(q) == fts.terms(c["query"]):
                skipped += 1
                continue
            cases.append(dict(c, id=c["id"] + "-respelled", query=q,
                              stratum="respelled-" + "+".join(sorted(set(shapes)))))
        name = f"gold-respelled{'-snake' if snake else ''}-{scope}.json"
        doc = [f"PRIVATE. Derived mechanically from gold-{scope}.json by make_respelled.py; see its docstring."]
        (out_dir / name).write_text(
            json.dumps({"_doc": doc, "gates": src["gates"], "snapshot": src["snapshot"], "cases": cases},
                       indent=1, ensure_ascii=False))
        print(f"  {scope}: {len(cases)} respelled, {skipped} unchanged by the rewrite and dropped")


if __name__ == "__main__":
    main()
