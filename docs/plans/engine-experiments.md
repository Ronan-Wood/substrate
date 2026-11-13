# Engine experiments: a plan for nine (report-only)

> **A plan, moved here 2026-11-12.** It was written 2026-11-07 and 2026-11-08, before the engine left
> Scripta, and is kept as written. Where it no longer holds, a dated note says so and the old text
> stays. Three things to know when reading it:
>
> - Code references are `file:line` in the monorepo of the day, where the engine's paths began
>   `substrate/substrate/`. The line numbers have drifted since.
> - The working notes it cites by name (the phase 1 result, the phase 2 progress log, the runtime
>   profile, the concurrency audit) are not in this repository. The phase 2 results are published in
>   [`EXPERIMENTS.md`](../../EXPERIMENTS.md), §"The 6,407-case sweep".
> - "The extraction", P2, L5 and the other step names come from the
>   [engine split plan](../history/engine-split-plan.md). The split finished 2026-11-10.

**Conclusion.**
- Run the two proofs and the Rust bridge's differential harness now. They need no models, so they can run beside the eval sweep.
- Run the two speed experiments as offline replays over the sweep's captured lists once the sweep reports.
- Leave every latency number, the tokenizer's hybrid run and the compose profile for a quiet machine.

Reading for this plan turned up one finding that should not wait for any experiment. **The guard withholds a name the index never records.** `guard.check` withholds `{scope}`, but `documents.vault` holds the vault's DIRECTORY name. Wherever the two differ, a guarded vault's own notes would pass every filter. It is latent, because no vault sets `guard_state` today. Experiment 7 states it as a theorem expected to fail. The fix is a normal `fix/` PR and need not wait for the Lean model (§7).

> **SUPERSEDED 2026-11-12: the guard finding is moot.** The fix landed as engine #6
> (`fix/guard-withholds-vault-directory`), and #13 removed the vault guard altogether in v0.2.1.
> See §7.

Nothing here changes code. Every number from earlier work is marked with its conditions. A number this plan proposes is a target, not a measurement.

| # | experiment | the question | success, in one number | machine | ships to |
|---|---|---|---|---|---|
| 1 | query router | Can a model-free classifier skip HyDE on keyword and identifier queries without losing accuracy? | MRR@5 ≥ the chosen profile − 0.005 (CI lower bound > −0.010), with HyDE called on ≤ 30% of queries | replay: loaded OK. Latency: quiet | engine repo, `retrieve/` |
| 2 | rerank less | How many cross-encoder pairs can be cut per query? | ≥ 40% fewer pairs per query than the recommended profile's 11.4, at ≤ 0.005 MRR cost | replay: loaded OK. Latency: quiet | engine repo, `retrieve/rerank_cross.py` |
| 3 | Ollama tuning | (running in another session) | that session's own criterion | quiet | engine config and docs |
| 4 | FTS5 tokenizer | Does the lexical gain survive hybrid, and how does it ship per platform? | hybrid Δ on respelled identifiers ≥ +0.10, no stratum significantly worse | needs the embedder | engine repo, a native crate |
| 5 | Rust `--connect` bridge | Can a static binary forward the protocol exactly? | 0 disagreements over ≥ 10,000 generated sessions; ≤ 5 MB resident per bridge | any | engine repo, a second binary |
| 6 | profile first | Is any non-model stage worth rewriting? | a stage qualifies only at ≥ 20% of wall with ≥ 50% pure-Python self time | quiet | probably nothing |
| 7 | Lean: the guard | Does any search path return a withheld passage? | T1 proved without `sorry`; 0 differential escapes over ≥ 10,000 worlds; 4 planted mutants caught | any | a fix PR, then an experiment folder. **Moot 2026-11-12:** fixed in #6, guard removed in #13 |
| 8 | TLA+: `serve` | Deadlock, lost refresh, torn index, unclean shutdown? | TLC exhausts the 2-reader × 2-scope config with 0 violations | any (bounded TLC workers) | an experiment folder; findings become fix PRs |

## How every accuracy number is read

