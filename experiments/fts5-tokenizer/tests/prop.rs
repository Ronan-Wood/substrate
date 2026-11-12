//! Property tests over the tokenizer's logic, driven the way the extension drives it: a scanner
//! that mimics `unicode61` + SCAN_TOKENCHARS finds the runs, and a folder that lowercases.
//! The real `unicode61` is exercised end to end by test_substrate_tok.py.

use proptest::prelude::*;
use substrate_tok::split::{tokenize, Folder, Sink, SCAN_TOKENCHARS, STREAM_ABOVE};

/// Maximal runs of alphanumerics and scan token chars, as (start, end) byte ranges.
fn scan(text: &str) -> Vec<(usize, usize)> {
    let mut runs = Vec::new();
    let mut start = None;
    for (i, c) in text.char_indices() {
        let tok = c.is_alphanumeric() || SCAN_TOKENCHARS.contains(c);
        match (tok, start) {
            (true, None) => start = Some(i),
            (false, Some(s)) => {
                runs.push((s, i));
                start = None;
            }
            _ => {}
        }
    }
    if let Some(s) = start {
        runs.push((s, text.len()));
    }
    runs
}

#[derive(Default)]
struct Lower {
    bad_pieces: usize,
}

impl Folder for Lower {
    fn fold(
        &mut self,
        piece: &[u8],
        out: &mut dyn FnMut(&[u8]) -> Result<(), i32>,
    ) -> Result<(), i32> {
        match std::str::from_utf8(piece) {
            Ok(s) => out(s.to_lowercase().as_bytes()),
            Err(_) => {
                self.bad_pieces += 1;
                out(&piece.to_ascii_lowercase())
            }
        }
    }
}

#[derive(Default)]
struct Record(Vec<(Vec<u8>, bool, usize, usize)>);

impl Sink for Record {
    fn emit(&mut self, t: &[u8], colocated: bool, s: usize, e: usize) -> Result<(), i32> {
        self.0.push((t.to_vec(), colocated, s, e));
        Ok(())
    }
}

fn text() -> impl Strategy<Value = String> {
    prop_oneof![
        "[a-zA-Z0-9_./\\\\: \\-\u{2010}\u{2011}\u{00AD}\u{200C}\u{200D}\u{2060}éÜß中—→“”…]{0,80}",
        any::<String>(),
    ]
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(20_000))]

    #[test]
    fn offsets_are_in_bounds_and_on_char_boundaries(t in text(), document in any::<bool>()) {
        let runs = scan(&t);
        let (mut f, mut r) = (Lower::default(), Record::default());
        tokenize(t.as_bytes(), &runs, document, &mut f, &mut r).unwrap();
        prop_assert_eq!(f.bad_pieces, 0, "a piece split a UTF-8 sequence");
        for (tok, _, s, e) in &r.0 {
            prop_assert!(s < e && *e <= t.len(), "{}..{} out of 0..{}", s, e, t.len());
            prop_assert!(t.is_char_boundary(*s) && t.is_char_boundary(*e), "{}..{} splits a char in {:?}", s, e, t);
            prop_assert!(runs.iter().any(|&(a, b)| a <= *s && *e <= b), "{}..{} outside every run", s, e);
            prop_assert!(!tok.is_empty());
        }
        if let Some(first) = r.0.first() {
            prop_assert!(!first.1, "the first term cannot be colocated");
        }
        if !document {
            prop_assert!(r.0.iter().all(|x| !x.1), "colocated term at query time");
        }
    }

    #[test]
    fn arbitrary_bytes_and_runs_never_panic_or_escape(
        bytes in proptest::collection::vec(any::<u8>(), 0..120),
        raw in proptest::collection::vec((0usize..140, 0usize..140), 0..8),
    ) {
        let runs: Vec<_> = raw.into_iter().map(|(a, b)| (a.min(b), a.max(b))).collect();
        let (mut f, mut r) = (Lower::default(), Record::default());
        tokenize(&bytes, &runs, true, &mut f, &mut r).unwrap();
        for (_, _, s, e) in &r.0 {
            prop_assert!(s < e && *e <= bytes.len());
        }
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(200))]

    /// Runs past STREAM_ABOVE take the streaming path: parts only, no whole terms, and still in
    /// bounds and on char boundaries.
    #[test]
    fn long_runs_stream_parts_only(t in "[a-zA-Z_.é\u{00AD}]{4097,6000}") {
        let runs = scan(&t);
        let (mut f, mut r) = (Lower::default(), Record::default());
        tokenize(t.as_bytes(), &runs, true, &mut f, &mut r).unwrap();
        prop_assert_eq!(f.bad_pieces, 0);
        for (tok, colocated, s, e) in &r.0 {
            prop_assert!(s < e && *e <= t.len());
            prop_assert!(t.is_char_boundary(*s) && t.is_char_boundary(*e));
            prop_assert!(!tok.is_empty());
            let run_len = runs.iter().find(|&&(a, b)| a <= *s && *e <= b).map(|&(a, b)| b - a).unwrap();
            prop_assert!(!(*colocated && run_len > STREAM_ABOVE), "whole term on a streamed run");
        }
    }
}

