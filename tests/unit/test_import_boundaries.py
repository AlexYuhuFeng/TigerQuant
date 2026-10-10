"""Import boundaries and write-path allowlists (defence in depth, not convention).

These tests enforce the layering in `docs/COMPONENT_MAP.md` and the single-write-path
rule of ADR-0007 §7 by inspecting the source tree, so a new module that violates the
architecture fails CI rather than a review.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "tigerquant"

#: Layer index; imports may only point at the same or a lower index.
LAYERS: dict[str, int] = {
    "core": 0,
    "events": 0,
    "config": 0,
    "market": 1,
    "evidence": 1,
    "portfolio": 1,
    "risk": 1,
    "decision": 1,
    "strategy": 1,
    "models": 2,
    "execution": 2,
    "ops": 2,
    "adapters": 3,
    "pipeline": 4,
    "dashboard": 4,
}

#: Optional third-party SDKs that must only ever be imported inside their adapter.
OPTIONAL_SDKS = {"tigeropen", "nautilus_trader", "litellm", "fastapi", "uvicorn"}

#: The one module allowed to call a broker write API.
BROKER_WRITE_MODULE = "adapters/tiger/execution.py"

BROKER_WRITE_NAMES = {"place_order", "modify_order", "cancel_order", "submit_order"}


def _modules() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found.append((rel, path))
    return found


def _layer_of(rel: str) -> int | None:
    top = rel.split("/", maxsplit=1)[0]
    if top.endswith(".py"):
        return LAYERS.get("pipeline", 4) if top == "cli.py" else LAYERS.get("core", 0)
    return LAYERS.get(top)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def test_no_upward_layer_imports() -> None:
    violations: list[str] = []
    for rel, path in _modules():
        source_layer = _layer_of(rel)
        if source_layer is None:
            continue
        for module in _imports(path):
            if not module.startswith("tigerquant."):
                continue
            target_top = module.split(".")[1]
            if target_top not in LAYERS:
                continue
            target_layer = LAYERS[target_top]
            if target_layer > source_layer:
                violations.append(f"{rel} (L{source_layer}) imports {module} (L{target_layer})")
    assert not violations, "layering violations:\n" + "\n".join(violations)


def test_optional_sdks_only_imported_inside_adapters() -> None:
    violations: list[str] = []
    for rel, path in _modules():
        for module in _imports(path):
            root = module.split(".")[0]
            if root in OPTIONAL_SDKS and not rel.startswith("adapters/"):
                violations.append(f"{rel} imports {module}")
    assert not violations, "optional SDK imported outside adapters/:\n" + "\n".join(violations)


def test_package_imports_without_optional_dependencies() -> None:
    """Importing the package must not require tigeropen/nautilus/litellm/fastapi."""
    for rel, path in _modules():
        if rel.startswith("adapters/"):
            continue
        text = path.read_text(encoding="utf-8")
        for sdk in OPTIONAL_SDKS:
            # A lazy import inside a function is allowed; a module-level one is not.
            assert f"\nimport {sdk}" not in text, f"{rel} imports {sdk} at module level"


def test_only_one_module_calls_a_broker_write_api() -> None:
    call_sites: list[str] = []
    for rel, path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            # The Nautilus adapter package is excluded because the engine's ExecutionClient
            # CONTRACT requires methods with these names: they are engine callbacks, not venue
            # calls, and the adapter's own gates decide whether anything reaches a broker.
            # Its parity harness drives that adapter, so it necessarily names them too.
            if "/adapters/nautilus/" in f"/{rel}":
                continue
            if name in BROKER_WRITE_NAMES and not rel.endswith(BROKER_WRITE_MODULE):
                call_sites.append(f"{rel}:{name}()")
    assert not call_sites, (
        f"broker write APIs may only be called from {BROKER_WRITE_MODULE}; found: {call_sites}"
    )


def test_decision_and_model_layers_cannot_reach_execution() -> None:
    """The AI decision plane must not be able to import the execution plane."""
    forbidden = {"tigerquant.execution", "tigerquant.adapters"}
    violations: list[str] = []
    for rel, path in _modules():
        top = rel.split("/")[0]
        if top not in {"decision", "strategy", "models", "evidence"}:
            continue
        for module in _imports(path):
            if any(module == item or module.startswith(item + ".") for item in forbidden):
                violations.append(f"{rel} imports {module}")
    assert not violations, "decision plane reached the execution plane:\n" + "\n".join(violations)


def test_no_source_file_contains_a_real_looking_secret() -> None:
    """Scan everything that ships, excluding tests.

    Tests deliberately embed fake credential-shaped strings to prove that redaction
    works; including them here would make the check meaningless. Everything that is part
    of the product — source, configuration, schema, tools, docs and prompts — is scanned.
    """
    from tigerquant.core.redaction import scan_for_secrets

    root = Path(__file__).resolve().parents[2]
    suffixes = {".py", ".yaml", ".yml", ".json", ".md", ".toml", ".cfg", ".ini"}
    scanned: list[Path] = []
    for top in ("src", "config", "schemas", "tools", "docs", "prompts"):
        base = root / top
        if base.is_dir():
            scanned.extend(p for p in base.rglob("*") if p.suffix in suffixes)

    offences: list[str] = []
    for path in scanned:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            hits = scan_for_secrets(line)
            if hits:
                offences.append(f"{path.relative_to(root)}:{lineno} {hits}")
    assert not offences, "credential-shaped material in the tree:\n" + "\n".join(offences)
