"""End-to-end tests of the `substrate` FTS5 tokenizer against real SQLite + unicode61.

Run: `cargo build --release && python3 -I test_substrate_tok.py`.
"""

from __future__ import annotations

import random
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import substrate_tok as st  # noqa: E402


def positional(toks):
    """The non-colocated stream: first term at each position, in order."""
    out, seen = [], set()
    for pos, term in toks:
        if pos not in seen:
            seen.add(pos)
            out.append(term)
    return out


def at(toks, pos):
    return sorted(t for p, t in toks if p == pos)


_OPEN: list[sqlite3.Connection] = []


def tearDownModule():
    for db in _OPEN:
        db.close()


def table(rows, tokenize="substrate"):
    db = st.load(sqlite3.connect(":memory:"))
    _OPEN.append(db)
    db.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize={st._quote(tokenize)})")
    db.executemany("INSERT INTO t(x) VALUES (?)", [(r,) for r in rows])
    return db


def match(db, expr):
    return [r[0] for r in db.execute("SELECT x FROM t WHERE t MATCH ? ORDER BY rowid", (expr,))]


class Splitting(unittest.TestCase):
    def test_camel_and_pascal_split_and_keep_the_whole(self):
        toks = st.tokens("IndexStore camelCase")
        self.assertEqual(positional(toks), ["index", "store", "camel", "case"])
        self.assertEqual(at(toks, 0), ["index", "indexstore"])
        self.assertEqual(at(toks, 2), ["camel", "camelcase"])

    def test_snake_kebab_dotted_and_paths(self):
        for text, parts, whole in [
            ("text_with_path", ["text", "with", "path"], "textwithpath"),
            ("kebab-case", ["kebab", "case"], "kebabcase"),
            ("substrate.store.fts", ["substrate", "store", "fts"], "substratestorefts"),
            ("store/fts.py", ["store", "fts", "py"], "storeftspy"),
            ("C:\\Users\\x", ["c", "users", "x"], "cusersx"),
        ]:
            with self.subTest(text=text):
                toks = st.tokens(text)
                self.assertEqual(positional(toks), parts)
                self.assertIn(whole, at(toks, 0))

    def test_camel_segment_inside_a_path_gets_its_own_whole(self):
        toks = st.tokens("store/IndexStore.py")
        self.assertEqual(positional(toks), ["store", "index", "store", "py"])
        self.assertEqual(at(toks, 0), ["store", "storeindexstorepy"])
        self.assertEqual(at(toks, 1), ["index", "indexstore"])

    def test_acronyms(self):
        toks = st.tokens("HTTPServer XMLHttpRequest IPv6 OAuth macOS")
        self.assertEqual(positional(toks),
                         ["http", "server", "xml", "http", "request", "ipv6", "oauth", "mac", "os"])

    def test_letters_and_digits_do_not_split(self):
        self.assertEqual(st.tokens("sha256 qwen3 v11"), [(0, "sha256"), (1, "qwen3"), (2, "v11")])

    def test_all_digit_compound_gets_no_whole(self):
        self.assertEqual(st.tokens("2026-11-06"), [(0, "2026"), (1, "11"), (2, "06")])

    def test_non_ascii_letters_fold_and_lower_to_upper_still_splits(self):
        toks = st.tokens("naïveBayes ÉcoleNormale")
        self.assertEqual(positional(toks), ["naive", "bayes", "ecole", "normale"])
        self.assertIn("naivebayes", at(toks, 0))

    def test_soft_hyphen_and_joiners_are_ignored(self):
        self.assertEqual(st.tokens("trans\u00adformer"), [(0, "transformer")])
        self.assertEqual(st.tokens("zero\u200dwidth"), [(0, "zerowidth")])

    def test_unicode_hyphens_join(self):
        toks = st.tokens("non\u2011breaking")
        self.assertEqual(positional(toks), ["non", "breaking"])
        self.assertIn("nonbreaking", at(toks, 0))

    def test_runs_of_only_joiners_emit_nothing(self):
        self.assertEqual(st.tokens("... -- // _ :"), [])

    def test_long_compound_keeps_its_parts_but_no_whole(self):
        long = "/".join(["segment"] * 30)  # 210 bytes of parts > MAX_CONCAT
        toks = st.tokens(long)
        self.assertEqual(len(toks), 30)
        self.assertEqual(positional(toks), ["segment"] * 30)


class ParityWithUnicode61(unittest.TestCase):
    PROSE = ("The engine — not the app — owns retrieval. “Refuse rather than mislead,” it said; "
             "the index's v11 schema → note_sha256, at 3.53.1. Café, naïve, Zürich. "
             "Wait… what's next? A/B tests, e.g. top-down vs bottom-up. "
             "The URLs, APIs, LLMs, UUIDs and ADRs stay whole.")

    def test_positional_stream_equals_unicode61_when_there_is_no_camel_case(self):
        self.assertEqual(positional(st.tokens(self.PROSE)),
                         positional(st.tokens(self.PROSE, "unicode61")))

    def test_typographic_punctuation_separates_like_unicode61(self):
        text = "a—b a–b “a” ‘a’ a→b a…b a·b"
        self.assertEqual(positional(st.tokens(text)), positional(st.tokens(text, "unicode61")))
        self.assertEqual(st.tokens("a—b"), [(0, "a"), (1, "b")])

    def test_arguments_reach_unicode61(self):
        self.assertEqual(st.tokens("Café", "substrate remove_diacritics 0"), [(0, "café")])
        self.assertEqual(st.tokens("Café", "substrate"), [(0, "cafe")])


