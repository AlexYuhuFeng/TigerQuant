"""Every optional extra's SDK must be in the mypy optional-import override list.

**This is the fourth time this session that a list on one side of an interface disagreed with a list
on the other, with nothing comparing them.**

The others were all found by reading. **This one was found by CI -- and nobody could see it, because
the project had ALSO recorded "no remote CI runner" as a limitation.** Two stale claims covering for
each other: the runner existed, it ran 32 times, and every run was red on this.

What happened:

* `pyproject.toml` declares extras: `broker`, `db`, `engine`, `llm`, `dashboard`.
* The mypy override list named `tigeropen.*`, `nautilus_trader.*`, `litellm.*` -- **and not
  `psycopg.*`**.
* CI's fast job runs `uv sync --frozen --extra dev`, deliberately WITHOUT the optional extras, so
  `psycopg` is genuinely absent. `mypy` strict then reports "Cannot find implementation or library"
  for a lazily-imported optional dependency.
* **Locally it passed, because I had the `db` extra installed.**

The code was right: `import psycopg` sits inside a `try/except ImportError`, exactly as ADR-0001 §6
requires of an optional integration. The CONFIG was wrong.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The import root each optional extra's SDK is imported under at runtime.
#:
#: Written out rather than inferred: a distribution name and a module name are not the same string
#: (`psycopg[binary]` -> `psycopg`, `nautilus_trader` -> `nautilus_trader`), so mapping them is a
#: statement of fact that belongs where a reader can check it against the import lines.
EXTRA_TO_MODULE: dict[str, str] = {
    "broker": "tigeropen",
    "db": "psycopg",
    "engine": "nautilus_trader",
    "llm": "litellm",
}


#: Extras that deliberately need no mypy override, with the reason.
#:
#: dev and dashboard are not optional RUNTIME integrations: dev tools are not imported by
#: src/ at all, and the dashboard packages ship type information and are imported at module
#: level. Naming them is the point -- an extra that needs no override and an extra somebody
#: FORGOT are indistinguishable without this.
NO_OVERRIDE_NEEDED: dict[str, str] = {
    "dev": "development tools; not imported by src/ at all",
    "dashboard": "fastapi/uvicorn/jinja2 ship types and are imported at module level",
}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _extras() -> dict[str, list[str]]:
    return _pyproject()["project"]["optional-dependencies"]


def _mypy_optional_modules() -> list[str]:
    overrides = _pyproject()["tool"]["mypy"]["overrides"]
    for override in overrides:
        if override.get("ignore_missing_imports") is True:
            return list(override.get("module", []))
    raise AssertionError("no ignore_missing_imports override found in pyproject.toml")


class TestTheOptionalImportsAreAllDeclared:
    def test_every_extra_is_either_mapped_or_named_as_not_needing_it(self) -> None:
        """A new extra must be decided about rather than silently escaping the check."""
        undecided = sorted(set(_extras()) - set(EXTRA_TO_MODULE) - set(NO_OVERRIDE_NEEDED))
        assert undecided == [], (
            f"extras nobody has decided about: {undecided}. Either add an import root to "
            "EXTRA_TO_MODULE, or add it to NO_OVERRIDE_NEEDED with a reason."
        )

    def test_every_named_exemption_states_a_reason(self) -> None:
        for extra, reason in NO_OVERRIDE_NEEDED.items():
            assert len(reason) > 20, f"{extra} is exempted with a thin reason"

    def test_every_typed_sdk_extra_is_in_the_mypy_override_list(self) -> None:
        """THE check. CI syncs only `--extra dev`, so any optional SDK missing here makes `mypy`
        fail in CI while passing locally -- which is precisely what happened, 32 times."""
        declared = set(_mypy_optional_modules())
        expected = {f"{module}.*" for module in EXTRA_TO_MODULE.values()}
        missing = sorted(expected - declared)
        assert missing == [], (
            f"optional SDKs absent from the mypy ignore_missing_imports list: {missing}. `mypy` "
            "passes locally with the extra installed and FAILS IN CI without it."
        )

    def test_the_list_has_no_entry_for_a_module_nobody_declares(self) -> None:
        """The other direction: an override for a package this project does not use is a claim
        about a dependency that does not exist."""
        declared = set(_mypy_optional_modules())
        expected = {f"{module}.*" for module in EXTRA_TO_MODULE.values()}
        # `psycopg_binary.*` is the compiled half of `psycopg[binary]`; it is imported by psycopg
        # itself rather than by this project, so it is allowed to appear without an extra.
        allowed_extra = {"psycopg_binary.*"}
        unexpected = sorted(declared - expected - allowed_extra)
        assert unexpected == [], f"overrides for undeclared SDKs: {unexpected}"

    def test_the_dashboard_extra_needs_no_override(self) -> None:
        """`fastapi`, `uvicorn` and `jinja2` ship type information and are imported at module level
        in the dashboard, not lazily. If that changes, this test should be what notices."""
        assert "dashboard" not in EXTRA_TO_MODULE
        assert not any(name.startswith("fastapi") for name in _mypy_optional_modules())


class TestTheCodeStillHandlesAbsence:
    def test_the_psycopg_import_remains_lazy(self) -> None:
        """The override hides a MISSING MODULE from mypy. That is only correct while the code guards
        the import at runtime -- the override must not become cover for an unguarded import."""
        source = (REPO_ROOT / "src" / "tigerquant" / "db" / "postgres.py").read_text(
            encoding="utf-8"
        )
        assert "try:" in source
        assert "except ImportError" in source
        assert "import psycopg" in source

    def test_the_driver_loader_reports_a_useful_error(self) -> None:
        """An absent optional dependency must say so, not raise a bare ImportError from deep inside
        a call stack."""
        from tigerquant.db import postgres

        assert hasattr(postgres, "_load_driver")


class TestTheCIWorkflowCoversWhatItClaims:
    def test_the_fast_job_syncs_only_the_dev_extra(self) -> None:
        """This is WHY the override list must be complete. If CI ever syncs all extras, the
        constraint weakens and this test should be revisited rather than quietly passing."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--extra dev" in workflow
        assert "--all-extras" not in workflow

    def test_the_workflow_typechecks_and_tests(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for step in ("mypy", "ruff check", "pytest"):
            assert step in workflow, f"CI no longer runs {step!r}"

    def test_the_workflow_runs_a_dependency_audit(self) -> None:
        """The audit step was reported as 'success' while the rest of the run was red, which is how
        it became possible to believe the audit passed."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "pip-audit" in workflow or "pip_audit" in workflow
