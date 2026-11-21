"""`tools/check-skips.py`, which holds the core job's skips to the tests its list names."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check-skips.py"

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">
<testcase classname="tests.test_a" name="test_ran" />
<testcase classname="tests.test_a" name="test_skips[x]"><skipped message="no PIL" /></testcase>
<testcase classname="tests.test_b" name="test_also_skips"><skipped message="no docling" /></testcase>
</testsuite></testsuites>"""


def _check(tmp_path: Path, listing: str) -> subprocess.CompletedProcess:
    (tmp_path / "junit.xml").write_text(JUNIT, "utf-8")
    (tmp_path / "skips.txt").write_text(listing, "utf-8")
    return subprocess.run([sys.executable, str(TOOL), str(tmp_path / "junit.xml"),
                           str(tmp_path / "skips.txt")], capture_output=True, text=True)


def test_the_named_skips_pass(tmp_path: Path) -> None:
    r = _check(tmp_path, "# why each skips\ntests.test_a::test_skips[x]  # no PIL\n\n"
                         "tests.test_b::test_also_skips\n")
    assert r.returncode == 0, r.stderr
    assert "2 skipped" in r.stdout


def test_a_skip_the_list_does_not_name_fails(tmp_path: Path) -> None:
    r = _check(tmp_path, "tests.test_a::test_skips[x]\n")
    assert r.returncode == 1
    assert "does not name it: tests.test_b::test_also_skips" in r.stderr


def test_a_named_test_that_ran_fails(tmp_path: Path) -> None:
    r = _check(tmp_path, "tests.test_a::test_skips[x]\ntests.test_b::test_also_skips\n"
                         "tests.test_a::test_ran\n")
    assert r.returncode == 1
    assert "did not skip: tests.test_a::test_ran" in r.stderr
