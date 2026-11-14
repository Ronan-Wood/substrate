"""What a compose tells the refresh pass that ran it: one JSON record per line, in a file the pass
names (`substrate compose --report PATH`, a flag the help does not list).

The pass used to keep every line of compose's stdout that started `pruned:` or `quarantined:`.
Compose prints note and folder names on stdout, and a vault's author chooses those names, so a
file named with a line break followed by `pruned: …` added a line of its own to the pass's
report. Escaping each place compose prints a name is a list of places. Here nothing the pass
reads is free text: each record is one JSON object, a name inside it is a JSON string, and the
pass renders the lines itself.

The file is private to the engine: it is not in `contract/`, and no client reads it.

A compose that refuses writes a `refused` record holding its FATAL text, as a JSON string, so the
pass shows why without splicing compose's stderr into its report; the stderr goes only to the
engine's log, each line under a fixed prefix (`refresh.run_pass`). A `quarantined` record names
the note's path, kind and key, never the reason, which quotes the note.

A report that does not end with an `end` record, holds a line that is not a record, or is
missing, was cut short. The pass says so in its report, loudly, rather than reading it as
"nothing pruned, nothing quarantined" (`read`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from substrate import quarantine

PRUNED = "pruned"
PRUNED_VAULT = "pruned_vault"
QUARANTINED = "quarantined"
REFUSED = "refused"
END = "end"

# The longest a `refused` record is shown in a pass's report.
MAX_REFUSED_CHARS = 2000


class Writer:
    """Appends records to the report, each flushed as it is written, so a compose that dies
    leaves the records before it and no `end`."""

    def __init__(self, path: str | os.PathLike) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
        self._fh = os.fdopen(fd, "w", encoding="utf-8")

    def write(self, kind: str, /, **fields) -> None:
        self._fh.write(json.dumps({**fields, "kind": kind}, ensure_ascii=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class Null:
    """The report of a compose that was not asked for one: run by hand."""

    def write(self, kind: str, /, **fields) -> None:
        pass

    def close(self) -> None:
        pass


def open_report(path: str | None) -> Writer | Null:
    return Null() if path is None else Writer(path)


def read(path: Path, exit_code: int) -> tuple[list[str], str | None]:
    """The report's lines as a pass shows them, and what is wrong with it, or None. Wrong: the
    file is missing or unreadable, a line is not a record, there is no `end`, or `end` names
    another exit code than compose's."""
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [], f"the compose report could not be read ({type(e).__name__})"
    lines: list[str] = []
    ended: int | None = None
    for n, raw in enumerate(text.splitlines(), 1):
        try:
            rec = json.loads(raw)
            line = _line(rec) if rec.get("kind") != END else None
        except (ValueError, AttributeError, KeyError, TypeError):
            return lines, f"line {n} of the compose report is not a record"
        if rec["kind"] == END:
            ended = rec.get("exit")
            continue
        if ended is not None:
            return lines, f"the compose report goes on after its end (line {n})"
        lines.append(line)
    if ended is None:
        return lines, ("the compose report has no end record: compose was cut short, or did not "
                       "start")
    if ended != exit_code:
        return lines, f"the compose report ends with exit {ended!r}, and compose exited {exit_code}"
    return lines, None


def _line(rec: dict) -> str:
    s = quarantine.shown
    kind = rec["kind"]
    if kind == PRUNED:
        return f"pruned: {s(rec['vault'])}/{s(rec['note'])}  ({s(rec['why'])}; removed " \
               f"{s(rec['folder'])})"
    if kind == PRUNED_VAULT:
        return (f"pruned: vault {s(rec['vault'])!r} now composes 0 notes; removed "
                f"{int(rec['removed'])} ingest folder(s)")
    if kind == QUARANTINED:
        what, key = quarantine.checked(rec["fault"], rec["key"])
        return f"quarantined: {s(rec['path'])}  ({what}{': ' + key if key else ''})"
    if kind == REFUSED:
        return f"refused: {s(str(rec['message']), MAX_REFUSED_CHARS)}"
    raise KeyError(kind)
