"""Tests of bench_large.py's guards, on synthetic indexes. Needs the engine, so run it in the engine's
environment:

    PY -I test_bench_large.py        (PY as in run_large.sh; SUBSTRATE_ENGINE as for build_index.py)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_PRIVATE = tempfile.TemporaryDirectory()
(Path(_PRIVATE.name) / "fts5-tokenizer").mkdir()
(Path(_PRIVATE.name) / "fts5-tokenizer" / "labels.json").write_text(json.dumps({"toy": "vault T"}))
os.environ["PRIVATE_EVAL"] = _PRIVATE.name
sys.path.insert(0, str(HERE))

import bench_large as b  # noqa: E402


def toy_pair(d: Path) -> None:
    """Both arms of an empty index at the engine's schema, `chunks_fts` under each tokenizer."""
    for arm in b.ARMS:
        db = sqlite3.connect(d / f"toy.{arm}.db", isolation_level=None)
        db.executescript(b.schema.DDL)
        db.execute(f"PRAGMA user_version={b.schema.SCHEMA_VERSION}")
        db.execute("DROP TABLE chunks_fts")
        db.execute(f"CREATE VIRTUAL TABLE chunks_fts USING fts5({b.FTS[arm]}\n)")
        db.close()


class Guards(unittest.TestCase):
    def test_extension_loads_by_file_name_not_by_directory(self):
        self.assertFalse(b._is_substrate_arm("/x/runs.substrate.v2/toy.unicode61.db"))
        self.assertTrue(b._is_substrate_arm("/x/toy.substrate.db"))
        self.assertTrue(b._is_substrate_arm("file:/x/toy.substrate.db?immutable=1"))

    def test_scratch_in_a_letter_case_variant_of_a_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            for p in (Path(d) / "bench-tmp", Path(d.swapcase()) / "sub" / "bench-tmp"):
                with self.subTest(p=p), self.assertRaises(SystemExit):
                    b.refuse_in_git(p)

    def test_scratch_outside_any_working_tree_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            b.refuse_in_git(Path(d) / "bench-rebuild")

    def test_t_quantile_never_narrower_than_the_true_one(self):
        self.assertGreaterEqual(b.t975(11), 2.201)
        self.assertGreaterEqual(b.t975(31), 2.040)


class Preflight(unittest.TestCase):
    def test_a_consistent_pair_passes(self):
        with tempfile.TemporaryDirectory() as d:
            toy_pair(Path(d))
            self.assertEqual(b.preflight(Path(d).resolve(), "toy"), {"chunks": 0})

    def test_an_index_that_disagrees_with_its_content_is_refused(self):
        # What a substrate arm built by another tokenizer build looks like to FTS5: index entries the
        # content does not produce. The structural check alone passes it.
        with tempfile.TemporaryDirectory() as d:
            toy_pair(Path(d))
            db = sqlite3.connect(Path(d) / "toy.substrate.db", isolation_level=None)
            db.execute("INSERT INTO chunks_fts(rowid, text_with_path) VALUES (1, 'orderLoader')")
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            db.close()
            with self.assertRaises(SystemExit) as e:
                b.preflight(Path(d).resolve(), "toy")
            self.assertIn("integrity-check", str(e.exception))
            self.assertNotIn("toy", str(e.exception).replace("vault T", ""))


class LoadGuard(unittest.TestCase):
    def test_a_block_a_burst_interrupts_is_redone_and_its_caches_reset(self):
        runs, resets = [], []

        def block():
            if not runs:  # a "burst" only during the first attempt
                subprocess.run(["/bin/sh", "-c", "sleep 5; :", "zz-bench-guard-probe"], check=True)
            runs.append(1)
            return "result"

        g = b.Guard(True, burst="zz-bench-guard-probe", max_load=1e9, resets=[lambda: resets.append(1)])
        self.assertEqual(g.run("probe", block), "result")
        self.assertEqual([e["kept"] for e in g.log], [False, True])
        self.assertEqual((len(runs), len(resets)), (2, 1))

    def test_a_refusal_saves_what_finished_before_it_exits(self):
        saved = []

        def block():
            subprocess.run(["/bin/sh", "-c", "sleep 5; :", "zz-bench-guard-probe"], check=True)

        g = b.Guard(True, burst="zz-bench-guard-probe", max_load=1e9, attempts=1,
                    on_refuse=lambda: saved.append(len(g.log)))
        with self.assertRaises(SystemExit):
            g.run("probe", block)
        self.assertEqual(saved, [1])

    def test_off_runs_the_block_once_without_logging(self):
        g = b.Guard(False)
        self.assertEqual(g.run("x", lambda: 7), 7)
        self.assertEqual(g.log, [])


class EndToEnd(unittest.TestCase):
    def test_a_scope_whose_cases_all_drop_out_reports_nothing_instead_of_crashing(self):
        class A:
            repeats, seed, tok_iters = 1, 7, 1
        with tempfile.TemporaryDirectory() as d:
            toy_pair(Path(d))
            res, _ = b.sec_e2e(Path(d).resolve(), "toy", [{"id": "c1", "query": "order loader", "doc": "absent"}],
                               "lex", None, b.TokTimer(1), A())
            self.assertEqual(res["retrieve"]["n"], 0)


if __name__ == "__main__":
    unittest.main()
