# Verified paths

Each path below deletes data or withholds it from a caller. Each has a Lean model of its rule and a
differential harness, which runs the real code and the model on the same generated inputs and fails
on any difference. [`CONTRIBUTING.md`](CONTRIBUTING.md#verified-paths) says when a path belongs here.

| path | code | what the model proves | model and harness | CI |
|---|---|---|---|---|
| `substrate blobs gc --unreferenced`, the planner | `gc_plan` in [`substrate/blob_lifecycle.py`](substrate/blob_lifecycle.py) | A plan never removes a blob that a note in any vault it must read names, and it refuses rather than plan when a note is unreadable, a reference is malformed or an inherited vault is missing. | [`experiments/lean-blob-gc/`](experiments/lean-blob-gc/README.md) | `lean`, on PRs that touch `substrate/` or the model |

The model covers planning only. `gc_apply`, which re-plans and removes under a lock, is checked by
inspection rather than proved, and concurrency isn't modelled. The model's README lists every place
it is simpler than the code.
