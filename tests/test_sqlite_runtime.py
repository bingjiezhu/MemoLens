from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SQLiteWalResetSafetyTests(unittest.TestCase):
    def test_official_fixed_release_matrix_is_branch_aware(self) -> None:
        from core.sqlite_runtime import is_wal_reset_safe

        expectations = {
            (3, 44, 5): False,
            (3, 44, 6): True,
            (3, 45, 0): False,
            (3, 47, 1): False,
            (3, 50, 6): False,
            (3, 50, 7): True,
            (3, 51, 0): False,
            (3, 51, 2): False,
            (3, 51, 3): True,
            (3, 52, 0): False,
            (3, 52, 1): False,
            (3, 52, 999): False,
            (3, 53, 0): True,
            (3, 53, 4): True,
            (3, 99, 0): True,
            (4, 0, 0): False,
        }

        for version, expected in expectations.items():
            with self.subTest(version=version):
                self.assertEqual(is_wal_reset_safe(version), expected)

    def test_invalid_or_incomplete_versions_fail_closed(self) -> None:
        from core.sqlite_runtime import is_wal_reset_safe

        for version in ((), (3,), (3, 51), (3, 51, -1), ("3", 51, 3), (4, 0, True)):
            with self.subTest(version=version):
                self.assertFalse(is_wal_reset_safe(version))

    def test_capability_is_bounded_and_contains_no_local_paths(self) -> None:
        from core.sqlite_runtime import sqlite_runtime_capability

        capability = sqlite_runtime_capability(
            python_version="3.14.2",
            sqlite_version="3.53.4",
            sqlite_version_info=(3, 53, 4),
        )

        self.assertEqual(
            capability,
            {
                "object": "memolens.sqlite_runtime",
                "schema_version": "1",
                "policy_id": "sqlite-wal-reset-2026-08-22-v1",
                "python_version": "3.14.2",
                "sqlite_version": "3.53.4",
                "wal_reset_safe": True,
                "journal_policy": "wal",
                "status": "ready",
                "reason_code": None,
            },
        )
        serialized = json.dumps(capability, sort_keys=True)
        self.assertNotIn(str(PROJECT_ROOT), serialized)
        self.assertNotIn("executable", serialized)
        self.assertNotIn("db_path", serialized)

    def test_policy_artifact_is_the_exact_shared_allowlist(self) -> None:
        policy = json.loads(
            (PROJECT_ROOT / "core" / "sqlite_runtime_policy.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            policy,
            {
                "object": "memolens.sqlite_runtime_policy",
                "schema_version": "1",
                "policy_id": "sqlite-wal-reset-2026-08-22-v1",
                "sqlite_major": 3,
                "backport_minimums": [[3, 44, 6], [3, 50, 7], [3, 51, 3]],
                "mainline_minimum": [3, 53, 0],
                "withdrawn_branches": [[3, 52]],
            },
        )

    def test_invalid_shared_policy_fails_closed(self) -> None:
        from core import sqlite_runtime

        with mock.patch.object(sqlite_runtime, "_SQLITE_RUNTIME_POLICY", None):
            self.assertFalse(sqlite_runtime.is_wal_reset_safe((3, 53, 4)))
            capability = sqlite_runtime.sqlite_runtime_capability(
                python_version="3.14.2",
                sqlite_version="3.53.4",
                sqlite_version_info=(3, 53, 4),
            )

        self.assertIsNone(capability["policy_id"])
        self.assertFalse(capability["wal_reset_safe"])
        self.assertEqual(capability["reason_code"], "sqlite_wal_reset_unsafe")

    def test_missing_malformed_or_duplicate_policy_fails_closed(self) -> None:
        from core import sqlite_runtime

        with tempfile.TemporaryDirectory(prefix="memolens-sqlite-policy-") as temporary:
            root = Path(temporary)
            invalid_policies = {
                "missing.json": None,
                "malformed.json": "{",
                "duplicate.json": '{"object":"first","object":"second"}',
                "unknown-field.json": json.dumps(
                    {
                        "object": "memolens.sqlite_runtime_policy",
                        "schema_version": "1",
                        "policy_id": "test",
                        "sqlite_major": 3,
                        "backport_minimums": [],
                        "mainline_minimum": [3, 53, 0],
                        "withdrawn_branches": [],
                        "unexpected": True,
                    }
                ),
            }
            for filename, content in invalid_policies.items():
                path = root / filename
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                with (
                    self.subTest(filename=filename),
                    mock.patch.object(sqlite_runtime, "_POLICY_PATH", path),
                ):
                    self.assertIsNone(sqlite_runtime._load_sqlite_runtime_policy())

    def test_unsafe_capability_has_stable_reason_code(self) -> None:
        from core.sqlite_runtime import (
            SQLITE_WAL_RESET_UNSAFE,
            UnsafeSQLiteRuntimeError,
            require_safe_sqlite_runtime,
            sqlite_runtime_capability,
        )

        capability = sqlite_runtime_capability(
            python_version="3.11.11",
            sqlite_version="3.47.1",
            sqlite_version_info=(3, 47, 1),
        )
        self.assertEqual(capability["status"], "unsafe")
        self.assertEqual(capability["reason_code"], SQLITE_WAL_RESET_UNSAFE)

        with (
            mock.patch(
                "core.sqlite_runtime.sqlite_runtime_capability",
                return_value=capability,
            ),
            self.assertRaises(UnsafeSQLiteRuntimeError) as raised,
        ):
            require_safe_sqlite_runtime()
        self.assertEqual(raised.exception.code, SQLITE_WAL_RESET_UNSAFE)
        self.assertEqual(raised.exception.capability, capability)
        self.assertIn("3.47.1", str(raised.exception))
        with self.assertRaises(TypeError):
            require_safe_sqlite_runtime(capability)  # type: ignore[call-arg]


class SQLiteRuntimeCliTests(unittest.TestCase):
    @staticmethod
    def _capability(*, safe: bool) -> dict[str, object]:
        return {
            "object": "memolens.sqlite_runtime",
            "schema_version": "1",
            "policy_id": "sqlite-wal-reset-2026-08-22-v1",
            "python_version": "3.14.2" if safe else "3.11.11",
            "sqlite_version": "3.53.4" if safe else "3.47.1",
            "wal_reset_safe": safe,
            "journal_policy": "wal",
            "status": "ready" if safe else "unsafe",
            "reason_code": None if safe else "sqlite_wal_reset_unsafe",
        }

    def test_json_mode_returns_machine_readable_safe_capability(self) -> None:
        from scripts import check_sqlite_runtime

        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch.object(
                check_sqlite_runtime,
                "sqlite_runtime_capability",
                return_value=self._capability(safe=True),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = check_sqlite_runtime.main(["--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), self._capability(safe=True))
        self.assertEqual(stderr.getvalue(), "")

    def test_unsafe_json_mode_uses_config_exit_code(self) -> None:
        from core.sqlite_runtime import sqlite_runtime_error_message
        from scripts import check_sqlite_runtime

        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch.object(
                check_sqlite_runtime,
                "sqlite_runtime_capability",
                return_value=self._capability(safe=False),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = check_sqlite_runtime.main(["--json"])

        self.assertEqual(exit_code, 78)
        self.assertEqual(json.loads(stdout.getvalue()), self._capability(safe=False))
        self.assertEqual(
            stderr.getvalue().strip(),
            sqlite_runtime_error_message(self._capability(safe=False)),
        )
        self.assertNotIn(str(PROJECT_ROOT), stderr.getvalue())

    def test_quiet_mode_emits_nothing_and_keeps_exit_status(self) -> None:
        from scripts import check_sqlite_runtime

        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch.object(
                check_sqlite_runtime,
                "sqlite_runtime_capability",
                return_value=self._capability(safe=False),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = check_sqlite_runtime.main(["--quiet"])

        self.assertEqual(exit_code, 78)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_real_checker_reports_the_current_interpreter(self) -> None:
        process = subprocess.run(
            [sys.executable, "-I", "scripts/check_sqlite_runtime.py", "--json"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertIn(process.returncode, {0, 78})
        payload = json.loads(process.stdout)
        self.assertEqual(payload["object"], "memolens.sqlite_runtime")
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["policy_id"], "sqlite-wal-reset-2026-08-22-v1")
        self.assertEqual(process.returncode == 0, payload["wal_reset_safe"])

    def test_real_checker_refuses_nonisolated_startup(self) -> None:
        process = subprocess.run(
            [sys.executable, "scripts/check_sqlite_runtime.py", "--json"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(process.returncode, 78)
        self.assertEqual(process.stdout, "")
        self.assertIn("isolated Python startup", process.stderr)


if __name__ == "__main__":
    unittest.main()
