//! The tokenizer's logic, with no FFI and no `unsafe`.
//!
//! Input is the original text plus one RUN at a time, as a scanner finds them (for the extension:
//! SQLite's `unicode61` with the joiners and ignorables below added as token characters). Each run
//! is split and folded here; folding is delegated to a [`Folder`] (for the extension: a second,
//! plain `unicode61`), so Unicode case and diacritic folding are SQLite's, not reimplemented.
//!
//! Per run:
//!   1. split at PATH separators (`. / \ :`) into SEGMENTS, the identifiers of a dotted name or path;
//!   2. split each segment at WORD joiners (`_ -` U+2010 U+2011) and ASCII camel boundaries into
//!      PIECES, dropping ignorables (U+00AD, U+200C, U+200D, U+2060), and fold each piece;
//!   3. emit the folded parts at consecutive positions;
//!   4. in DOCUMENT mode only, also emit the separator-free concatenation of the whole run, and of
//!      each multi-part segment of a multi-segment run (so `etl/load_orders.py` gets `loadorders`
//!      as well as `etlloadorderspy`), as colocated terms at the position of their first part.
//!
//! Camel rules: lower→Upper splits (`camelCase`, `macOS`). Upper→Upper-lower splits when at least
//! two capitals stay on the left, unless the lowercase that follows is a lone plural `s`
//! (`HTTPServer`, `getURLId`, `HTTPOk` split; `IPv6`, `OAuth`, and plural acronyms `URLs`, `APIs`,
//! `LLMs` stay whole). A digit never starts or
//! ends a boundary, so `sha256`, `qwen3` and also `s3Bucket`, `base64Encode` stay whole.
//! Non-ASCII letters never form a boundary.
//!
//! Bounds, both deliberate: a whole term longer than [`MAX_CONCAT`] bytes is not emitted, and a run
//! longer than [`STREAM_ABOVE`] bytes (a base64 blob, a minified line) is emitted part by part with
//! no whole terms. A camelCase identifier caught by either loses the single token `unicode61` would
//! have given it. Streaming holds no per-part state; memory is proportional to the longest segment.

#![forbid(unsafe_code)]

/// A whole term longer than this many bytes is not emitted (a long path keeps its parts).
pub const MAX_CONCAT: usize = 128;

/// Runs longer than this are streamed: parts only, no whole terms, no per-part buffering.
pub const STREAM_ABOVE: usize = 4096;

/// The token characters added to the scanner's `unicode61`, as its `tokenchars` argument.
pub const SCAN_TOKENCHARS: &str = "_-./\\:\u{2010}\u{2011}\u{00AD}\u{200C}\u{200D}\u{2060}";

/// Folds one piece of original text, handing each resulting token (normally exactly one) to `out`.
/// An implementation may swallow an error `out` returns (unicode61 turns SQLITE_DONE into OK), so
/// callers must not rely on `fold`'s result alone to learn that `out` failed.
pub trait Folder {
    fn fold(&mut self, piece: &[u8], out: &mut dyn FnMut(&[u8]) -> Result<(), i32>)
        -> Result<(), i32>;
}

/// Receives each term: bytes, whether it is colocated with the previous term, and the byte range
/// of original text it came from.
pub trait Sink {
    fn emit(&mut self, token: &[u8], colocated: bool, start: usize, end: usize) -> Result<(), i32>;
}

/// A joiner at the start of `z`: its byte length, and whether it separates path SEGMENTS
/// (`. / \ :`) rather than words within one (`_ -` U+2010 U+2011). `None` if `z` starts with none.
pub fn joiner(z: &[u8]) -> Option<(usize, bool)> {
    match z {
        [b'.' | b'/' | b'\\' | b':', ..] => Some((1, true)),
        [b'_' | b'-', ..] => Some((1, false)),
        [0xE2, 0x80, 0x90 | 0x91, ..] => Some((3, false)),
        _ => None,
    }
}