/// Offsets around a stripped ignorable name each piece's own bytes, not the whole word.
#[test]
fn offsets_around_an_ignorable_are_per_piece() {
    let t = "Index\u{00AD}Store";
    let (mut f, mut r) = (Lower::default(), Record::default());
    tokenize(t.as_bytes(), &scan(t), false, &mut f, &mut r).unwrap();
    let got: Vec<_> = r.0.iter().map(|(_, _, s, e)| &t[*s..*e]).collect();
    assert_eq!(got, ["Index", "Store"]);
}

/// The whole-run term spans its parts, not a trailing joiner the scanner kept in the run.
#[test]
fn whole_term_offsets_exclude_trailing_joiners() {
    let t = "see foo_bar. Next";
    let (mut f, mut r) = (Lower::default(), Record::default());
    tokenize(t.as_bytes(), &scan(t), true, &mut f, &mut r).unwrap();
    let whole = r.0.iter().find(|x| x.0 == b"foobar").unwrap();
    assert_eq!(&t[whole.2..whole.3], "foo_bar");
}

/// unicode61 turns SQLITE_DONE from its callback into OK. A sink error must still stop the run
/// when the folder swallows it, as it does on the streaming path.
#[test]
fn a_swallowed_sink_error_still_stops_the_run() {
    struct Swallow;
    impl Folder for Swallow {
        fn fold(&mut self, piece: &[u8], out: &mut dyn FnMut(&[u8]) -> Result<(), i32>)
            -> Result<(), i32> {
            let _ = out(&piece.to_ascii_lowercase());
            Ok(())
        }
    }
    struct DoneAfterOne(usize);
    impl Sink for DoneAfterOne {
        fn emit(&mut self, _: &[u8], _: bool, _: usize, _: usize) -> Result<(), i32> {
            self.0 += 1;
            if self.0 == 1 { Err(101) } else { Ok(()) }
        }
    }
    let text = "ab".to_string() + &"_cd".repeat(3000);
    let mut sink = DoneAfterOne(0);
    assert_eq!(tokenize(text.as_bytes(), &scan(&text), true, &mut Swallow, &mut sink), Err(101));
    assert_eq!(sink.0, 1, "kept emitting after the sink said stop");
}

/// A sink error stops emission and comes back out unchanged, on both paths.
#[test]
fn sink_errors_propagate() {
    struct Fail(usize);
    impl Sink for Fail {
        fn emit(&mut self, _: &[u8], _: bool, _: usize, _: usize) -> Result<(), i32> {
            self.0 += 1;
            if self.0 == 2 { Err(101) } else { Ok(()) }
        }
    }
    for text in ["a_b_c d e".to_string(), "x_".repeat(3000)] {
        let mut sink = Fail(0);
        let err = tokenize(text.as_bytes(), &scan(&text), true, &mut Lower::default(), &mut sink);
        assert_eq!(err, Err(101));
        assert_eq!(sink.0, 2, "emission continued after the error");
    }
}

#[test]
fn examples() {
    let cases: &[(&str, &[(&str, bool)])] = &[
        ("IndexStore", &[("index", false), ("indexstore", true), ("store", false)]),
        ("store/IndexStore.py", &[
            ("store", false), ("storeindexstorepy", true),
            ("index", false), ("indexstore", true), ("store", false), ("py", false),
        ]),
        ("HTTPServer IPv6", &[("http", false), ("httpserver", true), ("server", false), ("ipv6", false)]),
        ("2026-11-06", &[("2026", false), ("11", false), ("06", false)]),
        ("trans\u{00AD}former", &[("transformer", false)]),
        ("URLs APIs LLMs UUIDs ADRs", &[
            ("urls", false), ("apis", false), ("llms", false), ("uuids", false), ("adrs", false),
        ]),
        ("APIsAreFine", &[("apis", false), ("apisarefine", true), ("are", false), ("fine", false)]),
        ("foo_barBaz", &[
            ("foo", false), ("foobarbaz", true), ("bar", false), ("barbaz", true), ("baz", false),
        ]),
        ("src/foo_barBaz.py", &[
            ("src", false), ("srcfoobarbazpy", true), ("foo", false), ("foobarbaz", true),
            ("bar", false), ("barbaz", true), ("baz", false), ("py", false),
        ]),
        ("getURLId HTTPOk", &[
            ("get", false), ("geturlid", true), ("url", false), ("id", false),
            ("http", false), ("httpok", true), ("ok", false),
        ]),
        ("resp.page_token", &[
            ("resp", false), ("resppagetoken", true), ("page", false), ("pagetoken", true), ("token", false),
        ]),
        ("a—b", &[("a", false), ("b", false)]),
    ];
    for (input, want) in cases {
        let (mut f, mut r) = (Lower::default(), Record::default());
        tokenize(input.as_bytes(), &scan(input), true, &mut f, &mut r).unwrap();
        let got: Vec<_> = r.0.iter().map(|(t, c, _, _)| (String::from_utf8(t.clone()).unwrap(), *c)).collect();
        let want: Vec<_> = want.iter().map(|(t, c)| (t.to_string(), *c)).collect();
        assert_eq!(got, want, "{input}");
    }
}
