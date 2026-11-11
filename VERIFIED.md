# Verified paths

Each path in the first table deletes data or withholds it from a caller. Each has a Lean model of
its rule and a differential harness, which runs the real code and the model on the same generated
inputs and fails on any difference. [`CONTRIBUTING.md`](CONTRIBUTING.md#verified-paths) says when a path belongs here.

| path | code | what the model proves | model and harness | CI |
|---|---|---|---|---|
| `substrate blobs gc --unreferenced`, the planner | `gc_plan` in [`substrate/blob_lifecycle.py`](substrate/blob_lifecycle.py) | A plan never removes a blob that a note in any vault it must read names, and it refuses rather than plan when a note is unreadable, a reference is malformed or an inherited vault is missing. | [`experiments/lean-blob-gc/`](experiments/lean-blob-gc/README.md) | `lean`, on PRs that touch `substrate/` or the model |

The model covers planning only. `gc_apply`, which re-plans and removes under a lock, is checked by
inspection rather than proved, and concurrency isn't modelled. The model's README lists every place
it is simpler than the code.

## Planned

These paths withhold or delete data and have no model yet. Each names the reason it ships without
one and the experiment that will add it.

| path | code | what the model will prove | why not yet | follow-up |
|---|---|---|---|---|
| The vault guard, on every MCP read path | `declared_guard`, `check` and `withheld_names` in [`substrate/guard.py`](substrate/guard.py); `vault_exclusion` in [`substrate/store/index_store.py`](substrate/store/index_store.py); `status_payload` in [`substrate/introspect.py`](substrate/introspect.py) | No passage of a guarded vault whose check fails is returned by `search` (every arm), `expand` (both modes) or `documents`, and `status` counts, lists and drift-checks none of its notes. Every malformed or failed guard state, and every manifest that can't be read, withholds. | Until the model lands, [`tests/test_guard_vault_directory.py`](tests/test_guard_vault_directory.py) runs every arm that exists today against a real compose, and an arm added later is outside those tests. | A Lean model under `experiments/lean-guard/`, with a differential harness as `lean-blob-gc` has |
