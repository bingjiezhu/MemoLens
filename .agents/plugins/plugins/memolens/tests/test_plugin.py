from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import ProxyHandler


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_ROOT = PLUGIN_ROOT.parents[3]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_core import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    TRUST_LOCAL_API_ENV,
    MemoLensError,
    MemoLensGateway,
    _state_dir_candidates,
    validate_base_url,
)
from memolens_contracts import PLUGIN_VERSION  # noqa: E402


def _sqlite_source_state(path: Path) -> dict[str, object]:
    return {
        "directory_entries": sorted(item.name for item in path.parent.iterdir()),
        "files": {
            candidate.name: candidate.read_bytes()
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
            if candidate.exists()
        },
    }


class _MemoLensHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002, ANN001
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        server = self.server
        server.request_paths.append(self.path)
        if self.path == "/healthz":
            if server.redirect_health:
                self.send_response(302)
                self.send_header("Location", "/v1/settings")
                self.end_headers()
                return
            payload = {
                "status": "ok",
                "object": "health.check",
                "service": "memolens-backend",
                "api_version": "1",
                # Decoys ensure the plugin never trusts paths from healthz.
                "image_library_dir": str(server.decoy_dir),
                "db_path": str(server.decoy_dir / "decoy.db"),
            }
            if not server.valid_identity:
                payload["service"] = "not-memolens"
            self._json(payload)
            return
        if self.path == "/v1/settings":
            server.settings_requests += 1
            if not server.valid_settings:
                self._json({"object": "unexpected.settings", "effective": {}})
                return
            self._json(
                {
                    "object": "memolens.settings",
                    "effective": {
                        "image_library_dir": str(server.library_dir),
                        "db_path": str(server.db_path),
                        "app_state_dir": str(server.library_dir),
                        "settings_path": str(
                            server.library_dir / "backend-settings.json"
                        ),
                        "vision_profile_name": "local",
                        "query_profile_name": "local",
                        "embedding_backend": "semantic_hash",
                    },
                    "index_stats": {"asset_count": server.asset_count},
                }
            )
            return
        if self.path.startswith("/v1/atlas/workbench?"):
            self._json(
                {
                    "object": "atlas.workbench",
                    "memories": [
                        {
                            "id": "memory_trip",
                            "kind": "event",
                            "label": "Coast trip",
                            "asset_count": 1,
                            "representative_assets": [server.asset],
                        }
                    ],
                    "suggested_queries": ["coast sunset"],
                    "storylines": [],
                }
            )
            return
        if self.path == "/v1/atlas/cleanup":
            self._json(
                {
                    "object": "atlas.cleanup",
                    "duplicate_stack_count": 1,
                    "similar_stack_count": 0,
                    "low_quality_count": 0,
                    "missing_time_count": 0,
                    "missing_place_count": 0,
                    "people_review_count": 0,
                    "stacks": [
                        {
                            "id": "stack_1",
                            "kind": "duplicate",
                            "count": 2,
                            "assets": [server.asset],
                        }
                    ],
                }
            )
            return
        self._json({"object": "error"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        self.server.request_paths.append(self.path)
        if self.path != "/v1/retrieval/query":
            self._json({"object": "error"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.server.last_search = request
        self._json(
            {
                "object": "retrieval.query",
                "status": "completed",
                "data": [self.server.asset],
            }
        )


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.root / "Photo Library"
        (self.library / "trip").mkdir(parents=True)
        (self.library / "trip" / "sunset.jpg").write_bytes(b"not opened by plugin")
        self.db = self.root / "photo index.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE image_index (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    taken_at TEXT,
                    place_name TEXT,
                    country TEXT,
                    description TEXT,
                    tags_json TEXT,
                    combined_text TEXT,
                    aesthetic_score REAL,
                    technical_quality_score REAL,
                    embedding_backend TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO image_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "img_1",
                        "sunset.jpg",
                        "trip/sunset.jpg",
                        "2025-07-10T20:00:00",
                        "Big Sur",
                        "United States",
                        "Orange sunset above the Pacific coast",
                        '["sunset", "ocean", "coast"]',
                        "orange sunset pacific ocean big sur",
                        0.9,
                        0.8,
                        "semantic_hash",
                    ),
                    (
                        "img_2",
                        "unsafe.jpg",
                        "../unsafe.jpg",
                        None,
                        None,
                        None,
                        "Ocean sunset outside the library",
                        '["sunset"]',
                        "ocean sunset",
                        0.2,
                        0.2,
                        "semantic_hash",
                    ),
                ],
            )
            connection.commit()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MemoLensHandler)
        self.server.library_dir = self.library
        self.server.db_path = self.db
        self.server.decoy_dir = self.root / "health-decoy"
        self.server.valid_identity = True
        self.server.valid_settings = True
        self.server.redirect_health = False
        self.server.settings_requests = 0
        self.server.request_paths = []
        self.server.asset_count = 2
        self.server.last_search = None
        self.server.asset = {
            "id": "img_1",
            "filename": "sunset.jpg",
            "relative_path": "trip/sunset.jpg",
            "description": "Orange sunset above the Pacific coast",
            "tags": ["sunset", "ocean"],
            "score": 9.4,
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def gateway(self, *, trust_local_api: bool = False, **kwargs) -> MemoLensGateway:
        with mock.patch.dict(
            os.environ,
            {TRUST_LOCAL_API_ENV: "1" if trust_local_api else "0"},
            clear=False,
        ):
            return MemoLensGateway(**kwargs)

    def test_safe_default_never_contacts_or_trusts_loopback_service(self) -> None:
        self.server.library_dir = self.server.decoy_dir
        self.server.db_path = self.server.decoy_dir / "decoy.db"
        gateway = self.gateway(
            base_url=self.base_url,
            db_path=self.db,
            library_dir=self.library,
            timeout=1,
        )

        source_before = _sqlite_source_state(self.db)
        status = gateway.status()
        self.assertEqual(status["mode"], "safe_default_read_only")
        self.assertEqual(status["source"], "sqlite_read_only")
        self.assertFalse(status["local_api"]["enabled"])
        self.assertFalse(status["local_api"]["authenticated"])
        self.assertFalse(status["local_api"]["checked"])
        self.assertIsNone(status["local_api"]["available"])
        self.assertNotIn("path", status["database"])
        self.assertNotIn("library_dir", status)
        self.assertNotIn(str(self.root), json.dumps(status))

        search = gateway.search("ocean sunset", limit=5)
        self.assertEqual(search["source"], "sqlite_read_only")
        safe_match = next(item for item in search["results"] if item["id"] == "img_1")
        self.assertEqual(
            safe_match["absolute_path"],
            str((self.library / "trip" / "sunset.jpg").resolve()),
        )
        self.assertEqual(self.server.request_paths, [])
        self.assertEqual(_sqlite_source_state(self.db), source_before)
        with self.assertRaisesRegex(MemoLensError, TRUST_LOCAL_API_ENV) as memories:
            gateway.memories()
        self.assertEqual(memories.exception.code, "local_api_not_trusted")
        with self.assertRaises(MemoLensError) as cleanup:
            gateway.cleanup()
        self.assertEqual(cleanup.exception.code, "local_api_not_trusted")
        self.assertEqual(self.server.request_paths, [])

        with mock.patch.dict(
            os.environ, {TRUST_LOCAL_API_ENV: "true"}, clear=False
        ):
            truthy_alias = MemoLensGateway(db_path=self.db, library_dir=self.library)
        self.assertFalse(truthy_alias.trust_local_api)

    def test_api_commands_verify_identity_and_use_settings_paths(self) -> None:
        gateway = self.gateway(
            trust_local_api=True, base_url=self.base_url, timeout=1
        )
        status = gateway.status()
        self.assertEqual(status["source"], "local_api")
        self.assertEqual(status["mode"], "opt_in_local_api")
        self.assertTrue(status["local_api"]["trusted_by_user"])
        self.assertFalse(status["local_api"]["authenticated"])
        self.assertNotIn("library_dir", status)
        self.assertNotIn("path", status["database"])
        self.assertNotIn(str(self.root), json.dumps(status))
        self.assertEqual(status["database"]["index_stats"]["asset_count"], 2)
        self.assertEqual(gateway.library_dir, self.library.resolve())
        self.assertEqual(gateway.db_path, self.db.resolve())
        self.assertEqual(self.server.request_paths[:2], ["/healthz", "/v1/settings"])

        search = gateway.search("ocean sunset", limit=5)
        self.assertEqual(search["results"][0]["path_status"], "ok")
        self.assertEqual(
            search["results"][0]["absolute_path"],
            str((self.library / "trip" / "sunset.jpg").resolve()),
        )
        self.assertFalse(self.server.last_search["include_copy"])
        self.assertEqual(gateway.memories()["memory_count"], 1)
        cleanup = gateway.cleanup()
        self.assertTrue(cleanup["read_only"])
        self.assertEqual(cleanup["counts"]["duplicate_stack_count"], 1)

    def test_identity_mismatch_is_rejected_before_settings(self) -> None:
        self.server.valid_identity = False
        gateway = self.gateway(
            trust_local_api=True,
            base_url=self.base_url,
            db_path=self.db,
            timeout=1,
        )
        status = gateway.status()
        self.assertEqual(status["source"], "sqlite_fallback")
        self.assertEqual(
            status["local_api"]["error_code"], "service_identity_mismatch"
        )
        self.assertEqual(self.server.settings_requests, 0)

    def test_invalid_settings_are_not_cached_as_valid_health(self) -> None:
        self.server.valid_settings = False
        gateway = self.gateway(
            trust_local_api=True,
            base_url=self.base_url,
            db_path=self.db,
            timeout=1,
        )
        first = gateway.status()
        self.assertEqual(first["source"], "sqlite_fallback")
        self.assertEqual(first["local_api"]["error_code"], "invalid_response")

        self.server.valid_settings = True
        second = gateway.status()
        self.assertEqual(second["source"], "local_api")
        self.assertEqual(self.server.settings_requests, 2)

    def test_valid_identity_and_settings_are_cached_as_one_verified_pair(self) -> None:
        gateway = self.gateway(
            trust_local_api=True,
            base_url=self.base_url,
            timeout=1,
        )

        first = gateway.health()
        second = gateway.health()

        self.assertIs(first, second)
        self.assertEqual(self.server.request_paths, ["/healthz", "/v1/settings"])
        self.assertEqual(self.server.settings_requests, 1)

    def test_transport_refuses_redirects_and_bounds_timeout_and_response_size(self) -> None:
        self.server.redirect_health = True
        gateway = self.gateway(
            trust_local_api=True,
            base_url=self.base_url,
            timeout=999,
        )
        self.assertEqual(gateway.timeout, 30.0)
        with self.assertRaises(MemoLensError) as redirect:
            gateway.health()
        self.assertEqual(redirect.exception.code, "redirect_refused")
        self.assertEqual(self.server.settings_requests, 0)

        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, amount: int) -> bytes:
                self.amount = amount
                return b"x" * amount

        class RecordingOpener:
            def __init__(self) -> None:
                self.response = OversizedResponse()
                self.timeout = None

            def open(self, _request, *, timeout: float):
                self.timeout = timeout
                return self.response

        opener = RecordingOpener()
        gateway._opener = opener
        with self.assertRaises(MemoLensError) as oversized:
            gateway._request_json("/healthz", verify_identity=False)
        self.assertEqual(oversized.exception.code, "response_too_large")
        self.assertEqual(opener.response.amount, MAX_RESPONSE_BYTES + 1)
        self.assertEqual(opener.timeout, 30.0)

        minimum_timeout = self.gateway(
            trust_local_api=True,
            base_url=self.base_url,
            timeout=0,
        )
        self.assertEqual(minimum_timeout.timeout, 0.1)

    def test_sqlite_fallback_is_read_only_and_rejects_traversal(self) -> None:
        gateway = self.gateway(
            base_url="http://127.0.0.1:1",
            db_path=self.db,
            library_dir=self.library,
            timeout=0.1,
        )
        self.assertEqual(gateway.status()["status"], "ok")
        result = gateway.search("ocean sunset", limit=10)
        self.assertEqual(result["source"], "sqlite_read_only")
        self.assertEqual(result["scanned_count"], 2)
        statuses = {item["filename"]: item["path_status"] for item in result["results"]}
        self.assertEqual(statuses["sunset.jpg"], "ok")
        self.assertEqual(statuses["unsafe.jpg"], "rejected_outside_library")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_index").fetchone()[0], 2
            )

    def test_sqlite_connection_uses_uri_read_only_and_query_only(self) -> None:
        gateway = self.gateway(db_path=self.db, library_dir=self.library)
        source_before = _sqlite_source_state(self.db)
        with closing(gateway._sqlite_connection()) as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            # A checkpointed source uses an immutable private snapshot. SQLite
            # reports `delete` for that snapshot even though the validated source
            # database header remains WAL-mode.
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden_write (id INTEGER)")
        self.assertEqual(_sqlite_source_state(self.db), source_before)

    def test_sqlite_requires_wal_and_rejects_unsafe_sidecars(self) -> None:
        rollback = self.root / "rollback.db"
        with closing(sqlite3.connect(rollback)) as connection:
            connection.execute("CREATE TABLE image_index (id TEXT,filename TEXT,relative_path TEXT)")
            connection.commit()
        with self.assertRaises(MemoLensError) as required:
            self.gateway(db_path=rollback)._sqlite_connection()
        self.assertEqual(required.exception.code, "database_wal_required")

        unsafe = self.root / "unsafe-wal.db"
        with closing(sqlite3.connect(unsafe)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE image_index (id TEXT,filename TEXT,relative_path TEXT)")
            connection.commit()
        Path(f"{unsafe}-wal").write_bytes(b"not-a-valid-wal")
        Path(f"{unsafe}-shm").write_bytes(b"not-a-valid-shm")
        with self.assertRaises(MemoLensError) as unsafe_error:
            self.gateway(db_path=unsafe)._sqlite_connection()
        self.assertEqual(unsafe_error.exception.code, "database_wal_unsafe")

    def test_read_only_connection_consumes_a_valid_live_wal_without_writing(self) -> None:
        writer = sqlite3.connect(self.db)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO image_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "wal_match",
                    "wal.jpg",
                    "trip/wal.jpg",
                    None,
                    None,
                    None,
                    "visible only through the live wal",
                    '["live-wal"]',
                    "live wal",
                    0.5,
                    0.5,
                    "semantic_hash",
                ),
            )
            writer.commit()
            wal_path = Path(f"{self.db}-wal")
            shm_path = Path(f"{self.db}-shm")
            self.assertGreater(wal_path.stat().st_size, 32)
            self.assertGreaterEqual(shm_path.stat().st_size, 32_768)
            source_before = _sqlite_source_state(self.db)
            result = self.gateway(
                db_path=self.db, library_dir=self.library
            ).search("live wal")
            self.assertEqual(result["results"][0]["id"], "wal_match")
            self.assertEqual(_sqlite_source_state(self.db), source_before)
        finally:
            writer.close()

    def test_read_only_connection_rejects_a_live_wal_with_corrupt_frame(self) -> None:
        database = self.root / "checksum.db"
        writer = sqlite3.connect(database)
        original_wal: bytes | None = None
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE image_index (id TEXT PRIMARY KEY)")
            writer.execute("INSERT INTO image_index VALUES ('in-live-wal')")
            writer.commit()

            wal_path = Path(f"{database}-wal")
            original_wal = wal_path.read_bytes()
            self.assertGreater(len(original_wal), 56)
            corrupt_wal = bytearray(original_wal)
            corrupt_wal[56] ^= 0xFF
            wal_path.write_bytes(corrupt_wal)

            with self.assertRaises(MemoLensError) as unsafe_error:
                self.gateway(db_path=database)._sqlite_connection()
            self.assertEqual(unsafe_error.exception.code, "database_wal_unsafe")
        finally:
            if original_wal is not None:
                Path(f"{database}-wal").write_bytes(original_wal)
            writer.close()

    def test_sqlite_fallback_streams_past_ten_thousand_with_bounded_results(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executemany(
                "INSERT INTO image_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        f"filler_{index}",
                        f"filler-{index}.jpg",
                        f"bulk/filler-{index}.jpg",
                        None,
                        None,
                        None,
                        "ordinary archive frame",
                        "[]",
                        "ordinary archive frame",
                        0.1,
                        0.1,
                        "semantic_hash",
                    )
                    for index in range(10_001)
                ),
            )
            connection.execute(
                "INSERT INTO image_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "late_match",
                    "rare-zebra.jpg",
                    "bulk/rare-zebra.jpg",
                    "2026-01-01T00:00:00",
                    None,
                    None,
                    "A rare zebra appears after ten thousand indexed rows",
                    '["rare", "zebra"]',
                    "rare zebra",
                    1.0,
                    1.0,
                    "semantic_hash",
                ),
            )
            connection.commit()

        gateway = self.gateway(
            base_url="http://127.0.0.1:1",
            db_path=self.db,
            library_dir=self.library,
            timeout=0.1,
        )
        result = gateway.search("rare zebra", limit=3)
        self.assertEqual(result["scanned_count"], 10_004)
        self.assertLessEqual(len(result["results"]), 3)
        self.assertEqual(result["results"][0]["id"], "late_match")

    def test_loopback_resolution_and_proxy_bypass_are_enforced(self) -> None:
        with self.assertRaises(MemoLensError):
            validate_base_url("https://example.com")

        mixed_addresses = [
            (2, 1, 6, "", ("127.0.0.1", 5519)),
            (2, 1, 6, "", ("203.0.113.9", 5519)),
        ]
        with mock.patch("memolens_core.socket.getaddrinfo", return_value=mixed_addresses):
            with self.assertRaises(MemoLensError):
                validate_base_url("http://localhost:5519")

        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://203.0.113.9:8080",
                TRUST_LOCAL_API_ENV: "1",
            },
            clear=False,
        ):
            gateway = MemoLensGateway(base_url="http://127.0.0.1:1", timeout=0.1)
        proxy_handlers = [
            handler
            for handler in gateway._opener.handlers
            if isinstance(handler, ProxyHandler)
        ]
        # Supplying ProxyHandler({}) suppresses urllib's environment-derived
        # default handler; build_opener omits the empty handler itself.
        self.assertEqual(proxy_handlers, [])

    def test_discovery_never_uses_current_working_directory(self) -> None:
        hostile_cwd = self.root / "hostile-cwd"
        (hostile_cwd / ".memolens-state").mkdir(parents=True)
        previous_cwd = Path.cwd()
        try:
            os.chdir(hostile_cwd)
            with mock.patch.dict(
                os.environ,
                {"MEMOLENS_APP_STATE_DIR": "", "XDG_STATE_HOME": ""},
                clear=False,
            ):
                candidates = _state_dir_candidates()
        finally:
            os.chdir(previous_cwd)
        self.assertNotIn((hostile_cwd / ".memolens-state").resolve(), candidates)

    def test_marketplace_resolves_from_repository_root(self) -> None:
        manifest_path = MARKETPLACE_ROOT / ".agents" / "plugins" / "marketplace.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        release_version = json.loads(
            (MARKETPLACE_ROOT / "package.json").read_text(encoding="utf-8")
        )["version"]
        self.assertEqual(PLUGIN_VERSION, release_version)
        self.assertEqual(manifest["name"], "memolens-local")
        entry = next(item for item in manifest["plugins"] if item["name"] == "memolens")
        resolved_plugin = (MARKETPLACE_ROOT / entry["source"]["path"]).resolve()
        self.assertEqual(resolved_plugin, PLUGIN_ROOT)
        self.assertTrue((resolved_plugin / ".codex-plugin" / "plugin.json").is_file())
        plugin_manifest = json.loads(
            (resolved_plugin / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            plugin_manifest["interface"]["composerIcon"], "./assets/memolens.svg"
        )
        self.assertEqual(plugin_manifest["interface"]["logo"], "./assets/memolens.svg")
        self.assertEqual(
            plugin_manifest["version"].split("+", maxsplit=1)[0],
            PLUGIN_VERSION,
        )
        self.assertTrue(
            plugin_manifest["version"].startswith(f"{release_version}+codex."),
            plugin_manifest["version"],
        )
        prompts = plugin_manifest["interface"]["defaultPrompt"]
        self.assertIsInstance(prompts, list)
        self.assertEqual(len(prompts), 3)
        self.assertTrue(all(isinstance(prompt, str) for prompt in prompts))
        self.assertTrue(all(1 <= len(prompt) <= 128 for prompt in prompts))
        self.assertEqual(entry["policy"]["authentication"], "ON_USE")
        self.assertIn(
            release_version,
            (resolved_plugin / "README.md").read_text(encoding="utf-8"),
        )

    def test_cli_and_configured_mcp_start_from_non_repo_cwd_with_clean_stdout(self) -> None:
        cli_env = os.environ.copy()
        cli_env[TRUST_LOCAL_API_ENV] = "0"
        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "memolens_cli.py"),
                "--base-url",
                "http://127.0.0.1:1",
                "--db",
                str(self.db),
                "--library",
                str(self.library),
                "--timeout",
                "0.1",
                "status",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=cli_env,
        )
        self.assertEqual(json.loads(cli.stdout)["source"], "sqlite_read_only")
        self.assertEqual(cli.stderr, "")

        config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server_config = config["mcpServers"]["memolens"]
        self.assertEqual(server_config["command"], "python3")
        self.assertIsNotNone(shutil.which(server_config["command"]))
        self.assertEqual(server_config["cwd"], ".")
        self.assertIn(TRUST_LOCAL_API_ENV, server_config["env_vars"])

        env = os.environ.copy()
        env.update(
            {
                "MEMOLENS_BASE_URL": "http://127.0.0.1:1",
                "MEMOLENS_DB_PATH": str(self.db),
                "MEMOLENS_LIBRARY_DIR": str(self.library),
                TRUST_LOCAL_API_ENV: "0",
            }
        )
        frames = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2099-01-01"},
                    }
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "memolens_status", "arguments": {}},
                    }
                ),
            ]
        ) + "\n"
        mcp_cwd = (PLUGIN_ROOT / server_config["cwd"]).resolve()
        mcp = subprocess.run(
            [server_config["command"], *server_config["args"]],
            cwd=mcp_cwd,
            input=frames,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(mcp.stderr, "")
        stdout_lines = [line for line in mcp.stdout.splitlines() if line]
        responses = [json.loads(line) for line in stdout_lines]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(responses[1]["result"]["tools"]), 15)
        self.assertEqual(
            responses[2]["result"]["structuredContent"]["source"],
            "sqlite_read_only",
        )


if __name__ == "__main__":
    unittest.main()
