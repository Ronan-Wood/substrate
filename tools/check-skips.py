#!/usr/bin/env python3
"""Fail unless the tests a pytest junit report skipped are exactly the tests a list names.

    check-skips.py <junit.xml> <list>

The list holds one test id per line, `module::test`, as `tests/core-job-skips.txt` spells them.
`#` starts a comment, so each entry can say why it skips. A skip the list does not name fails, so a
test cannot start skipping unnoticed; so does a named test that ran, so the list cannot outlive the
reason for an entry.

Stdlib only, so it needs nothing the core job does not install.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def skipped(junit: Path) -> set[str]:
    """The ids of the test cases the report marks skipped."""
    return {f"{case.get('classname')}::{case.get('name')}"
            for case in ET.parse(junit).getroot().iter("testcase")
            if case.find("skipped") is not None}


def expected(listing: Path) -> set[str]:
    """The ids the list names, without comments or blank lines."""
    return {line for raw in listing.read_text("utf-8").splitlines()
            if (line := raw.split("#", 1)[0].strip())}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check-skips.py <junit.xml> <list>", file=sys.stderr)
        return 2
    junit, listing = map(Path, argv)
    got, want = skipped(junit), expected(listing)
    for test in sorted(got - want):
        print(f"check-skips: skipped, and {listing} does not name it: {test}", file=sys.stderr)
    for test in sorted(want - got):
        print(f"check-skips: named in {listing}, and it did not skip: {test}", file=sys.stderr)
    if got != want:
        return 1
    print(f"check-skips: {len(got)} skipped, each named in {listing}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
