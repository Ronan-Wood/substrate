# Engine split: moving `substrate/` into its own repository

> **A record, not a plan in progress.** The split it describes finished 2026-11-10; this is the plan as written on 2026-11-07, kept for the decisions it explains.

**Status: a plan, and nothing has run.** It was written 2026-11-07 against `main` at `dfd3091`, the
rewritten history. No code is changed, no repo is created and nothing is committed. You sign and run
every step.

**What it does.** `substrate/`, the Python 3.14 retrieval engine, moves to its own GitHub
repository, keeping its history. Scripta then builds against a **pinned engine release** instead
of a source tree beside it.

**How.** Eight small PRs in the monorepo draw the line first. Once they have landed, the
extraction is mechanical, and two checks prove it: a tree-hash equality and the denylist scan.

**Scope widened (2026-11-07).** **This plan now gates going public.** Scripta stays private until
the split is finished. At launch:
- the engine is its own repo: headless, runnable on macOS and Linux, and shipped as a wheel and a
  GHCR container image;
- Scripta is app-only, and takes engine versions by bumping a pinned release.

The new sections are:
- §7, portability;
- §8, distribution;
- §9, the update flow and the compatibility rule;
- §10, Scripta's history;
- §11, the public-readiness checklist;
- §12, sequencing.

§2.1, §2.5, X0 and §6 are revised to match.

