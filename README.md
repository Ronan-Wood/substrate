# substrate

[![tests](https://github.com/Ronan-Wood/substrate/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Ronan-Wood/substrate/actions/workflows/tests.yml)
[![licence: GPL-3.0-or-later](https://img.shields.io/badge/licence-GPL--3.0--or--later-blue)](LICENSE)
[![release](https://img.shields.io/github/v/release/Ronan-Wood/substrate?include_prereleases&sort=semver)](https://github.com/Ronan-Wood/substrate/releases)

**A local search engine for Markdown vaults that you and your AI tools can share.** substrate
composes a project vault and the vaults it inherits into a named **scope**, indexes the scope, and
answers search over it from a command line or an MCP server. It runs on your machine, and the only
model server it will talk to is one on loopback.

It is for people who keep their notes as Markdown, for example in Obsidian, and want one index that
they and any MCP client can search without the notes leaving the machine. It is also for developers
writing a client: [Scripta](https://github.com/Ronan-Wood/Scripta), a macOS app, bundles this engine,
and the engine also runs on its own.

- **Composition.** A vault's `.substrate.toml` names the vaults it inherits, such as a shared core,
  and a query sees them all as one scope.
- **Every passage carries its spine:** `status`, `doc_type`, `confidence` and `domains`. Archived
  and superseded notes stay out of results by default, and a superseded note shows up as a link on
  the note that replaced it.
- **Search reports what it used.** Lexical search (SQLite FTS5) works with nothing else installed.
  Vector search, HyDE and reranking run against local models. Each result states which of those
  arms ran, and the engine refuses to give an answer that looks complete but isn't.
- **Ingestion** reads Markdown with the standard library. PDFs, Office files, HTML, EPUB and images
  go through docling and Apple's on-device recognizer. `substrate formats` lists every format it
  accepts and every format it refuses.

## Status

| platform | state |
|---|---|
| **macOS on Apple silicon**, Python 3.14 | **Supported.** The engine is developed and shipped here. Apple Vision OCR and the Swift helpers under `tools/` exist only on macOS. |
| **Linux** | **On the way.** CI runs the core test suite on Ubuntu, but the locked dependency set, OCR and model defaults aren't portable yet. |
| **Docker** | **On the way.** No image is published yet. |

The bundle build tools (`tools/build-bundled-engine`, `slim-bundled-engine`,
`engine-interpreter-pin.py`, `tools/*.swift`) are macOS-only by design. They build the signed bundle
that Scripta ships.

## Install from source

You need [uv](https://docs.astral.sh/uv/). It installs a managed Python 3.14 and the locked
dependencies:

```sh
git clone https://github.com/Ronan-Wood/substrate
cd substrate
uv sync
```

The first sync is slow: OpenCV builds from source, because every prebuilt macOS wheel bundles a
GPL FFmpeg (`pyproject.toml` explains why).

## Quickstart: the demo vaults in 30 seconds

`vaults/demo-vault` is a project vault and `vaults/demo-core-vault` is the vault it inherits. Both
are synthetic, and they double as the test fixtures. Run every command from the root of the clone.
They keep the registry, the index and the socket in `.demo/` there, which git ignores, so each
step works in any terminal. `serve` still keeps its vector cache in `~/.substrate`. To start over,
delete `.demo/`.

There are no installed commands yet. Elsewhere in these docs, `substrate <command>` is short for
`uv run python -m substrate.cli <command>`, and `substrate-mcp` is short for
`uv run python -m substrate.mcp.server`, both run from the root of the clone.

**1. Compose the scope.** This reads the manifest, indexes both vaults as scope `demo`, and checks
the composition:

```sh
mkdir -p -m 700 .demo
uv run python -m substrate.cli compose vaults/demo-vault --registry .demo/scopes.toml --index-root .demo/index --db .demo/index.db
```

It ends with a `PASS` line for each assertion and `scope: 'demo' registered`.

**2. Query it.** `--no-vector` searches without embeddings, so no model server is needed:

```sh
uv run python -m substrate.cli query "engine boundary" --scope demo --registry .demo/scopes.toml --no-vector
```

The first hit is the current engine-boundary decision, with `supersedes=engine-boundary-old` on its
spine line. The old note it replaced isn't returned.

**3. Serve it.** This is the shared engine: an MCP server on a Unix socket that only your user can
reach, refreshing every registered scope on a timer. Leave it running in its own terminal:

```sh
uv run python -m substrate.cli serve --registry .demo/scopes.toml --socket .demo/substrate.sock
```

**4. Connect an MCP client.** The client launches the MCP server (`substrate.mcp.server`, which
calls itself `substrate-mcp`) with `--connect`. It speaks MCP over stdio and forwards each read to
the socket. A client starts it from a folder of its own choosing, so it needs absolute paths. Print
them. macOS refuses a socket path longer than 104 bytes, so if the last one is longer, clone the
repository somewhere shallower:

```sh
echo "$PWD" "$PWD/.demo/scopes.toml" "$PWD/.demo/substrate.sock"
```

Then add the server to your client's MCP configuration. Most clients take this shape:

```json
{
  "mcpServers": {
    "substrate": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/substrate", "python", "-m", "substrate.mcp.server",
               "--registry", "/path/to/substrate/.demo/scopes.toml",
               "--connect", "/path/to/substrate/.demo/substrate.sock",
               "--read-only"]
    }
  }
}
```

The client gets the read tools `search`, `expand`, `documents`, `list_scopes` and `status`. Ask it
to search scope `demo` for "engine boundary".

Without `--read-only`, the client also gets `ingest` and `amend`, which write notes. They run in the
engine, and only if the engine was started with `--writes=granted` and you granted that client:
`uv run python -m substrate.cli grant <client>`, where `<client>` is the name the refusal gives.
`ingest` takes the note's text, never a path, but a client can pass the text of any file it can
read itself, so grant only a client you trust that far, and never point a writing client at the
demo vault, which is tracked in git.
[`docs/security.md`](docs/security.md) covers what each transport exposes.

[`docs/demo-vault.md`](docs/demo-vault.md) walks through what the demo vaults contain and which
queries show inheritance, supersession and the withheld classes.

### Beyond the demo

- **Your own vault:** add a `.substrate.toml` beside your notes (see
  [`docs/configuration.md`](docs/configuration.md)) and run `compose` on it. Without `--db` and
  `--index-root`, the index goes to `~/.substrate/index/<scope>.db`.
- **Vector search:** run [Ollama](https://ollama.com) on its default port, pull
  `qwen3-embedding:0.6b`, and run `uv run python -m substrate.cli embed --scope <name>`.
- **The default socket:** without `--socket`, `serve` uses `~/.substrate/run/substrate.sock`, and
  `--connect` with no path finds it there. That is also where Scripta looks.

Each subcommand's `--help` describes the rest.

## Documentation

Start with [`docs/architecture.md`](docs/architecture.md) for how the engine fits together,
[`docs/configuration.md`](docs/configuration.md) to set up your own vault, and
[`docs/security.md`](docs/security.md) for who can reach what.
[`docs/README.md`](docs/README.md) lists every page, including the client contract, the design
principles and the experiment log.

## Repository layout

| path | what |
|---|---|
| `substrate/` | the engine package: ingestion, composition, the index, retrieval, the MCP server |
| `contract/` | what clients rely on: `contract.json` and the golden wire frames, generated and checked by the test suite |
| `tests/` | the test suite |
| `tools/` | the bundle build, the interpreter pin, the release script, and macOS helpers |
| `vaults/` | the synthetic demo vaults |
| `eval/` | the retrieval eval's gold cases, run by `./run.sh` at the root. It needs a document corpus that isn't in this repository |
| `docs/` | the documentation above |
| `third_party_licenses/` | licences of the components the bundle ships |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, the test suite, and the rules a change follows
to land. [`CHANGELOG.md`](CHANGELOG.md) lists what each release contains, and
[`RELEASING.md`](RELEASING.md) covers how a release is cut.

## Licence

GPL-3.0-or-later. See [`LICENSE`](LICENSE). Each release attaches its source tarball and a
`SHA256SUMS`.

## Security

Report a vulnerability privately, as [`SECURITY.md`](SECURITY.md) describes.
