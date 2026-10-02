"""Automated fresh-process B2B4 cross-host continuity harness.

This test intentionally does not claim a real Codex/DeepSeek model, UI, or
human native-gesture journey.  It observes two separately launched Python
processes with different PIDs, start nonces, and credential-state roots.  Each
worker reports that three named authority environment variables are absent;
that is not evidence of OS sandboxing or inability to discover sibling files.
A test-owned main-authority client approves each pending pairing through the
real HTTP authority route.
"""

from __future__ import annotations

from contextlib import closing
import gc
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from unittest.mock import patch

from werkzeug.serving import WSGIRequestHandler, make_server

from backend.src import (
    MAIN_AUTHORITY_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from core.config import Settings
from core.media_db import MediaRepository, canonical_json

if __package__:
    from tests import test_timeline_lowering_api as timeline_fixture
else:
    import test_timeline_lowering_api as timeline_fixture


WORKER_PATH = (
    Path(__file__).resolve().parent / "harnesses/b2b4_cross_host_worker.py"
).resolve()
EVIDENCE_SCOPE = "automated_python_process_harness"
REPORTED_ABSENT_AUTHORITY_ENV_VARS = (
    "MEMOLENS_MAIN_AUTHORITY_TOKEN",
    "MEMOLENS_DESKTOP_SESSION_TOKEN",
    "SQLITE_DB_PATH",
)
COMMAND_TIMEOUT_SECONDS = {
    "pair": 30.0,
    "status": 30.0,
    # save can perform status, three canonical reads, nonce/write, then three
    # verification reads; each production HTTP request is bounded at 10s.
    "save": 120.0,
    "shutdown": 10.0,
}
MAX_CAPTURED_PROCESS_OUTPUT_CHARS = 1_048_576
HOST_CONTRACTS = {
    "codex": {
        "adapter": "codex_plugin_process_adapter",
        "claimed_client_label": "Codex automated process adapter",
    },
    "deepseek": {
        "adapter": "deepseek_harness_process_adapter",
        "claimed_client_label": "DeepSeek Harness automated process adapter",
    },
}


def _timeline_head_projection(head: dict[str, object]) -> dict[str, object]:
    return {
        key: head[key]
        for key in (
            "revision",
            "revision_sha256",
            "timeline_id",
            "timeline_content_sha256",
            "blueprint_binding",
            "coverage_binding",
            "operation_id",
        )
    }


class _SilentRequestHandler(WSGIRequestHandler):
    def log(self, type: str, message: str, *args: object) -> None:  # noqa: A002
        del type, message, args


class _AdapterProcess:
    def __init__(
        self,
        *,
        host: str,
        base_url: str,
        state_dir: Path,
    ) -> None:
        self.host = host
        self.contract = HOST_CONTRACTS[host]
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(mode=0o700, parents=True)
        environment = {
            key: value
            for key in ("LANG", "LC_ALL", "PATH", "TMPDIR")
            if (value := os.environ.get(key))
        }
        environment.update(
            {
                "MEMOLENS_APP_STATE_DIR": str(self.state_dir),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(WORKER_PATH),
                "--host",
                host,
                "--base-url",
                base_url,
            ],
            cwd=self.state_dir,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._request_id = 0
        self.pid: int | None = None
        self.process_nonce: str | None = None
        self._stdout_chunks: list[str] = []
        self._stdout_size = 0
        self._stdout_overflow = False
        self._stderr_chunks: list[str] = []
        self._stderr_size = 0
        self._stderr_overflow = False
        self._stderr_lock = threading.Lock()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"b2b4-{host}-stderr-drain",
            daemon=True,
        )
        try:
            self._stderr_thread.start()
        except BaseException:
            self._terminate()
            self._close_streams()
            raise

    def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        while True:
            chunk = stderr.read(4096)
            if not chunk:
                return
            with self._stderr_lock:
                remaining = MAX_CAPTURED_PROCESS_OUTPUT_CHARS - self._stderr_size
                if remaining > 0:
                    captured = chunk[:remaining]
                    self._stderr_chunks.append(captured)
                    self._stderr_size += len(captured)
                if len(chunk) > remaining:
                    self._stderr_overflow = True

    def _record_stdout(self, line: str) -> None:
        remaining = MAX_CAPTURED_PROCESS_OUTPUT_CHARS - self._stdout_size
        if remaining > 0:
            captured = line[:remaining]
            self._stdout_chunks.append(captured)
            self._stdout_size += len(captured)
        if len(line) > remaining:
            self._stdout_overflow = True

    def captured_output_bytes(self) -> tuple[bytes, bytes]:
        with self._stderr_lock:
            stderr = "".join(self._stderr_chunks).encode("utf-8")
        return "".join(self._stdout_chunks).encode("utf-8"), stderr

    def command(self, command: str, **payload: object) -> dict[str, object]:
        if self.process.stdin is None or self.process.stdout is None:
            raise AssertionError("adapter process pipes are unavailable")
        self._request_id += 1
        request = {
            "request_id": self._request_id,
            "command": command,
            **payload,
        }
        self.process.stdin.write(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        )
        self.process.stdin.flush()
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            timeout = COMMAND_TIMEOUT_SECONDS.get(command)
            if timeout is None:
                raise AssertionError(f"unsupported adapter command: {command}")
            if not selector.select(timeout=timeout):
                self._terminate()
                raise AssertionError(f"{self.host} adapter process timed out")
        finally:
            selector.close()
        line = self.process.stdout.readline()
        if not line:
            return_code = self.process.poll()
            self._terminate()
            raise AssertionError(
                f"{self.host} adapter exited before replying (status={return_code})"
            )
        self._record_stdout(line)
        if self._stdout_overflow:
            self._terminate()
            raise AssertionError(f"{self.host} adapter stdout exceeded capture limit")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            self._terminate()
            raise AssertionError(f"{self.host} adapter returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise AssertionError(f"{self.host} adapter response is not an object")
        if (
            response.get("request_id") != self._request_id
            or response.get("adapter") != self.contract["adapter"]
            or response.get("evidence_scope") != EVIDENCE_SCOPE
            or response.get("reported_absent_authority_env_vars")
            != list(REPORTED_ABSENT_AUTHORITY_ENV_VARS)
            or type(response.get("pid")) is not int
            or type(response.get("process_nonce")) is not str
        ):
            raise AssertionError(f"{self.host} adapter process evidence is invalid")
        observed_pid = int(response["pid"])
        observed_nonce = str(response["process_nonce"])
        if self.pid is None:
            self.pid = observed_pid
            self.process_nonce = observed_nonce
        elif self.pid != observed_pid or self.process_nonce != observed_nonce:
            raise AssertionError(f"{self.host} adapter process identity changed")
        if response.get("ok") is not True:
            error = response.get("error")
            if isinstance(error, dict):
                raise AssertionError(
                    f"{self.host} adapter rejected {command}: "
                    f"{error.get('code')} ({error.get('type')})"
                )
            raise AssertionError(f"{self.host} adapter rejected {command}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise AssertionError(f"{self.host} adapter result is not an object")
        return result

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                try:
                    result = self.command("shutdown")
                    if result != {"shutdown": True}:
                        raise AssertionError(
                            f"{self.host} adapter shutdown was invalid"
                        )
                    self.process.wait(timeout=5.0)
                except Exception:
                    self._terminate()
                    raise
            self._stderr_thread.join(timeout=5.0)
            if self._stderr_thread.is_alive():
                raise AssertionError(f"{self.host} stderr drain did not stop")
            with self._stderr_lock:
                stderr_size = self._stderr_size
                stderr_overflow = self._stderr_overflow
            if stderr_overflow:
                raise AssertionError(f"{self.host} adapter stderr exceeded capture limit")
            if stderr_size:
                raise AssertionError(
                    f"{self.host} adapter wrote {stderr_size} bytes to stderr"
                )
            if self.process.returncode != 0:
                raise AssertionError(
                    f"{self.host} adapter exited with {self.process.returncode}"
                )
        finally:
            if self.process.poll() is None:
                self._terminate()
            self._stderr_thread.join(timeout=5.0)
            self._close_streams()

    def _close_streams(self) -> None:
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None and not stream.closed:
                stream.close()

    def _terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3.0)


class B2B4AutomatedCrossHostProcessHarnessTests(unittest.TestCase):
    """Process/config separation only; this is not T053/T054 host acceptance."""

    def setUp(self) -> None:
        self.temporary = None
        self.environment = None
        self._environment_started = False
        self.app = None
        self.client = None
        self.repository = None
        self.blueprints = None
        self.coverage = None
        self.timelines = None
        self.legacy_timelines = None
        self.server = None
        self.server_thread = None
        self._server_thread_started = False
        self.workers: dict[str, _AdapterProcess] = {}
        self.projects: dict[str, dict[str, object]] = {}
        self._approval_sequence = 0
        try:
            self._set_up_resources()
        except BaseException:
            # unittest skips tearDown after setUp fails. Suppress cleanup
            # failures here so the setup exception remains the root cause.
            self._cleanup_resources(suppress_errors=True)
            raise

    def _set_up_resources(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-b2b4-process-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "authority" / "media.db"
        self.desktop_token = "b2b4-process-desktop-token"
        self.main_token = "b2b4-process-main-token"
        self.epoch = "epoch_b2b4_process_harness"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(Path(__file__).resolve().parents[1] / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "authority"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": self.main_token,
                "MEMOLENS_RUNTIME_AUTHORITY_EPOCH": self.epoch,
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()
        self._environment_started = True
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()
        self.repository = self.app.extensions["media_repository"]
        assert isinstance(self.repository, MediaRepository)
        self.blueprints = self.app.extensions["blueprint_service"]
        self.coverage = self.app.extensions["coverage_service"]
        self.timelines = self.app.extensions["timeline_lowering_service"]
        self.legacy_timelines = self.app.extensions["timeline_service"]
        self.token = self.desktop_token
        for direction in ("codex_then_deepseek", "deepseek_then_codex"):
            project_id, payload, _sources = self._build_project()
            materialized = self.timelines.materialize_first_cut(
                project_id,
                payload,
                idempotency_key=f"b2b4-process-materialize-{direction}",
                expected_database_uuid=self.repository.database_uuid,
            )
            self.assertEqual(materialized.response_status, 201)
            workspace = self.timelines.read(project_id)
            head = workspace.get("head")
            self.assertIsInstance(head, dict)
            self.projects[direction] = {
                "project_id": project_id,
                "initial_head": dict(head),
            }

        self.server = make_server(
            "127.0.0.1",
            0,
            self.app,
            threaded=True,
            request_handler=_SilentRequestHandler,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="b2b4-authority-http",
            daemon=True,
        )
        self.server_thread.start()
        self._server_thread_started = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        for host in HOST_CONTRACTS:
            worker = _AdapterProcess(
                host=host,
                base_url=self.base_url,
                state_dir=self.root / "hosts" / host,
            )
            # A live child is registered before the next Popen is attempted.
            self.workers[host] = worker

    def tearDown(self) -> None:
        self._cleanup_resources(suppress_errors=False)

    def _cleanup_resources(self, *, suppress_errors: bool) -> None:
        errors: list[BaseException] = []

        workers = tuple(self.workers.values())
        self.workers.clear()
        for worker in workers:
            try:
                worker.close()
            except BaseException as exc:  # Preserve cleanup for the other process.
                errors.append(exc)
                try:
                    worker._terminate()
                except BaseException as terminate_exc:
                    errors.append(terminate_exc)

        server = self.server
        thread = self.server_thread
        thread_started = self._server_thread_started
        self.server = None
        self.server_thread = None
        self._server_thread_started = False
        if server is not None:
            if thread_started:
                try:
                    server.shutdown()
                except BaseException as exc:
                    errors.append(exc)
            try:
                server.server_close()
            except BaseException as exc:
                errors.append(exc)
        if thread is not None and thread_started:
            try:
                thread.join(timeout=5.0)
                if thread.is_alive():
                    errors.append(RuntimeError("authority server thread did not stop"))
            except BaseException as exc:
                errors.append(exc)

        app = self.app
        self.app = None
        self.client = None
        self.repository = None
        self.blueprints = None
        self.coverage = None
        self.timelines = None
        self.legacy_timelines = None
        if app is not None:
            try:
                shutdown_runtime_extensions(app.extensions)
            except BaseException as exc:
                errors.append(exc)

        environment = self.environment
        environment_started = self._environment_started
        self.environment = None
        self._environment_started = False
        if environment is not None and environment_started:
            try:
                environment.stop()
            except BaseException as exc:
                errors.append(exc)

        temporary = self.temporary
        self.temporary = None
        if temporary is not None:
            try:
                temporary.cleanup()
            except BaseException as exc:
                errors.append(exc)

        if errors and not suppress_errors:
            raise errors[0]

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringApiTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        return timeline_fixture.TimelineLoweringApiTests._build_project(self)

    def _pair_and_approve(self, host: str, project_id: str) -> str:
        worker = self.workers[host]
        requested = worker.command("pair", project_id=project_id)
        self.assertEqual(requested.get("project_id"), project_id)
        self.assertEqual(
            requested.get("claimed_client_label"),
            HOST_CONTRACTS[host]["claimed_client_label"],
        )
        self.assertEqual(requested.get("actions"), ["timeline.apply_edit"])
        self.assertIs(requested.get("native_confirmation_required"), True)
        self.assertIs(requested.get("pairing_secret_exposed"), False)
        pairing_id = requested.get("pairing_id")
        self.assertIsInstance(pairing_id, str)
        presentation = self.client.get(
            f"/v1/main/agent/pairings/{pairing_id}/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        projected = presentation.json.get("presentation")
        self.assertIsInstance(projected, dict)
        self.assertEqual(projected.get("project_id"), project_id)
        self.assertEqual(
            projected.get("claimed_client_label"),
            HOST_CONTRACTS[host]["claimed_client_label"],
        )
        self.assertEqual(projected.get("actions"), ["timeline.apply_edit"])
        current = _timeline_head_projection(
            self.repository.get_canonical_timeline_head(project_id)
        )
        self.assertEqual(projected.get("observed_timeline_head"), current)
        self._approval_sequence += 1
        approved = self.client.post(
            f"/v1/main/agent/pairings/{pairing_id}/approve",
            json={
                "presentation_sha256": presentation.json["presentation_sha256"],
                "native_gesture_nonce": (
                    f"automated_harness_main_approval_{self._approval_sequence:04d}"
                ),
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(approved.status_code, 201, approved.json)
        status = worker.command("status", project_id=project_id)
        self.assertEqual(status.get("status"), "active")
        self.assertIs(status.get("write_ready"), True)
        self.assertEqual(status.get("max_operations"), 1)
        self.assertEqual(status.get("remaining_operations"), 1)
        self.assertEqual(
            status.get("capability_id"),
            approved.json.get("capability_id"),
        )
        return str(approved.json["capability_id"])

    def _assert_process_and_host_state_evidence(self) -> None:
        forbidden_keys = {
            "timeline",
            "timeline_head",
            "expected_timeline_head",
            "operation_id",
            "receipt_id",
            "edit",
            "pending",
            "draft",
            "session",
        }
        sensitive_values = (
            str(self.db_path).encode("utf-8"),
            self.main_token.encode("utf-8"),
            self.desktop_token.encode("utf-8"),
        )
        expected_relative_files = {
            Path("agent-credentials")
            / (
                "project-"
                + hashlib.sha256(
                    str(project["project_id"]).encode("utf-8")
                ).hexdigest()[:32]
                + ".json"
            )
            for project in self.projects.values()
        }
        for host, worker in self.workers.items():
            files = sorted(path for path in worker.state_dir.rglob("*") if path.is_file())
            self.assertEqual(
                {path.relative_to(worker.state_dir) for path in files},
                expected_relative_files,
                host,
            )
            for path in files:
                raw = path.read_bytes()
                for sensitive in sensitive_values:
                    self.assertNotIn(
                        sensitive,
                        raw,
                        f"{host} credential state contains forbidden authority bytes",
                    )
                credential = json.loads(raw)
                self.assertIsInstance(credential, dict)
                self.assertTrue(forbidden_keys.isdisjoint(credential))
                self.assertEqual(credential.get("status"), "active")
                self.assertEqual(credential.get("actions"), ["timeline.apply_edit"])
            stdout, stderr = worker.captured_output_bytes()
            self.assertFalse(worker._stdout_overflow, host)
            self.assertFalse(worker._stderr_overflow, host)
            self.assertEqual(stderr, b"", host)
            for stream_name, raw in (("stdout", stdout), ("stderr", stderr)):
                for sensitive in sensitive_values:
                    self.assertNotIn(
                        sensitive,
                        raw,
                        f"{host} {stream_name} contains forbidden authority bytes",
                    )

    def _assert_journey_ledger(
        self,
        *,
        project_id: str,
        initial_head: dict[str, object],
        first: dict[str, object],
        second: dict[str, object],
        first_capability: str,
        second_capability: str,
        first_host: str,
        second_host: str,
    ) -> None:
        first_saved = first["verified_save"]
        second_saved = second["verified_save"]
        self.assertIsInstance(first_saved, dict)
        self.assertIsInstance(second_saved, dict)
        first_head = first_saved["result_head"]
        second_head = second_saved["result_head"]
        self.assertEqual(first["database_uuid"], self.repository.database_uuid)
        self.assertEqual(second["database_uuid"], self.repository.database_uuid)
        self.assertEqual(first["project_id"], project_id)
        self.assertEqual(second["project_id"], project_id)
        self.assertEqual(first["before_head"], initial_head)
        self.assertEqual(first["reread_head"], first_head)
        self.assertEqual(second["before_head"], first_head)
        self.assertEqual(second["reread_head"], second_head)
        self.assertEqual(first_head["revision"], initial_head["revision"] + 1)
        self.assertEqual(second_head["revision"], initial_head["revision"] + 2)
        self.assertEqual(
            _timeline_head_projection(
                self.repository.get_canonical_timeline_head(project_id)
            ),
            second_head,
        )
        with closing(self.repository._connect()) as connection:
            operations = connection.execute(
                "SELECT * FROM canonical_timeline_operations "
                "WHERE project_id=? ORDER BY sequence",
                (project_id,),
            ).fetchall()
            revisions = connection.execute(
                "SELECT * FROM canonical_timeline_revisions "
                "WHERE project_id=? ORDER BY revision",
                (project_id,),
            ).fetchall()
            desktop_receipts = connection.execute(
                "SELECT * FROM canonical_timeline_receipts WHERE project_id=?",
                (project_id,),
            ).fetchall()
            paired_receipts = connection.execute(
                "SELECT * FROM agent_project_command_receipts "
                "WHERE project_id=? ORDER BY created_at,operation_id",
                (project_id,),
            ).fetchall()
            self.assertEqual(len(operations), 3)
            self.assertEqual(len(revisions), 3)
            self.assertEqual(len(desktop_receipts), 1)
            self.assertEqual(len(paired_receipts), 2)
            self.assertEqual([row["sequence"] for row in operations], [1, 2, 3])
            self.assertEqual(operations[0]["id"], initial_head["operation_id"])
            self.assertEqual(operations[1]["id"], first_saved["operation_id"])
            self.assertEqual(operations[2]["id"], second_saved["operation_id"])
            self.assertEqual(operations[1]["parent_operation_id"], operations[0]["id"])
            self.assertEqual(operations[2]["parent_operation_id"], operations[1]["id"])

            expected_by_operation = {
                str(first_saved["operation_id"]): (
                    first,
                    first_capability,
                    first_host,
                ),
                str(second_saved["operation_id"]): (
                    second,
                    second_capability,
                    second_host,
                ),
            }
            for receipt in paired_receipts:
                evidence, capability_id, host = expected_by_operation[
                    str(receipt["operation_id"])
                ]
                command_result = evidence["command_result"]
                request_value = json.loads(receipt["request_json"])
                actor = json.loads(receipt["actor_json"])
                origin = json.loads(receipt["origin_json"])
                self.assertEqual(receipt["database_uuid"], self.repository.database_uuid)
                self.assertEqual(receipt["authenticated_principal"], "paired_agent")
                self.assertEqual(receipt["capability_id"], capability_id)
                self.assertEqual(
                    receipt["claimed_client_label"],
                    HOST_CONTRACTS[host]["claimed_client_label"],
                )
                self.assertEqual(receipt["command_type"], "timeline.apply_edit")
                self.assertEqual(receipt["resource_type"], "canonical_timeline")
                self.assertEqual(receipt["response_status"], 201)
                self.assertEqual(receipt["response_json"], canonical_json(command_result))
                self.assertEqual(
                    hashlib.sha256(receipt["response_json"].encode()).hexdigest(),
                    receipt["response_sha256"],
                )
                self.assertEqual(
                    request_value["expected_timeline_head"],
                    evidence["before_head"],
                )
                self.assertEqual(request_value["edit"], evidence["edit"])
                self.assertEqual(request_value["project_id"], project_id)
                self.assertEqual(
                    request_value["expected_database_uuid"],
                    self.repository.database_uuid,
                )
                self.assertEqual(
                    hashlib.sha256(receipt["request_json"].encode()).hexdigest(),
                    receipt["request_sha256"],
                )
                self.assertEqual(
                    actor,
                    {
                        "kind": "paired_agent",
                        "capability_id": capability_id,
                        "paired_subject_id": receipt["paired_subject_id"],
                        "claimed_client_label": HOST_CONTRACTS[host][
                            "claimed_client_label"
                        ],
                        "capability_secret_possession_verified": True,
                        "vendor_identity_verified": False,
                        "user_authority_verified": False,
                    },
                )
                self.assertEqual(
                    origin,
                    {
                        "principal": "paired_agent",
                        "surface": "agent_plugin_editor",
                    },
                )
                event = connection.execute(
                    "SELECT * FROM agent_project_capability_events "
                    "WHERE agent_project_command_receipt_id=?",
                    (receipt["id"],),
                ).fetchone()
                self.assertIsNotNone(event)
                self.assertEqual(event["event_schema_version"], "2")
                self.assertEqual(event["event_type"], "used")
                self.assertEqual(event["sequence"], 2)
                self.assertEqual(event["capability_id"], capability_id)
                self.assertEqual(event["operation_id"], receipt["operation_id"])
                self.assertEqual(event["action"], receipt["command_type"])
                self.assertEqual(event["created_at"], receipt["created_at"])
                capability_events = connection.execute(
                    "SELECT sequence,parent_event_id,parent_event_sha256,event_sha256 "
                    "FROM agent_project_capability_events "
                    "WHERE capability_id=? ORDER BY sequence",
                    (capability_id,),
                ).fetchall()
                self.assertEqual(len(capability_events), 2)
                self.assertIsNone(capability_events[0]["parent_event_id"])
                self.assertEqual(
                    capability_events[1]["parent_event_id"],
                    connection.execute(
                        "SELECT id FROM agent_project_capability_events "
                        "WHERE capability_id=? AND sequence=1",
                        (capability_id,),
                    ).fetchone()["id"],
                )
                self.assertEqual(
                    capability_events[1]["parent_event_sha256"],
                    capability_events[0]["event_sha256"],
                )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_two_fresh_adapter_processes_complete_both_directions(self) -> None:
        capabilities: dict[tuple[str, str], str] = {}
        for project in self.projects.values():
            project_id = str(project["project_id"])
            for host in ("codex", "deepseek"):
                capabilities[(host, project_id)] = self._pair_and_approve(
                    host,
                    project_id,
                )

        codex = self.workers["codex"]
        deepseek = self.workers["deepseek"]
        self.assertNotEqual(codex.pid, deepseek.pid)
        self.assertNotEqual(codex.process_nonce, deepseek.process_nonce)
        self.assertNotEqual(codex.state_dir, deepseek.state_dir)

        forward = self.projects["codex_then_deepseek"]
        forward_project = str(forward["project_id"])
        codex_first = codex.command(
            "save",
            project_id=forward_project,
            duration_ms=1_300,
        )
        deepseek_second = deepseek.command(
            "save",
            project_id=forward_project,
            duration_ms=1_700,
        )
        self._assert_journey_ledger(
            project_id=forward_project,
            initial_head=dict(forward["initial_head"]),
            first=codex_first,
            second=deepseek_second,
            first_capability=capabilities[("codex", forward_project)],
            second_capability=capabilities[("deepseek", forward_project)],
            first_host="codex",
            second_host="deepseek",
        )

        reverse = self.projects["deepseek_then_codex"]
        reverse_project = str(reverse["project_id"])
        deepseek_first = deepseek.command(
            "save",
            project_id=reverse_project,
            duration_ms=1_400,
        )
        codex_second = codex.command(
            "save",
            project_id=reverse_project,
            duration_ms=1_800,
        )
        self._assert_journey_ledger(
            project_id=reverse_project,
            initial_head=dict(reverse["initial_head"]),
            first=deepseek_first,
            second=codex_second,
            first_capability=capabilities[("deepseek", reverse_project)],
            second_capability=capabilities[("codex", reverse_project)],
            first_host="deepseek",
            second_host="codex",
        )
        # Close first so stderr has reached EOF and the parent scans the full
        # process transcript rather than a live-pipe prefix.
        for worker in self.workers.values():
            worker.close()
        self._assert_process_and_host_state_evidence()


class _InjectedWorkerStartupFailure(RuntimeError):
    pass


class _InjectedWorkerCleanupFailure(RuntimeError):
    pass


class B2B4HarnessSetupCleanupTests(unittest.TestCase):
    """Prove partial setUp failure releases every incrementally owned resource."""

    def _exercise_second_worker_popen_failure(self) -> None:
        candidate = B2B4AutomatedCrossHostProcessHarnessTests(
            "test_two_fresh_adapter_processes_complete_both_directions"
        )
        original_environment = dict(os.environ)
        original_popen = subprocess.Popen
        setup_failure = _InjectedWorkerStartupFailure(
            "injected second adapter Popen failure"
        )
        cleanup_failure = _InjectedWorkerCleanupFailure(
            "injected post-close cleanup failure"
        )
        observed: dict[str, object] = {}
        worker_popen_count = 0

        def popen_with_second_worker_failure(*args: object, **kwargs: object):
            nonlocal worker_popen_count
            argv = args[0] if args else kwargs.get("args")
            is_worker = isinstance(argv, (list, tuple)) and str(WORKER_PATH) in {
                str(item) for item in argv
            }
            if not is_worker:
                return original_popen(*args, **kwargs)
            worker_popen_count += 1
            if worker_popen_count == 1:
                return original_popen(*args, **kwargs)

            first_worker = candidate.workers["codex"]
            original_close = first_worker.close

            def close_then_report_cleanup_failure() -> None:
                original_close()
                raise cleanup_failure

            first_worker.close = close_then_report_cleanup_failure
            app = candidate.app
            repository = candidate.repository
            assert app is not None
            assert isinstance(repository, MediaRepository)
            observed.update(
                {
                    "worker": first_worker,
                    "server": candidate.server,
                    "thread": candidate.server_thread,
                    "temporary_root": candidate.root,
                    "repository": repository,
                    "repository_had_anchor": repository._blueprint_anchor is not None,
                    "runners": tuple(
                        app.extensions[name]
                        for name in (
                            "media_job_runner",
                            "render_job_runner",
                            "canonical_export_job_runner",
                        )
                    ),
                    "registered_workers": len(candidate.workers),
                }
            )
            raise setup_failure

        with patch.object(
            subprocess,
            "Popen",
            side_effect=popen_with_second_worker_failure,
        ):
            with self.assertRaises(_InjectedWorkerStartupFailure) as captured:
                candidate.setUp()

        self.assertIs(captured.exception, setup_failure)
        self.assertEqual(worker_popen_count, 2)
        self.assertEqual(observed["registered_workers"], 1)
        self.assertEqual(candidate.workers, {})
        # The setup-failure cleanup already ran. A second call must be a no-op.
        candidate._cleanup_resources(suppress_errors=False)

        worker = observed["worker"]
        assert isinstance(worker, _AdapterProcess)
        self.assertIsNotNone(worker.process.poll())
        self.assertFalse(worker._stderr_thread.is_alive())
        for stream in (worker.process.stdin, worker.process.stdout, worker.process.stderr):
            self.assertTrue(stream is None or stream.closed)

        server = observed["server"]
        thread = observed["thread"]
        self.assertIsNotNone(server)
        self.assertIsNotNone(thread)
        self.assertEqual(server.socket.fileno(), -1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(Path(observed["temporary_root"]).exists())
        self.assertTrue(
            all(runner._executor._shutdown for runner in observed["runners"])
        )
        repository = observed["repository"]
        assert isinstance(repository, MediaRepository)
        self.assertIs(observed["repository_had_anchor"], True)
        self.assertIsNone(repository._blueprint_anchor)
        self.assertTrue(
            dict(os.environ) == original_environment,
            "setUp environment patch was not restored",
        )

    def test_second_worker_popen_failure_preserves_cause_and_leaks_nothing(self) -> None:
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self._exercise_second_worker_popen_failure()
            gc.collect()
            gc.collect()
        resource_warning_count = sum(
            issubclass(item.category, ResourceWarning) for item in caught
        )
        self.assertEqual(
            resource_warning_count,
            0,
            f"setUp failure cleanup emitted {resource_warning_count} ResourceWarnings",
        )


if __name__ == "__main__":
    unittest.main()