**Branch base.** Every implementation PR branches from the rewritten `main` on GitHub:
`dfd3091`, or later (`0d02c3d` on 2026-11-07, after the new repo's #1 and #2). Never branch from
this checkout's local `main` ref, which is still the old history (`8f8bab3`). **The local rebuild
(rewrite runbook R15) comes first** (§12).

## 0. Decisions you need to make

| # | decision | recommendation |
|---|---|---|
| D1 | **The engine repo's name** | **Decided 2026-11-07: `Ronan-Wood/substrate`.** It matches the package, the CLI and the MCP server name, and none of the account's repos used it |
| D2 | First engine tag and version scheme | **Decided 2026-11-09: `v0.1.0`**, which is `pyproject.toml:3` today. After that, while pre-1.0, a minor bump for any change to `contract/` (a wire bump included), and a patch bump for anything else |
| D3 | Tags carried into the engine repo | **Decided 2026-11-09: none.** The `v*` tags are app versions. On `archive/substrate-engine`, see §4.1 |
| D4 | `(#NN)` in carried commit messages | **Decided 2026-11-09: link to the real PR where one exists, and never to a wrong one.** Numbers belong to the Scripta repo the commit was made in, so X3's commit callback splits by commit, not by number. After `dfd30914` (the last commit before the rewrite), `(#NN)` and reference-like bare `#NN` become `Ronan-Wood/Scripta#NN`. Up to it, they become non-linking `pre-split Scripta PR NN` (or `issue NN`), since the old repo was deleted and recreated. Prose such as "document #2" is left, and GitHub's autolink of it in the engine repo is accepted as harmless. X4 checks that no `(#NN)` or reference-like bare `#NN` is left, and that no link points above #16. The new Scripta repo has the same problem in its own history, since its numbering restarts at 1 |
| D5 | `exp/lean-blob-gc` | **Decided 2026-11-09: the branch is not carried, and its content is.** After X5, `experiments/lean-blob-gc/` (one commit, 13 files, on rewritten history, scan-clean) is added to the engine repo as a fresh commit, with its two import paths adjusted to the engine's layout (§12 step 4). The branch on GitHub is then deleted in rewrite R15 Part 2 |
| D6 | Collaborators on the engine repo | **Decided 2026-11-09: you only** |
| D7 | When to split | §6 and §12: first. Headless, Linux and Docker work happens in the engine repo after the extraction, and both repos stay private until §11 passes |
| D8 | The wheel's distribution name, and where it is published | **Name decided 2026-11-07: `substrate-engine`.** PyPI's `substrate` is a third-party SDK. The import package stays `substrate`, and the CLI and MCP command names do not change. **Still open:** publish to PyPI, or to GitHub releases only. L7 is the first step that needs the answer |
| D9 | Scripta's history | **Decided 2026-11-09: keep `substrate/`'s history in Scripta.** Delete the folder in one forward commit (X7), with no second filter pass (§10) |
| D10 | The pin-bump bot | **Renovate**, with a regex manager over `engine.lock`. Dependabot has no ecosystem for this file (§9.2) |

---

## 1. Coupling inventory

Each item was read at `dfd3091`. Classes A–C must be resolved before the extraction. D–F change
during it or after it.

### A. The build reads the engine tree

| where | what |
|---|---|
| `project.yml:299` | The "Bundle the substrate engine (Doc 5)" post-build phase |
| `project.yml:318` | `"$SRCROOT/substrate/tools/build-bundled-engine"`, a hard path into the subtree |
| `substrate/tools/build-bundled-engine:30` | `REPO=$(cd "$(dirname "$0")/.." && pwd)`. **Self-contained already**: every input lives under `substrate/`, including `uv.lock`, `pyproject.toml`, the interpreter pin, the four `tools/*.swift` helpers compiled at `:524`, `third_party_licenses/` at `:442`, and the package copied at `:290`. Pointed at any engine checkout, it builds the same thing |
| `build-bundled-engine:131-147` | The fingerprint covers file contents and paths relative to the engine root. An identical tree in another folder therefore gives an identical fingerprint, which the plan uses as a check (P6, X7) |

### B. Swift tests read Python source

| where | reads |
|---|---|
| `Core/Tests/SubstrateKitTests/PythonPayloadSource.swift:35` | The `#filePath` → repo-root loader behind every `PythonSource.load` |
| `RenderContractTests.swift:19,61,86,105,175,186,207,221,231,239,253,262,268,277,299,318,338,395-402` | `render.py`, `classes.py`, `retrieve/retriever.py`, `stack.py`, `refresh_state.py`, `introspect.py`, `freshness.py`, `mcp/server.py`, `spine.py`, all parsed by regex |
| `RenderContractTests.swift:371-374` | **`substrate/EXPERIMENTS.md`**, for the `EngineTier` numbers. This is a prose document, not code |
| `WireParityTests.swift:21` | `wire.py`: `VERSION`, `HEADER`, `MISMATCH`, `UNSTATED` |
| `SocketPathParityTests.swift:12` | `local_socket.py` `DEFAULT_SOCKET` (`:42`) |
| `Core/Tests/ScriptaCoreTests/LegacyEngineSocketTests.swift:100-102` | `local_socket.py`, for the lock suffix. **Not in the brief** |
| `Core/Tests/ScriptaCoreTests/NoteWriterTests.swift:20,54,73,95` | `spine.py`. **Not in the brief** |
| `Core/Tests/ScriptaCoreTests/TranscriptSpineParityTests.swift:25,62,94` | `spine.py`, `classes.py` |

**A correction to the brief.** `MappingParityTests.swift` reads no Python. It decodes the golden
frames (`:28,50`), so it belongs in class C. `SettingsCopyTests.swift:58` reads `Sources/Settings`,
which is app-only.

### C. The golden frames cross the tree in both directions

| where | what |
|---|---|
| `substrate/tests/_swift_fixtures.py:22` | `SWIFT_FIXTURES = parents[2] / "Core/Tests/SubstrateKitTests/Fixtures"`. The engine **writes** into the app's tree |
| `_swift_fixtures.py:154` (`assert_matches`) | It also **reads** them on every ordinary run, so **the engine suite fails without `Core/`**. Today the `engine` CI job only passes because the checkout is whole |
| `substrate/tests/test_swift_golden_frames.py:3`, `test_agent_mode.py:824-896` | The two captures. `:892` passes `SWIFT_FIXTURES.parents[3]` (the repo root) to the leak scan |
| `Core/Tests/SubstrateKitTests/GoldenFixtures.swift:6-13,106-108` | The Swift reader. Its provenance comment names `substrate/tests/…` and `substrate/vaults/demo-vault` |
| `Core/Package.swift:42` | `exclude: ["Fixtures"]` |

### D. The app knows the engine's file layout at runtime

These work across the split, but they assume an internal layout. Once the two ship apart, the
engine will be free to rename those files.

| where | probes |
|---|---|
| `Sources/App/SubstrateEngine.swift:220` | `substrate/refresh.py` exists → the engine "serves and refreshes" |
| `SubstrateEngine.swift:747,751` | `substrate/mcp/server.py`, `substrate/cli.py` in `~/.substrate/engine` (Debug tier) |
| `Sources/Help/HelpView.swift:63` | `substrate/mcp/bridge.py` + `substrate/local_socket.py` → `--connect` reaches the socket |
| `Sources/App/SubstrateCLI.swift:330` | Parses `ingest --help` (argparse output) and the `formats` table for the ingest surface |

### E. The app's CLI and filesystem contract with the engine

None of this moves, and all of it is contract that the split makes versioned.

| app side | engine side |
|---|---|
| `SubstrateLibraryModel.swift:1287-1290` `compose <vault> --index-root --db [--clean]`, `:1307` `embed --db`, `:530` `ingest` | `cli.py` |
| `SubstrateLibraryModel.swift:1257` exit 75 = declined | `refresh.py:47` `EX_DECLINED = 75` |
| `EngineRefresh.swift:122` `refresh`; `SubstrateEngine.swift:198,209` `serve --socket --read-only --hyde-model=` | `cli.py`, `refresh.py` |
| `Core/Sources/SubstrateKit/SubstrateTransport.swift:255` socket default | `local_socket.py:42` |
| `Core/Sources/SubstrateKit/SubstrateWire.swift` | `substrate/substrate/wire.py` |
| `ScriptaVault.swift:407` `.substrate.toml`, `:167` `guard_state`; `MCPStateFile.swift:33` heartbeat | `guard.py`, `scopes.py` |
| `SubstrateLibraryVault.swift:34` writes under `~/.substrate/scripta` | the engine's home (a known follow-up from the boundary audit) |

### F. CI, release and docs

| where | what |
|---|---|
| `.github/workflows/tests.yml:24-46` `engine` | `working-directory: substrate`, `PYTHONPATH=…/substrate`. A **required check** in the main ruleset |
| `tests.yml:48-112` `engine docling` | The same, on macOS, installing the interpreter at the pin's path |
| `tests.yml:114-127` `app core` | `swift test` in `Core/`. A **required check** |
| `.github/workflows/codeql.yml:28` `codeql python` | The engine is the repo's only Python (`git ls-files '*.py'` outside `substrate/`: none), so after the split this job analyses nothing |
| `.github/dependabot.yml:17` | `uv` at `/substrate` |
| Main ruleset (saved settings) | `required_status_checks: [engine, app core]`, linear history, signatures. **Removing the `engine` job without editing the ruleset blocks every merge** |
| `Distribution/build-and-notarize:179-182` | "A release is built from a commit": the clean-tree check covers `substrate/` only because it is inside |
| `Distribution/publish-release:65` | `gh release create`. No source tarball yet (rewrite plan, open item 1) |
| `Distribution/RELEASING.md:150-173` | Re-pinning the interpreter: `substrate/tools/engine-interpreter-pin.py --write … substrate/tools/engine-interpreter.pin` |
| `RELEASING.md:244,258` | `build-bundled-engine`, `guard.py` |
| `substrate/tools/engine-interpreter-pin.py:319,442,461`, `engine-interpreter.pin:2` | The **engine** tells you to read `Distribution/RELEASING.md`, which is an app file |
| `substrate/substrate/net.py:24` | Cites `Sources/Engine/Locality.swift`. That is the app's *LLM* engine, an unrelated folder with a confusable name |
| `substrate/run.sh:2`, `substrate/substrate/eval/runner.py:3` | Cite `./Eval/run.sh`. Prose only |
| `ARCHITECTURE.md:46,252,330`, `README.md:92`, `SECURITY.md:22` | Describe `substrate/` as a subtree |
| `substrate/` | No `LICENSE` file and no `README.md`. `pyproject.toml:6` says `GPL-3.0-or-later` |
| `substrate/substrate.db`, `substrate/vectors.db` | Tracked, empty SQLite schemas (0 rows in every table, since 2026-07-22). History also holds a `substrate.db-shm` |

### G. Local, off-repo coupling (your machine; no PR fixes this)

- `~/.local/bin/substrate` and `~/.local/bin/substrate-refresh` `cd` into
  `<monorepo>/substrate` and run `uv run` there. The `substrate` skill uses that CLI.
- `substrate-deploy:71` defaults the data root to `<checkout>/out-vault`. **The live registry's
  `db`/`index_root` entries point into the monorepo checkout's `substrate/out-vault`.** Removing
  `substrate/` from Scripta leaves those ignored files in place, but orphaned, and the CLI shims
  stop working. The fix is in X8.
- `~/.local/bin/substrate-mcp` bridges to the bundled engine in `/Applications/Scripta.app`. **The
  daily MCP path does not depend on the repo layout**, so the split cannot break it until the next
  app build.

### H. How much churn crosses the line

Measured on the rewritten `dfd3091` with `git show --name-only`. This checkout's local `main` ref
is still the pre-rewrite `8f8bab3`, which gives 475 / 211 and should not be counted.
- **18 of the last 60 commits** touch both `substrate/` and the app;
- **30 of 476** do over all of history (177 commits touch `substrate/`).

On the app side of those 18 commits, the paths are, in order of frequency: `Sources/App` (40 file
touches), `Core/Tests/SubstrateKitTests` (23), `Core/Sources/SubstrateKit` (13), `Sources/Settings`
(8), then `Core/Tests/ScriptaCoreTests`, `Sources/Help`, docs, CI and `Distribution`.

Most of them are the boundary work that has now landed (#82, #83, #85, #86, #90: socket, wire
version, model report, engine-owned refresh, attach), plus #91's fixture move. That is evidence that the line is drawn now,
not that it will keep moving at this rate.

---

## 2. Target design

### 2.1 What Scripta pins

There are three things, one file, and one check.

1. **The engine artifact: one source tarball per engine release,** made by
   `git archive --format=tar.gz --prefix=substrate-<v>/ v<v>` and attached to the engine repo's
   GitHub release with a `SHA256SUMS`. The same release also ships a wheel, an sdist and a
   container image for headless users (§8.1). **The app does not use those.** It pins the tarball,
   for two reasons:
   - `build-bundled-engine` needs `uv.lock`, `tools/*.swift`, the interpreter pin and
     `third_party_licenses/`, and a wheel carries none of them.
   - The GPL corresponding source has to be the whole tree anyway.

   It is not a prebuilt engine either. Signing, the hardened runtime and notarization belong to
   the app's identity and build (`project.yml:299-318`). The fingerprint already makes an
   unchanged rebuild cost about 3 s.
2. **The contract: `contract/` in the engine repo,** committed and generated by the engine:
   - `contract/contract.json` carries:
     - `wire` (`VERSION`, `HEADER`, `MISMATCH`, `UNSTATED`);
     - `socket` (`DEFAULT_SOCKET`, the lock suffix);
     - `cli` (`EX_DECLINED`);
     - the `spine` and `classes` vocabularies, `EXCLUDED_CLASSES` included;
     - `payloads`, the key sets of every builder `RenderContractTests` names today, extracted
       with Python's `ast`. That port is more robust than the Swift regex parser;
     - `tiers` (ceiling, floor, case count).
   - `contract/frames/` holds today's golden frames.
   - The tarball therefore carries the contract, and nothing is published separately.
3. **The wire version** is inside `contract.json`, and SubstrateKit's constant must equal it. The
   runtime check (`Substrate-Wire`, an exact match) is unchanged.

**`engine.lock`** sits at Scripta's root. It uses `key=value` lines, so it reads with `awk` the
way the interpreter pin does:

```
repo=Ronan-Wood/substrate
tag=v0.1.0
commit=<40-hex>
tarball=substrate-0.1.0.tar.gz
sha256=<64-hex>
```

**The check, in two places:**
- `Core/Tests/EngineContract/` is a **committed copy** of the pinned tarball's `contract/`, so
  `swift test` runs offline.
- CI proves the copy is byte-identical to the pinned tarball's `contract/`, and that the tarball's
  sha256 and the tag's commit match `engine.lock`.

### 2.2 How the parity tests survive

| today | after |
|---|---|
| Regex over `render.py` etc. (`PythonPayloadSource.swift`) | Read `contract.json` `payloads`. `PythonPayloadSource.swift` is deleted. Same assertions, same floors |
| `wire.py`, `local_socket.py` text | `contract.json` `wire`, `socket` |
| `spine.py`, `classes.py` text (5 test files) | `contract.json` `spine`, `classes` |
| `EXPERIMENTS.md` prose | `contract.json` `tiers`. The engine's generator reads `EXPERIMENTS.md`, and the engine suite fails if they diverge |
| Frames in `Core/Tests/SubstrateKitTests/Fixtures`, written by the engine | Frames are owned in the engine's `contract/frames/` and vendored to `Core/Tests/EngineContract/frames/` |

**What is gained.** The engine tests its own contract: `tests/test_contract.py` fails when the
committed `contract.json` differs from a fresh export, in the same way the frames are checked
today. The Swift side still asserts that SubstrateKit decodes the frames and matches the
vocabularies.

**What is lost.** A Swift test can no longer see an *unreleased* engine change. That is the point
of a pin: an engine change cannot break the app until the app bumps.

### 2.3 How a cross-cutting change lands: engine first, always

1. **The engine PR.** It holds the change, the regenerated `contract/`, and recaptured frames if
   any changed, and gets the engine CI green. Squash-merge it.
2. **The engine release.** You make a signed tag `vX.Y.Z` and run `tools/publish-engine-release
   vX.Y.Z` (P8), which uploads the tarball and `SHA256SUMS`.
3. **The Scripta PR.** `Distribution/fetch-engine --bump vX.Y.Z` rewrites `engine.lock` and
   re-vendors `Core/Tests/EngineContract/`. The PR adds the Swift changes and gets `engine pin` and
   `app core` green.

A wire bump (`wire.VERSION`) is the same flow. `SubstrateWire.version` moves in step 3, in the same
PR as the pin.

**While developing:**
- `SUBSTRATE_ENGINE_SRC=<engine checkout>` points the Xcode build at a working tree.
- `fetch-engine --vendor-from <engine checkout>` vendors an unreleased contract for local
  `swift test`.

CI refuses both, because `engine pin` fails when the vendored contract is not the pinned tarball's.
A draft app PR can pin a pre-release tag (`vX.Y.Z-rc.N`), and `build-and-notarize` refuses a
pre-release pin.

### 2.4 The bundled-engine build

- **`Distribution/fetch-engine`** downloads `engine.lock`'s tarball, checks its sha256, and
  extracts it to `vendor/substrate-<v>/` (ignored by git). It points `vendor/substrate` at that
  folder and writes a stamp.
- **The tarball's `contract/` is scanned before it is vendored.** `fetch-engine` runs the same host-string and
  path-token scans the engine's suite runs (`_swift_fixtures.host_strings`, `leaked_paths`) over it, because once the
  engine is a separate repo nothing in Scripta's tree would scan what a release carries.
- **The network is never used in the Xcode phase.** The phase calls
  `"${SUBSTRATE_ENGINE_SRC:-$SRCROOT/vendor/substrate}/tools/build-bundled-engine"`. With nothing
  there, it fails by name ("run Distribution/fetch-engine").
- **In the `Distribution` configuration**, the phase refuses `SUBSTRATE_ENGINE_SRC`, and it
  refuses a stamp that does not match `engine.lock`.
- **`build-and-notarize`** runs `fetch-engine` after its clean-tree check, so "a release is built
  from a commit" still covers the engine. The commit names the engine by hash.

### 2.5 CI

**The engine repo:**

| job | runs | required |
|---|---|---|
| `engine` | Today's job, ubuntu, 4 packages, `working-directory` dropped | yes |
| `engine docling` | Today's job, macOS 15 | no (it is not required today either) |
| `codeql python` | Moved from Scripta | no |
| `bundle` | `build-bundled-engine` unsigned into `$RUNNER_TEMP`, on tags and on `workflow_dispatch` only. It catches a broken bundle before an app bump does | no |
| `engine linux locked` | L1: `uv sync --frozen` on ubuntu with CPU torch, the whole suite, `-rsx`, and only the named darwin-only skips allowed | yes, once L1 lands |
| `image` | L6: builds the container image and smoke-tests `serve` against the demo vault (§8.2) | yes, once L6 lands |
| `release` | L8: on a `v*` tag push, builds and attests every artifact (§8.1) | — |

Dependabot covers `uv` at `/`, grouped as today, and `github-actions`.

**Scripta:**

| job | runs | required |
|---|---|---|
| `app core` | `swift test` against `Core/Tests/EngineContract/` | yes |
| `engine pin` | `fetch-engine --check`: sha256, tag → commit (`gh api …/git/ref/tags/<tag>`), vendored contract byte-identical to the tarball's | yes (it **replaces** `engine` in the ruleset) |

`codeql python` and the `uv` Dependabot entry are deleted.

### 2.6 Licensing and the source tarball

- **Both repos are GPL-3.0-or-later.** The engine repo gets a `LICENSE` (the same text as
  `Resources/Licenses/Scripta-GPL-3.0.txt`) and keeps `third_party_licenses/`.
  `build-bundled-engine:442` already bundles those into the app.
- **Each Scripta release** attaches:
  - `Scripta-<V>-source.tar.gz`, from `git archive v<V>`, which includes `engine.lock`;
  - **a copy** of the pinned engine tarball, byte-identical, with its sha256 checked against
    `engine.lock`.

  It is a copy, not a link, so the corresponding source stays with the binary even if the engine
  repo moves or prunes its releases.
- **The release text stays neutral**, for example: "Source: Scripta and the bundled engine,
  attached."
- **Before the split**, P7 attaches the single monorepo tarball, which already contains
  `substrate/`.

### 2.7 Where SubstrateKit lives: it stays in Scripta's `Core`

- **The roles divide cleanly.** SubstrateKit is the *consumer's* decoder. The contract (frames and
  `contract.json`) is the *producer's* and lives in the engine. That puts each half's tests with
  the code they test.
