## What this changes

<!-- What is true after this PR that wasn't before, and why. Link the issue it closes. -->

## Contract

<!-- Delete the lines that don't apply. -->
- No change to what a client sees.
- **Additive**, as `docs/client-contract.md` defines it. `contract/` is regenerated in this PR, and the wire version is bumped if that page says to.
- **Breaking**, as `docs/client-contract.md` defines it. `contract/` is regenerated in this PR, and the wire versions are bumped as that page says.

## Tests

- [ ] `uv run python -m pytest tests -q` passes. Count: <!-- e.g. 2074 passed, 2 skipped -->
- [ ] `./lint.sh` reports nothing new in the files this PR touches.
- [ ] A fix has a regression test, and I watched it fail with the fix reverted.
- [ ] A change to a path listed in `VERIFIED.md` keeps its harness green, and a new path that deletes or withholds data has a model or a stated reason it doesn't need one.
- [ ] Fixtures, examples and docs use only the demo vaults or generated data.

## Known follow-ups

<!-- Anything this PR deliberately leaves for later. -->
