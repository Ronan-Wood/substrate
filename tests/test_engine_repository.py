"""This tree is a repository of its own: what one needs at its root, and its staged `.github`.

`.github/` here is inert while the tree is `substrate/` inside Scripta, because GitHub reads only a
repository's root `.github`. It goes live when the engine is extracted, so it must already be
written for this tree at the root: nothing reaching into the app, and the required `engine` check
never behind a path filter, since a required check that never reports blocks every merge.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    # YAML 1.1 reads the bare key `on` as True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_the_licence_is_the_gpl_the_package_declares() -> None:
    text = (ROOT / "LICENSE").read_text()
    assert text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in text[:200]
    assert re.search(r'^license = "GPL-3.0-or-later"$', (ROOT / "pyproject.toml").read_text(), re.M)


def test_the_public_documents_are_there_and_name_no_ones_machine() -> None:
    for name in ("README.md", "SECURITY.md", "RELEASING.md"):
        # `/Users/Shared` is no one's home, and the runbook names the interpreter's folder there.
        text = re.sub(r"/Users/Shared(?![\w-])", "", (ROOT / name).read_text())
        assert "/Users/" not in text and "~/vaults" not in text, name
    assert "security/advisories/new" in (ROOT / "SECURITY.md").read_text()


def _strings(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif node is not None:
        yield str(node)


def test_the_staged_workflows_are_written_for_this_tree_at_the_root() -> None:
    """What they run, not what their comments say about the monorepo."""
    assert sorted(p.name for p in WORKFLOWS.glob("*.y*ml")) == ["codeql.yml", "docling.yml", "lean.yml",
                                                             "tests.yml"]
    for path in sorted(WORKFLOWS.glob("*.yml")):
        ran = "\n".join(_strings(_workflow(path.name)))
        for reach in ("engine-alone", "working-directory", "Core/", "project.yml", "Distribution/"):
            assert reach not in ran, f"{path.name} reaches outside this tree: {reach}"


def test_no_staged_workflow_runs_on_a_trigger_that_hands_out_secrets() -> None:
    """Once public, `pull_request_target`, `workflow_run` and `workflow_call` run with this
    repository's token on a fork's request; none of these jobs needs them."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert set(_workflow(path.name)["on"]) <= {"push", "pull_request", "workflow_dispatch", "schedule"}, path.name


def test_the_required_engine_check_runs_on_every_change() -> None:
    tests = _workflow("tests.yml")
    assert [job["name"] for job in tests["jobs"].values()] == ["engine", "engine convert"]
    for event in ("push", "pull_request"):
        trigger = tests["on"][event] or {}
        assert "paths" not in trigger and "paths-ignore" not in trigger, event


def _glob(pattern: str) -> re.Pattern:
    """A workflow path filter as GitHub reads it: `*` stops at `/`, `**` does not."""
    out = ""
    for part in re.split(r"(\*\*/?|\*)", pattern):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*"}.get(part, re.escape(part))
    return re.compile(out)


def test_engine_docling_skips_only_a_change_to_documents() -> None:
    """The only job where the darwin-only and docling-gated tests run, so it skips nothing they
    could read: no ignore pattern may match the package, the tests, the tools, the fixtures, the
    contract, the licences the bundle ships, or the lock."""
    docling = _workflow("docling.yml")
    assert [job["name"] for job in docling["jobs"].values()] == ["engine docling"]
    assert docling["jobs"]["docling"]["runs-on"].startswith("macos-")
    assert set(docling["on"]) == {"pull_request", "workflow_dispatch"}
    assert "paths" not in docling["on"]["pull_request"]
    ignored = [_glob(p) for p in docling["on"]["pull_request"]["paths-ignore"]]
    # From the tree, not git: this runs from a release's source tarball too.
    read = [p.relative_to(ROOT).as_posix() for top in ("substrate", "tests", "tools", "vaults",
            "contract", "third_party_licenses") for p in (ROOT / top).rglob("*") if p.is_file()]
    read += ["pyproject.toml", "uv.lock"]
    assert len(read) > 100
    for pattern in ignored:
        hit = [f for f in read if pattern.fullmatch(f)]
        assert not hit, f"{pattern.pattern} would skip a change to {hit[:3]}"


def test_dependabot_covers_this_tree_at_the_root() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    covered = {(u["package-ecosystem"], u["directory"]) for u in config["updates"]}
    assert covered == {("github-actions", "/"), ("uv", "/")}