/// Byte length of an ignorable at the start of `z`, else 0.
pub fn ignorable_len(z: &[u8]) -> usize {
    match z {
        [0xC2, 0xAD, ..] => 2,
        [0xE2, 0x80, 0x8C | 0x8D, ..] => 3,
        [0xE2, 0x81, 0xA0, ..] => 3,
        _ => 0,
    }
}

/// Length of the UTF-8 sequence a lead byte announces; 1 for a stray continuation byte, so
/// malformed input still advances.
fn utf8_len(b: u8) -> usize {
    match b {
        0x00..=0x7F => 1,
        0xC0..=0xDF => 2,
        0xE0..=0xEF => 3,
        0xF0..=0xF7 => 4,
        _ => 1,
    }
}

struct Part {
    off: usize,
    len: usize,
    seg: usize,
    word: usize,
    start: usize,
    end: usize,
}

/// Splits runs. Holds its buffers so one instance reuses them across every run of a call.
#[derive(Default)]
pub struct Splitter {
    arena: Vec<u8>,
    parts: Vec<Part>,
    clean: Vec<u8>,
    /// One entry per stripped ignorable: (index in `clean` it preceded, bytes stripped so far).
    skips: Vec<(usize, usize)>,
    whole: Vec<u8>,
}

impl Splitter {
    /// Split, fold and emit one run `text[start..end]`.
    pub fn run<F: Folder, S: Sink>(
        &mut self,
        text: &[u8],
        start: usize,
        end: usize,
        document: bool,
        folder: &mut F,
        sink: &mut S,
    ) -> Result<(), i32> {
        let end = end.min(text.len());
        if start >= end {
            return Ok(());
        }
        self.arena.clear();
        self.parts.clear();
        let stream = end - start > STREAM_ABOVE;
        let mut seg = 0;
        let mut seg_used = false;
        let mut word = 0;
        let mut s = start;
        let mut i = start;
        loop {
            let j = if i < end { joiner(&text[i..end]) } else { None };
            if i == end || j.is_some() {
                if i > s {
                    let produced = if stream {
                        self.word(text, s, i, (seg, word), folder, &mut Out::Stream(&mut *sink))?
                    } else {
                        self.word(text, s, i, (seg, word), folder, &mut Out::<S>::Buffer)?
                    };
                    seg_used |= produced;
                    word += usize::from(produced);
                }
                let Some((len, path)) = j else { break };
                if path && seg_used {
                    seg += 1;
                    seg_used = false;
                }
                i += len;
                s = i;
            } else {
                let k = ignorable_len(&text[i..end]);
                i += if k > 0 { k } else { utf8_len(text[i]).min(end - i) };
            }
        }
        if stream {
            return Ok(());
        }
        self.emit_buffered(document, sink)
    }

    /// One word `text[a..b]` (no joiners in it): strip ignorables, split at camel boundaries, fold
    /// each piece. Returns whether it produced any part.
    fn word<F: Folder, S: Sink>(
        &mut self,
        text: &[u8],
        a: usize,
        b: usize,
        (seg, word): (usize, usize),
        folder: &mut F,
        out: &mut Out<'_, S>,
    ) -> Result<bool, i32> {
        self.clean.clear();
        self.skips.clear();
        let mut stripped = 0;
        let mut i = a;
        while i < b {
            let k = ignorable_len(&text[i..b]);
            if k > 0 {
                stripped += k;
                self.skips.push((self.clean.len(), stripped));
                i += k;
            } else {
                self.clean.push(text[i]);
                i += 1;
            }
        }
        let (c, skips) = (&self.clean, &self.skips);
        // Offset in the original text of byte `k` of `clean`.
        let orig = |k: usize| a + k + skips.partition_point(|&(at, _)| at <= k).checked_sub(1)
            .map_or(0, |x| skips[x].1);
        let n = c.len();
        let lower = |k: usize| k < n && c[k].is_ascii_lowercase();
        let mut produced = false;
        let mut failed = None;
        let mut from = 0;
        for j in 1..=n {
            let cut = j == n
                || (c[j].is_ascii_uppercase()
                    && (c[j - 1].is_ascii_lowercase()
                        || (c[j - 1].is_ascii_uppercase()
                            && j - from >= 2
                            && lower(j + 1)
                            && (lower(j + 2) || c[j + 1] != b's'))));
            if !cut {
                continue;
            }
            // A piece starts at an ASCII capital (or the word's start) and ends before one (or at
            // the word's end), so both offsets land on character boundaries of the original text.
            let (ps, pe) = (orig(from), orig(j - 1) + 1);
            let (arena, parts) = (&mut self.arena, &mut self.parts);
            let r = folder.fold(&c[from..j], &mut |tok| {
                produced = true;
                let r = match out {
                    Out::Stream(sink) => sink.emit(tok, false, ps, pe),
                    Out::Buffer => {
                        parts.push(Part { off: arena.len(), len: tok.len(), seg, word, start: ps, end: pe });
                        arena.extend_from_slice(tok);
                        Ok(())
                    }
                };
                if let Err(rc) = r {
                    failed = Some(rc);
                }
                r
            });
            if let Some(rc) = failed {
                return Err(rc);
            }
            r?;
            from = j;
        }
        Ok(produced)
    }