- **Moving it would cost the engine repo.** It would need a macOS Swift toolchain in CI, for a
  client the engine never uses.
- **Its only consumer is the app.** Revisit when a second Swift client exists, for example a
  headless engine's own menu-bar controller. At that point it becomes its own package in the engine
  repo, versioned with the contract.

---

## 3. Ordered PRs in the monorepo (before the extraction)

Each is one small PR, squash-merged, with the tests listed and a regression test that fails on the
old code. They land in this order, because each relies on the one before it.

| # | branch | scope | tests |
|---|---|---|---|
| P1 | `test/engine-owns-golden-frames` | `git mv Core/Tests/SubstrateKitTests/Fixtures substrate/contract/frames`. `_swift_fixtures.py:22` → `parents[1] / "contract" / "frames"`. `test_agent_mode.py:892`'s scan root → the engine root. New `Distribution/engine-contract sync\|check` copies `substrate/contract/` → `Core/Tests/EngineContract/`. `GoldenFixtures.swift:106` reads that folder. It sits outside every target directory, so `Package.swift:42`'s `exclude` is dropped. The `app core` job runs `engine-contract check` | Engine suite. `swift test` (both bundles). Regression: edit one byte of a vendored frame → `engine-contract check` fails |
| P2 | `feat/engine-exports-contract` | `substrate/tools/export-contract` writes `substrate/contract/contract.json` (§2.1) from imports, `ast` and `EXPERIMENTS.md`. New `tests/test_contract.py` covers committed == fresh export, plus floors on builders and keys. The capture flag is `SUBSTRATE_CAPTURE_CONTRACT=1`, mirroring the frames. Vendored by `engine-contract sync`. No Swift change | Engine suite. Regression: export from a temp copy with one renamed payload key or one changed `wire.VERSION` → `test_contract` fails |
| P3 | `test/parity-reads-contract` | `WireParityTests`, `SocketPathParityTests`, `RenderContractTests`, `LegacyEngineSocketTests:100`, `NoteWriterTests:54,73,95` and `TranscriptSpineParityTests:62,94` read `Core/Tests/EngineContract/contract.json`. **Equivalence first:** a temporary test asserts that `PythonSource` output == `contract.json` for every function in `RenderContractTests:395-402`. Record the result in the PR body, then delete it together with `PythonPayloadSource.swift`. **Also:** declare the two keys `mcp/server.py:_tool_list_scopes` adds around `scopes_payload` (`retrieval_arms`, `server`), which P2's contract names as a gap; and move the `_unreachable` and `document_class … or None` spelling checks into engine tests | `swift test`, both bundles, same counts minus the deleted test. Regression: an `EngineContract(data:)` built with `wire.version + 1` fails `WireParity` |
| P4 | `ci/each-half-stands-alone` | `engine` and `engine docling` run from `git archive HEAD:substrate`, extracted into `$RUNNER_TEMP`, with no `Core/` present. `app core` runs `engine-contract check` first, then deletes `substrate/` from its runner checkout, then runs `swift test`. The job names are unchanged (they are required checks) | The jobs themselves. Regression: on a scratch branch, revert P1's `_swift_fixtures.py` line and the `engine` job fails |
| P5 | `refactor/engine-states-what-it-is` | `build-bundled-engine` and `substrate-deploy` write `engine.json` (`version`, `commit`, `wire`, `features: [serve, refresh, connect-unix]`). `SubstrateEngine.swift:220,747-751` and `HelpView.swift:63` read it, and fall back to today's probes when it is absent. Remove the fallback one release later. **Recommended, not blocking** | Swift unit tests for decode and feature gating. An engine test for the manifest writer. Regression: a manifest without `refresh` → spawns `substrate-mcp --socket` |
| P6 | `build/engine-src-override` | `project.yml:318` → `"${SUBSTRATE_ENGINE_SRC:-$SRCROOT/substrate}/tools/build-bundled-engine"`. The `Distribution` configuration refuses an override | Two Debug builds: the default, then the override at `git worktree add` of the same commit. The second phase must print the fingerprint skip, which proves the trees are identical. A `Distribution` build with the override set fails by name |
| P7 | `chore/release-source-tarball` | `publish-release` attaches `Scripta-<V>-source.tar.gz` (`git archive v<V>`). This is the rewrite plan's open item 1 | A dry-run flag that prints the asset list. `tar -tzf` lists `project.yml` and `substrate/pyproject.toml` |
| P8 | `chore/engine-tree-is-a-repo` | `substrate/LICENSE`, `README.md`, `SECURITY.md`. `substrate/.github/`: the `engine` and `engine docling` jobs, `codeql python`, `dependabot.yml` (`uv` at `/`). **These are inert here**, because GitHub reads only the root `.github`, but they become live at extraction. `tools/publish-engine-release`. Fix the engine's pointers to app files: `engine-interpreter-pin.py:319,442,461`, `.pin:2`, `net.py:24`, plus path-relative fixes in `CLAUDE.md`. Move the interpreter re-pin how-to (`RELEASING.md:148-175`) into the engine | `engine-interpreter-pin.py`'s tests (the message text). `actionlint` on `substrate/.github`. **Land it last and extract soon after:** until the extraction, the root and staged engine jobs are two copies |

**Not a PR: the X8 data move.** The engine's indexes are disposable caches, but the live registry
names their paths, so they get re-composed rather than moved.

---

## 4. Extraction runbook (you run every step)

**Shorthand:**

| name | meaning |
|---|---|
| `$NOTES` | the shared notes folder, as in the rewrite plan |
| `$E` | `~/engine-extract.git` |
| `$S` | a fresh clone of the new `Ronan-Wood/Scripta`, checked out at **the same SHA X1 mirrors** |

### 4.1 The tag `archive/substrate-engine`

**What it is.** An annotated tag on `142f948`, which is the same commit as `v0.2.0` and an ancestor
of `main`. Its message says the engine "moved into this repository on this branch", and that the
tag marks where that branch ended.

**What the history shows.** The engine was never a separate git repository with history here.
`main` has **one root** (`c69ae8b`, 2026-07-15). `substrate/` first appears on 2026-07-21
(`8b15922`, "Phase 0"). There are 136 engine commits up to the tag and 41 after it, 177 in all. The tag marks an
integration branch, so there is no older engine history to graft and no earlier repo to reconcile.

