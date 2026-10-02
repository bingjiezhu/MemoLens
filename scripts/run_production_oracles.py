#!/usr/bin/env python3
"""Execute every hash-bound production oracle by its exact test selector.

This runner deliberately does not execute whole files.  A manifest selector
must resolve to exactly one test, and that selected test must pass without a
skip, cancellation, todo, expected failure, or unexpected success.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType
import importlib.util
import unittest
from collections.abc import Iterable, Mapping
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.production_surface_inventory import (  # noqa: E402
    load_production_surface_inventory,
)


class OracleExecutionError(RuntimeError):
    """Raised when an oracle is absent, ambiguous, skipped, or unsuccessful."""


@dataclass(frozen=True)
class OracleOutcome:
    oracle_id: str
    runner: str
    selector: str
    tests_run: int = 1
    passed: int = 1
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    todo: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "cancelled": self.cancelled,
            "failed": self.failed,
            "oracle_id": self.oracle_id,
            "passed": self.passed,
            "runner": self.runner,
            "selector": self.selector,
            "skipped": self.skipped,
            "tests_run": self.tests_run,
            "todo": self.todo,
        }


def _safe_oracle_path(repo_root: Path, relative_path: str) -> Path:
    root = repo_root.resolve(strict=True)
    unresolved = root / relative_path
    try:
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise OracleExecutionError(
            f"Oracle path is missing or escapes the repository: {relative_path}"
        ) from exc
    if resolved != unresolved.absolute() or not resolved.is_file():
        raise OracleExecutionError(
            f"Oracle path must be a non-symlink regular file: {relative_path}"
        )
    return resolved


def _flatten_suite(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten_suite(item)
        elif isinstance(item, unittest.TestCase):
            yield item
        else:
            raise OracleExecutionError(
                f"Python test loader returned an unsupported item: {type(item).__name__}"
            )


def _select_unique_python_test(
    suite: unittest.TestSuite, selector: str
) -> unittest.TestCase:
    matches = [
        test
        for test in _flatten_suite(suite)
        if getattr(test, "_testMethodName", None) == selector
    ]
    if len(matches) != 1:
        raise OracleExecutionError(
            f"Python selector must resolve exactly once: {selector!r} "
            f"(matches={len(matches)})"
        )
    return matches[0]


def _load_test_module(path: Path) -> tuple[ModuleType, unittest.TestLoader]:
    module_name = f"_memolens_oracle_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise OracleExecutionError(f"Cannot load Python oracle module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    loader = unittest.TestLoader()
    return module, loader


def _assert_python_result(result: unittest.TestResult) -> None:
    invalid = {
        "errors": len(result.errors),
        "expected_failures": len(result.expectedFailures),
        "failures": len(result.failures),
        "skipped": len(result.skipped),
        "tests_run": result.testsRun,
        "unexpected_successes": len(result.unexpectedSuccesses),
    }
    if invalid != {
        "errors": 0,
        "expected_failures": 0,
        "failures": 0,
        "skipped": 0,
        "tests_run": 1,
        "unexpected_successes": 0,
    }:
        raise OracleExecutionError(
            f"Python oracle did not produce one unqualified pass: {invalid}"
        )


def _run_python_child(path: Path, selector: str) -> None:
    module, loader = _load_test_module(path)
    try:
        suite = loader.loadTestsFromModule(module)
        if loader.errors:
            raise OracleExecutionError(
                "Python test discovery failed: " + " | ".join(loader.errors)
            )
        selected = _select_unique_python_test(suite, selector)
        stream = StringIO()
        result = unittest.TextTestRunner(
            stream=stream,
            verbosity=2,
            failfast=True,
        ).run(unittest.TestSuite([selected]))
        sys.stdout.write(stream.getvalue())
        _assert_python_result(result)
    finally:
        sys.modules.pop(module.__name__, None)


def _exact_node_pattern(selector: str) -> str:
    escaped = re.sub(r"([\\^$.*+?()[\]{}|])", r"\\\1", selector)
    return f"^(?:{escaped})$"


_TAP_FIELDS = ("tests", "pass", "fail", "cancelled", "skipped", "todo")


def _parse_tap_summary(output: str, *, selector: str) -> dict[str, int]:
    selected_subtests = [
        line.removeprefix("# Subtest: ")
        for line in output.splitlines()
        if line.startswith("# Subtest: ")
    ]
    if selected_subtests != [selector]:
        raise OracleExecutionError(
            "TAP output must contain exactly the selected test as its sole subtest "
            f"(selector={selector!r}, subtests={selected_subtests!r})."
        )
    summary: dict[str, int] = {}
    for field in _TAP_FIELDS:
        matches = re.findall(rf"^# {field} ([0-9]+)$", output, flags=re.MULTILINE)
        if len(matches) != 1:
            raise OracleExecutionError(
                f"TAP output must contain one {field!r} summary (found={len(matches)})."
            )
        summary[field] = int(matches[0])
    expected = {
        "cancelled": 0,
        "fail": 0,
        "pass": 1,
        "skipped": 0,
        "tests": 1,
        "todo": 0,
    }
    if summary != expected:
        raise OracleExecutionError(
            f"Node selector did not produce one unqualified pass: {summary}"
        )
    return summary


def _run_subprocess(
    command: list[str], *, cwd: Path, echo_output: bool = True
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    if output and echo_output:
        sys.stdout.write(output)
        if not output.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if completed.returncode != 0:
        raise OracleExecutionError(
            f"Oracle subprocess exited with status {completed.returncode}: "
            f"{command[0]}"
        )
    return output


def _run_python_oracle(
    row: Mapping[str, Any], *, repo_root: Path, script_path: Path
) -> None:
    path = _safe_oracle_path(repo_root, str(row["path"]))
    _run_subprocess(
        [
            sys.executable,
            "-I",
            str(script_path),
            "--python-child",
            str(path),
            str(row["selector"]),
        ],
        cwd=repo_root,
    )


def _run_node_oracle(
    row: Mapping[str, Any], *, repo_root: Path, echo_output: bool = True
) -> None:
    path = _safe_oracle_path(repo_root, str(row["path"]))
    runner = str(row["runner"])
    if runner == "node-test":
        command = [
            "node",
            "--experimental-strip-types",
            "--test",
            f"--test-name-pattern={_exact_node_pattern(str(row['selector']))}",
            "--test-reporter=tap",
            str(path),
        ]
        cwd = repo_root
    elif runner == "photon-test":
        photon_root = repo_root / "photon-bot"
        tsx = _safe_oracle_path(photon_root, "node_modules/tsx/dist/cli.mjs")
        command = [
            "node",
            str(tsx),
            "--test",
            f"--test-name-pattern={_exact_node_pattern(str(row['selector']))}",
            "--test-reporter=tap",
            str(path),
        ]
        cwd = photon_root
    else:
        raise OracleExecutionError(f"Unsupported Node oracle runner: {runner}")
    output = _run_subprocess(command, cwd=cwd, echo_output=echo_output)
    _parse_tap_summary(output, selector=str(row["selector"]))


def run_all_oracles(*, repo_root: Path = REPO_ROOT) -> list[OracleOutcome]:
    inventory = load_production_surface_inventory()
    script_path = Path(__file__).resolve(strict=True)
    outcomes: list[OracleOutcome] = []
    for row in inventory["negative_oracles"]:
        oracle_id = str(row["id"])
        runner = str(row["runner"])
        selector = str(row["selector"])
        sys.stdout.write(
            json.dumps(
                {
                    "event": "oracle_start",
                    "oracle_id": oracle_id,
                    "runner": runner,
                    "selector": selector,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
        if runner == "python-unittest":
            _run_python_oracle(row, repo_root=repo_root, script_path=script_path)
        else:
            _run_node_oracle(row, repo_root=repo_root)
        outcome = OracleOutcome(
            oracle_id=oracle_id,
            runner=runner,
            selector=selector,
        )
        outcomes.append(outcome)
        sys.stdout.write(
            json.dumps(
                {"event": "oracle_pass", **outcome.as_dict()},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
    if not outcomes:
        raise OracleExecutionError("Production inventory contains no negative oracles.")
    return outcomes


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-child", action="store_true")
    parser.add_argument("child_path", nargs="?")
    parser.add_argument("child_selector", nargs="?")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        if arguments.python_child:
            if not arguments.child_path or not arguments.child_selector:
                raise OracleExecutionError(
                    "--python-child requires an oracle path and exact selector."
                )
            path = Path(arguments.child_path).resolve(strict=True)
            path.relative_to(REPO_ROOT.resolve(strict=True))
            _run_python_child(path, arguments.child_selector)
            return 0
        if arguments.child_path or arguments.child_selector:
            raise OracleExecutionError("Unexpected positional runner arguments.")
        outcomes = run_all_oracles()
        print(
            json.dumps(
                {
                    "failed": 0,
                    "gate": "production-negative-oracles",
                    "oracle_count": len(outcomes),
                    "passed": len(outcomes),
                    "skipped": 0,
                    "status": "passed",
                    "tests_run": len(outcomes),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, OracleExecutionError, ValueError) as exc:
        print(f"production oracle gate failed closed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
