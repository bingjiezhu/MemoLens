from __future__ import annotations

import hashlib
import hmac
import json
import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import MediaRepository, canonical_json
from backend.src import create_app, shutdown_runtime_extensions, swap_runtime
from backend.src.media.library_bootstrap import (
    BOOTSTRAP_BINDING_DOMAIN,
    BootstrapCandidateBinding,
    LibraryBootstrapError,
    candidate_binding_sha256,
    commit_library_bootstrap,
    load_bootstrap_candidate_binding,
    resolve_bootstrap_database_path,
)
from backend.src.media.video import MediaJobRunner


REQUEST_ID = f"lb_{'a' * 64}"
BINDING_SHA256 = "b" * 64
DATABASE_UUID = "12345678-1234-4123-8123-123456789abc"
MAIN_TOKEN = "main-authority-token"
DESKTOP_TOKEN = "desktop-session-token"
CHALLENGE = "c" * 64


def _response() -> dict[str, object]:
    return {
        "object": "memolens.library_bootstrap_result",
        "schema_version": "1",
        "request_id": REQUEST_ID,
        "status": "library_authority_committed",
        "scan_started": False,
        "scan_stage": "awaiting_scan_worker",
        "editor_state": "awaiting_grounded_timeline",
        "blueprint_authority": "unverified",
        "timeline_edit_granted": False,
    }


class LibraryBootstrapRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-bootstrap-runtime-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.database_path = self.root / "state" / "media.db"
        self.database_path.parent.mkdir()
        self.root_fd = os.open(
            self.library,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        identity = os.fstat(self.root_fd)
        self.binding = BootstrapCandidateBinding(
            request_id=REQUEST_ID,
            intent_created_at_ms=1_000,
            intent_expires_at_ms=301_000,
            native_confirmed_at_ms=2_000,
            canonical_root=self.library,
            database_path=self.database_path,
            expected_library_root_device=int(identity.st_dev),
            expected_library_root_inode=int(identity.st_ino),
            candidate_binding_sha256=BINDING_SHA256,
            root_fd=self.root_fd,
            settings=Mock(spec=Settings),
        )

    def tearDown(self) -> None:
        self.binding.close()
        self.temporary.cleanup()

    @staticmethod
    def _candidate_app(binding: BootstrapCandidateBinding):
        with patch.dict(
            os.environ,
            {
                "MEMOLENS_DESKTOP_SESSION_TOKEN": DESKTOP_TOKEN,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": MAIN_TOKEN,
            },
        ), patch.object(Settings, "from_env", side_effect=AssertionError("default runtime")), patch(
            "backend.src.configure_runtime"
        ) as configure:
            app = create_app(bootstrap_candidate=binding)
        configure.assert_not_called()
        app.config.update(TESTING=True)
        return app

    def test_fixed_unicode_binding_digest_matches_cross_language_vector(self) -> None:
        self.assertEqual(
            candidate_binding_sha256(
                canonical_root="/tmp/素材 库",
                database_path="/tmp/状态/MemoLens 数据.db",
                expected_library_root_device="42",
                expected_library_root_inode="9007199254740991",
            ),
            "7ae5b68db2ac7109d00072bd9585d7e4d00f3fbdb87f944313dcec144348145f",
        )
        material = canonical_json(
            {
                "canonical_root": "/tmp/素材 库",
                "database_path": "/tmp/状态/MemoLens 数据.db",
                "expected_library_root_device": "42",
                "expected_library_root_inode": "9007199254740991",
            }
        ).encode("utf-8")
        self.assertTrue(BOOTSTRAP_BINDING_DOMAIN.endswith(b"\0"))
        self.assertEqual(
            hashlib.sha256(BOOTSTRAP_BINDING_DOMAIN + material).hexdigest(),
            "7ae5b68db2ac7109d00072bd9585d7e4d00f3fbdb87f944313dcec144348145f",
        )

    def test_candidate_app_never_builds_a_default_runtime(self) -> None:
        app = self._candidate_app(self.binding)
        manager = app.extensions["runtime_manager"]
        self.assertIsNone(manager.current_bundle_or_none)
        identity = app.config["RUNTIME_HEALTH_IDENTITY"]
        self.assertEqual(identity.mode, "bootstrap_candidate")
        self.assertEqual(identity.bootstrap_request_id, REQUEST_ID)
        self.assertEqual(identity.candidate_binding_sha256, BINDING_SHA256)

    def test_health_v2_is_closed_and_binds_the_exact_candidate(self) -> None:
        app = self._candidate_app(self.binding)
        response = app.test_client().get(f"/healthz?contract=2&challenge={CHALLENGE}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            set(payload),
            {
                "api_version",
                "challenge_proof",
                "object",
                "runtime",
                "schema_version",
                "service",
                "sqlite_runtime",
                "status",
            },
        )
        self.assertEqual(
            payload["runtime"],
            {
                "mode": "bootstrap_candidate",
                "bootstrap_request_id": REQUEST_ID,
                "candidate_binding_sha256": BINDING_SHA256,
                "database_uuid": None,
                "schema_version": None,
                "runtime_generation_id": None,
            },
        )
        sqlite_runtime = payload["sqlite_runtime"]
        proof_message = "\n".join(
            [
                "memolens.health.v2",
                f"challenge={CHALLENGE}",
                "object=health.check",
                "status=ok",
                "service=memolens-backend",
                "api_version=1",
                "schema_version=2",
                f"policy_id={sqlite_runtime['policy_id']}",
                f"sqlite_version={sqlite_runtime['sqlite_version']}",
                "wal_reset_safe=true",
                f"journal_policy={sqlite_runtime['journal_policy']}",
                "runtime_mode=bootstrap_candidate",
                f"bootstrap_request_id={REQUEST_ID}",
                f"candidate_binding_sha256={BINDING_SHA256}",
                "database_uuid=null",
                "runtime_schema_version=null",
                "runtime_generation_id=null",
            ]
        )
        self.assertEqual(
            payload["challenge_proof"],
            hmac.new(
                DESKTOP_TOKEN.encode(),
                proof_message.encode(),
                "sha256",
            ).hexdigest(),
        )

    def test_main_authority_is_rejected_before_json_body_is_read(self) -> None:
        app = self._candidate_app(self.binding)
        with patch("backend.src.api.routes.strict_json_object") as decoder:
            response = app.test_client().post(
                "/v1/main/library-bootstrap",
                data=b"{" * 100,
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 403)
        decoder.assert_not_called()

    def test_closed_main_command_returns_original_body_and_replay_header_only(self) -> None:
        app = self._candidate_app(self.binding)
        command = {
            "object": "memolens.main_library_bootstrap_command",
            "schema_version": "1",
            "request_id": REQUEST_ID,
            "candidate_binding_sha256": BINDING_SHA256,
        }
        commit = Mock()
        commit.side_effect = [
            Mock(
                response=_response(),
                response_status=201,
                replayed=False,
                database_uuid=DATABASE_UUID,
            ),
            Mock(
                response=_response(),
                response_status=201,
                replayed=True,
                database_uuid=DATABASE_UUID,
            ),
        ]
        headers = {
            "X-MemoLens-Main-Authority": MAIN_TOKEN,
            "Idempotency-Key": REQUEST_ID,
        }
        with patch("backend.src.api.routes.commit_library_bootstrap", commit), patch(
            "backend.src.api.routes.build_runtime_extensions", return_value={}
        ), patch("backend.src.api.routes.swap_runtime"):
            first = app.test_client().post(
                "/v1/main/library-bootstrap",
                json=command,
                headers=headers,
            )
            replay = app.test_client().post(
                "/v1/main/library-bootstrap",
                json=command,
                headers=headers,
            )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(first.data, replay.data)
        self.assertNotIn("Idempotency-Replayed", first.headers)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")

    def test_wrong_binding_command_fails_before_core_commit(self) -> None:
        app = self._candidate_app(self.binding)
        valid_command = {
            "object": "memolens.main_library_bootstrap_command",
            "schema_version": "1",
            "request_id": REQUEST_ID,
            "candidate_binding_sha256": BINDING_SHA256,
        }
        cases = (
            (
                "candidate binding",
                {**valid_command, "candidate_binding_sha256": "d" * 64},
                REQUEST_ID,
            ),
            (
                "bootstrap request",
                {**valid_command, "request_id": f"lb_{'e' * 64}"},
                REQUEST_ID,
            ),
            ("idempotency authority", valid_command, f"lb_{'f' * 64}"),
        )
        for label, command, idempotency_key in cases:
            with self.subTest(label=label), patch(
                "backend.src.api.routes.commit_library_bootstrap"
            ) as commit:
                response = app.test_client().post(
                    "/v1/main/library-bootstrap",
                    json=command,
                    headers={
                        "X-MemoLens-Main-Authority": MAIN_TOKEN,
                        "Idempotency-Key": idempotency_key,
                    },
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.get_json()["code"],
                    "library_bootstrap_binding_mismatch",
                )
                commit.assert_not_called()

    def test_real_commit_swap_keeps_scan_queued_and_health_binds_active_runtime(self) -> None:
        settings = Settings.for_bootstrap_candidate(
            image_library_dir=self.library,
            db_path=self.database_path,
        )
        identity = os.fstat(self.root_fd)
        digest = candidate_binding_sha256(
            canonical_root=str(self.library),
            database_path=str(self.database_path),
            expected_library_root_device=str(identity.st_dev),
            expected_library_root_inode=str(identity.st_ino),
        )
        binding = BootstrapCandidateBinding(
            request_id=REQUEST_ID,
            intent_created_at_ms=1_000,
            intent_expires_at_ms=301_000,
            native_confirmed_at_ms=2_000,
            canonical_root=self.library,
            database_path=self.database_path,
            expected_library_root_device=int(identity.st_dev),
            expected_library_root_inode=int(identity.st_ino),
            candidate_binding_sha256=digest,
            root_fd=self.root_fd,
            settings=settings,
        )
        self.binding.root_fd = -1
        self.binding = binding
        app = self._candidate_app(binding)
        command = {
            "object": "memolens.main_library_bootstrap_command",
            "schema_version": "1",
            "request_id": REQUEST_ID,
            "candidate_binding_sha256": digest,
        }
        headers = {
            "X-MemoLens-Main-Authority": MAIN_TOKEN,
            "Idempotency-Key": REQUEST_ID,
        }
        built_extensions: list[dict[str, object]] = []

        def build(settings_arg, *, schema_prepared=False):
            self.assertIs(settings_arg, settings)
            self.assertTrue(schema_prepared)
            extensions: dict[str, object] = {
                "media_repository": MediaRepository(settings.db_path),
            }
            built_extensions.append(extensions)
            return extensions

        with patch("backend.src.api.routes.build_runtime_extensions", side_effect=build), patch(
            "backend.src.api.routes.swap_runtime", side_effect=swap_runtime
        ):
            first = app.test_client().post(
                "/v1/main/library-bootstrap",
                json=command,
                headers=headers,
            )
            replay = app.test_client().post(
                "/v1/main/library-bootstrap",
                json=command,
                headers=headers,
            )
        try:
            self.assertEqual(first.status_code, 201)
            self.assertEqual(replay.status_code, 201)
            self.assertEqual(first.data, replay.data)
            self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
            self.assertEqual(len(built_extensions), 1)
            with closing(sqlite3.connect(self.database_path)) as connection:
                jobs = connection.execute(
                    "SELECT kind,status,stage FROM media_jobs WHERE kind='library_scan'"
                ).fetchall()
            self.assertEqual(jobs, [("library_scan", "queued", "awaiting_scan_worker")])
            # A fresh bootstrap must initialize the compatibility baseline
            # before V14 closes legacy DDL. A normal restart validates it.
            ImageIndexRepository(self.database_path).ensure_schema()

            health = app.test_client().get(
                f"/healthz?contract=2&challenge={CHALLENGE}"
            ).get_json()
            runtime = health["runtime"]
            self.assertEqual(runtime["mode"], "active")
            self.assertEqual(runtime["bootstrap_request_id"], REQUEST_ID)
            self.assertEqual(runtime["candidate_binding_sha256"], digest)
            self.assertRegex(
                runtime["database_uuid"],
                re.compile(
                    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
                ),
            )
            self.assertEqual(runtime["schema_version"], 20)
            self.assertRegex(
                runtime["runtime_generation_id"],
                re.compile(r"\Aruntime_generation_[0-9a-f]{64}\Z"),
            )
        finally:
            for extensions in built_extensions:
                shutdown_runtime_extensions(extensions)

    def test_core_adapter_requires_migration_only_and_closed_response(self) -> None:
        class Repository:
            database_uuid = DATABASE_UUID

            def __init__(self) -> None:
                self.ensure_arguments: list[object] = []
                self.closed = False

            def ensure_schema(self, default_library_root=None) -> None:
                self.ensure_arguments.append(default_library_root)

            def commit_library_bootstrap(self, **_arguments):
                return {
                    "response": _response(),
                    "response_status": 201,
                    "replayed": False,
                }

            def close(self) -> None:
                self.closed = True

        repository = Repository()
        result = commit_library_bootstrap(
            self.binding,
            repository_factory=lambda _path: repository,  # type: ignore[arg-type]
        )
        self.assertEqual(result.response, _response())
        self.assertEqual(repository.ensure_arguments, [None])
        self.assertTrue(repository.closed)

        class InvalidRepository(Repository):
            def commit_library_bootstrap(self, **_arguments):
                response = _response()
                response["canonical_root"] = str(self_outer.library)
                return {
                    "response": response,
                    "response_status": 201,
                    "replayed": False,
                }

        self_outer = self
        with self.assertRaisesRegex(LibraryBootstrapError, "invalid"):
            commit_library_bootstrap(
                self.binding,
                repository_factory=lambda _path: InvalidRepository(),  # type: ignore[arg-type]
            )

    def test_sealed_binding_loader_uses_fixed_locator_and_pins_root_fd(self) -> None:
        app_state = self.root / "app-state"
        binding_dir = app_state / "library-bootstrap-bindings"
        binding_dir.mkdir(parents=True, mode=0o700)
        database_path = resolve_bootstrap_database_path(app_state, str(self.library))
        identity = os.stat(self.library, follow_symlinks=False)
        device = str(identity.st_dev)
        inode = str(identity.st_ino)
        digest = candidate_binding_sha256(
            canonical_root=str(self.library),
            database_path=str(database_path),
            expected_library_root_device=device,
            expected_library_root_inode=inode,
        )
        envelope = {
            "schema_version": "1",
            "kind": "library_bootstrap_candidate_binding",
            "request_id": REQUEST_ID,
            "intent_created_at_ms": 1_000,
            "intent_expires_at_ms": 301_000,
            "native_confirmed_at_ms": 2_000,
            "canonical_root": str(self.library),
            "database_path": str(database_path),
            "expected_library_root_device": device,
            "expected_library_root_inode": inode,
            "candidate_binding_sha256": digest,
        }
        binding_path = binding_dir / f"{REQUEST_ID}.binding.json"
        binding_path.write_text(json.dumps(envelope), encoding="utf-8")
        binding_path.chmod(0o600)
        with patch.object(
            Settings,
            "for_bootstrap_candidate",
            return_value=Mock(spec=Settings),
        ):
            loaded = load_bootstrap_candidate_binding(
                app_state_dir=app_state,
                request_id=REQUEST_ID,
            )
        try:
            self.assertEqual(loaded.candidate_binding_sha256, digest)
            self.assertEqual(
                (os.fstat(loaded.root_fd).st_dev, os.fstat(loaded.root_fd).st_ino),
                (identity.st_dev, identity.st_ino),
            )
        finally:
            loaded.close()

    def test_library_scan_dispatches_to_the_dedicated_active_runtime_runner(self) -> None:
        job_id = f"job_{'7' * 32}"
        library_root_id = f"root_{'e' * 24}"
        root_identity = os.stat(self.library, follow_symlinks=False)

        class Repository:
            database_uuid = DATABASE_UUID

            def __init__(self) -> None:
                self.updated = False

            def get_media_job(self, job_id: str) -> dict[str, object]:
                return {
                    "id": job_id,
                    "database_uuid": self.database_uuid,
                    "kind": "library_scan",
                    "status": "queued",
                    "stage": "awaiting_scan_worker",
                    "cancel_requested": False,
                }

            def get_library_scan_context(self, requested_job_id: str) -> dict[str, object]:
                return {
                    "object": "memolens.library_scan_context",
                    "schema_version": "1",
                    "request_id": REQUEST_ID,
                    "job_id": requested_job_id,
                    "database_uuid": self.database_uuid,
                    "library_root_id": library_root_id,
                    "canonical_root": str(self_library),
                    "expected_library_root_device": int(root_identity.st_dev),
                    "expected_library_root_inode": int(root_identity.st_ino),
                    "root_permission_fingerprint": "f" * 64,
                    "status": "queued",
                    "stage": "awaiting_scan_worker",
                    "attempt": 1,
                    "cancel_requested": False,
                    "checkpoint": {},
                    "error": None,
                }

            def update_media_job(self, _job_id: str, **_fields: object) -> None:
                self.updated = True

        self_library = self.library
        repository = Repository()
        runner = object.__new__(MediaJobRunner)
        runner.repository = repository
        runner._lock = MagicMock()
        runner._runtime_generation = f"runtime_generation_{'d' * 64}"
        runner._active_image_library_root_id = library_root_id
        runner._shutting_down = False
        runner._library_scan_processor = Mock()
        runner._run(job_id)
        self.assertFalse(repository.updated)
        runner._library_scan_processor.run.assert_called_once()
        _, kwargs = runner._library_scan_processor.run.call_args
        self.assertEqual(
            kwargs["runtime_generation_id"],
            f"runtime_generation_{'d' * 64}",
        )
        self.assertEqual(kwargs["active_library_root_id"], library_root_id)
        self.assertFalse(kwargs["should_stop"]())


if __name__ == "__main__":
    unittest.main()
