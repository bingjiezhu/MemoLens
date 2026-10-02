from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import memolens_cli  # noqa: E402
import memolens_core  # noqa: E402
import memolens_library_bootstrap as bootstrap_module  # noqa: E402
import memolens_mcp  # noqa: E402
from memolens_core import MemoLensError  # noqa: E402
from memolens_library_bootstrap import (  # noqa: E402
    LIBRARY_BOOTSTRAP_MAX_BYTES,
    LIBRARY_BOOTSTRAP_MAX_ENTRIES,
    decode_library_bootstrap_request,
    library_bootstrap_status,
    start_library_bootstrap,
)
from memolens_mcp import TOOL_ARGUMENT_FIELDS, TOOLS, call_tool  # noqa: E402


class LibraryBootstrapSpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-bootstrap-plugin-")
        self.state_dir = Path(self.temporary.name) / "app-state"
        self.environment = mock.patch.dict(
            os.environ,
            {"MEMOLENS_APP_STATE_DIR": str(self.state_dir)},
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _mcp_frame(
        request_id: int,
        name: str,
        arguments: dict[str, object],
    ) -> bytes:
        return (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _run_mcp_main(frames: bytes) -> tuple[int, list[dict[str, object]]]:
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(frames)
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            sys, "stdout", stdout
        ):
            result = memolens_mcp.main()
        return result, [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_start_writes_only_one_closed_0600_path_free_request(self) -> None:
        result = start_library_bootstrap(
            "agent-retry-001",
            now_ms=1_800_000_000_000,
        )

        self.assertEqual(result["status"], "queued_open_memolens")
        self.assertRegex(result["request_id"], r"^lb_[0-9a-f]{64}$")
        self.assertFalse(result["core_library_created"])
        self.assertFalse(result["database_created"])
        self.assertFalse(result["project_created"])
        self.assertEqual(result["next_action"], "open_memolens")
        self.assertEqual(result["expires_at_ms"], 1_800_000_300_000)
        self.assertNotIn("path", json.dumps(result).lower())

        spool = self.state_dir / "library-bootstrap-spool"
        self.assertEqual(stat.S_IMODE(spool.stat().st_mode), 0o700)
        entries = list(spool.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertEqual(stat.S_IMODE(entries[0].stat().st_mode), 0o600)
        payload = json.loads(entries[0].read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "kind",
                "request_id",
                "created_at_ms",
                "expires_at_ms",
            },
        )
        self.assertEqual(payload["request_id"], result["request_id"])
        serialized = json.dumps(payload).lower()
        for forbidden in ("path", "db", "token", "proof", "project", "idempotency"):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse((self.state_dir / "desktop-settings.json").exists())
        self.assertFalse((self.state_dir / "storage").exists())

    def test_start_is_idempotent_without_persisting_the_raw_key(self) -> None:
        first = start_library_bootstrap("same-key", now_ms=1_800_000_000_000)
        second = start_library_bootstrap("same-key", now_ms=1_800_000_000_123)
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["status"], "queued_open_memolens")
        self.assertEqual(second["expires_at_ms"], first["expires_at_ms"])
        spool = self.state_dir / "library-bootstrap-spool"
        self.assertEqual(len(list(spool.iterdir())), 1)
        self.assertNotIn("same-key", next(spool.iterdir()).read_text(encoding="utf-8"))

    def test_closed_decoder_rejects_malformed_oversize_and_payload_injection(self) -> None:
        valid = {
            "schema_version": "1",
            "kind": "library_bootstrap_intent",
            "request_id": "lb_" + "a" * 64,
            "created_at_ms": 1_800_000_000_000,
            "expires_at_ms": 1_800_000_300_000,
        }
        self.assertEqual(
            decode_library_bootstrap_request(json.dumps(valid).encode())["request_id"],
            valid["request_id"],
        )
        for injected in (
            {**valid, "path_hint": "/private/photos"},
            {**valid, "db_path": "/tmp/forged.db"},
            {**valid, "project_seed": {"id": "project"}},
            {**valid, "token": "secret"},
        ):
            with self.assertRaises(MemoLensError) as captured:
                decode_library_bootstrap_request(json.dumps(injected).encode())
            self.assertEqual(captured.exception.code, "bootstrap_request_invalid")
        with self.assertRaises(MemoLensError):
            decode_library_bootstrap_request(b"{")
        with self.assertRaises(MemoLensError) as captured:
            decode_library_bootstrap_request(b"x" * (LIBRARY_BOOTSTRAP_MAX_BYTES + 1))
        self.assertEqual(captured.exception.code, "bootstrap_request_oversize")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_spool_and_bounded_census_fail_closed(self) -> None:
        self.state_dir.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.state_dir / "library-bootstrap-spool")
        with self.assertRaises(MemoLensError) as captured:
            start_library_bootstrap("symlink-key", now_ms=1_800_000_000_000)
        self.assertEqual(captured.exception.code, "bootstrap_spool_unsafe")
        self.assertEqual(list(outside.iterdir()), [])

        (self.state_dir / "library-bootstrap-spool").unlink()
        spool = self.state_dir / "library-bootstrap-spool"
        spool.mkdir(mode=0o700)
        outside_entry = outside / "request.json"
        outside_entry.write_text("{}", encoding="utf-8")
        symlink_id = "lb_" + "d" * 64
        os.symlink(outside_entry, spool / f"{symlink_id}.request.json")
        with self.assertRaises(MemoLensError) as captured:
            start_library_bootstrap("symlink-entry", now_ms=1_800_000_000_000)
        self.assertEqual(captured.exception.code, "bootstrap_spool_unsafe")

        (spool / f"{symlink_id}.request.json").unlink()
        for index in range(LIBRARY_BOOTSTRAP_MAX_ENTRIES):
            request_id = f"lb_{index:064x}"
            entry = spool / f"{request_id}.receipt.json"
            entry.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "kind": "library_bootstrap_receipt",
                        "request_id": request_id,
                        "state": "native_cancelled",
                        "completed_at_ms": 1_800_000_000_000,
                        "expires_at_ms": 1_800_000_300_000,
                    }
                ),
                encoding="utf-8",
            )
            entry.chmod(0o600)
        stale_temp = spool / f".{'lb_' + 'f' * 64}.{'a' * 32}.tmp"
        stale_temp.write_bytes(b"stale-owned-temp")
        stale_temp.chmod(0o600)
        old = time.time() - 600
        os.utime(stale_temp, (old, old), follow_symlinks=False)
        with self.assertRaises(MemoLensError) as captured:
            start_library_bootstrap("too-many", now_ms=1_800_000_000_000)
        self.assertEqual(captured.exception.code, "bootstrap_spool_limit")
        self.assertFalse(stale_temp.exists())

    def test_status_is_bounded_path_free_and_unknown_ids_fail_closed(self) -> None:
        started = start_library_bootstrap("status-key", now_ms=1_800_000_000_000)
        status = library_bootstrap_status(
            started["request_id"], now_ms=1_800_000_000_001
        )
        self.assertEqual(status["status"], "queued_open_memolens")
        self.assertNotIn("path", json.dumps(status).lower())
        with self.assertRaises(MemoLensError) as captured:
            library_bootstrap_status("lb_" + "f" * 64)
        self.assertEqual(captured.exception.code, "bootstrap_request_not_found")

        expired = start_library_bootstrap(
            "expired-key", now_ms=1_800_000_000_000
        )
        expired_status = library_bootstrap_status(
            expired["request_id"], now_ms=1_800_000_300_000
        )
        self.assertEqual(expired_status["status"], "expired")
        self.assertEqual(
            expired_status["next_action"], "start_with_new_idempotency_key"
        )

    def test_unknown_status_on_fresh_state_is_a_zero_write_read(self) -> None:
        self.assertFalse(self.state_dir.exists())
        with self.assertRaises(MemoLensError) as captured:
            library_bootstrap_status("lb_" + "e" * 64)
        self.assertEqual(captured.exception.code, "bootstrap_request_not_found")
        self.assertFalse(self.state_dir.exists())

        with mock.patch.object(
            memolens_mcp,
            "MemoLensGateway",
            side_effect=AssertionError("STATUS_CONSTRUCTED_GATEWAY"),
        ) as gateway_factory:
            code, responses = self._run_mcp_main(
                self._mcp_frame(
                    3,
                    "memolens_library_bootstrap_status",
                    {"request_id": "lb_" + "e" * 64},
                )
            )
        self.assertEqual(code, 0)
        self.assertTrue(responses[0]["result"]["isError"])
        gateway_factory.assert_not_called()
        self.assertFalse(self.state_dir.exists())

    def test_terminal_receipt_wins_over_same_id_claim_after_crash(self) -> None:
        started = start_library_bootstrap(
            "receipt-crash-key", now_ms=1_800_000_000_000
        )
        spool = self.state_dir / "library-bootstrap-spool"
        request_path = spool / f"{started['request_id']}.request.json"
        claim_path = spool / f"{started['request_id']}.claim.json"
        os.link(request_path, claim_path)
        receipt_path = spool / f"{started['request_id']}.receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "kind": "library_bootstrap_receipt",
                    "request_id": started["request_id"],
                    "state": "native_cancelled",
                    "completed_at_ms": 1_800_000_000_001,
                    "expires_at_ms": started["expires_at_ms"],
                }
            ),
            encoding="utf-8",
        )
        receipt_path.chmod(0o600)
        terminal = library_bootstrap_status(
            started["request_id"], now_ms=1_800_000_000_002
        )
        self.assertEqual(terminal["status"], "native_cancelled")
        self.assertEqual(terminal["expires_at_ms"], started["expires_at_ms"])

    def test_core_commit_receipt_projects_only_path_free_created_facts(self) -> None:
        started = start_library_bootstrap(
            "core-commit-receipt-key", now_ms=1_800_000_000_000
        )
        spool = self.state_dir / "library-bootstrap-spool"
        request_path = spool / f"{started['request_id']}.request.json"
        request_path.unlink()
        receipt_path = spool / f"{started['request_id']}.receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "kind": "library_bootstrap_receipt",
                    "request_id": started["request_id"],
                    "state": "library_authority_committed",
                    "completed_at_ms": 1_800_000_000_001,
                    "expires_at_ms": started["expires_at_ms"],
                }
            ),
            encoding="utf-8",
        )
        receipt_path.chmod(0o600)

        committed = library_bootstrap_status(
            started["request_id"], now_ms=1_800_000_000_002
        )

        self.assertEqual(committed["status"], "library_authority_committed")
        self.assertEqual(committed["next_action"], "wait_for_library_scan")
        self.assertTrue(committed["core_library_created"])
        self.assertTrue(committed["database_created"])
        self.assertTrue(committed["project_created"])
        self.assertFalse(committed["scan_started"])
        self.assertFalse(committed["editor_ready"])
        self.assertFalse(committed["timeline_edit_granted"])
        self.assertFalse(committed["backend_contacted_by_plugin"])
        serialized = json.dumps(committed, sort_keys=True)
        for forbidden in ("canonical_root", "database_path", "device", "inode"):
            self.assertNotIn(forbidden, serialized)

        bootstrap_tool = next(
            tool
            for tool in TOOLS
            if tool["name"] == "memolens_library_bootstrap_status"
        )
        output_schema = bootstrap_tool["outputSchema"]
        self.assertIn(
            "library_authority_committed",
            output_schema["properties"]["status"]["enum"],
        )
        self.assertIn(
            "wait_for_library_scan",
            output_schema["properties"]["next_action"]["enum"],
        )
        committed_branch = output_schema["allOf"][0]["then"]["properties"]
        self.assertEqual(committed_branch["core_library_created"], {"const": True})
        self.assertEqual(committed_branch["database_created"], {"const": True})
        self.assertEqual(committed_branch["project_created"], {"const": True})

    def test_main_stdio_bootstrap_never_constructs_gateway_or_reads_locators(
        self,
    ) -> None:
        self.state_dir.mkdir(parents=True)
        settings = self.state_dir / "backend-settings.json"
        default_db = self.state_dir / "storage" / "photo_index.db"
        default_db.parent.mkdir()
        settings.write_text('{"db_path":"must-not-read"}', encoding="utf-8")
        default_db.write_bytes(b"must-not-probe-or-open")
        settings_before = settings.stat()
        db_before = default_db.stat()

        gateway = mock.patch.object(
            memolens_mcp,
            "MemoLensGateway",
            side_effect=AssertionError("BOOTSTRAP_CONSTRUCTED_GATEWAY"),
        )
        persisted = mock.patch.object(
            memolens_core,
            "_persisted_settings",
            side_effect=AssertionError("BOOTSTRAP_READ_BACKEND_SETTINGS"),
        )
        resolver = mock.patch.object(
            memolens_core,
            "resolve_local_paths",
            side_effect=AssertionError("BOOTSTRAP_PROBED_DB_LOCATOR"),
        )
        with gateway as gateway_spy, persisted as settings_spy, resolver as resolver_spy:
            code, started_frames = self._run_mcp_main(
                self._mcp_frame(
                    1,
                    "memolens_library_bootstrap_start",
                    {"request_idempotency_key": "stdio-zero-locator-key"},
                )
            )
            self.assertEqual(code, 0)
            started = started_frames[0]["result"]["structuredContent"]
            self.assertEqual(started["status"], "queued_open_memolens")

            code, status_frames = self._run_mcp_main(
                self._mcp_frame(
                    2,
                    "memolens_library_bootstrap_status",
                    {"request_id": started["request_id"]},
                )
            )
            self.assertEqual(code, 0)
            status = status_frames[0]["result"]["structuredContent"]
            self.assertEqual(status, started)
            gateway_spy.assert_not_called()
            settings_spy.assert_not_called()
            resolver_spy.assert_not_called()

        self.assertEqual(settings.stat(), settings_before)
        self.assertEqual(default_db.stat(), db_before)
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"db_path":"must-not-read"}')
        self.assertEqual(default_db.read_bytes(), b"must-not-probe-or-open")

    def test_main_stdio_constructs_one_lazy_gateway_for_non_bootstrap_tools(
        self,
    ) -> None:
        fake_gateway = mock.Mock()
        fake_gateway.status.return_value = {
            "object": "memolens.status",
            "status": "ready",
        }
        frames = self._mcp_frame(1, "memolens_status", {}) + self._mcp_frame(
            2, "memolens_status", {}
        )
        with mock.patch.object(
            memolens_mcp, "MemoLensGateway", return_value=fake_gateway
        ) as gateway_factory:
            code, responses = self._run_mcp_main(frames)
        self.assertEqual(code, 0)
        self.assertEqual(len(responses), 2)
        self.assertEqual(gateway_factory.call_count, 1)
        self.assertEqual(fake_gateway.status.call_count, 2)

    def test_status_ignores_only_bounded_controlled_temp_without_writing(self) -> None:
        self.state_dir.mkdir(parents=True)
        spool = self.state_dir / "library-bootstrap-spool"
        spool.mkdir(mode=0o700)
        request_id = "lb_" + "a" * 64
        controlled = spool / f".{request_id}.{'b' * 32}.tmp"
        controlled.write_bytes(b"partial-crash-body")
        controlled.chmod(0o600)
        before = controlled.stat()

        with self.assertRaises(MemoLensError) as captured:
            library_bootstrap_status(request_id)
        self.assertEqual(captured.exception.code, "bootstrap_request_not_found")
        self.assertEqual(controlled.stat(), before)
        self.assertEqual(list(spool.iterdir()), [controlled])

        escaped = spool / f".{request_id}.{'c' * 31}-escaped.tmp"
        escaped.write_bytes(b"not-an-owned-temp")
        escaped.chmod(0o600)
        with self.assertRaises(MemoLensError) as captured:
            library_bootstrap_status(request_id)
        self.assertEqual(captured.exception.code, "bootstrap_spool_unsafe")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_controlled_temp_symlink_and_oversize_still_fail_closed(self) -> None:
        self.state_dir.mkdir(parents=True)
        spool = self.state_dir / "library-bootstrap-spool"
        spool.mkdir(mode=0o700)
        request_id = "lb_" + "d" * 64
        outside = Path(self.temporary.name) / "outside-temp"
        outside.write_bytes(b"outside")
        linked = spool / f".{request_id}.{'e' * 32}.tmp"
        os.symlink(outside, linked)
        with self.assertRaises(MemoLensError) as captured:
            library_bootstrap_status(request_id)
        self.assertEqual(captured.exception.code, "bootstrap_spool_unsafe")

        linked.unlink()
        linked.write_bytes(b"x" * (LIBRARY_BOOTSTRAP_MAX_BYTES + 1))
        linked.chmod(0o600)
        with self.assertRaises(MemoLensError) as captured:
            library_bootstrap_status(request_id)
        self.assertEqual(captured.exception.code, "bootstrap_request_oversize")

    def test_start_cleans_stale_controlled_temp_but_not_fresh_temp(self) -> None:
        self.state_dir.mkdir(parents=True)
        spool = self.state_dir / "library-bootstrap-spool"
        spool.mkdir(mode=0o700)
        request_id = "lb_" + "1" * 64
        stale = spool / f".{request_id}.{'2' * 32}.tmp"
        fresh = spool / f".{request_id}.{'3' * 32}.tmp"
        for entry in (stale, fresh):
            entry.write_bytes(b"controlled-temp")
            entry.chmod(0o600)
        old = time.time() - 600
        os.utime(stale, (old, old), follow_symlinks=False)

        result = start_library_bootstrap("cleanup-temp-key")
        self.assertEqual(result["status"], "queued_open_memolens")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_same_key_concurrent_start_converges_under_writer_lock(self) -> None:
        request_id = bootstrap_module._derive_request_id("concurrent-same-key")
        start_gate = threading.Barrier(2)

        def start(now_ms: int) -> dict[str, object]:
            start_gate.wait(timeout=5)
            return start_library_bootstrap(
                "concurrent-same-key",
                now_ms=now_ms,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(start, 1_800_000_000_000)
            second_future = executor.submit(start, 1_800_000_000_123)
            first = first_future.result(timeout=5)
            second = second_future.result(timeout=5)

        self.assertEqual(first, second)
        self.assertEqual(first["request_id"], request_id)
        spool = self.state_dir / "library-bootstrap-spool"
        self.assertEqual(
            [entry.name for entry in spool.iterdir()],
            [f"{request_id}.request.json"],
        )

    def test_noreplace_eexist_strictly_reads_the_winner(self) -> None:
        original_publish = bootstrap_module._atomic_publish_noreplace

        def publish_exact_winner(
            spool: Path,
            directory_fd: int,
            temporary_name: str,
            target_name: str,
        ) -> bool:
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
            return original_publish(
                spool,
                directory_fd,
                temporary_name,
                target_name,
            )

        with mock.patch.object(
            bootstrap_module,
            "_atomic_publish_noreplace",
            side_effect=publish_exact_winner,
        ):
            result = start_library_bootstrap(
                "noreplace-exact-winner",
                now_ms=1_800_000_000_000,
            )
        self.assertEqual(result["status"], "queued_open_memolens")

        def publish_malformed_winner(
            spool: Path,
            directory_fd: int,
            temporary_name: str,
            target_name: str,
        ) -> bool:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(target_name, flags, 0o600, dir_fd=directory_fd)
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, b"{}")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
            return original_publish(
                spool,
                directory_fd,
                temporary_name,
                target_name,
            )

        with mock.patch.object(
            bootstrap_module,
            "_atomic_publish_noreplace",
            side_effect=publish_malformed_winner,
        ):
            with self.assertRaises(MemoLensError) as captured:
                start_library_bootstrap(
                    "noreplace-malformed-winner",
                    now_ms=1_800_000_000_000,
                )
        self.assertEqual(captured.exception.code, "bootstrap_request_invalid")

    def test_different_key_concurrency_cannot_cross_final_entry_bound(self) -> None:
        self.state_dir.mkdir(parents=True)
        spool = self.state_dir / "library-bootstrap-spool"
        spool.mkdir(mode=0o700)
        for index in range(LIBRARY_BOOTSTRAP_MAX_ENTRIES - 1):
            request_id = f"lb_{index:064x}"
            entry = spool / f"{request_id}.receipt.json"
            entry.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "kind": "library_bootstrap_receipt",
                        "request_id": request_id,
                        "state": "native_cancelled",
                        "completed_at_ms": 1_800_000_000_000,
                        "expires_at_ms": 1_800_000_300_000,
                    }
                ),
                encoding="utf-8",
            )
            entry.chmod(0o600)
        start_gate = threading.Barrier(2)

        def start(key: str) -> dict[str, object] | str:
            start_gate.wait(timeout=5)
            try:
                return start_library_bootstrap(key, now_ms=1_800_000_000_000)
            except MemoLensError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(start, key)
                for key in ("bounded-concurrent-a", "bounded-concurrent-b")
            ]
            outcomes = [future.result(timeout=5) for future in futures]
        self.assertEqual(
            sum(isinstance(outcome, dict) for outcome in outcomes),
            1,
        )
        self.assertEqual(outcomes.count("bootstrap_spool_limit"), 1)
        protocol_entries = [
            entry
            for entry in spool.iterdir()
            if entry.name.endswith((".request.json", ".claim.json", ".receipt.json"))
        ]
        self.assertEqual(len(protocol_entries), LIBRARY_BOOTSTRAP_MAX_ENTRIES)
        self.assertFalse(any(entry.name.endswith(".tmp") for entry in spool.iterdir()))

    @unittest.skipUnless(
        bootstrap_module.fcntl is not None and os.name == "posix",
        "POSIX flock unavailable",
    )
    def test_cross_process_writer_lock_serializes_multi_key_final_bound(self) -> None:
        self.state_dir.mkdir(parents=True)
        spool = self.state_dir / "library-bootstrap-spool"
        spool.mkdir(mode=0o700)
        for index in range(LIBRARY_BOOTSTRAP_MAX_ENTRIES - 1):
            request_id = f"lb_{index:064x}"
            entry = spool / f"{request_id}.receipt.json"
            entry.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "kind": "library_bootstrap_receipt",
                        "request_id": request_id,
                        "state": "native_cancelled",
                        "completed_at_ms": 1_800_000_000_000,
                        "expires_at_ms": 1_800_000_300_000,
                    }
                ),
                encoding="utf-8",
            )
            entry.chmod(0o600)

        worker = (
            "import json, os, pathlib, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['MEMOLENS_APP_STATE_DIR'] = sys.argv[2]\n"
            "from memolens_contracts import MemoLensError\n"
            "from memolens_library_bootstrap import start_library_bootstrap\n"
            "pathlib.Path(sys.argv[3]).write_text('ready', encoding='utf-8')\n"
            "try:\n"
            "    result = start_library_bootstrap(sys.argv[4])\n"
            "except MemoLensError as exc:\n"
            "    result = {'error': exc.code}\n"
            "print(json.dumps(result, separators=(',', ':')))\n"
        )
        ready_paths = [
            Path(self.temporary.name) / "worker-a.ready",
            Path(self.temporary.name) / "worker-b.ready",
        ]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        directory_fd = os.open(
            spool,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        processes: list[subprocess.Popen[str]] = []
        try:
            bootstrap_module.fcntl.flock(
                directory_fd,
                bootstrap_module.fcntl.LOCK_EX,
            )
            for index, ready in enumerate(ready_paths):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            worker,
                            str(SCRIPTS),
                            str(self.state_dir),
                            str(ready),
                            f"cross-process-key-{index}",
                        ],
                        cwd=self.temporary.name,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            deadline = time.monotonic() + 5
            while not all(path.exists() for path in ready_paths):
                if time.monotonic() >= deadline:
                    self.fail("cross-process workers did not reach the writer lock")
                time.sleep(0.01)
            time.sleep(0.1)
            self.assertTrue(all(process.poll() is None for process in processes))
            self.assertEqual(len(list(spool.iterdir())), LIBRARY_BOOTSTRAP_MAX_ENTRIES - 1)
            bootstrap_module.fcntl.flock(
                directory_fd,
                bootstrap_module.fcntl.LOCK_UN,
            )
            outputs = [process.communicate(timeout=5) for process in processes]
        finally:
            try:
                bootstrap_module.fcntl.flock(
                    directory_fd,
                    bootstrap_module.fcntl.LOCK_UN,
                )
            finally:
                os.close(directory_fd)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.assertTrue(all(stderr == "" for _stdout, stderr in outputs))
        outcomes = [json.loads(stdout) for stdout, _stderr in outputs]
        self.assertEqual(sum("status" in outcome for outcome in outcomes), 1)
        self.assertEqual(
            sum(outcome.get("error") == "bootstrap_spool_limit" for outcome in outcomes),
            1,
        )
        self.assertEqual(len(list(spool.iterdir())), LIBRARY_BOOTSTRAP_MAX_ENTRIES)

    def test_publish_fault_removes_its_controlled_temp(self) -> None:
        with mock.patch.object(
            bootstrap_module.os,
            "link",
            side_effect=OSError("fault before publish"),
        ):
            with self.assertRaises(MemoLensError) as captured:
                start_library_bootstrap("publish-fault-key")
        self.assertEqual(captured.exception.code, "bootstrap_spool_unavailable")
        spool = self.state_dir / "library-bootstrap-spool"
        self.assertEqual(list(spool.iterdir()), [])

    def test_bootstrap_cli_rejects_global_database_or_library_locators(self) -> None:
        for option, value in (
            ("--db", "/tmp/must-not-use.db"),
            ("--library", "/tmp/must-not-use-library"),
        ):
            with self.subTest(option=option):
                parsed = memolens_cli.build_parser().parse_args(
                    [
                        option,
                        value,
                        "library-bootstrap-start",
                        "--request-idempotency-key",
                        "reject-global-locator",
                    ]
                )
                with self.assertRaises(MemoLensError) as captured:
                    memolens_cli.run(parsed)
                self.assertEqual(captured.exception.code, "invalid_argument")

    def test_cli_and_mcp_share_spool_without_constructing_a_gateway(self) -> None:
        tool_names = {tool["name"] for tool in TOOLS}
        self.assertIn("memolens_library_bootstrap_start", tool_names)
        self.assertIn("memolens_library_bootstrap_status", tool_names)
        self.assertEqual(
            TOOL_ARGUMENT_FIELDS["memolens_library_bootstrap_start"],
            frozenset({"request_idempotency_key"}),
        )
        self.assertEqual(
            TOOL_ARGUMENT_FIELDS["memolens_library_bootstrap_status"],
            frozenset({"request_id"}),
        )

        with mock.patch.object(
            memolens_cli,
            "_gateway",
            side_effect=AssertionError("bootstrap reached MemoLensGateway"),
        ):
            parsed = memolens_cli.build_parser().parse_args(
                [
                    "library-bootstrap-start",
                    "--request-idempotency-key",
                    "shared-cli-key",
                ]
            )
            cli_result = memolens_cli.run(parsed)
        self.assertEqual(cli_result["status"], "queued_open_memolens")

        class NoGateway:
            def __getattribute__(self, name: str):
                raise AssertionError(f"bootstrap reached gateway attribute {name}")

        mcp_result = call_tool(
            "memolens_library_bootstrap_start",
            {"request_idempotency_key": "shared-mcp-key"},
            NoGateway(),
        )
        self.assertEqual(mcp_result["status"], "queued_open_memolens")
        mcp_status = call_tool(
            "memolens_library_bootstrap_status",
            {"request_id": mcp_result["request_id"]},
            NoGateway(),
        )
        self.assertEqual(mcp_status["status"], "queued_open_memolens")


if __name__ == "__main__":
    unittest.main()