- **The set.** The private eval set has 6,407 unique cases. It lives outside every repository and nothing from it enters a file here. Report each of these cohorts separately, never pooled:

  | cohort | unique cases |
  |---|---|
  | kw (agent keyword bag) | 1,694 |
  | lexq (a question in the note's own words) | 1,664 |
  | para (a blind paraphrase) | 1,326 |
  | identifier | 889 |
  | the shared vault (reported once, not six times) | 320 |
  | unanswerable | 235 |
  | negatives | 229 |
  | real | 50 |

  Source: `retrieval-tuning-phase1-result.md` in the shared session notes.
- **Resolution.** The paired per-case SD is about 0.30. That puts the pooled 95% CI near ±0.014, and per cohort it is about:

  | cohort | n | CI half-width |
  |---|---|---|
  | kw | 1,694 | ±0.014 |
  | para | 1,326 | ±0.016 |
  | identifier | 889 | ±0.020 |
  | **real** | **50** | **±0.083** |

  These are unclustered; clustering by target note widens them. **The real cohort decides nothing.** Report it, and never use it as a gate.
- **The cross-encoder subset is smaller.** The sweep scored cross-encoder pairs only on its *decision set*: 1,368 positives and 60 negatives (`retrieval-tuning-phase2-progress.md`, "Tier 2/3"). Any replay that includes rerank is limited to that set, which roughly doubles the per-cohort CIs, unless more pairs are scored. Scoring more pairs needs Ollama for hours.
- **The adoption rule is the sweep's.** A change is adopted only if no cohort is significantly worse and no negative leaks. The sweep rejected the best-scoring rerank mode (prob mode, 0.827–0.831) on identifier −0.048 alone.
- **Replay, not re-run.** The sweep captures each arm's ranked list per case once, then replays fusion, gate, pool and score mode offline. The replay was checked against the engine's own `retrieve()`:
  - 300/300 identical top-5, lexical and hybrid;
  - 200/200 with HyDE;
  - 0/100 stored verdicts disagreeing with `CrossEncoderReranker._score`.

  Experiments 1 and 2 reuse this harness (`experiments/retrieval-tuning/sweep/`, not in this repository). Accuracy therefore costs no model calls, and can run on a loaded machine.
- **Latency comes only from a quiet machine.** No latency figure from 2026-11-07 is a measurement: the machine had swap at 17–18 of 19 GB and a load of 15–70. Every latency claim below waits for a pass with Ollama alone, the other sessions idle, and figures given as medians and p90 over ≥ 30 queries per cohort.

---

## 1. Query router

**Hypothesis.** About 93% of real traffic is agent keyword bags (phase 1 result). A model-free classifier on the query text can send identifier and keyword-bag queries down lexical-precision fusion plus rerank, with no HyDE. Paraphrases would take the full path. Most of HyDE's 1.5–2.7 s would be saved at no measurable accuracy cost.

**Evidence so far.**

| fact | where |
|---|---|
| Plain RRF costs identifier −0.101 [−0.127, −0.077] and kw −0.070, and helps para +0.081. Measured on the largest scope, no HyDE, no rerank | phase 1 result, "Pilot" |
| Lexprec (lexical order when the lexical top is precise) is +0.084 [+0.075, +0.093] over shipped hybrid, and wins in every scope | phase 2 progress, "Results so far" |
| With rerank on, HyDE adds only +0.005 over the qwen3 query prefix (0.826 vs 0.821; the CIs overlap). The difference is in para: +0.016 against −0.003 | phase 2 progress, Tier 2/3 table |
| HyDE runs before fusion and before the rerank gate, so it is paid even on queries the gate then skips | `substrate/substrate/retrieve/retriever.py:452-482` (expand, then embed), `:528-543` (fuse, then rerank) |
| The gate reads the FUSED top, not the lexical top | `substrate/substrate/retrieve/rerank_cross.py:272` → `rerank.py:129-148` |
| A caller-chosen route already exists: `fast` drops HyDE and rerank, and its `expected_mrr` goes null | `substrate/substrate/mcp/server.py:576-602` |
| The capability's measured tier is keyed on the arm combination, so a routed answer needs its own tier or a null | `retriever.py:202` (`_measured_tier`), `:576` (`_capability`) |
| HyDE's cost: 2,652 ms median cold on the largest scope, 1,452–2,167 ms on others (loaded) | `runtime-profile-2026-11-06.md` |

**What the router must beat.** It must beat the **no-generation profile** (lexprec + qwen3 prefix + CE thr 0.9, pool 30: 0.821), not shipped (0.767). The router's whole upside over "no HyDE for anyone" is HyDE's para gain. If the router cannot keep most of that +0.019 on para, the answer is to drop HyDE everywhere, and that answer is cheaper.

**Method.**
1. **Arms**, all with lexprec and CE thr 0.9, pool 30, replayed offline:

   | arm | what it does |
   |---|---|
   | A | no-generation (prefix, no HyDE): the baseline |
   | B | recommended (HyDE on every query): the ceiling |
   | C | a hand-written rule router on query shape: token count, identifier shape (camel, snake, dotted, path), function-word ratio, question form |
   | D | a zero-feature router: HyDE only when the lexical top is NOT `_already_precise`. That is the gate, moved before HyDE |
   | E | a small learned classifier (logistic regression on C's features), cross-validated by target-note cluster |

2. **Measure:**
   - MRR@5 and R@5 per cohort, against both A and B;
   - the router's confusion matrix per cohort (which strata it sends to HyDE);
   - the fraction of queries that call HyDE;
   - HyDE calls saved × the quiet-machine HyDE median.
3. **Weight by traffic.** Re-weight the cohorts to the real traffic mix (93% kw) for a traffic-weighted latency estimate, and say it is an estimate.
4. **The real cohort** (n=50) is reported, labelled not significant.

**Success criterion.**
- The chosen router is within −0.005 of B pooled (CI lower bound > −0.010).
- It is no worse than B on para by more than 0.010.
- It is not significantly worse than A on any cohort.
- It calls HyDE on ≤ 30% of queries.
- On the quiet machine, the traffic-weighted median search time falls by ≥ 1.0 s against B.

**Cost.**
- 1–2 days for replay and analysis. No new model calls for accuracy; one quiet-machine pass for latency.
- Arm D is nearly free to build, since it reuses `_already_precise`.

**Risks.**
- The kw and para strata are generated: the paraphrases were written blind from a gist. A router tuned on their *shape* can learn the generator, not real users. So C and D, which carry no fitted weights, are preferred over E.
- E's weights would be derived from the private set. They would have to stay out of any commit, or be refit on synthetic fixture queries.
- The name `route` is taken: `retriever.py:500` uses it for outline routing. Call this one `query_class` throughout.

**What shipping it would change.**
- `retrieve()` gains a query-class decision before HyDE.
- `Capability` names the class that ran, and `_measured_tier` gains a tier per class (or a null).
- The payload's new key is a contract change under P2's `contract.json`.
- The change lands in the engine repo, after the sweep's fusion rule, which it depends on.

---

## 2. Rerank less

**Hypothesis.** Most cross-encoder pairs buy nothing. (a) Skipping rerank when the lexical top is precise, and (b) an adaptive pool sized by how contested the fused top is, can each cut pairs scored per query at no measurable MRR cost. (c) The third lever in the brief, caching per (query, passage), **is already shipped**; what is unknown is its hit rate.

**Evidence so far.**

| fact | where |
|---|---|
| The gate skips 57% of queries and is load-bearing: gate off costs kw −0.028 and identifier −0.041, both significant | phase 2 progress, "What the grid says" |
| Pool 10 gives 0.811, 20 gives 0.815, 30 and 40 give 0.818. Fused lists hold about 30 candidates | same |
| The recommended profile scores 11.4 CE pairs per query | phase 2, Tier 2/3 table |
| The shipped pool is 20; concurrency 4 | `rerank_cross.py:83`, `:187` |
| Rerank, when the gate fires: 4.6–4.9 s median (loaded); 9.4 s worst, a model swap. Concurrency 8 is no better than 4 (GPU-bound) | `runtime-profile-2026-11-06.md`, Breakdowns |
| **A cache per (query, passage) already exists**, keyed on the exact query string plus a hash of the passage text. An ABSTAIN is deliberately not cached | `rerank_cross.py:276-292`, `:285`, `:346` |
| `gate` and `pool` are absent from the cache key, so any pool policy reuses the same cache | `rerank_cross.py:202-208` |
| The final order is a stable partition of the pool (YES first), then the tail beyond the pool | `rerank_cross.py:390-391` |
| A CE call costs 942 ms median (p90 1,316) at 4-way concurrency, loaded: a cost indicator only | phase 2 progress, "Latency" |
| Graded P(yes) is available on Ollama 0.32.5, though the engine's note says otherwise (written against 0.20.3) | `rerank_cross.py:35-48`; phase 2 progress |

**Method.** Replay over `ce.db`: about 61,000 stored pairs, with P(yes) kept beside each binary verdict. The arms:
1. **Gate on the lexical top.** Under lexprec, the fused top IS the lexical top whenever that hit is precise. Verify the equivalence on the replay instead of assuming it, then measure the gate under each fusion rule the sweep may ship.
2. **Adaptive pool.** Size the pool by the RRF margin: score only the candidates whose fused score is within δ of the top, with a floor of 4 (one concurrency batch) and a cap of 30. Grid δ.
3. **Early exit.** Score in fused order, one batch of 4 at a time. Stop when a candidate in the batch reaches P(yes) ≥ 0.9 and outranks every unscored candidate's possible promotion under the threshold rule.
4. **Cache hit rate.** This has no offline answer, because nobody has logged real traffic. Add an opt-in counter (hits, misses, abstains per call) to a local build, and run it for a week of the operator's own sessions. Report aggregates only.
5. **Cost.** One quiet-machine calibration of pairs/s at concurrency 1, 2 and 4 turns pairs saved into seconds.

**Success criterion.**
- ≥ 40% fewer CE pairs per query than 11.4.
- Pooled MRR@5 ≥ the recommended profile − 0.005, with no cohort significantly worse.
- On the quiet machine, the median rerank wall time on gate-fired queries is ≤ 2.5 s.
- Caching counts as worth more work only if its hit rate is ≥ 20% of pairs.

**Cost.** 1 day of replay, one quiet calibration hour, a week of passive counting.

**Risks.**
- Early exit is order-dependent. Batched GPU scoring is not bit-exact (the ABSTAIN note, `rerank_cross.py:346`), so a verdict near 0.9 can flip between runs.
- The cache key hashes the bytecode of the scoring functions (`rerank_cross.py:140-160`). Moving to graded verdicts must change that hash, or a stored 1.0/0.0 would be read as a probability. Assert this in a test; do not assume it.
- The replay is limited to the decision set (see above).

**What shipping it would change.** The change is limited to `rerank_cross.py`:
- a pool policy;
- graded verdicts (`logprobs` on the request);
- the threshold rule.

Plus one capability field saying how many pairs were scored. It ships in the engine repo, together with or after the sweep's rule changes 2–3 ("Engine changes these profiles need").

---

## 3. Ollama tuning (referenced, not planned here)

This is running in another session. This plan does not redo it.

What it has to beat, from earlier measurements on a loaded machine:
- **Per-request overhead.** Every call pays 74 ms of `load_duration`, even with the model resident and any `keep_alive`. That is most of a 96–107 ms embed. A rerank call pays 137 ms of load overhead on 328 ms total (`runtime-profile-2026-11-06.md`).
- **Context length.** `OLLAMA_CONTEXT_LENGTH=32768` makes the 0.6b embedder 5.8 GB, and HyDE 4b 11 GB. Two models cannot then be co-resident without one being evicted (phase 2 progress, "Conditions").
- **Contention.** Two requests to the same model serialize and cost about 2 GB more. Generate beside rerank slows generate by 34% and each score 3–5× (`search-concurrency-audit-2026-11-06.md`).

**How it folds in.** Its result changes the *per-call constants*, not any ranking:
- the ms per embed, per CE pair and per HyDE generation;
- whether two models stay resident.

Experiments 1 and 2 decide on accuracy offline. Their latency conversion should use the post-tuning constants, so take their quiet-machine latency pass **after** experiment 3 reports.

---

## 4. The FTS5 tokenizer: shipping path in the engine repo

**State (referenced, not re-measured).** `experiments/fts5-tokenizer/`, uncommitted at the time and on branch `exp/fts5-tokenizer` since. Lexical-only on the large set, MRR@10 against `unicode61`, 95% CI clustered by note (the README, "Results — large set"):

| set | Δ |
|---|---|
| respelled identifiers (n=770) | **+0.201 ± 0.034** |
| camelCase asked as snake_case (n=230) | **+0.518 ± 0.083** |
| same-spelling identifiers | −0.007 ± 0.008 |
| keyword bags | −0.000 ± 0.002 |
| paraphrases | +0.005 ± 0.004 |

The brief quotes +0.210 for the respelled set; the README and handover say +0.201. **The hybrid run is still pending**: it failed while the embedder was serving another sweep. One known gap is an expected-failure test: a lowercased snake_case phrase over a camelCase word that is not the last word.

**The deciding measurement.** Hybrid (lexical + vector) on the large set, then the full stack, both with lexprec if the sweep ships it.

Success:
- hybrid Δ on respelled identifiers ≥ +0.10;
- no cohort significantly worse;
- full stack ≥ +0.05.

The small set's full-stack runs kept +0.06 to +0.10. This needs the embedder, and nothing else, so it waits only for the sweep to free Ollama.

**Shipping path, in `Ronan-Wood/substrate` after the extraction:**

| piece | plan |
|---|---|
| Where the crate lives | `native/fts5-tokenizer/` in the engine repo, a Cargo `cdylib`. `unsafe` stays confined to `src/ffi.rs`, as now |
| Targets | `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`. Build Linux on native arm64 runners, not QEMU. Set the glibc floor to the image's base, and to manylinux_2_28 if a wheel carries it |
| Release assets | `engine-release` uploads one prebuilt extension per target, listed in `SHA256SUMS` and covered by the build-provenance attestation the plan already gives the tarball (`ENGINE-SPLIT-PLAN.md` §8.1) |
| The wheel | No longer `py3-none-any` (§8.1). It becomes three platform wheels with the extension as package data, plus an sdist that needs `cargo`. **Decide before L7:** a pure wheel cannot carry it, and no fallback is safe (next row) |
| No fallback | The tokenizer is part of the FTS schema. An index built with `tokenize='substrate'` cannot be read by an engine without the extension, so every reader must have it, and an engine without it must refuse by name. This is a schema bump with drop-and-rebuild (README, "Engine changes the PR would need") |
| Loading | Enable through `SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION`, never `enable_load_extension(True)`, which also turns on SQL's `load_extension()` (README, "Shipping cost") |
| The GHCR image | A builder stage runs `cargo build --release`, the runtime stage copies the `.so`, and buildx builds amd64 and arm64 in CI from the lock |
| Scripta (macOS) | Library validation refuses an ad-hoc-signed dylib, so the bundle must sign it with the app's Team ID. `fetch-engine` takes the release's prebuilt `aarch64-apple-darwin` dylib, verifies its sha256 against `SHA256SUMS`, and `build-bundled-engine` signs it with every other Mach-O. This keeps a Rust toolchain out of the app build. Order: build and verify BEFORE the interpreter pin checks, re-verify the hash right before the signing loop (README, "Shipping cost") |
| CI | Per target: `cargo test`, `clippy`, and the Python tokenizer tests against that target's real SQLite. Add a parity job: `unicode61` against `substrate` over prose with no identifiers must give identical tokens |

**Risks.**
- **An interpreter without extension loading.** The Linux interpreter's `sqlite3` may be compiled without `load_extension`. This is unverified for the image's Python and for uv's managed builds; check it in L1's job before anything else.
- **The positional gap.** It stays a documented miss.
- **macOS signing.** The extension is one more Mach-O in the bundle, so the CI notarization path must cover it.

---

## 5. A Rust `substrate-mcp --connect` bridge

**Hypothesis.** A static Rust binary can replace the Python stdio bridge byte-for-byte on the wire, and cut each bridge from about 23 MB resident and 0.23 s of import (the brief's measurement, 11 bridges live) to a few MB and a few ms. It would also let a host with no Python reach a Dockerized engine.

**What the Python bridge does, and so what the binary must do exactly.**

| layer | behaviour | where |
|---|---|---|
| framing | stdin is newline-delimited JSON. A blank line is skipped. A line that will not parse gets `-32700`, id null, and the loop continues | `server.py:1285-1300` |
| frame shapes | A batch array is handled per member. An empty batch is `-32600`. A batch of only notifications gets no reply at all. A non-object is `-32600`. A frame with no `id` key is a notification and never gets a reply, for any method | `server.py:1177-1206` |
| answered locally | `initialize` (`protocolVersion` `2024-11-05`, `capabilities.tools`, `serverInfo`, the full `instructions` text); `notifications/initialized` and `initialized` (no reply); `tools/list`, minus the write tools under `--read-only` | `server.py:74-92`, `:1208-1221`, `_available_tools` |
| `tools/call` checks | Positional params get `-32602`. An unknown tool gets `-32602 unknown tool {name!r}`: **a Python `repr`**, so a non-string name prints as `None`, `123` or `['x']`. The read-only refusal is an `isError` result with a fixed text. An unknown method gets `-32601 unknown method {method!r}` | `server.py:1222-1254`, `:1282` |
| forwarding | Every read tool is a POST of `{"jsonrpc","id","method":"tools/call","params":{name,arguments}}`. The body is **ASCII-escaped** (a lone surrogate must survive). It carries `Content-Type: application/json` and `Substrate-Wire: 1`. One connection per call, with a 300 s timeout | `bridge.py:88-98`, `:135-139`; `local_socket.py:249-270`; `wire.py:29-31` |
| trust, before a byte is written | the socket path length; the directory owned by the euid with mode `& 0o077 == 0`; a symlink followed ONE level, with the target's directory checked too; the peer uid equal to the euid (`LOCAL_PEERCRED` on darwin, `SO_PEERCRED` on Linux) | `local_socket.py:66-135` |
| the reply | The reply's `Substrate-Wire` is checked before the body is decoded. A body that is not JSON-RPC becomes an `isError` naming the HTTP status. Error code `-32010` becomes the other-release text. **The `id` is rewritten to the caller's**: a 503 at the connection ceiling carries id null | `bridge.py:174-191`; `wire.py:67-91` |
| failure texts | Five distinct `isError` texts: untrusted, nothing listening, timeout, dropped, other release. Each carries the `start_hint` logic: default path, link target `shlex`-quoted, or re-register | `bridge.py:67-86`, `:141-172` |
| **writes** | `ingest` and `amend` are **not forwarded** (Doc 3 §3). They run in the bridge's own process through the same `handle()` and a per-process `PlanBook`. First the bridge asks the engine for `list_scopes`, with a 10 s timeout, and refuses on a different registry. Unknown counts as agreement. An `amend` token is burned on ANY refusal | `bridge.py:14-19`, `:100-133`; `server.py:1043-1050`, `:1255-1281` |

> **SUPERSEDED 2026-11-12: the bridge is a pure forwarder now.** Since engine #8,
> `substrate/mcp/bridge.py` forwards `ingest` and `amend` like every other `tools/call` ("the writes
> are forwarded too"), and the engine decides them against the user's grants. The bridge writes
> nothing and holds no PlanBook. So the **writes** row above, the design question below and its
> option (b) no longer apply: a Rust bridge has no write path to reimplement and no Python child to
> spawn. For a write it adds only what the Python bridge adds, the client-name, grant-token and
> client-info headers, with the token read from `~/.substrate/clients/<client>.token` on every
> write. The risk "(b)'s child" and "(b)'s write path" under "What shipping it would change" go
> with it.
>
> **Also 2026-11-12:** this bridge is now phase 3 of the one-track plan in §9's note. Build it only
> for clients that can't speak HTTP, and for Docker or remote engines. Its remote transport is §9's
> HTTPS with the client's token.

**The design question is the writes.** Running `ingest`/`amend` in Rust would mean reimplementing `notes.py` (1,160 lines), the frontmatter gates and the PlanBook. That is a second implementation of the one path that writes the operator's vaults. The options:

| option | effect | verdict |
|---|---|---|
| (a) Rust bridge is read-only | `ingest` disappears from every stdio client | reject: it removes a working capability |
| **(b) Rust forwards reads, and lazily spawns the Python bridge on the first write** | Python starts only in a session that writes. The child holds the PlanBook for the rest of the session, so tokens stay per-process. With no Python on the host, the write gets a named refusal | **recommend** |
| (c) writes over a second engine socket | puts the write primitive back on a socket any local process can reach | reject: Doc 3 §3 |

Option (b) fits the Docker case, because there the vaults are mounted `:ro` and `serve` is read-only anyway (`ENGINE-SPLIT-PLAN.md` §8.2). A host with no Python therefore loses nothing it could have had.

**Second transport.** On Docker Desktop for macOS, a host client reaches the container over TCP with a bearer token: `--connect http://127.0.0.1:8765 --token-file …`, which is L5 in the split plan (§7 item 14, §8.2). The binary should implement it from the start, refusing a token file that is not mode 0600 and comparing in constant time. **This is the main reason to build the bridge at all.** A static binary is the one way a machine with Docker and no Python reaches the engine.

**Method.**
1. **Build.** A single crate, `substrate-connect`: a stdio loop, an HTTP/1.1 client over a Unix socket and over TCP, the trust checks, and (b)'s child spawn. `initialize`'s instructions and the tool list are generated at build time from `contract/contract.json` (P2). They are never hand-copied, because a hand copy drifts on the next tool change.
2. **Differential harness**, after `exp/lean-blob-gc`'s pattern:
   - **Inputs.** Hypothesis generates frame sequences: valid and broken JSON, batches (empty, mixed, all notifications), ids of every JSON type, positional params, unknown tools and methods, unicode including lone surrogates, and tool names of every type for the `repr` text.
   - **A fake engine** on a Unix socket in a 0700 temp dir, scripted per case. It answers normally, or with a 503 and id null, a non-JSON body, a wire mismatch (header or `-32010`), a reply after the timeout (with the timeout shortened in both bridges), or a connection closed mid-reply. It also sets the directory to 0755 and puts a symlink in place.
   - **Both bridges run the same sequence.** Compare (i) everything each writes to stdout, as parsed JSON with the `content[].text` strings compared exactly, and (ii) everything the fake engine *received*: method, path, headers, body bytes. Forwarding "exactly" means (ii) as much as (i).
   - **The real engine.** A second pass runs against a real `substrate serve` on a fixture registry built from the demo vaults, never the operator's.
   - **Mutants.** Four planted bugs must each be caught: id not rewritten; body not ASCII-escaped; the directory-mode check off; a write forwarded instead of handled locally.
3. **Measure**, on the quiet machine:
   - resident memory per bridge, idle and after 100 calls;
   - cold start to the `initialize` reply;
   - added latency per forwarded call against the Python bridge (p50, p90).

**Success criterion.**
- 0 disagreements over ≥ 10,000 generated sessions and every scripted fault, and all 4 mutants caught.
- ≤ 5 MB resident per idle bridge.
- ≤ 20 ms to the `initialize` reply.
- ≤ 2 ms added p50 per forwarded call against Python.

**Cost.**
- About 3–5 days for the crate and harness.
- Per-platform builds, the same three targets as §4. On macOS the binary is Developer-ID-signed, and notarized if Scripta ships it.

**Risks.**
- **A second implementation of the trust checks.** These are security code; the differential harness is the control.
- **(b)'s child.** It has to survive the client's idle gaps and die with the bridge. Stdin EOF must end both, which is the leak class the single-server audit found.
- **Client configs.** Every client has `substrate-mcp --connect` registered. A new binary needs re-registration, or the Python entry point has to exec it, and that exec starts Python anyway.

**What shipping it would change.**
- A second binary in the engine release, next to the Python package.
- Install docs that register `substrate-connect`.
- The Python bridge stays as the differential oracle and as (b)'s write path, so it is not deleted.
- It needs P2's `contract.json`, so it lands after the extraction.

---

## 6. Profile before rewriting anything else

**Measurement plan only.** The evidence so far says no Rust:

| stage | measured (2026-11-06, conditions in the profile) | where the time goes |
|---|---|---|
| full search | Python < 200 ms; > 95% of a full search is HyDE and rerank | `runtime-profile-2026-11-06.md` |
| vector arm | 25 ms → 0.6 ms with the matrix cache, which has since shipped | same; `index_store.py:1141-1230` |
| matmul + argsort | 0.4–0.5 ms | numpy, already C |
| compose, no-op on the largest scope | 2.20 s, of which `ingest_markdown` is 1.72 s | it **re-reads and re-writes every note's jsonl even when nothing changed**. That is an algorithmic fix in Python, not a language problem |
| embed | 98% Ollama HTTP | Ollama |
| brute-force vectors | fine until about 250k–500k passages per scope | `vector-scale-handover.md` |

**Method.**
1. **Profilers:**
   - `py-spy record --native` (sampling, with SQLite and numpy frames shown) on compose: full, no-op and one-note-edited, on a copy of the largest scope;
   - `cProfile` for call counts;
   - `tracemalloc` for peak memory;
   - `perf_counter` per stage, as the existing profile did.
2. **Corpora:**
   - the vector-scale experiment's synthetic corpora at 10× and 100× the largest scope, so chunking and the FTS rebuild are seen at the size where they would matter;
   - copies of real indexes in a scratch directory only, never the live files;
   - profiles kept out of every repo, because stack samples carry note paths.
3. **Fix first.** Do the no-op compose fix (skip unchanged notes) before judging anything, then re-profile.
4. **The decision rule**, fixed before looking: a stage is a rewrite candidate only if, on a quiet machine after the algorithmic fixes, **it is ≥ 20% of the operation's wall time AND ≥ 50% of its time is pure-Python self time** (not SQLite, numpy, I/O or Ollama).

**Success criterion.** The rule above, applied per stage. The expected outcome is that no stage qualifies, and the no-op compose fix is the deliverable.

**Cost.** Half a day of profiling plus the fix.

**Risk.** It needs a quiet machine. Sampling under load mostly measures scheduler stalls.

**What shipping it would change.** Probably one Python fix in `ingest_markdown`'s change detection, in the engine repo, with a regression test that a no-op compose rewrites no jsonl.

---

## 7. Lean: the guard holds on every search path

> **SUPERSEDED 2026-11-12: moot.** Engine #6 (`fix/guard-withholds-vault-directory`, 2026-11-10)
> fixed the latent bug below: the server withheld the vault directory name as well as the scope, as
> agent mode did. Then #13 (2026-11-11, released in v0.2.1) removed the vault guard: `guard_state`
> is no longer a manifest key, nothing is withheld for it, and the vault-exclusion filter this
> section models is gone from the code. There is nothing left to prove. The section is kept as
> written.

**Hypothesis.** No read path returns a passage whose document belongs to a vault the guard withholds. The paths: lexical, vector (cached and scan), HyDE, multi-query, route, outlines, `expand`, and the `documents` tool. Two bugs in this class have shipped already:
- `b7b630bf`: a multi-query paraphrase was not held to the vault and entity filters;
- `e64f983b`: `expand` did not refuse a withheld vault's passage.

**Evidence so far, and the latent bug.**

| fact | where |
|---|---|
| The guard is checked once per tool call, at the chokepoint every scope-taking tool passes through | `server.py:148`, `:184` |
| `check` returns `Verdict(frozenset({scope}))`: **it withholds the SCOPE name**, and its docstring asserts "the vault's own name is the scope name" | `guard.py:102-108`, `:171-172` |
| `documents.vault` records the vault **directory** name, which the code says is deliberately not the manifest name | `vault.py:105-106`, `:304`, `:348` |
| Agent mode already withholds BOTH names, "because the guard keys on the scope and the index on the vault directory" | `agent.py:227-232` |
| The exclusion is `d.vault IS NULL OR d.vault NOT IN (…)`. An unattributed row passes, by design | `index_store.py:212-228` |
| Lexical, outline and vector (both paths) apply it | `index_store.py:610`, `:757`, `:1184`; the matrix path filters with the same `where`, `:1213-1222` |
| Multi-query variants pass `withheld_vaults` (the #87 fix) | `retriever.py:428-445` |
| Route's per-section search passes **no vault filter**. It is safe only because it is scoped to the `doc_id` of an outline that passed the filter | `retriever.py:500-520` |
| `expand`'s neighbours are scoped to one `doc_id` | `index_store.py:626-638`; `retriever.py:567` |
| The `expand` tool checks `hit.vault in verdict.withhold`, and so carries the same name mismatch | `server.py:637` |

So wherever a guarded vault's directory name differs from its scope name, the server withholds a name no document carries, and every filter above excludes nothing. No vault sets `guard_state` today, so nothing is exposed now. The first workspace vault that sets it, under a directory name other than its scope name, would be exposed. **This comes from reading the code and has not been reproduced.** The differential harness below reproduces it first.

**Method.**
1. **Model**, in Lean 4 under `experiments/lean-guard/`, the same layout as `exp/lean-blob-gc`:
   - **The world:** vaults (directory name, manifest name, optional `guard_state`), the guard state file (missing, unreadable, too large, not JSON, not an object, heartbeat non-numeric or bool, stale, from the future, `activeScope` wrong or missing), documents (`doc_id`, `vault : Option Name`, status, class, entity), and chunks (`doc_id`, seq, kind).
   - **`check` as a pure function** of the world and the clock.
   - **Each arm as a function** from (store, filters) to a list of chunks. Ranking is uninterpreted: an arm may return ANY sublist of the rows satisfying its SQL predicate, in any order. The proofs therefore cover every ranking, including future ones.
   - **Fusion and rerank** as functions whose output is a sub-multiset of their inputs' union (`retriever.py:314-333`; `rerank_cross.py:390-391`).
   - **The tools** `search`, `expand` (both modes) and `documents`.
2. **Theorems:**

   | name | statement |
   |---|---|
   | **T1, filter soundness** | For every world and query, every passage any tool returns has `vault = none` or `vault ∉ verdict.withhold`. Expected to prove. It rests on lemma L1 (every arm applies the exclusion, or restricts to a `doc_id` drawn from a hit that passed it) and L2 (all chunks of a document share its vault) |
   | **T2, guard intent** | For a guarded scope whose check fails, no passage of a document composed from that scope's own project vault is returned. **Expected to FAIL** on the current code whenever the directory name ≠ the manifest name. The counterexample is the latent bug. The fix shape is agent mode's: withhold both names |
   | **T3, fail closed** | Every malformed or failed state yields a non-empty `withhold`. Only a fresh, well-formed, matching state yields `OPEN` |
   | **T4, non-vacuity** | A concrete world exists where a guarded scope answers with its inherited vaults' passages, so T1 does not hold merely because everything is withheld |

   The NULL-vault exemption is stated inside T1, not proved away. It is a designed hole, and the theorem should show it.
3. **Differential harness:**
   - **Worlds.** Hypothesis generates small worlds (≤ 4 vaults, directory and manifest names that sometimes differ, every guard-state shape, documents with NULL vaults). Each is built into a real index with `IndexStore.upsert` in a temp home.
   - **The real calls.** Run the real `_tool_search` with deterministic fake arms: an embedder hashing text to a vector, an expander that appends text, a multi-query that returns two variants, a reranker that reverses. The suite already has fakes to start from (`tests/test_mcp_server.py:63`). Also call `_tool_expand` on every chunk ref, in both modes, and `_tool_documents`.
   - **The model.** The compiled Lean model reads the same world as JSON and returns the allowed set.
   - **Assertion.** Real ⊆ allowed (T1), and the T2 check reported separately.
   - **Determinism.** Runs are derandomized, and every check is an explicit `raise`.
4. **Mutants**, each of which must be caught:
   - drop the vector arm's exclusion;
   - revert `b7b630bf`'s variant filters;
   - revert `e64f983b`'s `expand` check;
   - make a missing state file fail open.

**Success criterion.**
- T1, T3 and T4 proved with no `sorry`, and `Axioms.lean` showing only `propext`, `Quot.sound` and `Classical.choice`.
- ≥ 10,000 differential worlds with 0 escapes from T1's allowed set.
- All 4 mutants caught within 3,000 worlds each.
- T2's counterexample reproduced against the real code, then gone under the fix.

**Cost.** 3–5 days, about what `lean-blob-gc` took. CPU only, so it can run during the sweep.

**Risks.**
- **Model–code gap in the SQL.** The model reads the filter predicate; the code builds strings. The differential harness is what catches a predicate the model states but the SQL does not apply.
- **The theorem only covers modelled arms.** A new arm added later is outside it until modelled, which is how #87 happened. Mitigation: make the harness's arm list fail when `retriever.py` grows an arm it does not know. This needs a cheap static check.

**What shipping it would change.**
1. **Now, as a normal fix PR** (`fix/guard-withholds-vault-directory`): `server._open` withholds both the scope name and the project vault's directory name, as `agent._withheld` does. Its regression test is a guarded vault whose directory name ≠ its scope name. It fails on today's code.
2. **The experiment** lands as `experiments/lean-guard/` in the engine repo, run by hand, not in CI (it needs the Lean toolchain).

---

## 8. TLA+: `serve` concurrency

**Hypothesis.** `substrate serve` runs several concurrent pieces:
- reader threads, serialized by one dispatch lock;
- a refresh loop spawning compose and embed subprocesses under the cross-process `vaults.lock`;
- external composers taking the same lock (the app, a terminal);
- a two-stage shutdown.

Together they never deadlock, never lose a refresh, never let a reader see a half-written document, and never leave a child running after exit.

**Evidence so far (the actors and their locks).**

| actor | what it holds | where |
|---|---|---|
| reader thread | the dispatch lock for one whole tool call. It opens the index per call with `migrate=False`; the vector-cached path runs in one read transaction | `server.py:1605-1625`, `:1828`; `index_store.py:1199-1222` |
| refresh loop | serial by construction: a pass, then `stop.wait(every)` (default 900 s) | `refresh.py:313-319`, `:54` |
| a pass, per scope | compose with `--no-wait`: on the lock busy, exit 75, recorded as "a compose was in flight" and *nothing recorded*. Then embed with `--wait 1800`. Each is a subprocess, spawned under `_children_lock` and refused once `_closing` is set | `refresh.py:144-161`, `:256-279`, `:47`, `:58` |
| `vaults.lock` | an `flock` taken at the CLI entry of compose and embed, waiting up to 60 s, then exit 75 | `cli.py:2659-2678`; `scopes.py:62-93`; `refresh.py:83-97` |
| index writes | one transaction **per document** (`upsert`, `remove`) and per vector batch | `index_store.py:420-432`, `:519-526`, `:1124-1126` |
| shutdown | signal → ignore further stops → `loop.stop` and `begin_closing` → `terminate_children` (TERM, 10 s, KILL) → `loop.join(15)` → `end_closing`. Separately, `serve_unix`'s `finally` closes the listener and takes the dispatch lock with a 30 s timeout as the "no handler inside `handle()`" barrier | `server.py:2080-2124`, `:1855-1871`; `refresh.py:164-190` |
| the refresh PR's own review | 3 adversary rounds found 4 shutdown bugs (a false freeze, children spawned after stop, a join on a loop never started, a crash read as an interruption), all fixed | `engine-serve-refresh-pr-body.md` |

**Method.**
1. **PlusCal model.** Two scopes, two reader threads, one loop, one external composer, one signal source, two documents per scope, a small `every`. Encode the real lock kinds: Python `Lock` (not reentrant), `flock` (released by the kernel when the holding process exits), and subprocess exit and kill.
2. **Properties:**

   | property | kind | statement |
   |---|---|---|
   | NoDeadlock | safety | TLC's deadlock check, plus no cycle in the waits-for graph over the dispatch lock, `_children_lock`, `vaults.lock` and `loop.join` |
   | NoTornDocument | safety | A reader's snapshot holds each document whole, old or new, never a mix of its chunks |
   | MatrixMatchesSnapshot | safety | The vector matrix served is the one for the snapshot's `vector_generation`, the #89 invariant |
   | EventualRefresh | liveness, under weak fairness | Every vault change made while `serve` runs is followed by a recorded `refreshed` of that scope, from a compose that *started after the change*, unless the scope freezes or shutdown begins |
   | BoundedDelay | safety | That compose starts within 2 × `every`. A declined compose delays the change; it must not lose it |
   | CleanShutdown | safety | After `serve` returns: no child runs, no child started after `begin_closing`, nothing recorded for a child the shutdown ended, and no `handle()` in progress unless the 30 s barrier timed out (the documented truncation hazard) |

3. **Properties expected to FAIL,** stated so the answer is a decision rather than a surprise:
   - **Whole-pass atomicity.** A reader between two documents' commits sees a scope that is half old and half new.
   - **One answer, one snapshot.** A single `search` reads coverage, FTS and vectors in separate statements. A compose committing between them can fuse hits from two snapshots.
   - **New chunks with no vectors yet.** This is caught and labelled, not prevented (`retriever.py:376-396`, the coverage degrade).

   TLC will produce the traces. Whether to buy whole-pass atomicity (build a new index and swap it in) is the user's call.
4. **Trace validation against the real code.** An env-gated event log at each lock acquire and release, spawn, exit and commit is mapped onto the spec's actions. Replay it from `tests/test_refresh_pass.py`'s scenarios and from a stress run that SIGTERMs at random points, kills a compose mid-pass, and races an external compose. Every real trace must be a behaviour of the spec.

**Success criterion.**
- TLC exhausts the state space of the 2-reader × 2-scope config with 0 violations of NoDeadlock, NoTornDocument, MatrixMatchesSnapshot, BoundedDelay and CleanShutdown, and EventualRefresh holds under fairness.
- ≥ 1,000 real traces validated with 0 divergences.
- Each expected failure above is shown by a TLC trace.

**Cost.** 3–4 days. It needs the TLA+ tools (Java) and bounded TLC workers, so it can run beside the sweep.

**Risks.**
- **Model–code gap on OS semantics:** `flock` inheritance across `fork`/`exec`, Python delivering signals only to the main thread, `communicate()` under a TERM. Trace validation is the control.
- **Size.** The state space grows fast with readers. Two readers already cover every lock interleaving that matters, because one dispatch lock serializes them.

**What shipping it would change.**
- `experiments/tla-serve/` in the engine repo.
- Every violation becomes a normal fix PR with a regression test.
- The two atomicity results feed a decision record, not code.

### 8a. Smaller: properties of the sweep's fusion rule, once it ships

The rule from the sweep: when the lexical top passes `_already_precise`, keep lexical order first. Properties to prove:

| property | statement |
|---|---|
| P1 | If the lexical top is precise, it is `fused[0]` |
| **P2, end to end** | With the gate on, it is also the final top-1. The gate reads `fused[0]` (`rerank_cross.py:272`), and must use **the same predicate with the same constants** |
| P3 | The output is a duplicate-free subset of the union of the arms' lists |
| P4 | When the lexical top is not precise, the output equals plain RRF (the rule is a conservative extension) |
| P5 | The rule inherits T1 from §7 for free |

Prove them in Lean over the §7 arm model, and test them in Python with Hypothesis against the shipped fusion function. P2 is the one with a real failure mode: the rule and the gate come to disagree if someone edits the precision constants in one place only. Cost: about 1 day after the rule lands.

---

## 9. Clients connect straight to the engine, with no process per client

> **REPLANNED 2026-11-12: §5, §9 and server mode are now one track** (issue E9). The original text
> below is kept. Two facts changed it:
> - every client the user grants already has a token in `~/.substrate/clients/<client>.token`,
>   which is the bearer this section asked for;
> - the engine decides writes against those grants, so an HTTP client can write under the same
>   grant, plan and confirm. Item 4's "still read-only" no longer applies.
>
> The track, in order:
> 1. `serve` speaks MCP Streamable HTTP on loopback with the client token as the bearer. This
>    replaces today's unauthenticated `--http` listener instead of sitting beside it, which settles
>    the "does `--http` stay" question this section was blocked on.
> 2. A decision: how a client knows it reached the real engine. Locally, option (a), (b) or (c)
>    below. For server mode, with the engine on another machine, it means HTTPS with a verifiable
>    certificate.
> 3. §5's Rust `substrate-connect`, only for stdio-only clients and for Docker or remote engines,
>    built if the client matrix shows a need.

**Hypothesis.** An MCP client that supports MCP's HTTP transport can register a URL and reach the one shared `serve` directly. That removes the per-client bridge process (§5 measured about 23 MB and 0.23 s each, 11 live) for those clients, rather than just shrinking it.

**What exists already.** `substrate-mcp --http` is the same `handle()` behind loopback TCP (`server.py:15-35`). It is read-only by default, so `ingest` and `amend` are neither listed nor run. It refuses a non-loopback `Host`, any `Origin`, and any body that is not `application/json` (`server.py:1490-1518`). It is documented as a developer convenience, not the shared server's address.

**Why it isn't the address today. The blocker is authentication, not writes.**
- **Loopback TCP authenticates nobody.** Any user on the machine can query it (`server.py:34-38`).
- **While the engine is down, any process can bind the port**, and every client then sends its queries to the impostor. A client cannot tell.
- **The Unix socket closes both holes.** A 0700 directory and a peer-uid check on both ends. No MCP client can dial a Unix socket, which is why each one starts a bridge.

**Design to test.**
1. **Speak MCP's Streamable HTTP exactly.** Check `--http` against the spec: the `Accept` handling, `Mcp-Session-Id`, and whether a JSON-only reply (no SSE) is enough for each target client. `server.py` has neither the session header nor SSE today.
2. **The client authenticates with a bearer token.** It reuses L5's `--token-file` (split plan §7 item 14): mode 0600, constant-time compare, and `substrate token new`. Most HTTP MCP clients can send a fixed header. Check each target client.
3. **The server authenticates back, so a squatter learns nothing.** A token alone doesn't stop the squatter: the client sends the token *and* the query to whoever holds the port. Options to compare:
   - (a) a per-install port that a registered launch agent always holds;
   - (b) TLS on loopback with a pinned self-signed certificate, if clients allow pinning;
   - (c) accept the gap and say so: on a single-user Mac, a process running as you can already read the vaults.

   This is the decision the experiment must produce. Expect (c) to be the honest answer for most clients.
4. **Still read-only.** Writes stay on the stdio paths (§5 option (b)). Direct HTTP never carries `ingest` or `amend`.

**Method.**
- **A client matrix:** which clients register an HTTP MCP server with a custom header (Claude Code does, via `claude mcp add --transport http … --header`). Check the others.
- **The same differential harness as §5,** with a third column: an HTTP client against `serve`, compared with the Python bridge for every read tool.
- **The squat test:** the engine is stopped and a script binds the port. Record what each client sends it.
- **Memory and latency,** on the quiet machine, against the Python bridge and the §5 binary.

**Success criterion.**
- 0 disagreements with the bridge on reads.
- Each target client connects with no process of its own.
- A written answer to item 3 that the user signs off.

**Cost.** About 2–3 days after L5, sharing §5's harness.

**What shipping it would change.**
- `serve` gains a token-gated MCP-over-HTTP listener beside the Unix socket.
- The install docs give HTTP clients a URL and a token, not a command.
- The §5 binary stays for stdio-only clients, and the Python bridge stays for writes.

---

## Recommended order, and what can run together

**Now, beside the sweep.** None of these needs a model or a quiet machine; together they use a few cores.

| order | work | why now |
|---|---|---|
| 1 | **The guard fix PR** (§7, "What shipping it would change", item 1) | a security bug one vault setting away from live. It needs no proof to fix |
| 2 | **7, Lean guard** | CPU only; confirms the fix and covers every arm |
| 3 | **8, TLA+ serve** | CPU only (bound TLC to 2–4 workers); answers the atomicity questions before anyone changes `serve`'s locking |
| 4 | **5, Rust bridge: the crate and the fake-engine differential** | no models. The real-engine pass and the memory/latency numbers wait for a quiet machine. It ships only after the extraction (it needs P2's `contract.json`) |

Items 2, 3 and 4 are independent and touch no shared files, so they can run in parallel sessions.

> **SUPERSEDED 2026-11-12:** items 1 and 2 are done or moot. The guard fix landed as #6 and #13
> removed the guard (§7).

**After the sweep reports** (offline replay; no models; loaded is fine):

| order | work | depends on |
|---|---|---|
| 5 | **2, Rerank less** (accuracy) | the sweep's chosen profile and `ce.db` |
| 6 | **1, Query router** (accuracy) | 2's pool and gate, since the router's baseline is the profile 2 leaves |
| 7 | **8a, fusion-rule properties** | the rule shipping |

Items 5 and 6 share the replay harness and its data, so run them in one session, one after the other.

**When Ollama is free** (embedder only):
- 8. **4, the tokenizer's hybrid run**, then the full stack. If it passes, the shipping path in §4 is the first native code in the engine repo, after the extraction and before L7 fixes the wheel's form.

**Quiet machine only**, in one sitting:
- 9. **3's result folded in** (when the Ollama session reports), then the latency passes of **1, 2 and 5**, with the post-tuning constants.
- 10. **6, profile compose**: last, because the expected answer is a Python fix and no rewrite.

**After L5 in the engine repo** (it needs the token file):
- 11. **9, direct HTTP clients.** Run it after 5's crate and harness exist, since it reuses them. Its latency pass joins item 9's sitting.

**Where it ships.** Every engine change and experiment folder above goes to `Ronan-Wood/substrate` once the extraction is done. The guard fix is the exception: it is small enough to land in the monorepo first if the extraction is more than a few days away. Branch names:
- `fix/guard-withholds-vault-directory`: **done**, engine #6 (2026-11-10)
- `exp/lean-guard`: **moot**, the guard was removed in #13 (2026-11-11)
- `exp/tla-serve`
- `exp/rust-connect-bridge`
- `exp/direct-http-clients`
- `exp/rerank-less`
- `exp/query-router`
- `feat/fts5-tokenizer`
- `perf/compose-skip-unchanged`

**2026-11-12:** earlier work this plan cites now lives on engine branches: the FTS5 tokenizer
experiment on `exp/fts5-tokenizer`, the vector-scale experiment on `exp/vector-scale`, and the
runtime-profile scripts on `exp/runtime-profile`.