class Querying(unittest.TestCase):
    DOCS = ["see IndexStore here", "see index_store here", "see index-store here",
            "an index of the store", "see indexstore here", "snakeCase helper"]

    def setUp(self):
        self.db = table(self.DOCS)

    def test_whole_term_matches_every_spelling_of_the_identifier(self):
        self.assertEqual(match(self.db, '"indexstore"'), self.DOCS[0:3] + [self.DOCS[4]])

    def test_phrase_over_parts_matches_the_compound_not_the_prose(self):
        self.assertEqual(match(self.db, '"index store"'), self.DOCS[0:3])

    def test_a_part_matches_alone(self):
        self.assertEqual(match(self.db, '"store"'), self.DOCS[0:4])

    def test_snake_case_query_matches_camel_case_doc(self):
        self.assertEqual(match(self.db, '"snake_case"*'), [self.DOCS[5]])

    def test_lowercased_snake_query_matches_camel_inside_snake(self):
        # The engine lowercases queries, so `foo_barBaz` arrives as the phrase `foo barbaz`.
        db = table(["call foo_barBaz here", "call foo bar baz here"])
        self.assertEqual(match(db, '"foo_barbaz"'), ["call foo_barBaz here"])

    @unittest.expectedFailure
    def test_known_gap_lowercased_phrase_over_a_camel_word_before_the_last(self):
        # unicode61 matches these; substrate gives a camel word's pieces their own positions, so the
        # phrase `foobar baz` finds `baz` one position late. Mirroring unicode61's positions closes
        # it but loses `snake_case` → `snakeCase` (README, Findings), so it stays open.
        for doc, q in [("call fooBar_baz here", '"foobar_baz"'), ("call a_bC_d here", '"a_bc_d"')]:
            self.assertEqual(match(table([doc]), q), [doc])

    def test_prefix_on_whole_term(self):
        self.assertEqual(match(self.db, '"indexst"*'), self.DOCS[0:3] + [self.DOCS[4]])

    def test_unicode61_baseline_for_contrast(self):
        u = table(self.DOCS, "unicode61")
        self.assertEqual(match(u, '"indexstore"'), [self.DOCS[0], self.DOCS[4]])

    def test_highlight_offsets_point_at_the_part(self):
        row = self.db.execute(
            "SELECT highlight(t, 0, '[', ']') FROM t WHERE t MATCH 'store' AND rowid=1"
        ).fetchone()
        self.assertEqual(row[0], "see Index[Store] here")

    def test_highlight_offsets_around_an_ignorable(self):
        db = table(["see Index\u00adStore here"])
        row = db.execute("SELECT highlight(t, 0, '[', ']') FROM t WHERE t MATCH 'store'").fetchone()
        self.assertEqual(row[0], "see Index\u00ad[Store] here")

    def test_colocated_terms_do_not_count_toward_column_length(self):
        db = table(["IndexStore"])
        sz = db.execute("SELECT sz FROM t_docsize").fetchone()[0]
        self.assertEqual(sz, bytes([2]))  # two parts; the whole term is not counted


class Loading(unittest.TestCase):
    def test_sql_load_extension_is_never_authorized(self):
        db = st.load(sqlite3.connect(":memory:"))
        _OPEN.append(db)
        with self.assertRaisesRegex(sqlite3.OperationalError, "not authorized"):
            db.execute("SELECT load_extension('/nonexistent')")


class Arguments(unittest.TestCase):
    def test_tokenchars_and_separators_are_refused(self):
        for arg in ("tokenchars '#'", "separators '.'", "TokenChars '#'", "SEPARATORS '.'"):
            db = st.load(sqlite3.connect(":memory:"))
            _OPEN.append(db)
            with self.subTest(arg=arg), self.assertRaises(sqlite3.Error):
                db.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize=\"substrate {arg}\")")


class Robustness(unittest.TestCase):
    ALPHABET = list("aZb_-./\\: 9") + ["\u00ad", "\u200d", "\u2011", "é", "Ü", "—", "→", "“", "中"]

    def test_fuzz_insert_query_and_integrity_check(self):
        rng = random.Random(7)
        docs = ["".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 80)))
                for _ in range(2000)]
        db = table(docs)
        db.execute("INSERT INTO t(t) VALUES('integrity-check')")
        for d in docs[:200]:
            for term in d.split():
                cleaned = term.replace('"', "")
                if cleaned:
                    db.execute("SELECT count(*) FROM t WHERE t MATCH ?",
                               ('"' + cleaned + '"',)).fetchone()

    def test_invalid_utf8_does_not_crash(self):
        db = table([])
        rng = random.Random(11)
        for _ in range(500):
            blob = bytes(rng.randrange(256) for _ in range(rng.randint(0, 60)))
            db.execute("INSERT INTO t(x) VALUES (CAST(? AS TEXT))", (blob,))
        db.execute("INSERT INTO t(t) VALUES('integrity-check')")

    def test_a_connection_without_the_extension_cannot_read_the_table(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "x.db")
            db = st.load(sqlite3.connect(path))
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='substrate')")
            db.execute("INSERT INTO t VALUES ('IndexStore')")
            db.commit()
            db.close()
            plain = st._orig_connect(path)
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such tokenizer"):
                plain.execute("SELECT * FROM t WHERE t MATCH 'store'").fetchall()
            plain.close()


if __name__ == "__main__":
    unittest.main()
