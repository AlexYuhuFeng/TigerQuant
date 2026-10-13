"""A hand-built `Namespace` fixture must mirror the flags the command reads.

**THIS CAUSED TWO OF THE THREE CI FAILURES ON THE HANG-DETECTION WORK.** `cmd_supervise` began reading
`args.hang_after`, and five tests failed with `AttributeError` because their fixture builds a
`Namespace` by hand. Nothing compared the two definitions of the CLI's surface -- the parser's and the
fixture's -- so the fixture silently became stale.

I then fixed that fixture and **hit a real bug behind it**: `args.heartbeat` is legitimately `None`, and
the new code called `read_heartbeat(None)`. That one was the product, not a fixture, and it is covered
in `test_hang_reporting.py`.

The commit that fixed the fixture said the detection mechanism was "effective, late, and worth
replacing with a test that compares the two sets." This is that test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = (
    (
        "cmd_supervise",
        ROOT / "src/tigerquant/ops/supervisor_cli.py",
        ROOT / "tests/unit/test_supervise_cli.py",
    ),
)


def attributes_read(source: Path, function: str) -> set[str]:
    """Every `args.<name>` a function reads, EXCLUDING anything inside a nested function.

    The nesting matters: the parser and the handler live in the same module, and a nested helper may
    read a differently-named argument object. Reading only the target function's own body keeps this
    about the command's surface rather than about every `args.` in the file.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        ),
        None,
    )
    if target is None:
        raise AssertionError(f"{function} not found in {source.name}")

    found: set[str] = set()
    for node in ast.walk(target):
        # Do not descend into nested definitions.
        if isinstance(node, ast.FunctionDef) and node.name != function:
            continue
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
        ):
            found.add(node.attr)
    return found


def fixture_keys(source: Path) -> set[str]:
    """Keys of the `base` dict in the test file's `ns()` helper."""
    text = source.read_text(encoding="utf-8")
    start = text.index("base: dict")
    end = text.index("base.update", start)
    return set(re.findall(r'"(\w+)":', text[start:end]))


class TestTheFixtureMirrorsTheCommand:
    def test_both_sides_were_parsed(self) -> None:
        """Without this, a broken extractor makes every assertion below vacuous -- the failure mode
        this project has hit repeatedly."""
        for function, source, tests in COMMANDS:
            assert attributes_read(source, function), f"no `args.` reads found in {function}"
            assert fixture_keys(tests), f"no fixture keys found in {tests.name}"

    def test_the_fixture_provides_every_attribute_the_command_reads(self) -> None:
        """THE check. A flag added to the parser but not the fixture breaks tests in CI, late and
        indirectly, which is exactly what happened."""
        for function, source, tests in COMMANDS:
            read = attributes_read(source, function)
            provided = fixture_keys(tests)
            missing = sorted(read - provided)
            assert missing == [], (
                f"{tests.name} does not provide {missing}, which {function} reads. A hand-built "
                "Namespace is a second definition of the same surface and nothing keeps them in sync."
            )

    def test_the_fixture_does_not_provide_attributes_nobody_reads(self) -> None:
        """The other direction. An entry nothing reads is a flag that was removed and left behind,
        which makes the fixture look like it covers more than it does."""
        for function, source, tests in COMMANDS:
            read = attributes_read(source, function)
            provided = fixture_keys(tests)
            extra = sorted(provided - read)
            assert extra == [], (
                f"{tests.name} provides {extra}, which {function} does not read. Either the flag was "
                "removed, or the fixture is describing a surface that does not exist."
            )

    def test_the_named_surface_is_not_empty(self) -> None:
        """A guard on the size, so a refactor that emptied either side fails here rather than making
        the two comparisons above trivially true."""
        for function, source, tests in COMMANDS:
            assert len(attributes_read(source, function)) >= 5
            assert len(fixture_keys(tests)) >= 5

    def test_every_command_under_check_still_exists(self) -> None:
        for function, source, _ in COMMANDS:
            assert source.is_file(), f"{source} is gone, so {function} is not checked"


class TestTheExtractorItself:
    """The extractor is the check. If it silently returns nothing, everything above passes."""

    def test_it_finds_attributes_in_a_known_function(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.py"
        sample.write_text(
            "def cmd(args):\n    print(args.alpha)\n    print(args.beta)\n",
            encoding="utf-8",
        )
        assert attributes_read(sample, "cmd") == {"alpha", "beta"}

    def test_it_ignores_attributes_on_other_objects(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.py"
        sample.write_text(
            "def cmd(args):\n"
            "    print(args.real)\n"
            "    print(other.ignored)\n"
            "    print(self.also_ignored)\n",
            encoding="utf-8",
        )
        assert attributes_read(sample, "cmd") == {"real"}

    def test_it_does_not_descend_into_nested_functions(self, tmp_path: Path) -> None:
        """A nested helper may read a different argument object, and counting it would make this
        about every `args.` in the file instead of about the command's surface."""
        sample = tmp_path / "sample.py"
        sample.write_text(
            "def cmd(args):\n"
            "    def inner(args2):\n"
            "        return args2.hidden\n"
            "    return args.shown\n",
            encoding="utf-8",
        )
        assert attributes_read(sample, "cmd") == {"shown"}

    def test_a_missing_function_raises_rather_than_returning_nothing(self, tmp_path: Path) -> None:
        """Returning an empty set would make the comparison pass against any fixture, which is the
        exact failure this file is about."""
        sample = tmp_path / "sample.py"
        sample.write_text("def something_else(args):\n    return args.x\n", encoding="utf-8")
        try:
            attributes_read(sample, "cmd")
        except AssertionError:
            return
        raise AssertionError("a missing function must raise, not return an empty set")