def _runs(workflow: str, job: str) -> str:
    return "\n".join(step.get("run", "") for step in _workflow(workflow)["jobs"][job]["steps"])


def test_the_engine_check_installs_the_core_and_nothing_else() -> None:
    """The required check is the core job: what the lock holds for the core, no extra, no package
    named by hand, and every skip named."""
    runs = _runs("tests.yml", "engine")
    assert "uv export --frozen --no-dev --no-emit-project -o" in runs
    assert "--extra" not in runs and "--all-extras" not in runs and "uv sync" not in runs
    installs = [line for line in runs.splitlines() if "uv pip install" in line]
    assert installs and all("--require-hashes -r" in line for line in installs), installs
    assert "tools/check-skips.py" in runs and "tests/core-job-skips.txt" in runs
    assert (ROOT / "tests" / "core-job-skips.txt").is_file()


def test_engine_docling_runs_both_extras_with_no_skip() -> None:
    runs = _runs("docling.yml", "docling")
    assert "--extra convert --extra convert-ml" in runs
    assert "! grep -q '<skipped'" in runs


def test_engine_convert_installs_convert_alone_and_runs_the_round_trips() -> None:
    """The `convert` extra and nothing of `convert-ml`, from the lock with hashes, the round-trips
    opted in, and its skips held to its own list."""
    job = _workflow("tests.yml")["jobs"]["engine-convert"]
    runs = _runs("tests.yml", "engine-convert")
    assert job["name"] == "engine convert"
    assert job["env"]["SUBSTRATE_DOCLING_TESTS"] == "1"
    assert "uv export --frozen --no-dev --no-emit-project --extra convert -o" in runs
    assert "convert-ml" not in runs and "--all-extras" not in runs and "uv sync" not in runs
    installs = [line for line in runs.splitlines() if "uv pip install" in line]
    assert installs and all("--require-hashes -r" in line for line in installs), installs
    assert "import importlib.util as u, docling.document_converter" in runs
    assert all(m in runs for m in ("'torch'", "'transformers'", "'cv2'", "'ocrmac'", "'docling_parse'"))
    assert "tools/check-skips.py" in runs and "tests/convert-job-skips.txt" in runs
    assert (ROOT / "tests" / "convert-job-skips.txt").is_file()
    setup = [s for s in job["steps"] if "setup-uv" in s.get("uses", "")]
    assert setup and setup[0]["with"]["version"] == "0.12.2"


def test_engine_convert_builds_its_sdists_under_the_cutoff_and_nothing_else() -> None:
    """An sdist is built with tools pinned by version alone, so its install takes `docling.yml`'s
    cutoff. `uv pip` resolves every pin under it, so it is on that step only, and every package the
    step installs must predate it, or the job installs nothing."""
    import tomllib

    job = _workflow("tests.yml")["jobs"]["engine-convert"]
    docling = _workflow("docling.yml")["jobs"].values()
    (cutoff,) = {j["env"]["UV_EXCLUDE_NEWER"] for j in docling if "UV_EXCLUDE_NEWER" in j.get("env", {})}
    assert "UV_EXCLUDE_NEWER" not in job.get("env", {})
    (step,) = [s for s in job["steps"] if "UV_EXCLUDE_NEWER" in s.get("env", {})]
    assert step["env"]["UV_EXCLUDE_NEWER"] == cutoff
    assert step["run"] == 'uv pip install --require-hashes -r "$RUNNER_TEMP/sdist.txt"'
    taken = set(re.findall(r"/\^([\w.-]+)==/", _runs("tests.yml", "engine-convert")))
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))["package"]
    spec = importlib.util.spec_from_file_location("_tiers", ROOT / "tests" / "test_dependency_tiers.py")
    tiers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tiers)
    convert = tiers._closure("convert")
    sdists = {p["name"]: p["sdist"]["upload-time"] for p in lock
              if p["name"] in convert and "sdist" in p and not p.get("wheels")}
    assert taken == set(sdists), (taken, sdists)
    assert all(t < cutoff for t in sdists.values()), sdists


def test_the_convert_job_skips_nothing_the_core_job_runs() -> None:
    """`engine convert` installs everything `engine` does and more, so a test it skips that the core
    job runs is one the extra broke."""
    spec = importlib.util.spec_from_file_location("_check_skips", ROOT / "tools" / "check-skips.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    convert, core = (tool.expected(ROOT / "tests" / f) for f in ("convert-job-skips.txt",
                                                                 "core-job-skips.txt"))
    assert convert <= core, convert - core


