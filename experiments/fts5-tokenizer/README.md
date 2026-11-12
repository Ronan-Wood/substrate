# `substrate` FTS5 tokenizer (experiment)

**Recommendation: ship, as an audited PR.** On the large private eval set (7,537 scored cases over six
vaults, CIs clustered by note), lexical-only and hybrid:

| | lexical-only | hybrid (lexical + vector) |
|---|---|---|
| identifiers respelled (770), MRR@10 | **+0.201 ± 0.034** | **+0.147 ± 0.031** |
| ↳ recall@10 | 0.762 → 0.990 | 0.781 → 0.960 |
| camelCase asked as snake_case (230), MRR@10 | **+0.518 ± 0.083** | **+0.399 ± 0.074** |
| same-spelling identifiers (1,225) | −0.007 ± 0.008 | **−0.010 ± 0.006** |
| keyword bags (2,250) | −0.000 ± 0.002 | −0.002 ± 0.002 |
| paraphrases (1,810) | **+0.005 ± 0.004** | +0.003 ± 0.004 |
| whole set as written | −0.000 ± 0.002 | −0.001 ± 0.002 |
| lexical latency per search, mean (vaults A, B) | **+2.6 ms (+22%)** | **+1.3 ms (+5% of the call)**, vault A |
| `chunks_fts` size | +8–26%, which is +0.3–0.8% of the index file | same |

- **The gain is large where it applies, and holds under hybrid.** When the query spells an identifier
  differently from the note (`pageToken` for `page_token`, `order_loader` for `OrderLoader`), the gain
  is significant in every vault under both stacks. snake/kebab respelled as camelCase gains +0.507
  lexical-only and +0.395 hybrid.
- **The set as written barely moves.** Under hybrid, one small loss is significant: same-spelling
  identifiers lose 0.010, and keyword bags lose 0.003 recall@5. No negative case leaked in either arm.
- **The gain only matters if people respell.** The respelled cases are a mechanical rewrite of the set's
  own identifier cases (*Respelled sets*). How often a real user or agent does that is not measured.
- **The cost is in the index, not the tokenizer.** Query-side tokenizing is 15% faster than
  unicode61's. Searches are slower because a prefix term (`"order"*`) now also expands over the
  colocated whole terms. The bare MATCH takes +0.22 ms, and the engine's lexical call +2.6 ms on
  average, with vault B's p95 going from 22 to 35 ms. Under hybrid, where the embed call dominates, it
  is +1.3 ms of a 27 ms call. The cost was measured on 2 of 6 vaults (*Runtime cost*).
- **One known gap, kept on purpose.** A lowercased snake_case query over a camelCase word that is not
  last in its identifier misses: `foobar_baz` for `fooBar_baz`. unicode61 matches it. Closing the gap
  costs the camelCase → snake_case gain (*Findings* 6).
- **Shipping has costs of its own.** The dylib must be Team-ID signed, and the PR must build the crate
  before the interpreter's pin checks, never after (*Shipping cost*).

Status: experiment only. No engine code changed, and nothing touched `substrate/out-vault/` or `~/.substrate`.

**Privacy.** The real-vault indexes measured here are the operator's own notes, so nothing from them is in
this directory: no query, answer, note title, heading, path or case id. They appear only as aggregate
numbers labelled vault A to vault F. The gold sets, the per-case results and the logs live outside every
repository (see *Reproduce*). Every example below is synthetic.

## What it does

A loadable SQLite extension, written in Rust, that registers an FTS5 tokenizer named `substrate`. It
wraps SQLite's own `unicode61` through `fts5_api`, so Unicode case folding and diacritic folding are
SQLite's (`remove_diacritics 1` by default). Other arguments pass through, except `tokenchars` and
`separators` in any letter case, which it refuses because they would redefine its joiners. On top of
that, it splits:

| input | positional terms | colocated whole terms (document side only) |
|---|---|---|
| `OrderLoader` | `order` `loader` | `orderloader` |
| `page_token`, `kebab-case` | `page` `token` | `pagetoken` |
| `store/OrderLoader.py` | `store` `order` `loader` `py` | `storeorderloaderpy`, `orderloader` |
| `etl/load_orders.py` | `etl` `load` `orders` `py` | `etlloadorderspy`, `loadorders` |
| `page_tokenId` | `page` `token` `id` | `pagetokenid`, `tokenid` |
| `HTTPServer getURLId IPv6 OAuth` | `http` `server` `get` `url` `id` `ipv6` `oauth` | `httpserver`, `geturlid` |
| `URLs APIs LLMs` | `urls` `apis` `llms` | — |
| `trans`U+00AD`former` | `transformer` | — |

- **The whole term is the separator-free concatenation.** `fts.py` lowercases query terms and keeps only
  `[A-Za-z0-9_]`, so a whole term that kept its separators could never be queried.
- **Whole terms are `FTS5_TOKEN_COLOCATED`.** They don't count toward column length, so BM25's length
  normalisation is unchanged.
