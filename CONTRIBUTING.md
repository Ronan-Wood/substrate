# Contributing

Thanks for helping. This page covers how to set up, how to check a change, and the rules a change
follows to land. For something to work on, see [`ROADMAP.md`](ROADMAP.md), which lists the work in the order we
expect to take it, or the issues labelled
[`good first issue`](https://github.com/Ronan-Wood/substrate/labels/good%20first%20issue). Comment on an
issue before you start on it, so two people don't build the same thing.

**Security problems don't go in public issues.** Report them as [`SECURITY.md`](SECURITY.md)
describes.

## Set up

```sh
git clone https://github.com/Ronan-Wood/substrate
cd substrate
uv sync
```

`uv sync` installs the locked environment, Python 3.14 included. The first run is slow, because
OpenCV builds from source. The README's
[quickstart](README.md#quickstart-the-demo-vaults-in-30-seconds) runs the engine against the demo
vaults, with its registry and index in `.demo/` rather than in your `~/.substrate`.

## Check a change

```sh
uv run python -m pytest tests -q
./lint.sh
```

- `lint.sh` runs ruff with bug-catching rules only. Formatting isn't checked. It isn't a CI check
  yet, and `main` still has some old findings, so make sure it reports nothing new in the files you
  touched.
- The docling round-trips are opt-in: set `SUBSTRATE_DOCLING_TESTS=1` to run them.
- Some tests run only on macOS. CI runs them in the `engine docling` job.

| check | runs | required |
|---|---|---|
| `engine` (`tests` workflow, Ubuntu) | every push to `main` and every PR | **yes** |
| `engine docling` (macOS, the locked environment minus OpenCV, no skips allowed) | PRs, unless they change only Markdown at the root, `docs/`, `experiments/`, the licence or the other CI files | no |
| `lean` (Ubuntu, the models of the [verified paths](#verified-paths) against the real code) | PRs that touch `substrate/`, `pyproject.toml`, `uv.lock`, a model or its workflow | no |
| `codeql python` (`code scanning` workflow) | pushes to `main`, PRs, and weekly | no |

## Issue labels

| label | means |
|---|---|
| `needs-decision` | waits on a maintainer's choice, which the issue names. Please don't start it until the choice is recorded on the issue |
| `pip-blocker` | stops `pip install substrate-engine` working on an ordinary vault. These come first |
| `good first issue` | small, and needs no models and no special hardware |
| `needs-models` | needs a local model server and models to finish. The unit tests usually don't |
| `needs-quiet-machine` | needs a latency run with only the model server busy |
| `experiment` | states its success criterion first and reports in `EXPERIMENTS.md` either way |
| `verified-path` | a Lean model plus a differential harness, as [`VERIFIED.md`](VERIFIED.md) describes |

## The rules

1. **`main` takes signed commits on a linear history, through squash merges only.** Every change
   lands as a PR, and the PR lands as one commit. GitHub signs the squash commit it makes, so the
   commits on your branch don't need to be signed. Rebase merges and merge commits are turned off.
   `engine` must pass on a branch that is up to date with `main`.
2. **Every fix ships a regression test that fails on the old code**, the way the bug did. Check it
   yourself: revert the fix, watch the test fail, then restore the fix. A test that passes either
   way proves nothing.
3. **The contract changes engine first.** `contract/contract.json` and `contract/frames/` are
   generated from the code, and the suite fails if the committed files differ from a fresh export.
   A change that alters what a client sees lands and is released here first, and clients, Scripta
   included, follow by bumping the release they pin. Regenerate the files in the same PR, and say
   whether the change is additive or breaking. [`docs/client-contract.md`](docs/client-contract.md)
   defines both and says which wire version each one bumps.
4. **Only synthetic demo data goes in fixtures.** Fixtures, examples, docs and commit messages use
   the demo vaults, `vaults/demo-vault` and `vaults/demo-core-vault`, or data a test generates.
   Never use real notes, names, queries, note titles, eval cases from a private corpus, or paths
   under a home folder. The contract tests scan the generated files for host paths, but don't rely
   on that scan to catch everything.

## Verified paths

A path that deletes data or withholds it from a caller gets a formal model and a differential
harness: a Lean model of the rule, and a test that runs the real code and the model on the same
generated inputs and fails on any difference. [`VERIFIED.md`](VERIFIED.md) lists each modeled path
with its model and harness, and the `lean` job runs them. A change to a listed path keeps its
harness green. A new path that deletes or withholds data either adds a model or says in the PR why
it doesn't need one.

## Style

- **Commit and PR titles** follow the repo's convention: `fix: …`, `feat: …`, `docs: …`, `test: …`,
  `chore: …` or `ci: …`, with an optional area in parentheses, such as `fix(frontmatter): …`.
  The title says what is now true.
- **Branch names** use the same prefixes: `fix/…`, `feat/…`, `docs/…`.
- **Docs and notes** follow the writing standard in
  [`vaults/demo-core-vault/00-operator/WRITING.md`](vaults/demo-core-vault/00-operator/WRITING.md):
  lead with the conclusion, use tables for anything enumerable, and attach conditions to every
  number.
- **Code comments** explain why, not what. The engine refuses rather than misleads, and a change
  should keep it that way: see [`docs/principles.md`](docs/principles.md).

## Experiments

An experiment states its success criterion before it runs. It gets its own folder,
`experiments/<name>/`, and reports its result in [`EXPERIMENTS.md`](EXPERIMENTS.md), whether it
passed or failed.

- Report aggregates only.
- A latency number needs a quiet machine, with only the model server busy. Say so when yours wasn't
  quiet.

## Licence

The engine is GPL-3.0-or-later. See [`LICENSE`](LICENSE).