# `python … -c "…"` or `'…'`, and `python … - <<EOF` heredocs, any interpreter name and flags.
_INLINE = re.compile(r"""python[\d.]*(?:\s+-[^c\s]\S*(?:\s+[^-\s]\S*)?)*?\s+-c\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""",
                     re.S)
_HEREDOC = re.compile(r"""python[\d.]*[^\n]*?\s-\s*<<-?\s*['"]?(\w+)['"]?[^\n]*\n(.*?)\n\s*\1\s*$""",
                      re.S | re.M)
# The engine named as Python names it: what is left of a step once the forms above are taken out
# must not say this, or Python the scan cannot read imports the engine unchecked.
_ENGINE = re.compile(r"(?:\b(?:import|from)\s+|-m\s+)substrate\b|\bsubstrate\.\w")


def _inline_python(run: str):
    for double, single in _INLINE.findall(run):
        # Inside double quotes the shell drops a backslash-newline and undoes these escapes.
        yield re.sub(r'\\([\\"$`])', r"\1", double.replace("\\\n", "")) if double else single
    for _tag, body in _HEREDOC.findall(run):
        yield body


def _engine_imports(run: str) -> tuple[list[str], list[str]]:
    """The engine modules one `run:` step's Python imports, and what is wrong: a module that will
    not import, a name it lacks, Python that will not parse, or the engine named anywhere the scan
    cannot read. Fails closed: what it cannot read is a problem, never a pass."""
    found, problems = [], []
    if hit := _ENGINE.search(_HEREDOC.sub(" ", _INLINE.sub(" ", run))):
        problems.append(f"names the engine where this test cannot read it: {hit.group(0)!r}")
    for code in _inline_python(run):
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            problems.append(f"Python this test cannot parse ({e.msg}): {code[:60]!r}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("substrate"):
                names = [(node.module, a.name) for a in node.names]
            elif isinstance(node, ast.Import):
                names = [(a.name, None) for a in node.names if a.name.startswith("substrate")]
            else:
                continue
            for module, name in names:
                found.append(module)
                try:
                    loaded = importlib.import_module(module)
                except ImportError as e:
                    problems.append(f"{module} ({e})")
                    continue
                # A name can be a submodule, which only a package has.
                if name is not None and not hasattr(loaded, name) and not (
                        hasattr(loaded, "__path__") and importlib.util.find_spec(f"{module}.{name}")):
                    problems.append(f"{module}.{name}")
    return found, problems


def test_every_engine_name_a_workflow_step_imports_exists() -> None:
    """A step's inline Python is outside the suite: `docling_version` moved out of `docling_arm`,
    every test passed, and `engine docling` failed on its import check. Imported rather than read:
    `test_dependency_tiers` holds every engine module importable with the core alone, so this
    runs in `engine` with nothing else."""
    found, problems = [], []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for job, spec in _workflow(path.name)["jobs"].items():
            for i, step in enumerate(spec.get("steps", [])):
                imports, wrong = _engine_imports(step.get("run") or "")
                found += [f"{path.name}: {m}" for m in imports]
                problems += [f"{path.name} {job} step {i}: {w}" for w in wrong]
    # The scan must see the step it exists for, or a pattern that matches nothing passes.
    assert any(f.startswith("docling.yml: substrate.extract") for f in found), found
    assert not problems, problems


def test_the_workflow_scan_fails_closed() -> None:
    ok = 'uv run --no-sync python -c "import os;\\\n from substrate.extract.convert import docling_version"'
    assert _engine_imports(ok) == (["substrate.extract.convert"], [])
    assert _engine_imports("python - <<'EOF'\nfrom substrate import cli\nEOF\n")[1] == []
    assert _engine_imports('python -c "from substrate.extract.convert import nope"')[1] == [
        "substrate.extract.convert.nope"]
    assert "cannot parse" in _engine_imports('python -c "from substrate import ("')[1][0]
    for unread in ('python -Bc "from substrate import cli"', "python <<EOF\nimport substrate\nEOF",
                   "uv run python -m substrate.cli version"):
        assert "cannot read" in _engine_imports(unread)[1][0], unread
    for none in ('uv run --no-sync python tools/check-skips.py "$RUNNER_TEMP/junit.xml" x.txt',
                 "uv run --no-sync python -m pytest tests -q", "ruff check substrate/ tests/"):
        assert _engine_imports(none) == ([], []), none