- **Each identifier in a path gets its own whole term.** `. / \ :` separate a path's identifiers,
  while `_ -` and camel boundaries split words inside one. So `load_orders` in `etl/load_orders.py` is
  findable as `loadorders`.
- **So does each camelCase word inside a joined identifier.** `tokenId` in `page_tokenId` gets
  `tokenid`, because `fts.py` turns the query `page_tokenid` into the phrase `page tokenid`, and without
  that term the phrase could not match (v2, below).
- **Prose is unchanged.** For text with no camelCase, the positional stream matches `unicode61` exactly,
  and a test asserts this. Plural acronyms (`URLs`, `APIs`) count as no camelCase: they stay whole.
- **Typographic punctuation was already handled.** `unicode61` already treats em dashes, curly quotes,
  `→` and `…` as separators. The only typographic gain is soft hyphens and zero-width joiners, which used
  to split a word in two.

The core logic is in `src/split.rs`, which has `#![forbid(unsafe_code)]`. All `unsafe` is in `src/ffi.rs`:
the entry point, the tokenizer vtable, and the two calls into `unicode61`. The first draft was in C and is
kept in `reference-c/` for reference only; it is not built. Before the review fixes, the Rust build
produced exactly the C draft's index: every (term, doc, column, position) row matched across all three
corpora, 1.1 M rows. It now differs on purpose, through the acronym rule and the per-identifier whole
terms.

## Build and test

```bash
PATH=/opt/homebrew/opt/rustup/bin:$PATH cargo build --release
```

```bash
PATH=/opt/homebrew/opt/rustup/bin:$PATH cargo test --release
```

```bash
python3 -I test_substrate_tok.py
```

| suite | what it checks | result |
|---|---|---|
| `tests/prop.rs` (proptest: 20,000 cases × 2, 200 on runs over 4 KB; plus examples, per-piece offsets and error propagation) | every emitted offset is in bounds, on a UTF-8 boundary and inside its run; the folder never receives a split character; no colocated term at query time or on a streamed run; arbitrary bytes and runs never panic; a sink error stops emission even when the folder swallows it, as unicode61 does with SQLITE_DONE; a whole term's offsets span its parts, not a trailing joiner | 8/8 pass |
| `test_substrate_tok.py` (30 tests, one an expected failure for the known gap; real SQLite 3.53.4) | splitting, acronyms, camelCase inside snake_case, folding, `unicode61` parity (prose with plural acronyms included), MATCH behaviour, highlight offsets (also around a soft hyphen), column size, refused arguments, 2,000-document fuzz + `integrity-check`, invalid UTF-8, the failure mode without the extension, SQL `load_extension()` never authorized | 29 pass, 1 expected failure |
| `cargo clippy --all-targets` | lints | 0 warnings |

**Regression tests are checked by mutation.** Each fix from review ships a test that fails on the code
before it:
- In Rust, putting back any of these fails its test: either earlier acronym rule, `_` as a path
  separator, whole-word offsets, the swallowed sink error, the run's span on the whole term, or v1's
  `split.rs` without per-word whole terms (`examples` fails; the Python test for it fails on v1 too).
- In Python, a case-sensitive argument check fails its test.
- The C draft, which has the old behaviour, fails the 4 new Python tests.
- A planted bug (stripping one byte of a 3-byte ignorable) is caught by the property test, with minimal
  input `"\u{200c}"`.

## Reproduce

```bash
python build_index.py SRC.db SCRATCH/idx NAME
```

Run that once per corpus, with NAME `fixture`, `vault-a` or `vault-b`. SRC is only read. Then:

```bash
./run_matrix.sh SCRATCH/idx SCRATCH/runs SCRATCH/vector-cache.db
```

```bash
python summarize.py SCRATCH/runs
```

- **Vector cache:** `SCRATCH/vector-cache.db` is a copy of `out/vector-cache.db`; never pass the real one.
- **Python:** the engine's own environment, through `PY` (default `../../substrate/.venv/bin/python`).
- **Private gold sets:** the identifier sets are read from `$PRIVATE_GOLD`, which defaults to
  `~/CodeHome/substrate-eval-private/fts5-tokenizer/`. They are outside every repository; `run_matrix.sh`
  refuses the identifier sets if that folder is missing.
- **Per-case ranks:** `cases.py IDX CORPUS GOLD.json` prints them for the lexical-only stack.
- **Unscripted checks:** the C-vs-Rust parity check and the small set's MATCH latency (*Cost*) were
  one-off checks, not scripted (2026-11-06). The large set's runtime cost is scripted: `bench_large.py`.

The large set, once `build_index.py` has made `<scope>.{unicode61,substrate}.db` for the six scopes
(with SRC a copy, never a live WAL database):

```bash
python make_respelled.py
```

```bash
python make_respelled.py snake
```

```bash
./run_large.sh SCRATCH/idx SCRATCH/query-cache.db lex
```

```bash
SET=respelled ./run_large.sh SCRATCH/idx SCRATCH/query-cache.db lex
```

