"""A note's frontmatter names a blob in valid YAML, and the reference scan must find it by its key.

Each note names the blob's checksum, with a blank line or a comment line between the key and its
value, or between two items:

    source_sha256:        source_sha256:        source_sha256:        audio_sha256:
                            # the originals         # the original        - <other>
      - <checksum>          - <checksum>          <checksum>              # and the other side
                                                                          - <checksum>

Before Ronan-Wood/Scripta#3 the scan read a key's `- item` lines only until the first line that was
not one, so it missed each of these, and once the blob was older than seven days the plan listed it
for removal. Scripta#3 reads the frontmatter as YAML, and added a backstop: any checksum anywhere in
a note keeps its blob. The backstop alone would keep these blobs too, so a case counts as found only
when the key scan itself attributes the checksum to the note.

    uv run --no-sync python experiments/lean-blob-gc/harness/repro_hidden_reference.py

Prints each case's YAML value, then what the plan does with the blob. The exit code says what the
run found:

- 1: some case's reference is missed by the key scan: the plan removes the blob, or keeps it only
  by the backstop;
- 0: the key scan finds every one;
- 2: the repro itself is broken. An import or a case crashed, or a control misbehaved: the plain
  list must reach a plan that keeps the blob, and a note naming nothing must reach a plan that
  removes it.

Writes only under a temporary folder of its own, which it removes at the end.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(os.path.realpath(tempfile.mkdtemp(prefix="blobgc-repro-")))
os.environ["HOME"] = str(ROOT / "home")
os.environ.pop("SUBSTRATE_REGISTRY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def broken(why: str) -> None:
    """Exit 2: the repro could not run, which says nothing about the bug either way."""
    traceback.print_exc()
    shutil.rmtree(ROOT, ignore_errors=True)
    print(f"the repro is broken ({why}), so it says nothing about the bug")
    sys.exit(2)


try:
    import yaml

    from substrate import blob_lifecycle as life
    from substrate import blobs, scopes
except Exception:
    broken("an import failed")

DATA = b"the only copy of a recording\n"
C = hashlib.sha256(DATA).hexdigest()
OTHER = hashlib.sha256(b"the other side\n").hexdigest()
SHAPES = {
    "blank line": ("source_sha256", f"source_sha256:\n\n  - {C}"),
    "comment in a block list": ("source_sha256", f"source_sha256:\n  # the originals\n  - {C}"),
    "comment before the value": ("source_sha256", f"source_sha256:\n  # the original\n  {C}"),
    "comment between two items": (
        "audio_sha256", f"audio_sha256:\n  - {OTHER}\n  # and the other side\n  - {C}"),
    "blank line between two items": ("audio_sha256", f"audio_sha256:\n  - {OTHER}\n\n  - {C}"),
    "control: plain block list": ("source_sha256", f"source_sha256:\n  - {C}"),
    "control: names nothing": ("source_sha256", "source_sha256: []"),
}


def case(name: str, key: str, front: str) -> str:
    """`removed`, `kept`, `backstop` or `refused`: what the plan does with the blob, and `backstop`
    when it keeps it only because the checksum appears somewhere in the note, the backstop added
    after this repro was written, while the key scan itself missed the reference."""
    root = ROOT / name.replace(" ", "-").replace(":", "")
    home, vault = root / "home", root / "v-vault"
    (vault / "03-references").mkdir(parents=True)
    (home / ".substrate").mkdir(parents=True)
    (vault / ".substrate.toml").write_text('name = "v"\ninherits = []\n', "utf-8")
    blobs.config_path = lambda: home / ".substrate" / blobs.CONFIG_NAME
    blobs.default_root = lambda: home / ".substrate" / "blobs"
    scopes.DEFAULT_REGISTRY = home / ".substrate" / "scopes.toml"
    registry = root / "scopes.toml"
    registry.write_text(f'[scopes.v]\nvault = "{vault}"\ndb = "{root / "v.db"}"\n', "utf-8")

    (root / "a.pdf").write_bytes(DATA)
    put = blobs.put(root / "a.pdf")
    old = 1_000_000_000
    os.utime(put.path, (old, old))
    (vault / "03-references" / "a.md").write_text(f"---\ntitle: t\n{front}\n---\n\n# t\n", "utf-8")

    named = yaml.safe_load(f"title: t\n{front}")[key]
    try:
        outcome = "removed" if C in life.gc_plan(registry).remove else "kept"
        detail = f"the plan {'removes' if outcome == 'removed' else 'keeps'} the blob"
        by_key = any(r.key != "text" for r in life.scan(registry).refs.get(C, []))
        if outcome == "kept" and not by_key:
            outcome, detail = "backstop", "the plan keeps the blob, but only by the backstop"
    except life.Refused as e:
        outcome, detail = "refused", f"refused: {(str(e).splitlines() or [''])[0]}"
    print(f"{name}:\n  YAML says {key} = {named!r}\n  {detail}\n")
    return outcome


if __name__ == "__main__":
    try:
        removed = {name: case(name, key, front) for name, (key, front) in SHAPES.items()}
    except Exception:
        broken("a case crashed")
    shutil.rmtree(ROOT)
    kept, freed = removed.pop("control: plain block list"), removed.pop("control: names nothing")
    if kept == "backstop":
        print("the key scan misses even the plain block list: only the backstop kept the blob")
        sys.exit(1)
    if (kept, freed) != ("kept", "removed"):
        print(f"the controls gave {kept!r} and {freed!r}, not 'kept' and 'removed': the repro is "
              "broken")
        sys.exit(2)
    hit = [n for n, r in removed.items() if r in ("removed", "backstop")]
    print(f"{len(hit)} of {len(removed)} spellings have a reference the key scan misses"
          + (f": {', '.join(hit)}" if hit else ""))
    sys.exit(1 if hit else 0)