    fn emit_buffered<S: Sink>(&mut self, document: bool, sink: &mut S) -> Result<(), i32> {
        let parts = &self.parts;
        let Some(last) = parts.last() else { return Ok(()) };
        let all_digits = self.arena.iter().all(u8::is_ascii_digit);
        let wholes = document && parts.len() > 1 && !all_digits;
        let span = |from: usize, same: &dyn Fn(&Part) -> bool| {
            parts[from..].iter().take_while(|q| same(q)).count()
        };
        for (i, p) in parts.iter().enumerate() {
            sink.emit(&self.arena[p.off..p.off + p.len], false, p.start, p.end)?;
            if !wholes {
                continue;
            }
            // Whole terms for the run, then for each multi-part segment and each multi-part word
            // that is not already the same span. A word's own term is what lets the lowercased
            // query `foo_barbaz` (from `foo_barBaz`) match: its phrase is `foo` then `barbaz`.
            if i == 0 {
                // The parts' own span, not the run's, which can carry a trailing `.` or `_`.
                concat(&self.arena, parts, &mut self.whole, p.start, last.end, sink)?;
            }
            let seg_len = span(i, &|q| q.seg == p.seg);
            if (i == 0 || parts[i - 1].seg != p.seg) && seg_len > 1 && seg_len < parts.len() {
                let g = &parts[i..i + seg_len];
                concat(&self.arena, g, &mut self.whole, p.start, g[seg_len - 1].end, sink)?;
            }
            if i == 0 || parts[i - 1].word != p.word {
                let word_len = span(i, &|q| q.word == p.word);
                let in_seg = parts.iter().filter(|q| q.seg == p.seg).count();
                if word_len > 1 && word_len < in_seg {
                    let g = &parts[i..i + word_len];
                    concat(&self.arena, g, &mut self.whole, p.start, g[word_len - 1].end, sink)?;
                }
            }
        }
        Ok(())
    }
}

enum Out<'a, S> {
    Buffer,
    Stream(&'a mut S),
}

fn concat<S: Sink>(
    arena: &[u8],
    parts: &[Part],
    whole: &mut Vec<u8>,
    start: usize,
    end: usize,
    sink: &mut S,
) -> Result<(), i32> {
    if parts.iter().map(|p| p.len).sum::<usize>() > MAX_CONCAT {
        return Ok(());
    }
    whole.clear();
    for p in parts {
        whole.extend_from_slice(&arena[p.off..p.off + p.len]);
    }
    sink.emit(whole, true, start, end)
}

/// Convenience for callers that already hold every run: split each in order.
pub fn tokenize<F: Folder, S: Sink>(
    text: &[u8],
    runs: &[(usize, usize)],
    document: bool,
    folder: &mut F,
    sink: &mut S,
) -> Result<(), i32> {
    let mut splitter = Splitter::default();
    for &(start, end) in runs {
        splitter.run(text, start, end, document, folder, sink)?;
    }
    Ok(())
}
