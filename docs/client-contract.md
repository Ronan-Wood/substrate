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
than being silently dropped. And it walks every frame, and one `status` reply that fills every row
the frames leave empty, and fails on any object the contract does not declare. Two exceptions are
named in the test: the `by_*` maps, whose keys are vault names, tiers and vocabulary values, and
each row of `status.entities`, a SQL row the export does not read.

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
| A changed meaning that every client in the range meets as something it already handles: a tool refusal, a refused compose it reads as a frozen scope, or a call or compose that now succeeds where it refused | `VERSION` |
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

**Wire 4 (0.3.0) adds reply keys and changes meanings, and `MIN_VERSION` stays 1.** Against 0.2.2,
the last release at wire 3:

| change | PR | kind | what a wire-2 or wire-3 client sees |
|---|---|---|---|
| `status.defaulted` (the object `defaulted`): notes that took the default `status` or `doc_type` | #56 | additive key | nothing new |
| a note with no `status` or `doc_type` composes as `active` or `reference`; one that spells an engine key another way (`Status:`), or puts a YAML tag or alias on one, is refused | #56 | changed meaning | the first note in answers, where it refused the scope. The second is quarantined (#65), so a note that answered at 0.2.2 may not |
| `status.quarantined`, each note by `vault`, `path`, `kind` and `key` (the object `quarantined_note`); `drift.quarantined`; `refresh.quarantined` in every `refresh` block; the `quarantine` section's `kinds` and `keys` | #65 | additive keys, with closed vocabularies new at wire 4; changed meaning | a note that fails on its own no longer freezes its scope. The scope reads as current, the note is in no answer and not in `drift.added`, and `refresh.note` says how many notes are left out |
| `status.identical`, with the objects `identical`, `identical_duplicate`, `identical_group` and `note_ref` | #73 | additive key; changed meaning | byte-identical notes no longer refuse the scope. A copy under the same `doc_id` is left out, not in `drift.added`, and its text answers under the first note's path |
| `status.engine_keys` (`renamed` rows of `vault`, `field`, `key`; `guarded`), and the kind `withheld_value_under_renamed_key` | #74 | additive key; the kind is in a vocabulary new at wire 4; changed meaning | `drift.changed` lists a note whose bytes are unchanged when its vault's renames changed. `amend` of a held note is refused, as 0.2.2 refused it for its frozen class |
| `ingest` with no `folder` refuses where `04-synthesis` does not exist, instead of creating it | #58 | changed meaning | a tool refusal |
| `amend` refuses a tier 1 or 2 note under a nested folder, such as `02-areas/00-operator/` | #58 | changed meaning | a tool refusal |
| `ingest` and `amend` refuse in a project vault with no `.substrate.toml` | #58 | changed meaning | a tool refusal |
| `ingest`'s `folder` is any relative path, not one of three names; with no `writable_folders`, `amend` takes a note in any folder compose indexes | #58 | changed meaning | the three names still take a write, and an `amend` outside them plans where it refused. The new folder-name refusals apply only to names a wire-3 client cannot send |
| `ingest` and `amend` refuse what the vault's `skip` skips, and `_meta.md` in any case | #54 | changed meaning | a tool refusal |
| an unknown top-level manifest key refuses compose, `ingest` and `amend` | #69 | changed meaning | a tool refusal. A scope that composed at 0.2.2 fails its next refresh and reads as frozen, and `status` gives the reason in `drift.error` |
| a note with no title and no heading composes, titled by its file name | #70 | changed meaning | the note in answers, its file name as `title` and in `citation`. `ingest` and `amend` take such a note |
| compose refuses a scope whose ingest folder is a link, another user's, or writable by every user | #18 | changed meaning | the scope reads as frozen |

The rest of 0.3.0, `substrate init` and `--version` among it, changes nothing a client sees. The
contract also newly declares `engine_health_arms`, the keys of `retrieval_mode.health.arms`, which
wire 3 already sent.
Nothing a wire-3 client decodes is removed or renamed, and no vocabulary it knows gains a value.
The write tools' changed meanings, #69's and #18's refused composes, every compose that now
succeeds where it refused, and #74's `drift.changed`, which reads as a stale scope that the next
compose clears, are changes an older client already handles. They bump `VERSION` alone, as wire 2's
`read_only` did.

**One change is breaking by the table, and `MIN_VERSION` stays 1 regardless, by the decision in
#57.** A note that answered at 0.2.2 can now be quarantined (#56, #65), and an older client reads
that wrongly: the note drops out of its answers, `drift.added` no longer lists it, and the scope
reads as current. Only `refresh.note`, as text, says notes are left out; which ones is under
`status.quarantined`. Raising `MIN_VERSION` would refuse every such client outright, a wire-2
Scripta among them.

`tests/test_wire_older_clients.py` holds the contract to 0.2.2's, and sends a refusal from each PR
that adds one to a real engine, stating wire 2 and wire 3.

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