```bash
SET=snake ./run_large.sh SCRATCH/idx SCRATCH/query-cache.db lex
```

```bash
python score_large.py lex resp-lex snk-lex
```

`run_large.sh` writes per-case JSON and logs under `$PRIVATE_EVAL/fts5-tokenizer/results/`. They hold
real queries, so they never go into this directory. `score_large.py` prints aggregates only, under the
labels in the private `labels.json`. `RESULTS=<subfolder>` keeps a second tokenizer build's runs apart.

The runtime cost, on a quiet machine only (it measures wall time; about an hour with the defaults):

```bash
python bench_large.py SCRATCH/idx
```

```bash
"$PY" -I test_bench_large.py
```

`--sections`, `--stacks` and `--scopes` pick parts; the docstring says what each measures, and `--help`
gives the counts. `--guard` runs each block under the runtime audit's load guard. `--pool A.cases.json
B.cases.json` merges and pools separate runs. Hybrid needs the local Ollama embedder. For an OS-cold
cache, run `--sections cold --purge` on its own: sudo asks for a password once, at the start.

## Results — large set (lexical 2026-11-07; hybrid and cost 2026-11-08)

Conditions:
- **Gold:** the large private eval set (`~/CodeHome/substrate-eval-private/gold/`), six vaults, read
  only.
  - 7,537 positive cases are scored. They fall into these families:
    - `identifier`: the identifier spelled as the note spells it;
    - `kw`: an agent-style keyword bag;
    - `lexq`: a question in the note's words;
    - `para`: a blind paraphrase;
    - `real`: mined from transcripts.
  - Unanswerable cases are not scored. Negative cases are counted as leaks.
- **Indexes:** copies of the six composed indexes taken 2026-11-06 at schema v11. Each pair of arms is
  one copy with only `chunks_fts` rebuilt. Chunks: vault A 1,402, B 7,206, C 3,918, D 2,387, E 1,629,
  F 646.
- **Engine:** `79c234e`. Lexical-only means `--no-vector --no-hyde --no-rerank`, at k = 10.
- **Hybrid:** `--no-hyde --no-rerank`, so lexical + vector fused, with `qwen3-embedding:0.6b` on
  **Ollama 0.40.15**. Every chunk has its vector, and no case fell back. HyDE and rerank were not run.
- **Scoring:** `score_large.py`, on `both_rank` (answer and attribution in one chunk). Each Δ is
  `substrate − unicode61`, paired per case. The 95% CI is cluster-robust by target note, with t at g−1
  degrees of freedom. A cell with one note gets no CI. **Bold** means the CI excludes 0.
- **Versions:** v1, v2 and a rejected v3 (*Findings* 6). The tables are v2.
  - v2 adds per-word whole terms. It came from a same-spelling loss found on this set, so its numbers
    are not out of sample.
  - Pooled, v2 matches v1 to within 0.002. The one per-vault cell that moved is vault E's
    `identifier`: −0.012 under v1, −0.005 under v2.

### The set as written (lexical-only)

| family | n (notes) | MRR@10 | recall@5 | recall@10 |
|---|---|---|---|---|
| identifier | 1225 (371) | 0.778 → 0.771 (−0.007 ± 0.008) | 0.956 → 0.955 (−0.001 ± 0.005) | 0.988 → 0.988 (+0.000 ± 0.002) |
| kw | 2250 (604) | 0.639 → 0.639 (−0.000 ± 0.002) | 0.931 → 0.930 (−0.000 ± 0.003) | 0.986 → 0.986 (+0.000 ± 0.000) |
| lexq | 2202 (602) | 0.556 → 0.554 (−0.001 ± 0.003) | 0.851 → 0.853 (+0.001 ± 0.005) | 0.949 → 0.950 (+0.001 ± 0.002) |
| para | 1810 (548) | 0.337 → 0.342 (**+0.005 ± 0.004**) | 0.581 → 0.592 (**+0.011 ± 0.006**) | 0.722 → 0.727 (**+0.005 ± 0.005**) |
| real | 50 (50) | 0.637 → 0.637 (−0.000 ± 0.006) | 0.900 → 0.900 (+0.000 ± 0.058) | 0.980 → 0.960 (−0.020 ± 0.040) |
| all | 7537 (698) | 0.565 → 0.564 (−0.000 ± 0.002) | 0.827 → 0.830 (**+0.003 ± 0.003**) | 0.912 → 0.913 (+0.001 ± 0.001) |

Per vault, the `all` MRR Δ is between −0.005 and +0.004 everywhere. Two of the 30 vault × family cells
have a CI that excludes 0: vault E's `lexq` at −0.013 ± 0.011, and vault C's `para` at +0.007 ± 0.006.
With 30 cells, about one or two such cells are expected by chance.
Negative cases leaked: 0 in both arms, in all six vaults (229 cases).

### Respelled sets (lexical-only)

`make_respelled.py` rewrites only the identifier each `identifier` case names in its `answer`, one token
at a time. The rest of the query, the answer and the attribution are left alone:
- a path or dotted name becomes its words;
- `snake_case` or `kebab-case` becomes camelCase (*joined*), unless its parts are all digits;
- camelCase becomes words.

With `snake`, camelCase becomes snake_case instead, in a separate set. Camel boundaries are
`split.rs`'s, so `URLs` stays whole. A case is kept only if the engine's `fts.terms()` changes. That
left 770 cases on 296 notes, and 230 on 141 for the snake set. The sets are synthetic in their
spelling only. They test whether a query can bridge a different spelling, not how often anyone asks
that way.

An earlier version rewrote every token in the query, prose and dates included, and kept 821 cases at
+0.210 ± 0.033. A review found the problem, and these numbers replace those.

| shape | n (notes) | MRR@10 | recall@5 | recall@10 |
|---|---|---|---|---|
| camel → words | 230 (141) | 0.545 → 0.632 (**+0.087 ± 0.035**) | 0.735 → 0.913 (**+0.178 ± 0.065**) | 0.813 → 0.983 (**+0.170 ± 0.057**) |
| joined → camel | 250 (139) | 0.400 → 0.907 (**+0.507 ± 0.077**) | 0.480 → 0.992 (**+0.512 ± 0.094**) | 0.484 → 0.992 (**+0.508 ± 0.094**) |
| path → words | 290 (162) | 0.731 → 0.757 (**+0.026 ± 0.014**) | 0.883 → 0.917 (**+0.034 ± 0.025**) | 0.962 → 0.993 (**+0.031 ± 0.026**) |
| all | 770 (296) | 0.568 → 0.769 (**+0.201 ± 0.034**) | 0.708 → 0.940 (**+0.232 ± 0.043**) | 0.762 → 0.990 (**+0.227 ± 0.042**) |
| camel → snake (`snake` set) | 230 (141) | 0.350 → 0.868 (**+0.518 ± 0.083**) | 0.478 → 0.965 (**+0.487 ± 0.088**) | 0.517 → 0.996 (**+0.478 ± 0.088**) |

Per vault, the `all` MRR Δ is +0.143 to +0.217, and the snake set's is +0.432 to +0.643. Both CIs
exclude 0 in all six vaults.

### Hybrid (lexical + vector)

| family | n (notes) | MRR@10 | recall@5 | recall@10 |
|---|---|---|---|---|
| identifier | 1225 (371) | 0.707 → 0.697 (**−0.010 ± 0.006**) | 0.862 → 0.856 (−0.006 ± 0.007) | 0.967 → 0.965 (−0.002 ± 0.004) |
| kw | 2250 (604) | 0.649 → 0.647 (−0.002 ± 0.002) | 0.869 → 0.866 (**−0.003 ± 0.003**) | 0.967 → 0.966 (−0.001 ± 0.002) |
| lexq | 2202 (602) | 0.622 → 0.622 (+0.000 ± 0.002) | 0.857 → 0.858 (+0.000 ± 0.003) | 0.943 → 0.943 (+0.000 ± 0.002) |
| para | 1810 (548) | 0.460 → 0.464 (+0.003 ± 0.004) | 0.666 → 0.668 (+0.002 ± 0.005) | 0.777 → 0.780 (+0.003 ± 0.004) |
| real | 50 (50) | 0.635 → 0.635 (−0.000 ± 0.005) | 0.900 → 0.900 (+0.000 ± 0.000) | 0.960 → 0.960 (+0.000 ± 0.000) |
| all | 7537 (698) | 0.605 → 0.604 (−0.001 ± 0.002) | 0.816 → 0.815 (−0.001 ± 0.002) | 0.914 → 0.915 (+0.000 ± 0.001) |
| respelled: camel → words | 230 (141) | 0.489 → 0.535 (**+0.047 ± 0.030**) | 0.696 → 0.791 (**+0.096 ± 0.039**) | 0.809 → 0.935 (**+0.126 ± 0.056**) |
| respelled: joined → camel | 250 (139) | 0.500 → 0.895 (**+0.395 ± 0.092**) | 0.564 → 0.968 (**+0.404 ± 0.106**) | 0.592 → 0.992 (**+0.400 ± 0.110**) |
| respelled: path → words | 290 (162) | 0.613 → 0.627 (**+0.014 ± 0.010**) | 0.838 → 0.859 (**+0.021 ± 0.019**) | 0.921 → 0.952 (**+0.031 ± 0.022**) |
| respelled: all | 770 (296) | 0.539 → 0.686 (**+0.147 ± 0.031**) | 0.706 → 0.874 (**+0.168 ± 0.039**) | 0.781 → 0.960 (**+0.179 ± 0.041**) |
| camel → snake (`snake` set) | 230 (141) | 0.409 → 0.808 (**+0.399 ± 0.074**) | 0.509 → 0.926 (**+0.417 ± 0.079**) | 0.609 → 0.991 (**+0.383 ± 0.081**) |

- **The vector arm narrows the gain but keeps most of it.** It already finds some respelled notes on its
  own, so the starting MRR is higher and the gain smaller: +0.147 respelled, against +0.201
  lexical-only. Per vault, the respelled `all` gain is +0.104 to +0.168, and the snake set's is +0.331
  to +0.479. All twelve CIs exclude 0.
- **The set as written: two pooled cells are significant, both small losses.** Same-spelling
  identifiers lose 0.010 MRR, and keyword bags lose 0.003 recall@5. Per vault, the `all` Δ is −0.004 to
  +0.000. Two of the 30 vault × family cells exclude 0: vault B's `identifier` at −0.009 ± 0.009, and
  vault C's `para` at +0.005 ± 0.005.
- **No negative case leaked** in either arm, in any vault (229 cases).

### Not measured on the large set

- **The full stack** (HyDE + rerank) was not run. The small set below is the only full-stack evidence.
- **Runtime cost on vaults C–F** (*Runtime cost*).

### Runtime cost (`bench_large.py`, vaults A and B only)

**This covers 2 of the 6 vaults, and vault B has no hybrid end-to-end number.** The bench ran under the
runtime audit's load guard. A block starts only when no build or test burst is running and the 1-minute
load is below 3, and it is redone if a burst starts mid-block. Another session's CI-replay and test
bursts kept the machine busy. Vault A's first block waited 25 minutes, a burst hit vault B's hybrid block
on all three tries, and the guard then stopped the run, as designed. Vault B's lexical end-to-end block
ended at a load of 4.6 with no burst process running; it is kept under the guard's rule, but treat it
with care. Accuracy was complete, so the run was stopped there. `bench_large.py --guard --scopes "vault
C,vault D,vault E,vault F"` finishes the rest on a quiet machine, and `--scopes "vault B" --sections e2e
--stacks hybrid` fills the gap. `--pool` merges the runs.

Conditions:
- Apple M2 Max, macOS 27.0.1, SQLite 3.54.1 (the engine environment's), Ollama 0.40.15.
- Every case in each gold file: 976 in vault A, 2,516 in vault B.
- Warm timings are the median of 3 passes per case, after one warm-up pass, with arms alternating.
- "Cold" means a fresh connection per query, so SQLite's cache is empty and the OS file cache is warm.
- Δ is substrate − unicode61, paired per query, with a t CI over queries.

| measure | vault A unicode61 → substrate | vault B unicode61 → substrate |
|---|---|---|
| `chunks_fts` size | 479 → 565 KB (+17.9%; +0.8% of the file) | 1.57 → 1.97 MB (+25.5%; +0.8% of the file) |
| full `chunks_fts` rebuild, incl. WAL checkpoint | 0.01 → 0.02 s | 0.04 → 0.08 s |
| query-side tokenizing, AND + OR, p50 | 33.3 → 29.0 µs (−4.3 ± 0.1 µs) | 32.3 → 28.1 µs (−4.1 ± 0.1 µs) |
| ↳ A/A control (unicode61 vs unicode61) | +0.01 ± 0.03 µs | −0.00 ± 0.01 µs |
| bare MATCH (AND, bm25 top 10), p50 / p95 | 0.064 / 0.18 → 0.162 / 0.39 ms | 0.27 / 0.81 → 0.38 / 2.20 ms |
| `store.search` (AND + OR top-up), p50 / p95 | 0.89 / 1.65 → 1.16 / 2.11 ms | 2.64 / 5.50 → 2.95 / 8.23 ms |
| cold `store.search`, p50 / p95 | 1.37 / 2.10 → 1.59 / 2.57 ms | 3.31 / 6.31 → 3.59 / 9.49 ms |
| opening the store (substrate's includes the extension load) | 0.43 → 0.51 ms | 0.57 → 0.68 ms |
| lexical-only `retrieve()`, p50 / p95 | 2.69 / 4.57 → 3.93 / 6.50 ms | 11.2 / 22.1 → 12.7 / 35.5 ms |
| ↳ Δ mean, and as a share of the call | +1.25 ± 0.03 ms, 31% | +3.08 ± 0.17 ms, 21% |
| hybrid `retrieve()`, p50 / p95 | 9.1 / 11.2 → 10.4 / 13.3 ms | not measured |
| ↳ Δ mean, and as a share of the call with its embed | +1.30 ± 0.07 ms, 4.8% of 27 ms | not measured |

- **The tokenizer itself costs nothing at query time:** it is about 15% faster than unicode61's, well
  outside the A/A floor. The cause is not established. substrate registers through FTS5's v1 tokenizer
  API, and the built-in may take a slower path.
- **The time goes to the index.** A prefix term (`"order"*`) now also expands over the colocated
  whole terms (`orderloader`, …), so each MATCH walks more postings. A `retrieve()` call runs a median
  of 10 MATCH expressions (up to five searches, each AND then OR), so this adds up. FTS5's `prefix=` index option might win the time back. That is untested.
- **One-off costs are small.** Loading the extension takes 5 ms on the process's first load, then
  0.13 ms per connection. The rebuild that shipping runs once per index takes under 0.1 s on these
  vaults.

## Results — small set (v1, 2026-11-06)

Conditions:
- Measured 2026-11-06 with `substrate eval` at worktree `f1c8c41`.
- **Corpora:**
  - fixture: `out/substrate.db`, the three reference documents, schema v8;
  - vault A: a real composed vault, 1,402 chunks, schema v10;
  - vault B: a real composed vault, 7,206 chunks, schema v10.
- All three are scratch copies, upgraded to v11. The v8 copy is stamped without v9's `document_class`
  relabel, which retrieval does not branch on.
- Each pair of arms is the same copy with only `chunks_fts` rebuilt, so chunks, chunk_ids and vectors
  are identical. Vectors were complete in every full-stack run.
- Full stack means `qwen3-embedding:0.6b`, HyDE `qwen3.5:4b-mlx` and the `--cross-encoder` rerank
  `dengcao/Qwen3-Reranker-4B:Q4_K_M` (pool 20, gate on).
- HyDE and rerank outputs came from a copy of `out/vector-cache.db`, so both arms saw identical expansions.
- MRR is on `both_rank` (answer and attribution in one chunk), k = 5, `unicode61 → substrate`.
- All of Results comes from one run of `run_matrix.sh` on the tokenizer as it is now.

### Gold sets (`eval/gold.json` on the fixture, `eval/gold-vault.json` on vault A)

| corpus | stack | lexical cohort MRR | semantic cohort MRR |
|---|---|---|---|
| fixture (72 cases) | lexical-only | 0.905 → 0.905 (n 28) | 0.367 → 0.382 (n 44) |
| fixture | full | 0.936 → 0.936 | 0.667 → 0.679 |
| vault A (34 cases) | lexical-only | 0.466 → 0.486 (n 21) | 0.326 → 0.314 (n 13) |
| vault A | full | 0.581 → 0.622 | 0.596 → 0.596 |

In the lexical-only runs, 5 cases improved, 2 got worse and 99 were unchanged. Both losses were vault A
cases at rank 3–5 that dropped just past k.

### Identifier queries (private gold sets, written for this experiment)

Each identifier is asked twice, with the rest of the question identical:
- **Same:** spells it as the note does.
- **Respelled:** uses words for camelCase, camelCase for snake_case, or words for a path.

All targets are eligible passages (active/complete, not `conversation`). Answers were copied from the
note before any of these queries ran. Every respelled query yields a different `fts.terms()` list from its
twin. An earlier version had four that did not, because kebab-case became words or a stopword dropped
out. Those were rewritten before this run.

| vault | stack | same spelling (n 10) | respelled (n 10) |
|---|---|---|---|
| vault A | lexical-only | 0.532 → 0.517 | 0.503 → **0.592** |
| vault A | full | 0.625 → 0.675 | 0.667 → **0.728** |
| vault B | lexical-only | 0.383 → 0.403 | 0.250 → **0.375** |
| vault B | full | 0.607 → 0.587 | 0.525 → **0.625** |

Lexical-only per case: 10 improved, 2 got worse, 28 unchanged.
- **Gains:** 6 misses became found, 5 of them respelled. One of those is found only because each
  identifier in a path gets its own whole term.
- **Losses:** one same-spelling case dropped from rank 5 to a miss, displaced by one new passage in the
  retriever's fusion. One respelled case went from rank 2 to rank 3.

### Cost

| measure | unicode61 | substrate |
|---|---|---|
| vault B `chunks_fts` rebuild (7,206 chunks, 3.5 MB of text; `build_index.py` prints it) | 0.04 s | 0.09 s |
| peak memory inserting one 5 MB row, `a_a_…` / `aBaB…` (the C draft, which buffered: 88 MB on `a_a_…`) | 26 / 35 MB | 35 / 40 MB |
| distinct terms: fixture / vault A / vault B | 16,398 / 7,741 / 13,649 | 19,934 / 11,296 / 27,663 |
| index file size, vault B | 48.7 MB | 49.0 MB |
| MATCH latency | 0.14 ms | 0.14 ms |

The rebuild cost is paid only when FTS rows are written: 0.06 s on top of vault B's 2.2 s no-op compose.
Runs longer than 4096 bytes are streamed (parts only, no whole terms), so memory does not grow with
a pasted blob. No run in the three corpora is that long. A camelCase identifier in a streamed run, or one
whose whole term would exceed 128 bytes, loses the single token `unicode61` gave it: a deliberate bound.

## Findings

1. **There is no prefix double-counting.** An earlier draft claimed that `"foo"*` counted a `foo-bar`
   twice, once as `foo` and once as the whole term `foobar`. A review disproved that, and it was
   re-checked: FTS5 merges same-position hits, so `foo-bar zz` and `foo baz zz` score the same bm25. The
   vault A loss it was meant to explain is a reshuffle of near-tied lexical hits at ranks 3–6. Outline
   routing and RRF then push the target from rank 5 to just past k. No single mechanism was found.
2. **The respelled cohort is where the gain is**, and almost all of it comes from camelCase splitting plus
   the concatenated whole term. The same-spelling cohort barely moves, because `unicode61` already splits
   snake_case and paths at their punctuation.
3. **The full stack absorbs much of the lexical difference** on the gold sets, but not on the identifier
   set. The embedder does not reliably bridge `pageToken` ↔ `page_token` either (small set only).
4. **The large set confirms 2, and shows where the gain comes from.** Joined → camel gains +0.507:
   `unicode61` indexes `page_token` as `page` `token`, so the query `pagetoken` finds nothing, and only the
   whole term bridges it. Path → words gains least (+0.026), because `unicode61` already splits at `/` and
   `.`.
5. **Same-spelling losses are mostly near-tie swaps.** One mechanism was found. The note has camelCase
   inside snake_case (`page_tokenId`), and the lowercased query becomes a phrase that no longer matches.
   v2's per-word whole terms fix it, but only 1 of v1's losing cases was that, and v2 added no new loss.
   The pooled loss barely moved (−0.008 → −0.007, not significant). What remains is rank movement among
   close BM25 scores, mostly on path-shaped queries.
6. **There is one positional gap, and closing it costs more than it buys.** A review found it: a camel
   word's pieces take their own positions, but unicode61 gives the word one. So the lowercased phrase
   `foobar baz` (from the query `foobar_baz`) finds `baz` one position late in `fooBar_baz`. v2's word
   terms cover only a camel word that comes last.

   A v3 gave each word one position, as unicode61 does: later pieces colocated, and a camel query word
   as one token. Paired against v2 on the same cases:
   - same-spelling identifiers: +0.005 ± 0.005;
   - the respelled set: +0.000 ± 0.002;
   - camel → snake: **−0.510 ± 0.084**, because `snake_case` then asks for two positions where
     `snakeCase` has one.

   v3 also missed its own text in two cases, which a re-review found: a camel word in a streamed run,
   and one past `MAX_CONCAT`. v3 was dropped. The gap is an expected-failure test in
   `test_substrate_tok.py`.

## Open

- **Close the positional gap without losing camel → snake,** if a sample of real queries shows the
  lowercased-snake-over-camel shape matters. FTS5 positions are one-dimensional, so no layout serves
  both, which leaves a query-side rewrite in the engine.
- **Delete the stale C build.** `substrate_tok.dylib` in this directory is gitignored, and deleting it
  needs the user's OK.
- **Finish the runtime cost on vaults C–F** and vault B's hybrid block, on a quiet machine
  (*Runtime cost*).
- **Try FTS5's `prefix=` index option** on both arms, to see whether it wins back the prefix-expansion
  time the whole terms cost.
- **Run the full stack (HyDE + rerank) on the large set**, if the small set's full-stack evidence is not
  enough.
- **Find out how often queries respell an identifier.** The large set's own identifier cases don't, and
  the gain lives entirely in the ones that do. A sample of real agent queries would settle how much the
  +0.20 and +0.52 are worth.
- **Optionally split at digit→Upper.** `s3Bucket` and `base64Encode` stay whole today, as documented in
  `split.rs`. Splitting them would mean adding that boundary.

## Shipping cost

**Python and SQLite (checked on the installed app, v0.3.4).**
- The bundled `runtime/bin/python3.14` is CPython 3.14.7 with SQLite 3.53.1, statically linked into
  `libpython3.14.dylib`, with `ENABLE_FTS5` and without `OMIT_LOAD_EXTENSION`.
  `Connection.enable_load_extension(True)` works, and `load_extension(path, entrypoint=...)` exists.
- The bundled SQLite exports no `sqlite3_*` symbols, so the dylib must go through the routine table
  (`libsqlite3-sys` with `loadable_extension`), as this one does.
- Apple's SDK `sqlite3ext.h` defines `SQLITE_OMIT_LOAD_EXTENSION`. A C build would need vendored
  amalgamation headers; the Rust build avoids this.

**Code signing.**
- `python3.14` runs with the hardened runtime and only `allow-jit`, so library validation is on. Loading
  the ad-hoc-signed dylib into it is **refused** (dlopen: code signature not valid for the process).
  SQLite truncates that error message.
- `build-bundled-engine` already signs every Mach-O it stages with the Developer ID, `--options runtime`
  and the timestamp, then verifies each one. A dylib staged into the tree is therefore signed with the
  same Team ID, and library validation allows that.
- No new entitlement is needed. **Not verified end to end:** signing needs the Developer ID keychain item,
  so it was not run here.

**Toolchain.**
- Homebrew `rustup` (keg-only, `/opt/homebrew/opt/rustup/bin`, not on PATH) with the stable 1.99.1
  toolchain, minimal profile, in `~/.rustup`. Crates cache in `~/.cargo/registry`.
- `rust-toolchain.toml` pins 1.99.1. Release builds run `--frozen --offline` (see below), and the
  crates should be vendored (`cargo vendor`) so an offline build works.
- The bundle is arm64-only, as is `python3.14` and `Scripta`, so no universal build is needed. A universal
  build would add `rustup target add x86_64-apple-darwin` and `lipo -create`.
- The dylib's minOS is 11.0, which matches the interpreter. Its install name is `@rpath/…` (set in
  `build.rs`) and it is 319 KB stripped. It links only libSystem, so the "links only macOS or bundled
  libraries" check passes.

**`build-bundled-engine` integration (for the PR).** Cargo is not Apple's toolchain. A build runs
third-party code: `libsqlite3-sys`'s `build.rs` and the build-dependencies it compiles (`pkg-config`,
`vcpkg`, `syn`, `quote`, `proc-macro2`, `prettyplease`). The Swift arms compile after the interpreter's
final `pin_tool --recheck`, where the script promises that nothing but checked code runs before signing.
Building the crate at that point would let a compromised build-dependency write into `$STAGE` and have
it Developer-ID-signed. So the order is:

1. **Build first, outside the stage.** Before `$STAGE` exists, run
   `cargo build --release --frozen --offline` from vendored crates. Vendor with `cargo vendor` into the
   crate; a committed `.cargo/config.toml` replaces the crates.io source. Use a target dir outside
   `$STAGE` and `$DEST`, and a scrubbed environment: `env -i` with PATH and HOME set, and no
   `RUSTFLAGS`, `RUSTC_WRAPPER` or `CARGO_*` inherited.
2. **Record the dylib's sha256.**
3. **Stage it after `--recheck`.** Copy it to `$STAGE/lib/libsubstrate_tok.dylib`, outside `runtime/`.
   `engine-interpreter.pin` is an exact manifest, and an added file there is refused.
4. **Re-verify the sha256 immediately before the signing loop.** Then every Mach-O signed is either
   pinned or hash-checked.
5. **Fingerprint the inputs.** `fingerprint()` hashes `Cargo.toml`, `Cargo.lock`, `build.rs`,
   `rust-toolchain.toml`, `.cargo/config.toml`, `src/**`, the vendor directory and `rustc -vV`. It
   hashes the sources, not the dylib, for the reason the script gives for the Swift arms.
6. **Add license text.** Add `libsqlite3-sys`'s MIT text to `third_party_licenses/`. `proptest` is
   dev-only and does not ship.

**Engine changes the PR would need.** This experiment patches `sqlite3.connect` and makes none of these.
- **Load it on every connection that reads or writes `chunks_fts`.** That is `schema.connect`,
  `connect_immutable` and the writers: the `chunks_ai` and `chunks_ad` triggers write to the FTS table.
  Without the extension, SQLite raises "no such tokenizer".
- **Resolve the dylib by an absolute path relative to the engine root,** for example
  `<engine>/lib/libsubstrate_tok.dylib`, never from an environment variable or the working directory.
  `substrate_tok.py`'s `SUBSTRATE_TOK_LIB` override is for this experiment only. Under library
  validation an override could still choose any Team-ID- or Apple-signed image. On the unhardened DEBUG
  engine under `~/.substrate/engine` it could choose any dylib.
- **Enable loading through the C API only.** Use
  `conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, True)`; it is present in the bundled
  CPython. Do not use `enable_load_extension(True)`, which also turns on SQL's `load_extension()`.
  Load before the first statement and disable the setting straight after.
- **Register a versioned tokenizer name,** `substrate_v1`, and bump it with any change to `split.rs` or
  to the tokenizer's arguments. `chunks_fts` is external-content, so `chunks_ad` re-tokenizes the OLD row
  to delete its terms. A different split rule would delete the wrong terms and corrupt the index
  silently; with a versioned name, a mismatched dylib fails as "no such tokenizer".
  Library validation also accepts an older Team-ID-signed build.
- **Bump the schema version,** because the FTS contract changes. Drop-and-rebuild then follows the
  existing path.
- **Add a CI build of the crate.**
- **Expect the plain `sqlite3` CLI to lose access:** it can no longer MATCH against `chunks_fts`.

## Files

| file | job |
|---|---|
| `src/split.rs`, `src/ffi.rs`, `src/lib.rs`, `build.rs`, `Cargo.toml`, `rust-toolchain.toml` | the extension |
| `tests/prop.rs` | proptest properties |
| `substrate_tok.py` | loader: `load(conn)`, `install()` (patches `sqlite3.connect`), `tokens(text)` |
| `test_substrate_tok.py` | end-to-end tests |
| `build_index.py` | makes both arms from one scratch copy; refuses live index paths |
| `run_eval.py` | `substrate eval` with the extension loaded and no baseline |
| `run_matrix.sh` | the full eval matrix behind Results |
| `cases.py`, `summarize.py` | per-case ranks; the MRR table |
| `run_large.sh`, `score_large.py`, `make_respelled.py` | the large private set: runs, paired clustered scoring, the respelled cases |
| `bench_large.py`, `test_bench_large.py` | the large set's runtime cost: latency, query-side tokenizing, `chunks_fts` size, rebuild time; tests of its guards |
| `reference-c/` | the first draft in C, kept for reference, not built |
