# Verified paths

Each path in the first table deletes data or withholds it from a caller. Each has a Lean model of
its rule and a differential harness, which runs the real code and the model on the same generated
inputs and fails on any difference. [`CONTRIBUTING.md`](CONTRIBUTING.md#verified-paths) says when a path belongs here.

| path | code | what the model proves | model and harness | CI |
|---|---|---|---|---|
| `substrate blobs gc --unreferenced`, the planner | `gc_plan` in [`substrate/blob_lifecycle.py`](substrate/blob_lifecycle.py) | A plan never removes a blob that a note in any vault it must read names, and it refuses rather than plan when a note is unreadable, a reference is malformed or an inherited vault is missing. | [`experiments/lean-blob-gc/`](experiments/lean-blob-gc/README.md) | `lean`, on PRs that touch `substrate/` or the model |
| `substrate compose`, which notes it quarantines and which copies it leaves out | `decide` and `ceiling_for` in [`substrate/quarantine.py`](substrate/quarantine.py), the copies `resolve_scope` leaves out in [`substrate/vault.py`](substrate/vault.py), and the compose that applies them | A note that failed is never indexed, and the scope is refused exactly when it has a fault that is not one note's own, more notes failed than the ceiling allows, or none was left to index. Of several notes deriving one `doc_id` from the same bytes, exactly the first by path is decided and the rest are copies, neither indexed nor quarantined. So every note that passed is indexed unless the scope is refused, or it is a copy and a note composed in its place (indexed, or quarantined) derives its `doc_id`: since #71 that is the statement, where it was "every note that passed is indexed". That the note in its place is indexed is proved only under an assumption the code makes likely and does not guarantee, that a copy fails exactly when that note does. The harness runs the real compose, so a quarantined note's earlier version staying indexed, a copy indexed or left unnamed, a copy's note said to be indexed when it was quarantined, or a refusal that removes an ingest folder or reports a note, is a disagreement. | [`experiments/lean-quarantine/`](experiments/lean-quarantine/README.md) | `lean`, on PRs that touch `substrate/` or the model |

The blob-gc model covers planning only. `gc_apply`, which re-plans and removes under a lock, is checked by
inspection rather than proved, and concurrency isn't modelled. Each model's README lists every place
it is simpler than the code.

## Planned

These paths withhold or delete data and have no model yet. Each names the reason it ships without
one and the experiment that will add it.

| path | code | what the model will prove | why not yet | follow-up |
|---|---|---|---|---|
| Converted files withheld after the worker fails on them | `quarantine_decision` in [`substrate/extract/conversion_quarantine.py`](substrate/extract/conversion_quarantine.py), and the driver `through_worker` in [`substrate/extract/convert.py`](substrate/extract/convert.py) that applies it | A file is never both converted and quarantined. A fault (a hang, a crash, an untrustworthy frame) never yields a conversion. Quarantine follows exactly two faults in a row, and docling's own answer (a refusal, a missing extra, an unknown format) or a file that changed is never retried and never quarantines. A matching record stops the file before any attempt unless `--retry-quarantined` is given, another converter's record never does, and only a conversion that went through removes one. | Until the model lands, [`tests/test_convert_worker.py`](tests/test_convert_worker.py) checks the decision on every outcome sequence a driver produces, and the driver against scripted workers. It withholds one file from one ingest, loudly, with the way out named. | A Lean model under `experiments/lean-convert-quarantine/` with a differential harness over the real driver, in the PR after #25's worker |
| Calls left out of an agent snapshot | `_publish`, `_fill`, `_withdraw_if_sources_revoked` and `withdraw_dropped` in [`substrate/agent.py`](substrate/agent.py) | Without `include_sources`, no row, page or rowid of a conversation-class document reaches the published file, and no copy that may hold one is served after the entry stops letting calls in, is deleted, or cannot be read. | Until the model lands, [`tests/test_agent_mode.py`](tests/test_agent_mode.py) checks the published bytes, every table and the rowids against one fixture holding a call in each place a snapshot carries it. | A Lean model under `experiments/lean-agent-snapshot/`, with a differential harness as `lean-blob-gc` has |
