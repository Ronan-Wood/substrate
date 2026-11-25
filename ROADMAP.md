# Roadmap

substrate is a local knowledge engine for Markdown vaults. It composes a project vault and the
vaults it inherits into a named **scope**, indexes it, and answers search over it from the command
line or through an MCP server. It runs on your machine and talks only to a model server on loopback
(Ollama today).

**The next goal is `pip install substrate-engine` working on anyone's vault, on macOS and Linux.**
Today the engine ships inside the Scripta app on Apple silicon. On `main`, an ordinary Obsidian
vault now composes in the common case, with a manifest and no edits to its notes (theme 1 lists
the known gaps), but no release has that yet and nothing is on PyPI.
What stands between here and `pip install` is the converter carve-out and publishing (theme 2),
and the decisions its issues want settled before the first release (#75, #76, #90). The themes
below are in the order we expect to take them. Measured retrieval changes, performance in CI and a
few experiments run alongside.

**Want to pick one up?** Comment on its issue first, so two people don't build the same thing, and
read [`CONTRIBUTING.md`](CONTRIBUTING.md). An issue someone has said they're taking, or that has an
open pull request, is taken. Items labelled **`needs-decision`** wait on a maintainer's choice; the
issue says what the choice is. Please don't start one until it is made. Items labelled **`good
first issue`** need no models and no special hardware.

## Words used below

| word | meaning |
|---|---|
| vault | a folder of Markdown notes |
| scope | a project vault plus the vaults it inherits, composed and indexed as one |
| compose | read a scope's vaults and write its index |
| spine | the frontmatter fields retrieval filters and ranks on: `status`, `doc_type`, `confidence` |
| manifest | `.substrate.toml` in a project vault: its name, what it inherits, and its settings |
| `serve` | the long-running engine: answers over a Unix socket and keeps indexes fresh |
| bridge | `substrate-mcp --connect`, a small process that relays one MCP client's stdio to `serve` |
| grant | the user's consent for one named client, stored by `substrate grant`. Writes need one today |
| contract | `contract/contract.json` and `contract/frames/`: what clients may rely on, generated from the code |
| lexprec | a fusion rule: when the keyword top hit is a precise match, keep keyword order first |
| MRR@5 | mean reciprocal rank of the right answer within the top 5. Higher is better |
| quiet machine | a latency run with only the model server busy |
| quarantine | leave a note out of the index, or stop retrying a converted file, and name it and why, so one fault does not stop the rest |
| extra | an optional install group: `convert` (docling's format readers) or `convert-ml` (models, OCR) |
| worker | the process docling converts in, which the engine bounds, retries and quarantines |

**How the numbers here were measured.** Accuracy comes from a private evaluation set built from
real notes, so it isn't published. Deltas are paired, with 95% confidence intervals clustered by
note, and a change is adopted only if no group of queries gets significantly worse and no negative
case leaks. No latency figure here is a quiet-machine measurement yet. The default stack must fit a
32 GB laptop. A number this page proposes is a target, not a result.

## Order at a glance

```mermaid
flowchart TD
    subgraph pip["1. Compose a stranger's vault (core done)"]
        PB1["#21 missing spine → defaults"]
        PB2["#22 quarantine one bad note"]
        PB3["#23 writable_folders"]
        PB4["#24 skip list"]
        PB5["#55 title from file name"]
        PB8["#71 identical notes compose"]
        PB6["#53 rename spine keys"]
    end
    subgraph pkg["2. Converter and PyPI"]
        CV1["#25 converter phase 1 (3 of 4 planned PRs merged)"]
        CV2["#26 PyPI substrate-engine"]
    end
    L1["3. #27 Linux CI, full lock"]
    subgraph ret["4. Retrieval"]
        R3["#28 new defaults"]
        R4["#29 per-request num_ctx"]
        R2["#30 ship FTS5 tokenizer?"]
    end
    M["5. MCP wins M1–M3"]
    CN["6. Consent CN1–CN4"]
    subgraph perf["7. Perf in CI"]
        P1["#38 bench suite"] --> P2["#39 instruction-count gate"]
    end
    subgraph exp["8. Experiments"]
        E4["#43 rerank less"] --> E5["#44 query router"]
        E9["#42 one central engine over HTTP (Rust bridge = phase 3)"]
    end
    PB1 --> CV2
    PB3 --> CV2
    PB4 --> CV2
    PB5 --> CV2
    PB8 --> CV2
    PB6 --> CV2
    CV1 --> CV2
    L1 -.-> CV1
    D90["#90 minimum Python"] -.-> CV2
    R3 --> E4
    P1 -.-> E9
    classDef done fill:#d4edda,stroke:#2e7d32,color:#000
    classDef part fill:#fff3cd,stroke:#b8860b,color:#000
    class PB1,PB2,PB3,PB4,PB5,PB6,PB8 done
    class CV1 part
```

Solid arrows are hard dependencies; dotted ones mean "decide or measure first". Green nodes are
merged to `main`; the yellow one is partly merged.

---

## 1. Compose a stranger's vault (pip blockers): done

All seven are merged to `main` and their issues are closed: [#21](https://github.com/Ronan-Wood/substrate/issues/21), [#22](https://github.com/Ronan-Wood/substrate/issues/22), [#23](https://github.com/Ronan-Wood/substrate/issues/23),
[#24](https://github.com/Ronan-Wood/substrate/issues/24), [#55](https://github.com/Ronan-Wood/substrate/issues/55), [#71](https://github.com/Ronan-Wood/substrate/issues/71) and [#53](https://github.com/Ronan-Wood/substrate/issues/53). The pull request for each is under Shipped. A vault of
plain notes now composes: a missing spine takes defaults, one bad note is quarantined, the
manifest declares what is skipped, where writes go and which keys the engine reads, and notes with
the same bytes compose. None of it is in a release yet. Closed beside them: [#59](https://github.com/Ronan-Wood/substrate/issues/59) (first run: `substrate init`) and [#52](https://github.com/Ronan-Wood/substrate/issues/52) (an unknown manifest key refuses). What grew out of it is still open:

| id | item | status |
|---|---|---|
| [#60](https://github.com/Ronan-Wood/substrate/issues/60) | Two faults that belong to one file still refuse the whole scope: a skipped file that declares a spine, and a source's `_meta.md` the vault layer can't read. Decided: both are quarantined and count toward the ceiling. | planned |
| [#61](https://github.com/Ronan-Wood/substrate/issues/61) | A refused compose's report is built from structure, not from failure text that quotes a note's value, and names a folder it removed before it refused. | planned |
| [#62](https://github.com/Ronan-Wood/substrate/issues/62) | A bug: over MCP, `drift.error` quotes a broken `_meta.md` and its absolute path, where a quarantined note is named by kind and key only. | planned |
| [#63](https://github.com/Ronan-Wood/substrate/issues/63) | A bug: `amend` reads a note's class key less strictly than compose does, so a frozen note whose key is spelled another way can be amended. | planned |
| [#64](https://github.com/Ronan-Wood/substrate/issues/64) | `ingest` and `amend` should refuse, at the plan, a note whose declared `doc_id` another note already has: today the write succeeds and the next compose refuses the whole scope. The issue's identical-bytes case predates #73, which made such notes compose. | planned |
| [#75](https://github.com/Ronan-Wood/substrate/issues/75) | Decide what compose does when a vault's `[engine_keys]` renames shrink or change: today, removing one un-withholds notes labelled only under the new name. | `needs-decision` |
| [#76](https://github.com/Ronan-Wood/substrate/issues/76) | Decide which heading, if any, names a note that declares no title. Today, with no level-1 heading, the first heading of any level does, so a month of daily notes made from one template are all titled `Tasks`. | `needs-decision` |

## 2. Converter carve-out, then PyPI

Document conversion (Office, HTML, scanned PDFs, images) accounts for 954 of the 987 MiB of
packages in Scripta 0.3.5's bundled engine, through docling and torch. The core needs `pypdfium2`, `pyyaml` and
`numpy`, about 26 MiB.

| id | item | status |
|---|---|---|
| [#25](https://github.com/Ronan-Wood/substrate/issues/25) | **Phase 1.** Two extras: `[convert]` (docling-slim's torch-free format readers, the same readers the engine runs today) and `[convert-ml]` (layout and table models, OCR, granite-docling). `numpy` becomes a declared core dependency. Every docling conversion runs in a stdio worker process, which the engine bounds, retries once and then quarantines. Decided: D1–D10, as the issue's comments record them (D2 is always the worker, D4 a pure wheel first), not as the recommendation column in its body has them. Four pull requests were planned. Merged: the extras with a core-only `engine` job (PR #80), the `engine convert` job (PR #88), and the worker (PR #91). Remaining: the conversion cache. A Lean model of the worker's quarantine rule (PR #101), outside the four, is merged too. The Linux `[convert-ml]` job waits for [#27](https://github.com/Ronan-Wood/substrate/issues/27). | 3 of the 4 planned pull requests merged |
| [#26](https://github.com/Ronan-Wood/substrate/issues/26) | **Phase 2.** Publish `substrate-engine` to PyPI (the name `substrate` belongs to another project). The commands keep their names. Needs a build system, `[project.scripts]` and Trusted Publishing from a tag-gated job. Decided on #25 (D4): the first release is one pure wheel for macOS and Linux; the macOS wheel with the Apple reader follows under [#30](https://github.com/Ronan-Wood/substrate/issues/30). Documented install: `uv tool install --python 3.14 'substrate-engine[convert]'`. | `needs-decision`, after [#25](https://github.com/Ronan-Wood/substrate/issues/25), with [#90](https://github.com/Ronan-Wood/substrate/issues/90) settled first |

The conversion worker's reviews and the first real documents turned up follow-ups. Each is its own
issue:

| id | item | status |
|---|---|---|
| [#86](https://github.com/Ronan-Wood/substrate/issues/86) | Decide what to do about an ordinary web page being refused at ingest: Docling leaves out the text before the first heading, and hidden elements. | `needs-decision` |
| [#87](https://github.com/Ronan-Wood/substrate/issues/87) | The HTML coverage probe joins text with spaces where Docling does not, so a drop cap is refused and a number split across tags is not checked. | planned |
| [#89](https://github.com/Ronan-Wood/substrate/issues/89) | Decide what `--docling-models` does with a PDF page that is only an image, since Docling's OCR is off in this engine. | `needs-decision` |
| [#90](https://github.com/Ronan-Wood/substrate/issues/90) | Decide the minimum Python version: `html.parser` before 3.14.7 hides text after an empty comment. It should be settled before [#26](https://github.com/Ronan-Wood/substrate/issues/26) publishes. | `needs-decision` |
| [#94](https://github.com/Ronan-Wood/substrate/issues/94) | The conversion worker's memory is not limited. | planned |
| [#95](https://github.com/Ronan-Wood/substrate/issues/95) | The coverage probes read a document in the engine with no bound: a zip bomb can exhaust its memory, and a FIFO in the file's place blocks it. | planned |
| [#96](https://github.com/Ronan-Wood/substrate/issues/96) | One worker per converted file pays the docling import every time; a worker that outlives one file needs a design first. | planned |
| [#97](https://github.com/Ronan-Wood/substrate/issues/97) | `ingest --pages` is checked by the PDF readers, not the CLI, so a bad range is not exit 2. | taken, pull request open |
| [#98](https://github.com/Ronan-Wood/substrate/issues/98) | A file's name reaches the terminal unfiltered in refusals and log lines. | `good first issue` |

Later converter phases, not filed yet: a conversion benchmark and reader bake-off (which also picks
the Linux OCR default, D10), an escalation router between tiers, and Scripta bundling the pinned
extras.

## 3. Linux

| id | item | status |
|---|---|---|
| [#27](https://github.com/Ronan-Wood/substrate/issues/27) | A CI job that installs the full locked environment on Ubuntu (`uv sync --frozen --extra convert --extra convert-ml`) and runs the whole suite, with only named macOS-only tests allowed to skip. It needs torch pinned to its CPU build on Linux and OpenCV built from source as on macOS. It is also the Linux `[convert-ml]` job [#25](https://github.com/Ronan-Wood/substrate/issues/25) defers. Today's Linux jobs install the locked core (`engine`) and the core plus `convert` (`engine convert`, PR #88). | planned |

## 4. Retrieval

| id | item | status |
|---|---|---|
| [#28](https://github.com/Ronan-Wood/substrate/issues/28) | Four rule changes, no new model: lexprec fusion, the Qwen3 query-instruct prefix, graded rerank verdicts (promote at P(yes) ≥ 0.9) and a rerank pool of 30. MRR@5 on the decision set (1,368 positives, 60 negatives): **0.767 shipped → 0.821**, with no HyDE call and no negative leaked. Open question: whether the default drops HyDE. One PR per change, then a latency pass. | `needs-decision` |
| [#29](https://github.com/Ronan-Wood/substrate/issues/29) | Send `num_ctx` with each model request (4096 rerank, 8192 embed). Measured: embedder 4.8 → 1.2 GB resident, reranker 8.7 → 2.9 GB, with identical outputs. | `needs-decision` |
| [#30](https://github.com/Ronan-Wood/substrate/issues/30) | Ship the FTS5 identifier tokenizer (a Rust SQLite extension; the experiment is on branch `exp/fts5-tokenizer`). Respelled identifiers gain MRR@10 +0.201 keyword-only and +0.147 hybrid, with no change on the set as written. Shipping means a schema bump, per-platform builds and signing inside the macOS app, and it makes the wheel per-platform, after the first, pure one. | `needs-decision` |

## 5. MCP: small wins

Independent, small, and good first issues.

| id | item |
|---|---|
| [#31](https://github.com/Ronan-Wood/substrate/issues/31) | `expand mode=note` pages with `offset` and `max_chars`, so a note longer than 20,000 characters can be read to the end. `documents` already pages the same way. |
| [#32](https://github.com/Ronan-Wood/substrate/issues/32) | An `include_superseded` flag, default false, on `search` and `documents`, so a `supersedes` link can be followed. |
| [#33](https://github.com/Ronan-Wood/substrate/issues/33) | `search` accepts a `doc_type` filter, as `documents` already does. Today it returns an error. |

## 6. Extending consent

Writes already need a per-client grant (`substrate grant`). These extend the same model.

| id | item | status |
|---|---|---|
| [#34](https://github.com/Ronan-Wood/substrate/issues/34) | Per-client read grants: `substrate grant <client> --reads <scopes> [--sources]`. `--sources` lets a trusted client see call transcripts without passing `include_sources` on every call. | planned |
| [#35](https://github.com/Ronan-Wood/substrate/issues/35) | `substrate grant <client> --direct`: a granted client's `ingest` writes in one call. `amend` keeps plan-then-commit, because its token is a compare-and-swap on the note. | planned |
| [#36](https://github.com/Ronan-Wood/substrate/issues/36) | `[blobs] allow_synced = true` lets the blob store live in a folder a sync client uploads, with a warning about eviction. Owner, mode and ACL checks stay. | planned |
| [#37](https://github.com/Ronan-Wood/substrate/issues/37) | Whether search's models (embedder, HyDE, reranker) may run on a cloud model server when the user opts in per feature, as Scripta now allows for its own features. Proxy, redirect and peer-address hardening stays either way. | `needs-decision` |

## 7. Performance in CI

Nothing measures performance in CI today. The goal is every stage measured and nothing quietly
getting slower.

| id | item | status |
|---|---|---|
| [#38](https://github.com/Ronan-Wood/substrate/issues/38) | One benchmark suite, locally and in CI: search per stage, compose (full, no-op, one note edited), cold start, memory, index size and idle CPU, on seeded synthetic corpora at 1×, 10× and 100× the demo vaults. Model stages use a deterministic stub in CI. | planned |
| [#39](https://github.com/Ronan-Wood/substrate/issues/39) | A Cachegrind instruction-count gate on Linux for CPU-bound stages, failing a PR past a set threshold. Then an interleaved wall-clock A/B that reports only, and memory and size gates. | after [#38](https://github.com/Ronan-Wood/substrate/issues/38) |

## 8. Experiments and verified paths

Each experiment states its success criterion before it runs, and reports in
[`EXPERIMENTS.md`](EXPERIMENTS.md) whether it passed or not.

| id | item | depends on | status |
|---|---|---|---|
| [#40](https://github.com/Ronan-Wood/substrate/issues/40) | A TLA+ model of `serve`: readers, granted writes, the refresh loop, outside composers and shutdown. Checks deadlock, torn documents, lost refreshes and clean shutdown, then validates real traces. | — | planned |
| [#41](https://github.com/Ronan-Wood/substrate/issues/41) | A Lean model of the agent snapshot, the path listed as Planned in [`VERIFIED.md`](VERIFIED.md): without `include_sources`, nothing from a call reaches a published snapshot. | — | planned |
| [#42](https://github.com/Ronan-Wood/substrate/issues/42) | **One central engine for every client.** Phase 1: `serve` speaks MCP Streamable HTTP on loopback, with each granted client's existing token as the bearer, so there's no bridge process and writes go through the same grant, plan and confirm. Phase 2: how a client knows it reached the real engine, locally (launch agent, pinned TLS, or a documented gap) and for server mode on another machine (HTTPS). Phase 3: a static Rust `substrate-connect`, only for stdio-only clients and for Docker or remote use. | decision (phase 2) | `needs-decision` |
| [#43](https://github.com/Ronan-Wood/substrate/issues/43) | Rerank less: skip, size or stop the rerank pool, replayed offline. Target ≥ 40% fewer pairs than [#28](https://github.com/Ronan-Wood/substrate/issues/28)'s 11.4 per query, ≤ 0.005 MRR@5 lost. | [#28](https://github.com/Ronan-Wood/substrate/issues/28) | planned |
| [#44](https://github.com/Ronan-Wood/substrate/issues/44) | A model-free query router that skips HyDE on keyword and identifier queries. | [#43](https://github.com/Ronan-Wood/substrate/issues/43) | planned |
| [#45](https://github.com/Ronan-Wood/substrate/issues/45) | A no-op compose rewrites nothing: profiled in November, it re-read and re-wrote every note's intermediate file. Then profile compose at 10× and 100× scale. | — | planned |

Experiment results that live on branches: `exp/fts5-tokenizer` ([#30](https://github.com/Ronan-Wood/substrate/issues/30)), `exp/vector-scale` (brute-force
vector search holds to about 250k–500k passages per scope) and `exp/runtime-profile` (where a search
spends its time).

## 9. Repository hygiene

| id | item | status |
|---|---|---|
| [#47](https://github.com/Ronan-Wood/substrate/issues/47) | A CI gate that fails a PR whose diff or commit messages carry personal data: structural rules that need no list (home-folder paths, cloud-sync folder paths, frames pointing outside the demo fixtures, database files), plus a match against a private list that is never readable in the repo. Local hooks exist; CI has nothing. Scripta gets the same gate. | planned |

## 10. Needs a decision: multi-machine sync

| id | item | status |
|---|---|---|
| [#46](https://github.com/Ronan-Wood/substrate/issues/46) | Sync vaults between machines without a third-party sync folder. A design exists: each machine keeps its vaults in a local git repository, and `substrate sync` exchanges encrypted git bundles through an object-storage bucket. Indexes never sync; each machine recomposes. It is proposed, not built. Build it or shelve it? Backups should land before sync makes a mistake reach every machine at once. | `needs-decision` |

---

## Later (no issue yet)

Kept from the previous roadmap; each gets an issue when its turn comes.

| area | items |
|---|---|
| Linux and Docker | OCR on Linux through Tesseract (with [#25](https://github.com/Ronan-Wood/substrate/issues/25)'s bake-off); portable model defaults (no `-mlx` tags); the blob store's privacy checks on Linux (ACLs, sync folders by name, `ctime`); `serve` over TCP with a bearer token; a multi-arch container image; a release workflow with attestations and an SBOM; automated engine bumps in Scripta |
| Getting started | `substrate new` to create a vault; `substrate doctor`; `substrate mcp setup <client>`; first-run walkthroughs, measured |
| Verified paths | Lean models of the compose prune, scope composition, supersession and the frontmatter YAML emitter; incremental compose proved equal to full compose (unlocks [#45](https://github.com/Ronan-Wood/substrate/issues/45)'s larger speedup) |
| Performance | a per-stage latency budget; model-side passes; hot paths in Rust or C only where [#38](https://github.com/Ronan-Wood/substrate/issues/38) shows a gain; start-up and footprint |
| Retrieval | a Lean and Hypothesis proof of the lexprec rule once [#28](https://github.com/Ronan-Wood/substrate/issues/28) ships; one quiet-machine latency sitting for [#28](https://github.com/Ronan-Wood/substrate/issues/28), [#42](https://github.com/Ronan-Wood/substrate/issues/42)'s phase 3, [#43](https://github.com/Ronan-Wood/substrate/issues/43) and [#44](https://github.com/Ronan-Wood/substrate/issues/44) |
| App side | Scripta attaching to a remote engine over TCP; backups that sync can't overwrite |
| Docs | `docs/experiments/`, one page per experiment, with `EXPERIMENTS.md` as its index |

## Shipped

Released:

| what | PR |
|---|---|
| The engine has its own repository, signed release tags and source tarballs | the split, in Scripta's repository |
| A Lean model of `blobs gc --unreferenced`, run against the planner in CI; `VERIFIED.md` | #3 |
| A newcomer can install, run and contribute from the repo alone | #4 |
| **v0.1.0** | the tag, on the merge of #1 |
| A YAML block list no longer drops a note's frontmatter | #5 |
| The embedder refuses a model that runs in Ollama's cloud | #7 |
| Consented writes: `substrate grant`, `revoke`, `grants`, `serve --writes=granted` | #8 |
| Frontmatter ends only at a whole `---` line | #10 |
| MCP `ingest` takes the note as content and never reads a caller's path | #11 |
| **v0.2.0** | #12 |
| The vault guard is removed; agent snapshots leave out calls unless a vault opts in | #13 |
| A write log, `provenance` on passages, and `substrate writes check` | #14 |
| **v0.2.1** | #15 |
| The golden contract frames are one capture again, with a test that keeps them so | #16 |
| **v0.2.2** | #17 |

Notable merges to `main` since v0.2.2, in no release yet:

| what | PR |
|---|---|
| Agent snapshots follow their entries, HyDE refuses a cloud model at launch, and `ingest` never writes through a planted name | #18 |
| `EXPERIMENTS.md` and the engine's plans match what shipped; a roadmap, and CONTRIBUTING points to it | #20 (closes #48), #49 |
| A manifest declares what compose skips (`skip`), and the fixed list is its default | #54 (closes #24) |
| A note with no `status` or `doc_type` composes with the defaults, and `status` counts them | #56 (closes #21) |
| A manifest declares where `ingest` and `amend` write (`writable_folders`), and any indexed folder is the default | #58 (closes #23) |
| A note that fails on its own is quarantined and the rest of the scope is indexed; past `quarantine_ceiling` compose refuses again. It has a Lean model and harness, in `VERIFIED.md` | #65 (closes #22) |
| An unknown top-level key in the manifest refuses compose | #69 (closes #52) |
| A note with no title and no heading takes its file name as its title | #70 (closes #55) |
| `substrate init`, `--version`, `help`, and help that says where to start | #72 (closes #59) |
| Notes with the same bytes compose, and a copy under one `doc_id` is left out and named | #73 (closes #71) |
| A manifest renames the frontmatter keys the engine reads its spine from (`[engine_keys]`) | #74 (closes #53) |
| The CodeQL regex alerts and the dependency alerts that had a fix are closed | #77 |
| Wire 4, with `MIN_VERSION` staying 1 | #78 (closes #57) |
| The core is three packages; document conversion is the `convert` and `convert-ml` extras | #80 (part of #25) |
| The HTML coverage probe reads the page with BeautifulSoup, as Docling's reader does | #85 |
| An `engine convert` CI job runs the suite on the core and `convert` alone, and `substrate formats` names what an uninstalled extra would add | #88 (part of #25) |
| Docling converts in a worker process, which the engine bounds, retries and quarantines | #91 (part of #25) |
| CodeRabbit reviews pull requests as advice, and CONTRIBUTING says what review a PR gets | #99 |
| A Lean model of the conversion quarantine rule, run against the real rule in CI; `VERIFIED.md` | #101 (part of #25) |

**Dropped:** the guard correctness fix and the Lean proof of the guard, because the guard itself
was removed in #13.
