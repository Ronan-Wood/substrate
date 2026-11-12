# The client contract

A client, such as Scripta's `SubstrateKit` or `substrate-mcp --connect` from another install,
relies on three things: [`contract/contract.json`](../contract/contract.json), the golden frames in
[`contract/frames/`](../contract/frames/), and the wire version each request and reply carries. A
client tests against these files rather than against this package's source.

## `contract/contract.json`

This is what the engine states about itself, exported from the code by
[`tools/export-contract.py`](../tools/export-contract.py).

| key | what it fixes |
|---|---|
| `wire` | the wire version range the engine serves (`version`, `min_version`), the `Substrate-Wire` header, and the JSON-RPC error code for a mismatch |
| `socket` | the default socket path (`~/.substrate/run/substrate.sock`) and its lock suffix |
| `cli` | the exit code a command gives when it declines because another holds the lock (`75`) |
| `grants` | the headers a forwarded write carries (`Substrate-Client`, `Substrate-Grant`, `Substrate-Client-Info`) and the modes a shared engine runs writes in (`none`, `granted`, `open`), one of which the `server` block reports as `writes` |
| `notes` | the range of note formats the engine reads |
| `spine` | the closed vocabularies: `statuses`, `doc_types`, `confidences`, and which statuses are retrieved by default |
| `classes` | document classes, their chunk policies, and which classes are withheld by default |
| `retrieval` | the retrieval arms and the states each can report, and the engine health states |
| `refresh` | the outcomes a refresh pass records |
| `payloads` | the keys of every reply payload: `passage`, `expand`, `documents`, `refresh`, `engine_health` and the rest |
| `extends` | which payloads are supersets of others |
| `engine_manifest` | the features a bundled engine declares (`connect-unix`, `refresh`, `serve`) |
| `yaml` | the code-point ranges the frontmatter writer leaves unchanged |
| `tiers` | the measured retrieval tiers (the eval's case count, ceiling and floor) |

`tests/test_contract.py` exports the contract afresh and fails if the committed file differs. It
also checks the reader itself, so that a renamed key or a moved version changes the export rather
than being silently dropped.

## The golden frames

`contract/frames/*.frame.json` are the exact bytes the engine sends for each tool (`search`,
`expand`, `documents`, `list_scopes`, `status`, agent mode, the error frames). Clients test their
decoders against them, and Scripta vendors a byte-identical copy.

`tests/test_swift_golden_frames.py` produces them from the demo vaults only. It composes
`vaults/demo-vault` into a scratch registry, runs one refresh pass with no model server reachable,
and replaces the workspace path with `/demo-substrate`. Without the capture variable it compares
each frame's shape (every key, and which values are null) with what the engine emits now, and
scans the committed bytes for host paths.

## The wire version

[`substrate/wire.py`](../substrate/wire.py) holds one integer, `VERSION`, and the oldest version
still served, `MIN_VERSION`. It is not the index schema version, which moves for changes no client
decodes.

- Each request carries `Substrate-Wire: <n>`. An absent header means 1.
- The engine answers any client from `MIN_VERSION` to `VERSION`, with a reply shaped for `VERSION`.
  An older client in that range ignores the keys it doesn't know.
- A request outside the range isn't dispatched. It gets a JSON-RPC error naming both versions.

| change | what it bumps |
|---|---|
| **Additive:** a new tool, an optional argument, a new reply key that a newer client will rely on | `VERSION` |
| An optional key whose absence every client already handles | nothing |
| **Breaking:** a tool, argument or key removed or renamed, an argument made required, a default, type or meaning changed, a new value in a closed vocabulary | `VERSION` and `MIN_VERSION` |

**Clients from before the range match exactly.** A client older than wire versioning reads only a
reply stating its own version, so the first bump of `VERSION` refuses every such client, whatever
`MIN_VERSION` says. Bump it only once those clients are gone. The docstring of `wire.py` is the
authority for these rules, and this table summarizes it.

**`read_only` was redefined at wire 2** as "runs no write without a grant", true for `writes`
`none` and `granted`. That changes a meaning, which the table calls breaking, but no wire-1 client
acts on the engine refusing writes, so `MIN_VERSION` stayed 1.

**Wire 3 is additive.** Every passage, in `search` and in `expand`, carries `provenance`: what the
engine's write log says about its note (`state`, with `client`, `op` and `at` for an engine write)
and whether the note's own `amended_by` / `written_by` line agrees (`stamp`, `stamped_by`). The
`list_scopes` server block carries `writes_log`. The vocabularies are in the contract's `writes`
section.

## Changing the contract

The contract changes **engine first**. The change lands and is released here, and clients follow by
bumping the engine release they pin.

1. Make the change in the engine.
2. Regenerate the files by running the tests with the capture variables set:

   ```sh
   SUBSTRATE_CAPTURE_CONTRACT=1 uv run python -m pytest tests/test_contract.py -q
   SUBSTRATE_CAPTURE_SWIFT_FIXTURES=1 uv run python -m pytest tests/test_swift_golden_frames.py tests/test_agent_mode.py -q
   ```

3. Commit the regenerated files in the same PR, and say in the PR whether the change is additive or
   breaking.
