"""The engine's three tiers: the core, and the `convert` and `convert-ml` extras.

The core reads Markdown, text and a PDF's text layer, and must run with only what it declares. Before
the extras existed, `numpy` was imported at module level and declared nowhere: it arrived through
docling, so the first install without docling would have failed on `import substrate.store`. These
read the source, `pyproject.toml`, `uv.lock` and the Dependabot config; nothing here needs an extra.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "substrate"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
PROJECT = PYPROJECT["project"]["name"]
LOCK = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))

# The core's import names, and the distribution that provides each.
CORE = {"pypdfium2": "pypdfium2", "yaml": "pyyaml", "numpy": "numpy"}
# What the extras provide that the engine imports itself.
EXTRAS = {"docling", "docling_core", "openpyxl", "bs4"}
# What `convert` must not install: the model stack, which is `convert-ml`'s.
MODEL_STACK = {"torch", "torchvision", "transformers", "opencv-python-headless", "ocrmac"}


def _name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower()


def _imports(tree: ast.Module, *, module_level: bool):
    """(line, top-level name) for each absolute import, at module level or anywhere."""
    def walk(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not module_level:
                    yield from walk(node.body)
                continue
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                yield from walk(node.orelse)  # the branch that runs
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield node.lineno, alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.lineno, node.module.split(".")[0]
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                yield from walk(getattr(node, field, []) or [])
    yield from walk(tree.body)


def _third_party(*, module_level: bool) -> dict[str, list[str]]:
    """Each third-party name the package imports, with where."""
    found: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), str(path))
        for line, name in _imports(tree, module_level=module_level):
            if name == "substrate" or name in sys.stdlib_module_names:
                continue
            found.setdefault(name, []).append(f"{path.relative_to(ROOT)}:{line}")
    return found


def _closure(*extras: str) -> set[str]:
    """Every package the lock can install for the project with these extras, whatever the platform."""
    packages = {p["name"]: p for p in LOCK["package"]}
    assert len(packages) == len(LOCK["package"]), "the lock forks a package; walk every version"
    seen: set[tuple[str, str | None]] = set()
    todo = [(PROJECT, extra) for extra in (None, *extras)]
    while todo:
        name, extra = todo.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        package = packages[name]
        deps = package.get("dependencies", []) if extra is None else \
            package.get("optional-dependencies", {}).get(extra, [])
        for dep in deps:
            todo += [(dep["name"], None)] + [(dep["name"], e) for e in dep.get("extra", [])]
    return {name for name, _ in seen} - {PROJECT}


def test_the_core_is_three_packages() -> None:
    assert {_name(r) for r in PYPROJECT["project"]["dependencies"]} == set(CORE.values())
    assert _closure() == set(CORE.values())


def test_every_module_level_import_is_declared_by_the_core() -> None:
    """So importing any engine module needs only what the core declares, as `numpy` once did not."""
    declared = {_name(r) for r in PYPROJECT["project"]["dependencies"]}
    stray = {name: where[0] for name, where in _third_party(module_level=True).items()
             if name not in CORE or CORE[name] not in declared}
    assert not stray, f"imported at module level and not declared by the core: {stray}"


def test_every_import_is_one_a_tier_provides() -> None:
    stray = {name: where for name, where in _third_party(module_level=False).items()
             if name not in CORE.keys() | EXTRAS}
    assert not stray, f"imports that neither the core nor an extra provides: {stray}"


def test_convert_installs_no_model_stack_and_convert_ml_holds_all_of_convert() -> None:
    convert, convert_ml = _closure("convert"), _closure("convert-ml")
    assert "docling-slim" in convert
    assert not convert & MODEL_STACK, convert & MODEL_STACK
    assert convert <= convert_ml and MODEL_STACK <= convert_ml, (convert - convert_ml,
                                                                 MODEL_STACK - convert_ml)


def _foreign_self_references(project: str, requirements: list[str]) -> list[str]:
    """The requirements that name this project under a name it does not have."""
    def norm(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()
    return [r for r in requirements
            if norm(_name(r)) in {"substrate", "substrate-engine"} and norm(_name(r)) != norm(project)]


def test_an_extra_that_names_the_project_names_it_as_it_is_called() -> None:
    """`convert-ml` asks for `substrate[convert]`, which is this project only while it is called
    `substrate`. Published as `substrate-engine`, the same line asks PyPI for another project's
    package, so the rename must carry it along."""
    requirements = [*PYPROJECT["project"]["dependencies"],
                    *(r for extra in PYPROJECT["project"]["optional-dependencies"].values() for r in extra)]
    assert not _foreign_self_references(PROJECT, requirements)
    assert _foreign_self_references("substrate-engine", requirements) == [
        r for r in requirements if _name(r) == "substrate"] != []


def test_the_lock_no_longer_holds_the_docling_meta_package() -> None:
    """`docling` is a meta-package over `docling-slim[standard]`, and `standard` is what brought
    RapidOCR, the chunking stack and the rest of what the engine never runs."""
    assert "docling" not in {p["name"] for p in LOCK["package"]}


def test_nothing_asks_for_the_docling_meta_package_version() -> None:
    """No extra installs it, so the lookup raises: in the extractor string it refused a conversion
    that had worked, and in `engine docling`'s import check it would fail that job before its tests."""
    asks = re.compile(r"""version\(\s*['"]docling['"]\s*\)""")
    paths = [*PACKAGE.rglob("*.py"), *ROOT.glob("*.py"), *(ROOT / "tools").iterdir(),
             *(ROOT / ".github").rglob("*.yml")]
    hits = [f"{p.relative_to(ROOT)}" for p in paths if p.is_file() and asks.search(p.read_text("utf-8", errors="replace"))]
    assert not hits, f"these read the `docling` distribution, which no extra installs: {hits}"


def test_dependabot_groups_one_per_tier() -> None:
    """Each tier's group is exactly what that tier adds to the lock, by name, so a bump to any package
    the tier installs is reviewed with it; the catch-all is last, because a package goes to the
    first group that matches it."""
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    (uv,) = [u for u in config["updates"] if u["package-ecosystem"] == "uv"]
    groups = {name: group["patterns"] for name, group in uv["groups"].items()}
    assert list(groups) == ["core", "convert", "convert-ml", "other"]
    assert groups["other"] == ["*"]
    named = [p for name in ("core", "convert", "convert-ml") for p in groups[name]]
    assert len(named) == len(set(named)) and not any("*" in p for p in named), named
    assert set(groups["core"]) == _closure() == set(CORE.values())
    assert set(groups["convert"]) == _closure("convert") - _closure()
    assert set(groups["convert-ml"]) == _closure("convert-ml") - _closure("convert")