**Implications for the extraction:**
- **Under `--subdirectory-filter`, the tag would move.** Its commit is app-only, so filter-repo
  re-points the tag at the nearest engine ancestor, and its message ("this repository", "the same
  commit is v0.2.0") becomes false.
- **Drop it from `$E`** (D3). To keep the marker, re-create it in the engine repo after X6, as
  `archive/monorepo-integration`, on the mapped commit, with a new neutral message.
- **Scripta keeps the original.**

### X0. Preconditions

1. P1–P8 are merged.
2. On `main`, the `engine` job (standalone since P4) and `app core` are green.
3. The rewrite has finished through R13, and the local rebuild R15 is done. **R14 (going public)
   now waits for §11.**
4. **D1–D6, D8's name and D9 are decided.** D1 and D8's name were decided 2026-11-07; D2–D6 and
   D9 on 2026-11-09. D8's publishing target is still open, and nothing before L7 needs it.
5. **A read token for the private engine repo.** While both repos are private, Scripta's
   `engine pin` job cannot download the engine's release assets with its own `GITHUB_TOKEN`. Add a
   fine-grained token as a secret: contents read-only, on the engine repo only. Delete it once the
   engine repo is public.

### X1. Mirror

```bash
git clone --mirror https://github.com/Ronan-Wood/Scripta.git "$E"
```
```bash
git -C "$E" for-each-ref --format='%(refname)' refs/pull refs/tags | xargs -n1 git -C "$E" update-ref -d
```
```bash
git -C "$E" update-ref -d refs/heads/exp/lean-blob-gc
```
```bash
git -C "$E" for-each-ref --format='%(refname) %(objectname:short)'
```
**Verify:** exactly `refs/heads/main` remains, at the SHA `gh api repos/Ronan-Wood/Scripta/commits/main --jq .sha` reports.

### X2. Pre-scan, which covers the commits merged since the rewrite

```bash
python3 -I "$NOTES/rewrite/scan.py" "$E" "$NOTES/rewrite/denylist.txt" --paths
```
```bash
python3 -I "$NOTES/rewrite/scan-binary.py" "$E" "$NOTES/rewrite/denylist.txt" substrate/
```
**Verify:**
- `FAILING PATTERNS (H/M/L): 0`.
- `scan-binary.py` lists only the known three binary blobs (`substrate.db`, `vectors.db`,
  `substrate.db-shm`) with `0` hits. `scan.py` skips binary blobs by design (`b"\0" not in
  data[:8000]`), which is why this second scanner exists.
- The baseline on the rewritten mirror is 3 blobs under `substrate/` and 11 repo-wide, with 0 hits.

### X3. Filter

```bash
cd "$E" && uvx --from git-filter-repo git-filter-repo --force --subdirectory-filter substrate
```
**D4:** add `--commit-callback "$NOTES/d4/commit_callback.py"`, with
`D4_PRE_REWRITE="$NOTES/d4/pre-rewrite-commits.txt"` in the environment. On the local stack, the
exact command is in the notes folder's `split-x1-x4-sheet.md`.
**Verify:** exit 0. `git -C "$E" ls-tree --name-only main` shows `pyproject.toml`, `substrate`,
`tests`, `tools`, `.github` (staged in P8) and `LICENSE`, and does not show `Core` or `project.yml`.

### X4. Prove

1. **The tree is identical**, which is the strongest single check:
   ```bash
   test "$(git -C "$E" rev-parse 'main^{tree}')" = "$(git -C "$S" rev-parse 'main:substrate')" && echo same-tree
   ```
2. **The commit count.** `$E` keeps every commit that touches `substrate/`, and also every commit
   that was already empty in Scripta. The history rewrite left 52 such commits (`--prune-empty
   never`), and 24 of them come through. At the local stack's tip `57c659f4` that is 215 against
   Scripta's 191, rehearsed. `--prune-empty always` in X3 would drop the 24.
   ```bash
   git -C "$E" rev-list --count main
   ```
   ```bash
   git -C "$S" rev-list --count main -- substrate
   ```
3. **The scans** run again on `$E`. `scan.py` gives `FAILING PATTERNS (H/M/L): 0`, and
   `scan-binary.py` gives 3 blobs with 0 hits. Run `scan-binary.py` with **no path prefix** here:
   after the filter, the blobs sit at the root, so `substrate/` would match none of them. The
   denylist's path-keyed allowances need the filtered path too: `tools/engine-interpreter.pin` was
   added on 2026-11-09 beside `substrate/tools/engine-interpreter.pin`.
4. **The emails** are only the noreply address, `noreply@github.com` and Dependabot:
   ```bash
   git -C "$E" log --format='%ae%n%ce' | sort | uniq -c
   ```
5. **The refs** are exactly one head and no tags:
   ```bash
   git -C "$E" for-each-ref
   ```
6. **The tip stands alone:**
   ```bash
   git clone "$E" /tmp/engine-tip && cd /tmp/engine-tip && uv venv -q --python 3.14 && uv export -q --frozen --only-group test --no-emit-project -o /tmp/engine-tip-test.txt && uv pip install -q --require-hashes -r /tmp/engine-tip-test.txt && uv pip install -q pypdfium2 pillow numpy pyyaml && PYTHONPATH=$PWD uv run --no-sync python -m pytest tests -q
   ```
   It should report the same passed and skipped counts as Scripta's `engine` job on the same SHA.
   At `dfd3091` that was 1380 passed and 2 skipped; at the local stack's tip `57c659f4`, 2074
   passed and 2 skipped.
7. **The bundle builds the same.** Run `tools/build-bundled-engine /tmp/eng-bundle` (unsigned). Its
   `.build-fingerprint` should equal one built from `$S/substrate`.
8. **Skim the subjects**, as in R13: `git -C "$E" log --format='%s' | less`.
9. **D4:** `python3 -I "$NOTES/d4/check.py" "$E"` prints `D4 PASS`. No `(#NN)` and no
   reference-like bare `#NN` is left, and no link points above #16.

### X5. Create the engine repo

```bash
gh repo create Ronan-Wood/substrate --private
```
```bash
git -C "$E" push https://github.com/Ronan-Wood/substrate.git refs/heads/main:refs/heads/main
```

**Then the settings.**
- Reuse `r11-apply-settings.sh` on a copy of Scripta's saved settings, editing `required_status_checks`
  to `[engine]`, and run it with `DRY=1` first. It sets:
  - squash-only;
  - the main ruleset: linear history, signatures, required `engine`;
  - immutable `v*` tags;
  - Dependabot alerts and security updates.
- Enable secret scanning, push protection and private vulnerability reporting, as in R14.

**Verify:**
- `git ls-remote` shows one head;
- the first `engine` run on `main` is green;
- running `r12-compare-settings.sh` against the edited copy reads `same:` throughout.

**On a deadlock:** the staged `.github` means `engine` reports from the first push. If it does not,
apply the required check only after a run exists.

### X6. First engine release

1. Make a signed tag: `git tag -s v0.1.0 -m 'substrate 0.1.0'` on the pushed `main` (D2).
2. Push it.
3. Run `tools/publish-engine-release v0.1.0`.

**Verify:**
- the release has `substrate-0.1.0.tar.gz` and `SHA256SUMS`;
- `shasum -a 256` of the downloaded file matches;
- the extracted tree equals `git archive v0.1.0`. In an empty scratch folder, holding the
  downloaded tarball and a clone at `<clone>`:
  ```bash
  mkdir a b && tar -xzf substrate-0.1.0.tar.gz -C a && git -C <clone> archive v0.1.0 | tar -x -C b && diff -r a/substrate-0.1.0 b && echo same-content
  ```

### X7. The Scripta extraction PR: `refactor/engine-pinned-release`

**What the PR changes:**
- **The pin.** Add `engine.lock` and `Distribution/fetch-engine` (§2.4).
  - Add `vendor/` to `.gitignore`.
  - In `project.yml:318`, the default becomes `$SRCROOT/vendor/substrate`.
  - `engine-contract` takes the vendored tarball as its source.
- **Remove the subtree:** `git rm -r substrate`. You run it, in your commit; it is listed here
  because it is a removal.
- **`tests.yml`:**
  - delete `engine` and `engine docling`;
  - add `engine pin`;
  - drop `app core`'s `rm -rf substrate` step.
- **Other CI:** delete the `python` job from `codeql.yml`, and the `uv` entry from
  `dependabot.yml`.
- **Docs:**
  - `ARCHITECTURE.md:46,252,330`, `README.md:92` (the layout table), `SECURITY.md:22` (engine
    reports go to the engine repo);
  - `RELEASING.md:148-175` (now a pointer), `:244`, `:258`.
- **`publish-release`** attaches the engine tarball copy (§2.6).

**Before merging, edit the main ruleset.** Change the required checks from `engine` to `engine pin`.
This PR's own run makes the `engine pin` check exist. Without the edit, the required `engine` never
reports and the merge is blocked.

**Tests:**
- `swift test`, both bundles: 258 + 149 at `dfd3091`, minus what P3 deleted.
- `engine pin` is green.
- A Debug app build from the fetched tarball prints the fingerprint skip against a build made
  before the PR. That shows the bundled engine is byte-identical.
- `./Eval/run.sh` passes.

### X8. Local fallout

1. **Clone the engine repo** to `~/substrate`.
2. **Repoint the shims.** `~/.local/bin/substrate` and `~/.local/bin/substrate-refresh` `cd` into
   the new clone. Check what still names the old path:
   ```bash
   grep -rl '<monorepo-checkout>/substrate' ~/.local/bin ~/.claude 2>/dev/null
   ```
3. **Move the data.**
   1. Re-compose every registered scope into a data root outside any checkout (for example under
      `~/.substrate/`), with `compose --index-root … --db …`.
   2. Check each scope's `status` and spot-check a `search`.
   3. Only after that, remove the old `out-vault/` and `out/`. **Ask first:** these are your
      indexes.
4. **Redeploy.** Run `tools/substrate-deploy` from the new clone. `substrate-deploy:115-117`
   already handles a repo-root engine, because its prefix is then empty.
5. **Move in-flight engine work.** Engine-only patches go through `git format-patch` in the old
   worktree, then `git am -p2` in the new clone, which strips `substrate/`. Patches that touch both
   halves are split by hand. Try `-p2` on one patch first.
6. **Clean up the monorepo checkout.** Its ignored `substrate/.venv`, `out/` and `out-vault/` stay
   behind after X7 merges, because git does not delete ignored files. Remove them after step 3.
   **Ask first.**

---

## 5. Risks

| risk | likelihood | mitigation |
|---|---|---|
| A required check that never reports blocks every Scripta merge | high if forgotten | X7's ruleset edit happens before the merge. X5 stages `.github` so the engine repo's first push reports |
| The engine suite still reaches into `Core/` somewhere P1 missed | medium | P4 runs it from `git archive HEAD:substrate` for weeks before the extraction. X4.6 repeats that on the filtered tip |
| Personal data in commits merged after the rewrite | low | X2 pre-scans, and X4.3 scans again. **`scan.py` does not scan binary blobs**, so `scan-binary.py` covers them |
| Velocity: 30% of recent commits cross the line, and each now costs two PRs and a release | medium | The boundary PRs (#82–#90) are done, and §6 lands the two known cross-cutting items first. A local override and `-rc` tags keep the inner loop to one machine |
| A GPL source offer that rots, if the engine release is deleted or the repo renamed | low | A byte-identical copy rides on every Scripta release (§2.6), and `v*` tags are immutable in both repos |
| GitHub release assets can be replaced | low | `engine.lock` pins the sha256 and the commit, and `fetch-engine` and `engine pin` refuse a mismatch |
| `git archive` gzip bytes differ across git versions, so a regenerated tarball never matches | certain | Never regenerate. Pin the uploaded asset's sha256, and verify the content against the tag (X6), not the bytes |
| Skew: a separately-run headless engine of another wire version | expected later | `Substrate-Wire` is an exact match, and the attach path refuses with the reason (`EngineAttach`). Spawning the bundled engine is unaffected |
| Engine docs (`SESSION-HANDOFF.md`, `EXPERIMENTS.md:614`, etc.) cite monorepo paths | certain | They are records. P8 fixes the live pointers only; the history docs stay as written |
| Local registry and shims are stranded (§1 G) | certain | X8 steps 2–3, before X7 merges on your machine |
| `pyproject.toml:5` says `>=3.11` while the engine is 3.14-only; an open worktree `chore/python-314-minimum` changes it | low | Land it before X1, so the engine repo's first tree states the truth |
| The interpreter pin's prefix names your home folder | accepted | It is a kept default (rewrite decision 3), and it carries into the engine repo unchanged. **It is now a public repo's CI path, so consider re-pinning at a neutral prefix before §11** |
| A pip-installed wheel resolves other dependencies than `uv.lock` (RapidOCR, prebuilt OpenCV with GPL FFmpeg) | certain if docling is a wheel dependency | The core wheel leaves docling out. The docling formats are supported only through the image or `uv sync --frozen` (§8.1) |
| The Linux lock has never been installed (CUDA torch, an OpenCV source build) | high | L1 before any image work, as a required job |
| The socket is unreachable from the host on Docker Desktop for macOS | expected; to measure | L5's TCP plus token, published on `127.0.0.1` only. Scripta never depends on it (§8.2) |
| Both repos private, so Scripta CI cannot fetch engine assets | certain until public | X0's fine-grained read token, deleted when the engine goes public |
| Building a branch from this checkout's old-history `main` ref | medium | §12 step 1: R15 first, then the `merge-base --is-ancestor dfd3091` check |

---


## 6. Recommendation: split first, then go headless, then go public

**Widened scope, so the original question is settled.** A headless, Linux-capable engine is
required at launch, and Scripta stays private until the split is finished. That leaves only the
order within the launch, which §12 gives in full. In short:

**1. Do P1–P8 now.** They pay off whether or not the extraction ever happens:
- each half becomes testable alone;
- the contract becomes explicit and machine-checked;
- GPL source ships with every release.

**2. Before X1, land the two cross-cutting headless items** the boundary audit marked as needed
before a headless release:
- the engine config file (`serve.toml`);
- moving `~/.substrate/scripta/index` out of the engine's home.

Both touch the app and the engine together, so they are cheapest while they share one tree.

**3. Then extract, and do the headless work (L1–L8, §7–§8) in the engine repo.** None of it
belongs in Scripta:
- the Linux CI;
- the container image;
- the wheel;
- the release automation;
- the engine's own issues and security reports.

**4. Why now.** Nothing is public, so the order costs no PRs, issues or stars. The engine's history
is a subset of a history that was just proven clean.

---

## 7. Portability inventory: what is macOS-only in `substrate/`

**What the Linux CI already proves.** The `engine` job runs on ubuntu-latest with Python 3.14 and
four packages (pytest, pypdfium2, pillow, numpy). On the new repo's `main` (`0d02c3d`, run
`42431852549`) it gave **1365 passed, 17 skipped**. The macOS `engine docling` job on the same SHA
gave 1382 passed and 0 skipped.

It proves:
- the native and converted-text ingest paths;
- the pdfium text layer;
- chunking, the index and lexical retrieval;
- the MCP server and the Unix socket, through the `SO_PEERCRED` branch;
- the blob store, through the `os.getxattr` fallback;
- refresh and the lock.

It does **not** prove:
- any docling format (docling is not installed there);
- that `uv.lock` installs on Linux at all. The lock resolves torch to its CUDA build there
  (`tests.yml:51-52`), and nothing has ever run `uv sync` on Linux;
- the OpenCV source build on Linux;
- OCR;
- any model arm (there is no Ollama);
- a container.

**The 17 skips are not listed,** because the job runs `-q` without `-rs`. L1 adds `-rsx`. The
known darwin-only skips are `test_blobs.py:168,184`, `test_slim_bundled_engine.py:94` and
`test_amend.py:1286`.

**What found nothing.** There is no keychain or `security` call, and no launchd code: launchd
appears only in comments, at `markdown/ingest.py:55,272` and `spine.py:170`. There is no TCC
handling in the engine.

| # | item | where | on Linux today | Linux equivalent or fallback | PR |
|---|---|---|---|---|---|
| 1 | Image OCR | `extract/convert.py:488` names `OcrMacOptions()`. `pyproject.toml:14` has `ocrmac; sys_platform == 'darwin'` | The docling image path fails. Images cannot be read | Tesseract, through docling's `TesseractCliOcrOptions`, and `tesseract` (Apache-2.0) in the image. Until then, a **named refusal** ("image OCR needs Apple Vision or tesseract") instead of a docling traceback | L2 |
| 2 | The model-free document reader (scans, images) | `extract/apple_arm.py:25` `bin/extract-apple`, a Swift binary using Vision. `pdftext_arm.py:7` sends scanned pages there | `AppleReaderUnavailable`. Text-layer PDFs still work, but scanned pages do not | The same Tesseract arm for pages with no text layer, selected when `bin/extract-apple` is absent | L2 |
| 3 | The Apple embedder, HyDE and reranker arms | `embed/engine.py:285-296` (`embed-apple`), `retrieve/expand.py:322-365` (`hyde-fm`), `retrieve/rerank.py:194-247` (`rerank-fm`); built at `build-bundled-engine:515-525` with `swiftc` | Opt-in only (`apple`, `apple-fm`). `available()` is false, so `stack.build` reports the arm `unavailable` (`stack.py:208-288`) and never crashes. **No change needed** | None. Foundation Models is Apple-only. Document the arm as macOS-only | doc |
| 4 | An MLX model tag | `retrieve/expand.py:84` and `retrieve/rerank.py:38`, `DEFAULT_MODEL = "qwen3.5:4b-mlx"`. MLX tags exist only on Apple silicon | The defaults that matter are portable: `stack.py:81-83` (embedder `qwen3-embedding:0.6b`, HyDE `auto`, cross-encoder reranker). `pick_auto` (`expand.py:94`) picks from what Ollama lists | Make the class defaults non-MLX, or `auto`, so a Linux operator who names the arm without a model gets one that exists. Test it on a listing without `-mlx` models | L3 |
| 5 | Metal and MPS | `extract/docling_arm.py:20-31` forces `AcceleratorDevice.CPU` (MPS failed with float64, measured 2026-09-16) | Already portable: the CPU is forced everywhere | Optional CUDA later. Not for launch | — |
| 6 | The torch and docling wheels | `pyproject.toml:8` `docling` (pulls torch); `tests.yml:51-52`, where the lock resolves to the CUDA torch on Linux | Untested. A `uv sync` would pull gigabytes of CUDA | `[tool.uv.sources]` pins torch to the PyTorch CPU index under `sys_platform == 'linux'`, then re-lock. CPU wheels exist for amd64 and arm64 | L1 |
| 7 | OpenCV built from source with FFmpeg off | `pyproject.toml:40,59-60` (`no-binary-package`, the CMake flags) | It applies on Linux too, and the Linux prebuilt wheels also bundle FFmpeg, so the source build is needed there as well. Never built on Linux | Build it once in the image's builder stage and in L1's cached CI job, with the same flags. Add `apt` build deps (cmake, g++) | L1, L6 |
| 8 | The sync-folder refusal for the blob store | `blobs.py:143` (`~/Library/CloudStorage`, `~/Library/Mobile Documents`, `~/OneDrive*`); file-provider xattrs at `:58-59`, `:161` | It refuses only `~/OneDrive*`. Linux sync clients carry no file-provider markers | Add Linux client folders by name (`~/Dropbox`, `~/Nextcloud`, `~/pCloudDrive`, …), and state that detection on Linux is by name only | L4 |
| 9 | The extended-ACL check on private files | `blobs.py:86-97,121-135`, `acl_get_fd_np` through libc, darwin only | `_has_acl` returns False, so a POSIX ACL granting others read passes unseen | Read `system.posix_acl_access` with `os.getxattr`. Ubuntu CI has `setfacl`, so it can be tested | L4 |
| 10 | Peer credentials on the Unix socket | `local_socket.py:68-75`: darwin `LOCAL_PEERCRED`, else `SO_PEERCRED`, else refuse | Portable, and exercised by the Linux job | Rootless Docker and Podman map uids, so the peer-uid check must be measured there (§8.2) | L6 |
| 11 | File identity without birthtime | `store/index_store.py:265`, `getattr(st, "st_birthtime", None) or st.st_ctime_ns` | Falls back to `ctime`, which moves on chmod and xattr changes. The result is a spurious cache invalidation: slower, never wrong (the docstring's generation guard) | Accept it. Add a test that a chmod invalidates rather than corrupts | L4 (test only) |
| 12 | Finder's locked flag | `test_amend.py:1286` skips with "no file flags on this platform" | A skip, which is correct | — | — |
| 13 | Ollama discovery | `127.0.0.1:11434` at `embed/engine.py:27`, `retrieve/expand.py:85`, `rerank.py:39`, `rerank_cross.py:82`. The loopback-only egress guard is `net.py` | Portable on a host. **In a container, the host's Ollama is not loopback**, and `host.docker.internal` is correctly refused | Share a network namespace with Ollama rather than widening the guard (§8.2) | L6 |
| 14 | The TCP listener | `mcp/server.py:1928` (`--http`), `:1319` default `127.0.0.1:8765`, `:1350` `_host_is_loopback`. No authentication | Portable, but loopback-only, and **a container's loopback is unreachable through a published port** | `--http 0.0.0.0:PORT` is allowed only with `--token-file`, a bearer token checked in constant time. The host publishes only on `127.0.0.1` (§8.2) | L5 |
| 15 | `requires-python` | `pyproject.toml:5` `>=3.11`, while everything runs on 3.14 | It misstates the minimum | Already in flight (`chore/python-314-minimum`). Land it before X1 | before X1 |
| 16 | macOS-only tools | `tools/build-bundled-engine`, `slim-bundled-engine` (BSD `find -xattrname`), `engine-interpreter-pin.py`, `tools/*.swift` | They build the app bundle only, so this is correct | Keep them macOS-only, and say so in the engine README | doc |

**The other engine paths are home-relative and portable:** `scopes.py:44` `~/.substrate`,
`stack.py:86`, and `paths.py:84-86` (`~/.cache/...`). In a container, `HOME` decides them (§8.2).

---

## 8. Distribution

### 8.1 What each engine release publishes

Everything comes from **one signed tag**, built by the `release` workflow (L8). It runs only on a
`v*` tag push, which you make with `git tag -s`. Before it builds anything, it checks
`verification.verified` on the tag object through the API, and it stops on an unsigned tag.

| artifact | for | notes |
|---|---|---|
| `substrate-<v>.tar.gz` + `SHA256SUMS` | the app's bundle build, and GPL source | `git archive` of the tag (§2.1) |
| A wheel (`py3-none-any`) and an sdist | `uv tool install` / `pipx` users | Needs `[tool.uv] package = true` (today `package = false`, `pyproject.toml:21`), a build backend, and entry points `substrate` and `substrate-mcp`. **Distribution name `substrate-engine` (D8); import package `substrate`.** **The core wheel leaves docling out:** `uv`'s `override-dependencies` and `no-binary-package` (`pyproject.toml:26-29,40`) do not travel in wheel metadata, so a pip-installed docling would pull RapidOCR and a prebuilt OpenCV with GPL FFmpeg. The wheel covers md/txt/pdf-text, lexical search and the Ollama arms. The docling formats are supported **through the image or a `uv sync --frozen` checkout only**. Publish to GitHub releases; PyPI as well is the open half of D8 |
| `ghcr.io/ronan-wood/substrate:<v>` | headless and Linux | Multi-arch (linux/amd64 and linux/arm64), built in CI from `uv.lock` (never built locally and pushed), and pushed by digest. Labelled `org.opencontainers.image.source` and `licenses=GPL-3.0-or-later` |
| Build-provenance attestations and an SBOM | verification | `actions/attest-build-provenance` for the tarball, the wheel and the image. Consumers check with `gh attestation verify` |
| The release itself | — | Created as a **draft**. You publish it, which keeps the human step. Turn on "immutable releases" in the repo settings so published assets cannot be replaced |

**Job permissions.** The release job alone gets `contents: write`, `packages: write`,
`id-token: write` and `attestations: write`. Every other job stays `contents: read`.

**GPL for the image.** It contains GPL and third-party code plus Debian packages:
- the release's source tarball covers the engine;
- `third_party_licenses/` ships inside the image;
- for the Debian base, the image README points at Debian's source archive for the exact package
  versions, and the SBOM lists them.

### 8.2 `serve` in Docker

**Pick the transport by host OS:**

| host | transport | auth |
|---|---|---|
| **Linux** | **The Unix socket, as on macOS today.** Mount the run directory: `-v ~/.substrate/run:/home/substrate/.substrate/run`, and run with `--user "$(id -u):$(id -g)"` | The existing peer-uid checks on both ends (`local_socket.py:96,126`). Clients on the host connect with `substrate-mcp --connect <socket>`. **Measure under rootless Docker and Podman**, where uids are mapped |
| **macOS (Docker Desktop)** | **TCP.** A Unix socket inside a bind mount is not expected to be reachable from the host across Docker Desktop's VM; L6 measures this before deciding | L5's token: `--http 0.0.0.0:8765 --token-file /run/secrets/substrate-token`, published as `-p 127.0.0.1:8765:8765`. The token file is mode 0600 and generated by `substrate token new`. **Scripta never uses this path.** It runs its bundled engine over the socket. For MCP clients, `substrate-mcp --connect http://127.0.0.1:8765 --token-file …` |

**Vault mounts:**
- **Vaults are mounted read-only** (`:ro`). `serve` is read-only by default (`mcp/server.py:1940`)
  and compose writes only to the index root.
- **The container owns its engine home.** It gets its own registry, indexes and `vaults.lock` on a
  named volume at `/home/substrate/.substrate`. It does not share the host's `~/.substrate`:
  - two engines must never write one index;
  - `flock` across a macOS bind mount is not something to rely on.
- **Mount each vault at the same absolute path it has on the host.** The registry stores absolute
  vault paths (`scopes.py`), so the CLI output then reads the same inside and out.
- **Guarded vaults stay withheld.** A vault whose `.substrate.toml` declares `guard_state` finds no
  fresh heartbeat in a container, so its own notes are withheld: the guard fails closed
  (`guard.py:28`). That is intended, because the Docker engine never serves Scripta's guarded
  workspace calls.

**Models.** Ollama must be reachable as loopback (item 13 above):
- **Linux:** `--network host`, or Compose with `network_mode: "service:ollama"`, so that
  `127.0.0.1:11434` is Ollama inside the same network namespace.
- **macOS:** Ollama inside Docker runs on the CPU only, with no Metal. The practical choices are
  `--lexical-only` or a sidecar run on the CPU.
- **Do not widen the egress guard to `host.docker.internal`.** It exists so that the corpus never
  leaves the machine through a misconfigured host.

**The image:**
- multi-stage: the builder compiles OpenCV and runs `uv sync --frozen --no-dev`, with CPU torch on
  Linux (L1); the runtime is `debian-slim` plus `tesseract-ocr` and a non-root `substrate` user;
- `ENTRYPOINT ["substrate", "serve"]`, `HEALTHCHECK` through `substrate status`;
- a `.dockerignore` that admits only the tracked tree, so a local build context can never pick up
  `out/`, `out-vault/` or `.venv`.

**The smoke test (`image` CI job, L6):**
1. Compose the synthetic `vaults/demo-vault` into the container's home.
2. Run `serve` in `--lexical-only` mode.
3. From the runner, call `list_scopes` and `search` through `substrate-mcp --connect`.
4. Check that the replies have the shape of the published `contract/frames/`. The same fixtures as
   the app.

### 8.3 The macOS app keeps its bundled interpreter

**Nothing in the bundle changes** except where its source comes from. This is §2.4, restated
against the widened scope:
- `fetch-engine` downloads the pinned release's tarball, the same tag that produced the wheel and
  the image;
- it checks the sha256;
- `build-bundled-engine` builds the relocatable uv CPython 3.14, checked against the interpreter
  pin, plus the venv and the Swift arms, then signs them with the app's identity.

**The app does not use the wheel or the image.** The bundle needs `uv.lock`, the pin and
`tools/*.swift`, and the macOS-only arms (Vision, Foundation Models) exist only in the bundle.

**The bundle is a different build from the same source.** The macOS bundle and the Linux image
are two builds of one tagged tree. `engine.json` (P5) records the tag and commit in both, so
`status` can report which build is answering.

---

## 9. The update flow and the compatibility rule

### 9.1 What the app pins

`engine.lock` (§2.1) gains a `wire=` line: the wire version this app build speaks. The `engine pin`
CI job checks that four values are equal:
- `engine.lock`'s `wire`;
- `contract.json`'s `wire.version` in the vendored copy;
- the vendored copy against the pinned tarball's `contract/`;
- `SubstrateWire.version` (through `WireParityTests`).

### 9.2 The bot PR

**Dependabot cannot do this.** None of its ecosystems reads a custom lock file.
- A git-submodule pin would work for Dependabot, but `git archive` omits submodules, which breaks
  the GPL tarball.
- A Dockerfile `FROM` pin would pin the image, not the tarball.

**Renovate can,** with a regex `customManager` over `engine.lock`:
- datasource `github-releases` for `tag=`;
- `github-tags` for the `commit=` digest.

**The bot proposes, and you complete.** Renovate cannot compute the tarball's sha256, and it
should not re-vendor the contract. Each engine release therefore goes like this:
1. Renovate opens `chore(deps): engine vX.Y.Z` with `tag` and `commit` bumped.
2. `engine pin` fails by name: "sha256 and contract not updated; run
   `Distribution/fetch-engine --bump`".
3. On the bot branch, you run `fetch-engine --bump`. It fills `sha256`, re-vendors
   `Core/Tests/EngineContract/` from the tarball, and prints the contract diff classification
   (§9.3). You commit (signed) and push.
4. `engine pin` and `app core` go green, and the parity tests run against the fixtures published
   for that version.
5. If §9.3 says the release is breaking, the same PR also carries the SubstrateKit changes and the
   `wire=` bump.

This keeps the review step (`audit → review → implement → verify`): no unattended bump can merge,
because the required check needs your re-vendor commit.

**Alternative:** a scheduled workflow in Scripta that runs `fetch-engine --bump` and opens a
complete PR. It needs a GitHub App or token secret, because PRs opened with `GITHUB_TOKEN` do not
trigger CI, and the only gain is skipping step 3. Not recommended.

### 9.3 What an engine release may change without an app release

**The rule is mechanical.** `tools/contract-diff <old> <new>` (L8, also run by `fetch-engine
--bump`) classifies every change to `contract.json` and `contract/frames/`. In the engine repo's CI,
a **breaking** change without a `wire.VERSION` bump fails.

**Allowed without an app release** ("additive"):
- a new key in a reply;
- a new tool, a new optional argument;
- a new CLI subcommand or flag;
- a new format in `formats`. The app parses that table by shape (`SubstrateCLI.swift:345`);
- retrieval quality, performance, models and dependencies;
- an index schema bump. The engine rebuilds its own index. **One caveat:** Scripta's attach policy
  (`EngineAttach`) refuses an engine on another index schema, so an older app stops attaching to a
  newer headless engine, and today that leaves it with no engine at all (§9.4);
- the docs, the tests, the image and the wheel.

**Breaking: needs a `wire.VERSION` bump and an app release before any app relies on it:**
- a reply key removed or renamed, or its type or meaning changed;
- a new value in a closed vocabulary (the spine fields, `document_class`, arm states);
- the JSON-RPC error codes the app maps (`wire.MISMATCH`).

**Host contract: no wire bump, but an app release must land first or together.** This is
`contract.json`'s `host` section, which the app's own CLI calls and files rely on (§1 E):
- the CLI flags the app passes: `compose`, `embed`, `refresh`, `serve`, `ingest`;
- `EX_DECLINED = 75`;
- `DEFAULT_SOCKET`, the lock suffix;
- `.substrate.toml` keys, `guard_state` and the heartbeat format, the registry format.

These are versioned as `contract.host_version`, a second integer. The engine must keep accepting
the old host contract for one minor release after changing it.

**What a mismatch does at runtime.** An app and an engine of different wire versions refuse each
other loudly; that is the existing exact match. **The app does not then use its bundled engine.**
The socket is taken, so it shows the refusal (`SubstrateEngine.swift:399`, `.socketBusy`) and has
no search until that engine stops. §9.4 is what keeps a headless upgrade from doing that.

### 9.4 Declared compatibility: ranges, not equality

**The problem.** Once the engine releases on its own, the running headless engine is often weeks
ahead of the one Scripta bundles. Today both checks are equality: `SubstrateClient` refuses another
wire version, and `EngineAttach.verdict` (`EngineAttach.swift:36`) refuses another index schema. A
refusal is not a fallback (§9.3), so every such upgrade costs Scripta its search.

**The contract declares ranges,** on the model of Docker's daemon API (`ApiVersion` and
`MinAPIVersion`). `contract.json` gains:
- `wire.version` and `wire.min_version`: what the engine speaks, and the oldest it still answers;
- `notes.version` and `notes.min_version`: the frontmatter contract it indexes (`spine`, `classes`,
  `yaml`), and the oldest it still reads;
- `index.schema` and `index.readable`: the schema it writes, and the range it can read. Under the
  recommendation below, only the app's own engine is held to it.

**The check becomes "inside the declared range",** on both sides, and in one shared rule so every
client gets it, not only Scripta. A client of version *c* attaches when `min_version ≤ c ≤ version`.
The engine refuses a client below its minimum with `wire.MISMATCH` and both numbers. `tools/contract-diff`
(§9.3) then classifies range changes: raising `version` with the minimum unchanged is additive;
raising a minimum is breaking.

**The alternative, recommended for the index:** the attached app stops writing indexes. Today it
does: its own CLI composes for the Library, the recordings and "Refresh now" (`EngineAttach.swift:20`).
That is the only reason the schema must match. If the attached engine's refresh loop does all
indexing, the schema matters only when Scripta runs its own engine, and the app and an attached
engine need agree on two things only: the wire, and the note format. What that changes:
- **"Refresh now"** cannot run `substrate refresh` against an attached engine's indexes, and
  `ingest` and `amend` are refused on the read-only transport. It needs a new call that only brings
  the next pass forward, coalesced with the loop so no caller can make it run more often. An engine
  without that feature (`engine_manifest` features) shows when its next pass is due instead.
- **The Library** writes notes and the registry as now, and the new scope shows as pending until
  the engine's pass composes it, instead of "composed" on return.
- **Recordings** write their note and stop composing it, so a new call is searchable after the
  next pass, or at once through the refresh call.

**Recommendation:** ranges for the wire and the note format, and the alternative for the index, so
the engine never has to read old schemas. **It is a contract change**, so it lands engine first
(§2.3): `export-contract` writes the new keys, `engine-contract sync` re-vendors them, and
`engine-contract check` and the parity tests read the ranges, not the single numbers. **It goes in
§12 step 3, before X1:** it touches the app and the engine together, and it must be in `v<first>`,
or the first app pinned to it attaches only to that exact engine.

---

## 10. Scripta's history: keep `substrate/` or drop it before the first public push?

**The question.** Nothing is public, so a second filter pass (`git filter-repo --path substrate
--invert-paths`) is still possible before Scripta's first public push.

| | keep (one forward `git rm -r substrate`) | drop (a second rewrite) |
|---|---|---|
| Old tags `v0.1.0`–`v0.3.4` | **Still build the app**, engine included | Break: the app no longer builds at any old tag |
| The 30 cross-cutting commits | Intact | Lose their engine half. Their messages describe changes that are no longer there |
| `git bisect` across app/engine bugs | Works | Only on app-side causes |
| SHAs | Unchanged from `dfd3091`, so the R15 rebuild is done once | **Every SHA changes again.** This means a third local migration (R15 again), the commit map, every in-flight worktree, and the new repo's #1/#2. The `main` ruleset blocks a force push, so it is either a ruleset edit or delete-and-recreate again |
| Personal-data risk | None added: the history is already scanned clean (rewrite R6, R12) | None removed: there is nothing left to remove |
| Size | `substrate/` is 5.0 MB of the history's 10.6 MB on disk (pack 10.2 MiB): trivial | Saves about 5 MB |
| Clarity | Two public copies of the engine's history, until the deletion commit. The README says where the engine lives now | One canonical history |

**Recommendation: keep it (D9).**
- Dropping buys tidiness and about 5 MB.
- It costs reproducible old tags, the intact cross-cutting commits, and a third SHA migration,
  right after the second.
- It removes no sensitive content.

Same license, same owner; a duplicated, clean GPL history is not a liability. The removal commit is
X7.

---

## 11. Ready to go public: the checklist

**The bar is a clean two-repo split** (§12): the engine in its own repository, and Scripta holding
no `substrate/`. Linux and Docker (L1–L8) come after it. Because nothing is pushed until both
repositories are public, the checklist has three parts:
- what is proven locally **before** going public;
- the split itself, finished **after**, once CI runs;
- what comes **later**.

### Before going public: proven locally

**The engine (`$E`, from X1–X4):**
- [ ] X4 passes:
  - the tree, the commit count, the emails and the refs;
  - the tip's suite, standalone;
  - the bundle's fingerprint.
- [ ] A fresh mirror passes `scan.py` (0 failing) and `scan-binary.py` (0 hits).
- [ ] The repo is ready to read: `LICENSE`, a `README` (install from source, or Scripta on macOS,
  and which arms are macOS-only), `SECURITY.md` and `third_party_licenses/`.
- [ ] An R13-style skim of the subjects, docs and `vaults/demo-*`.

**Scripta:**
- [ ] P8 and the step-3 items are committed, each with its CI jobs replayed green locally, and
  with `scan-added.py` at 0 failing.
- [ ] Declared compatibility (§9.4) is in both halves, tested on each:
  - an engine one wire version ahead, its minimum at this app's version, is attached;
  - one whose minimum is above it is refused, with both numbers;
  - while attached, "Refresh now" and a new Library scope run through the engine's pass, and this
    app's CLI writes no index.
- [ ] A fresh mirror passes `scan.py` (0 failing) and `scan-binary.py` (0 hits).

**Both repositories:**
- [ ] No open PR or branch carries vault-derived content.
- [ ] The rewrite plan's pre-push denylist hook (its open item 2) is installed in both local clones.
- [ ] The settings scripts have run with `DRY=1`, for each repository.

### The clean split: finished after going public

Then the engine repository is created public (X5), and Scripta follows rewrite R14.

**The engine repo:**
- [ ] On `main`, green: `engine` (ubuntu), `engine docling` (macOS 15, zero skips) and
  `codeql python`.
- [ ] The release `v<first>` is published from a signed tag, with `verification.verified: true`,
  and carries the tarball and `SHA256SUMS` (X6).
- [ ] The settings are on:
  - rulesets: `main` with linear history, signatures and the required checks; immutable `v*` tags;
  - immutable releases;
  - squash-only merges;
  - secret scanning and push protection;
  - private vulnerability reporting;
  - Dependabot (`uv`, `github-actions`).

**Scripta:**
- [ ] P8 and the step-3 PRs merged green.
- [ ] `substrate/` is removed (X7), and `engine.lock` pins `v<first>`, with `wire=` set.
- [ ] `app core` and `engine pin` are green and required.
- [ ] `engine` and `engine docling` are gone, and `codeql python` and the `uv` Dependabot entry are
  removed.
- [ ] **The app builds from the pinned release.** Check four things:
  - a `Distribution` build from `fetch-engine` succeeds;
  - `build-and-notarize` reaches `source=Notarized Developer ID`;
  - the app spawns its bundled `substrate serve`;
  - the Library lists the demo scope.
- [ ] **Attach is exercised both ways.** An engine outside the declared range is refused with the
  reason, and one inside it is attached.
- [ ] `publish-release`'s dry run lists `Scripta-<V>-source.tar.gz` and the engine tarball copy.
- [ ] The docs describe the pin, not a subtree:
  - `ARCHITECTURE.md`, `README.md` and `SECURITY.md`;
  - an R13 skim.
- [ ] The rewrite plan's R14 settings are applied: private vulnerability reporting, secret
  scanning and push protection.

### Later: after the split (L1–L8, §9.2)

**The engine repo:**
- [ ] `engine linux locked` (L1, only the named darwin skips) and `image` (L6) are green on `main`.
- [ ] A release carries the wheel and sdist, the multi-arch GHCR image, and attestations that
  `gh attestation verify` accepts (L7, L8).
- [ ] **The image runs `serve` against the demo vault:**
  - on Linux over the socket, from a host `substrate-mcp --connect`, `list_scopes` and `search`
    return the published frames' shape;
  - on macOS Docker Desktop over TCP with a token, or that path is documented as unsupported
    (L5/L6 decide).
- [ ] The wheel installs into a clean venv on Linux and macOS (`uv tool install`), and compose plus
  search on the demo vault work.
- [ ] The image is checked too: `docker save`, extract the layers, and run `scan.py`'s patterns
  over the engine tree inside it. Expect it to equal the tag's tree, plus no host paths
  (`/Users/`, the home folder).
- [ ] Dependabot covers `docker`, and the README adds install via the wheel and Docker.

**Scripta:**
- [ ] **The bot flow has run once end to end.** Engine `vX.Y.Z-rc.1`, then Renovate's PR, then
  `fetch-engine --bump`, then green.

---

## 12. Sequencing

**Built locally, pushed once.** This private repository's Actions minutes are exhausted until
December, so nothing is pushed until both repositories are public, when CI runs on free runners.
Until then the gate is the local replay of each CI job (P4–P8 describe them), and each PR body says
where a replay cannot stand in for GitHub's runner.

**The bar for going public is a clean two-repo split:** the engine in its own repository, and
Scripta holding no `substrate/`. L1–L8 (Linux, Docker, the wheel, the release workflow) come after.
So do §11's boxes that need them: `engine linux locked`, `image`, the wheel, the multi-arch image
and attestations. The rest of §11 still gates.

1. **Done.** The local rebuild (rewrite R15), and P1–P7, merged on GitHub (#7, #10, #11, #13–#16).
2. **Local, committed and not pushed,** each branch cut from the one before, so they stack:
   1. **P8** (`chore/engine-tree-is-a-repo`, committed as `f5b3994a`);
   2. **the step-3 items:** `serve.toml`, the index location out of the engine's home, and
      declared compatibility (§9.4). `chore/python-314-minimum` merged as #4;
   3. **X1–X4 on the local stack:**
      - X1 mirrors this checkout, with `refs/heads/main` set to the stack's tip, not GitHub's `main`;
      - X2–X4 then run as written.
3. **Both repositories go public.** Scripta follows rewrite R14. The engine repository is created
   public at X5, from `$E`, with its settings.
4. **Then everything is pushed,** and every PR merges green on free runners:
   1. Scripta's P8 and step-3 PRs;
   2. **`experiments/lean-blob-gc/` into the engine repository** (D5), one fresh commit on its
      `main` after X5, by the steps in the notes folder's `split-lean-blob-gc.md`. Then
      `exp/lean-blob-gc` on GitHub is deleted as part of rewrite R15 Part 2;
   3. X6, the first engine release;
   4. X7, the Scripta extraction PR; then X8, the local fallout;
   5. Scripta's first public release from the pinned engine, with both source tarballs (rewrite
      R16).
5. **L1–L8 in the engine repository:**

   | # | branch | scope (from §7 and §8) | tests |
   |---|---|---|---|
   | L1 | `ci/linux-locked-engine` | CPU torch on Linux through `[tool.uv.sources]`, then re-lock. The OpenCV source build on Linux. The `engine linux locked` job (`-rsx`, skips allowlisted by test id). `-rsx` on `engine` | The job green. Regression: an unlisted skip fails it |
   | L2 | `feat/ocr-on-linux` | The Tesseract arm for images and scanned pages (§7 items 1 and 2). A named refusal when there is neither Vision nor tesseract | OCR round-trip on a Pillow-drawn image on Linux. The refusal text, with tesseract absent |
   | L3 | `fix/portable-model-defaults` | The non-MLX class defaults (item 4) | `pick_auto` and the class defaults against a listing with no `-mlx` models |
   | L4 | `fix/private-store-on-linux` | POSIX ACLs, Linux sync folders, the birthtime test (items 8, 9, 11) | `setfacl` → store refused. A folder named `Dropbox` → refused. chmod → invalidation, not corruption |
   | L5 | `feat/serve-tcp-token` | `--http` beyond loopback only with `--token-file`; `substrate token new`; token support in `--connect` | No-token non-loopback bind → refused. A wrong token → 401. The Host guard still applies |
   | L6 | `build/container-image` | The Dockerfile, `.dockerignore`, the `image` smoke job, the transport measurements (§8.2) | The smoke test. Peer-uid under rootless Podman. The Docker Desktop socket measurement, recorded |
   | L7 | `feat/wheel` | `package = true`, the backend, entry points, the `substrate-engine` name (D8), the core-only dependencies | `uv build`, then a clean-venv install, then compose and search on the demo vault |
   | L8 | `ci/release-workflow` | The tag-verified release job, the artifacts, attestations, `tools/contract-diff` | On a `-rc` tag, every artifact is produced and attested, and an unsigned tag stops it |

6. **Scripta's first pin bump through the bot flow** (§9.2). This is an end-to-end proof, not only
   a version change.

**What this order changes:**
- **X0's read token is not needed.** The engine repository is public from X5, so `engine pin` can
  read its release assets with its own `GITHUB_TOKEN`. X0's other preconditions become: P8 and the
  step-3 items committed locally with their replays green, in place of "merged, and green on `main`".
- **X2 covers commits that exist only locally.** It scans every commit reachable in `$E`, and X1
  built `$E` from the local stack, so the scan covers them. Scripta's own push gets the same scans
  first: `scan-added.py` per branch, as P1–P8 ran it, and the rewrite's pre-push denylist hook.
- **Two copies until X7.** The engine repository starts from the local stack, before Scripta's P8
  and step-3 PRs merge. A fix round on one of those PRs that touches `substrate/` lands in the
  engine repository too, engine first (§2.3). X7 merges only when the engine repository's
  `main^{tree}` equals Scripta's `main:substrate` (X4's first check, run again).
- **The first push may need a fix round.** Two gaps the local replays cannot close:
  - `engine` runs on Ubuntu in CI and on macOS locally;
  - the staged workflows have never run.

  Budget one fix round for each repository's first CI run.

**Steps that cannot run before the push:**

| step | why |
|---|---|
| X1's check against `gh api …/commits/main` | GitHub's `main` lacks the local stack: check against the stack's tip instead |
| X5's required `engine` check | it exists only once the engine repository's first run reports (X5's "On a deadlock") |
| X6 | `publish-engine-release` reads the tag, its signature and `refs/heads/main` from GitHub |
| X7 | it fetches X6's release, its `engine pin` job runs on GitHub, and it needs P8 and the step-3 PRs merged first |
| §9.2's bot flow, §11 | they need both repositories live |
