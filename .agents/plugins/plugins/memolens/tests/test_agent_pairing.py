from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_agent import AgentPairingWorkflow  # noqa: E402
from memolens_agent_client import (  # noqa: E402
    AgentApiClient,
    TIMELINE_PREVIEW_ACTION,
    operation_proof,
    status_proof,
)
from memolens_agent_credentials import (  # noqa: E402
    load_agent_credential,
    save_agent_credential,
)
from memolens_cli import build_parser  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402


class _FakeAgentClient:
    base_url = "http://127.0.0.1:5519"

    def __init__(self) -> None:
        self.last_pair: dict[str, object] | None = None
        self.last_status: dict[str, object] | None = None
        self.last_write: dict[str, object] | None = None
        self.status_payload: dict[str, object] = {
            "object": "memolens.agent_pairing_status",
            "status": "active",
            "capability": {
                "capability_id": "cap_approved",
                "database_uuid": "db-uuid-1",
                "expires_at": "2026-08-22T12:15:00+00:00",
            },
            "remaining_operations": 20,
        }

    def create_pairing(self, **kwargs):  # noqa: ANN003, ANN201
        self.last_pair = kwargs
        return {
            "object": "memolens.agent_pairing",
            "pairing_id": "pair_123",
            "display_code": "491203",
            "database_uuid": "db-uuid-1",
            "status": "pending",
            "created_at": "2026-08-22T12:00:00+00:00",
            "expires_at": "2026-08-22T12:15:00+00:00",
        }

    def pairing_status(self, pairing_id, *, proof_secret):  # noqa: ANN001, ANN201
        if pairing_id != "pair_123":
            raise AssertionError("unexpected pairing")
        self.last_status = {
            "pairing_id": pairing_id,
            "proof_secret": proof_secret,
        }
        return self.status_payload

    def blueprint_write(self, **kwargs):  # noqa: ANN003, ANN201
        self.last_write = kwargs
        return {
            "object": "creative_blueprint.command_result",
            "schema_version": "1",
            "project_id": kwargs["project_id"],
            "authority": {"state": "unverified", "verified": False},
        }


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload, separators=(",", ":")).encode()

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_args):  # noqa: ANN002, ANN204
        return False

    def read(self, maximum: int) -> bytes:
        return self.payload[:maximum]


class _RecordingOpener:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    def open(self, request, *, timeout):  # noqa: ANN001, ANN201
        self.requests.append(request)
        if timeout <= 0 or not self.responses:
            raise AssertionError("unexpected Agent client request")
        return _JsonResponse(self.responses.pop(0))


class _ThumbnailResponse:
    headers = {"Content-Type": "image/jpeg"}

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_args):  # noqa: ANN002, ANN204
        return False

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class _ThumbnailOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[object] = []

    def open(self, request, *, timeout):  # noqa: ANN001, ANN201
        if timeout <= 0:
            raise AssertionError("thumbnail timeout must be positive")
        self.requests.append(request)
        return _ThumbnailResponse(self.body)


class AgentPairingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary.name) / "state"
        self.environment = mock.patch.dict(
            os.environ,
            {"MEMOLENS_APP_STATE_DIR": str(self.state_dir)},
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_operation_proof_is_frozen_and_request_bound(self) -> None:
        arguments = {
            "proof_secret": "11" * 32,
            "capability_id": "cap_1",
            "nonce": "nonce_1",
            "method": "POST",
            "canonical_path": "/v1/agent/creative/projects/proj_1/blueprint/commit",
            "database_uuid": "db-1",
            "project_id": "proj_1",
            "body": {"b": 2, "a": 1},
            "idempotency_key": "retry-1",
        }
        observed = operation_proof(**arguments)
        body_digest = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
        message = "\n".join(
            (
                "MLCAP1",
                "cap_1",
                "nonce_1",
                "POST",
                arguments["canonical_path"],
                "db-1",
                "proj_1",
                body_digest,
                "retry-1",
            )
        )
        expected = hmac.new(bytes.fromhex("11" * 32), message.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(observed, expected)
        for field, replacement in (
            ("nonce", "nonce_2"),
            ("canonical_path", "/v1/agent/creative/projects/proj_2/blueprint/commit"),
            ("database_uuid", "db-2"),
            ("project_id", "proj_2"),
            ("idempotency_key", "retry-2"),
        ):
            changed = dict(arguments)
            changed[field] = replacement
            self.assertNotEqual(operation_proof(**changed), observed)
        changed_body = dict(arguments)
        changed_body["body"] = {"a": 2, "b": 2}
        self.assertNotEqual(operation_proof(**changed_body), observed)

    def test_media_thumbnail_is_identity_only_bounded_loopback_read(self) -> None:
        opener = _ThumbnailOpener(b"\xff\xd8verified-thumbnail\xff\xd9")
        client = AgentApiClient(
            "http://127.0.0.1:5519",
            timeout=1,
            opener=opener,
        )
        client._identity_checked = True

        observed = client.media_thumbnail(
            media_kind="video",
            asset_id="asset_123",
            span_id="segment_456",
        )
        self.assertEqual(observed, b"\xff\xd8verified-thumbnail\xff\xd9")
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:5519/v1/video-segments/segment_456/thumbnail",
        )
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Accept"), "image/jpeg")
        self.assertIsNone(request.data)

        with self.assertRaisesRegex(MemoLensError, "identity is invalid"):
            client.media_thumbnail(
                media_kind="image",
                asset_id="../../private",
            )
        self.assertEqual(len(opener.requests), 1)

    def test_status_proof_is_frozen_and_scope_bound(self) -> None:
        secret = "22" * 32
        observed = status_proof(
            proof_secret=secret,
            kind="nonce",
            identity="cap_1:blueprint.commit_proposal",
        )
        expected = hmac.new(
            bytes.fromhex(secret),
            b"MLSTATUS1\nnonce\ncap_1:blueprint.commit_proposal",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(observed, expected)
        self.assertNotEqual(
            observed,
            status_proof(
                proof_secret=secret,
                kind="nonce",
                identity="cap_1:blueprint.restore_revision",
            ),
        )
        self.assertNotEqual(
            observed,
            status_proof(
                proof_secret=secret,
                kind="pairing",
                identity="cap_1:blueprint.commit_proposal",
            ),
        )

    def test_transport_status_and_nonce_require_exact_possession_proofs(self) -> None:
        secret = "33" * 32
        opener = _RecordingOpener(
            {"object": "agent.pairing", "status": "pending"},
            {"object": "agent.operation_nonce", "nonce": "nonce_1"},
        )
        client = AgentApiClient(
            "http://127.0.0.1:5519",
            timeout=1,
            opener=opener,
        )
        client._identity_checked = True
        client.pairing_status("pair_1", proof_secret=secret)
        client.request_nonce(
            "cap_1",
            action="blueprint.commit_proposal",
            proof_secret=secret,
        )

        pairing_request, nonce_request = opener.requests
        self.assertEqual(pairing_request.get_method(), "GET")
        self.assertEqual(
            pairing_request.get_header("X-memolens-agent-status-proof"),
            status_proof(
                proof_secret=secret,
                kind="pairing",
                identity="pair_1",
            ),
        )
        self.assertEqual(nonce_request.get_method(), "POST")
        self.assertEqual(
            json.loads(nonce_request.data),
            {
                "action": "blueprint.commit_proposal",
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
            },
        )
        self.assertEqual(
            nonce_request.get_header("X-memolens-agent-status-proof"),
            status_proof(
                proof_secret=secret,
                kind="nonce",
                identity="cap_1:blueprint.commit_proposal",
            ),
        )
        self.assertNotIn(secret, str(pairing_request.header_items()))
        self.assertNotIn(secret, str(nonce_request.header_items()))

    def test_timeline_write_uses_the_closed_paired_route_and_request_bound_proof(self) -> None:
        secret = "44" * 32
        body = {
            "expected_blueprint": {
                "revision": 1,
                "content_sha256": "a" * 64,
                "semantic_sha256": "b" * 64,
                "operation_id": "blueprint_op_1",
            },
            "expected_coverage": {
                "revision": 1,
                "content_sha256": "c" * 64,
                "evidence_manifest_sha256": "d" * 64,
                "operation_id": "coverage_op_1",
            },
            "expected_timeline_head": {
                "revision": 1,
                "revision_sha256": "e" * 64,
            },
            "edit": {"op": "move_clip", "clip_id": "clip_1", "to_index": 1},
        }
        opener = _RecordingOpener(
            {"object": "agent.operation_nonce", "nonce": "nonce_timeline_1"},
            {"object": "canonical_timeline.command_result"},
        )
        client = AgentApiClient(
            "http://127.0.0.1:5519",
            timeout=1,
            opener=opener,
        )
        client._identity_checked = True
        client.timeline_write(
            project_id="proj_1",
            body=body,
            idempotency_key="timeline-write-1",
            credential={
                "capability_id": "cap_1",
                "database_uuid": "db-1",
                "proof_secret": secret,
            },
        )

        nonce_request, write_request = opener.requests
        self.assertEqual(
            json.loads(nonce_request.data)["action"], "timeline.apply_edit"
        )
        path = "/v1/agent/creative/projects/proj_1/timeline/edit"
        self.assertEqual(write_request.full_url, f"http://127.0.0.1:5519{path}")
        self.assertEqual(write_request.get_method(), "POST")
        self.assertEqual(json.loads(write_request.data), body)
        self.assertEqual(
            write_request.get_header("X-memolens-agent-proof"),
            operation_proof(
                proof_secret=secret,
                capability_id="cap_1",
                nonce="nonce_timeline_1",
                method="POST",
                canonical_path=path,
                database_uuid="db-1",
                project_id="proj_1",
                body=body,
                idempotency_key="timeline-write-1",
            ),
        )
        self.assertNotIn(secret, str(write_request.header_items()))

    def test_timeline_restore_uses_distinct_action_path_and_request_bound_proof(self) -> None:
        secret = "55" * 32
        head = {
            "revision": 3,
            "revision_sha256": "a" * 64,
            "timeline_id": "timeline_1",
            "timeline_content_sha256": "b" * 64,
            "blueprint_binding": {"revision": 1},
            "coverage_binding": {"revision": 2},
            "operation_id": "timeline_op_3",
        }
        restore_from = {**head, "revision": 1, "operation_id": "timeline_op_1"}
        body = {
            "expected_blueprint": {"revision": 1},
            "expected_coverage": {"revision": 2},
            "expected_timeline_head": head,
            "restore_from": restore_from,
        }
        opener = _RecordingOpener(
            {"object": "agent.operation_nonce", "nonce": "nonce_restore_1"},
            {"object": "canonical_timeline.command_result"},
        )
        client = AgentApiClient(
            "http://127.0.0.1:5519",
            timeout=1,
            opener=opener,
        )
        client._identity_checked = True
        client.timeline_write(
            command="restore",
            project_id="proj_1",
            body=body,
            idempotency_key="timeline-restore-1",
            credential={
                "capability_id": "cap_1",
                "database_uuid": "db-1",
                "proof_secret": secret,
            },
        )

        nonce_request, write_request = opener.requests
        self.assertEqual(
            json.loads(nonce_request.data)["action"],
            "timeline.restore_revision",
        )
        path = "/v1/agent/creative/projects/proj_1/timeline/restore"
        self.assertEqual(write_request.full_url, f"http://127.0.0.1:5519{path}")
        self.assertEqual(json.loads(write_request.data), body)
        self.assertEqual(
            write_request.get_header("X-memolens-agent-proof"),
            operation_proof(
                proof_secret=secret,
                capability_id="cap_1",
                nonce="nonce_restore_1",
                method="POST",
                canonical_path=path,
                database_uuid="db-1",
                project_id="proj_1",
                body=body,
                idempotency_key="timeline-restore-1",
            ),
        )

    def test_timeline_structural_edit_uses_distinct_action_path_and_closed_body(self) -> None:
        secret = "66" * 32
        body = {
            "expected_blueprint": {"revision": 1},
            "expected_coverage": {"revision": 2},
            "expected_timeline_head": {"revision": 3},
            "structural_edit": {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1500,
            },
        }
        opener = _RecordingOpener(
            {"object": "agent.operation_nonce", "nonce": "nonce_structural_1"},
            {"object": "canonical_timeline.command_result"},
        )
        client = AgentApiClient(
            "http://127.0.0.1:5519",
            timeout=1,
            opener=opener,
        )
        client._identity_checked = True
        client.timeline_write(
            command="structural_edit",
            project_id="proj_1",
            body=body,
            idempotency_key="timeline-structural-1",
            credential={
                "capability_id": "cap_1",
                "database_uuid": "db-1",
                "proof_secret": secret,
            },
        )

        nonce_request, write_request = opener.requests
        self.assertEqual(
            json.loads(nonce_request.data)["action"],
            "timeline.apply_structural_edit",
        )
        path = "/v1/agent/creative/projects/proj_1/timeline/structural-edit"
        self.assertEqual(write_request.full_url, f"http://127.0.0.1:5519{path}")
        self.assertEqual(json.loads(write_request.data), body)
        rendered = json.dumps(body, sort_keys=True)
        for forbidden in ("offset_ms", "draft_id", "operations", '"timeline"'):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(
            write_request.get_header("X-memolens-agent-proof"),
            operation_proof(
                proof_secret=secret,
                capability_id="cap_1",
                nonce="nonce_structural_1",
                method="POST",
                canonical_path=path,
                database_uuid="db-1",
                project_id="proj_1",
                body=body,
                idempotency_key="timeline-structural-1",
            ),
        )

    def test_credential_is_private_and_summary_never_contains_secret(self) -> None:
        secret = "ab" * 32
        summary = save_agent_credential(
            {
                "object": "memolens.agent_project_credential",
                "schema_version": "1",
                "base_url": "http://127.0.0.1:5519",
                "project_id": "proj_1",
                "pairing_id": "pair_1",
                "capability_id": None,
                "subject_id": "agent_1",
                "claimed_client_label": "Codex claim",
                "proof_secret": secret,
                "database_uuid": "db-1",
                "actions": ["blueprint.commit_proposal", "blueprint.restore_revision"],
                "created_at": "2026-08-22T12:00:00+00:00",
                "expires_at": "2026-08-22T12:15:00+00:00",
                "status": "pending",
            }
        )
        self.assertNotIn(secret, json.dumps(summary))
        loaded = load_agent_credential("proj_1")
        self.assertEqual(loaded["proof_secret"], secret)
        directory = self.state_dir / "agent-credentials"
        files = list(directory.glob("project-*.json"))
        self.assertEqual(len(files), 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            files[0].chmod(0o644)
            with self.assertRaises(MemoLensError) as raised:
                load_agent_credential("proj_1")
            self.assertEqual(raised.exception.code, "agent_credential_insecure")

    def test_credential_load_uses_nofollow_fstat_and_bounded_fd_read(self) -> None:
        secret = "cd" * 32
        save_agent_credential(
            {
                "object": "memolens.agent_project_credential",
                "schema_version": "1",
                "base_url": "http://127.0.0.1:5519",
                "project_id": "proj_1",
                "pairing_id": "pair_1",
                "capability_id": None,
                "subject_id": "agent_1",
                "claimed_client_label": "Codex claim",
                "proof_secret": secret,
                "database_uuid": "db-1",
                "actions": ["blueprint.commit_proposal"],
                "created_at": "2026-08-22T12:00:00+00:00",
                "expires_at": "2026-08-22T12:15:00+00:00",
                "status": "pending",
            }
        )
        real_open = os.open
        real_fstat = os.fstat
        real_read = os.read
        opened_flags: list[int] = []
        opened_descriptors: list[int] = []
        fstat_descriptors: list[int] = []
        read_calls: list[tuple[int, int, int]] = []

        def recording_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
            descriptor = real_open(path, flags, *args, **kwargs)
            opened_flags.append(flags)
            opened_descriptors.append(descriptor)
            return descriptor

        def recording_fstat(descriptor):  # noqa: ANN001, ANN202
            fstat_descriptors.append(descriptor)
            return real_fstat(descriptor)

        def recording_read(descriptor, maximum):  # noqa: ANN001, ANN202
            chunk = real_read(descriptor, maximum)
            read_calls.append((descriptor, maximum, len(chunk)))
            return chunk

        with (
            mock.patch("memolens_agent_credentials.os.open", side_effect=recording_open),
            mock.patch("memolens_agent_credentials.os.fstat", side_effect=recording_fstat),
            mock.patch("memolens_agent_credentials.os.read", side_effect=recording_read),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("path read forbidden")),
        ):
            loaded = load_agent_credential("proj_1")

        self.assertEqual(loaded["proof_secret"], secret)
        self.assertEqual(len(opened_descriptors), 1)
        self.assertIn(opened_descriptors[0], fstat_descriptors)
        self.assertTrue(read_calls)
        self.assertTrue(
            all(descriptor == opened_descriptors[0] for descriptor, _, _ in read_calls)
        )
        self.assertLessEqual(sum(size for _, _, size in read_calls), 16_385)
        if getattr(os, "O_NOFOLLOW", 0):
            self.assertTrue(opened_flags[0] & os.O_NOFOLLOW)

    def test_credential_load_stops_after_one_byte_over_limit(self) -> None:
        directory = self.state_dir / "agent-credentials"
        directory.mkdir(mode=0o700, parents=True)
        digest = hashlib.sha256(b"proj_1").hexdigest()[:32]
        path = directory / f"project-{digest}.json"
        path.write_bytes(b"x" * 32_768)
        path.chmod(0o600)
        real_read = os.read
        observed_requested_bytes = 0

        def recording_read(descriptor, maximum):  # noqa: ANN001, ANN202
            nonlocal observed_requested_bytes
            observed_requested_bytes += maximum
            return real_read(descriptor, maximum)

        with mock.patch(
            "memolens_agent_credentials.os.read",
            side_effect=recording_read,
        ):
            with self.assertRaises(MemoLensError) as raised:
                load_agent_credential("proj_1")
        self.assertEqual(raised.exception.code, "agent_credential_invalid")
        self.assertLessEqual(observed_requested_bytes, 16_385)

    def test_credential_load_enforces_exact_byte_boundary(self) -> None:
        save_agent_credential(
            {
                "object": "memolens.agent_project_credential",
                "schema_version": "1",
                "base_url": "http://127.0.0.1:5519",
                "project_id": "proj_1",
                "pairing_id": "pair_1",
                "capability_id": None,
                "subject_id": "agent_1",
                "claimed_client_label": "Codex claim",
                "proof_secret": "01" * 32,
                "database_uuid": "db-1",
                "actions": ["blueprint.commit_proposal"],
                "created_at": "2026-08-22T12:00:00+00:00",
                "expires_at": "2026-08-22T12:15:00+00:00",
                "status": "pending",
            }
        )
        directory = self.state_dir / "agent-credentials"
        digest = hashlib.sha256(b"proj_1").hexdigest()[:32]
        path = directory / f"project-{digest}.json"
        serialized = path.read_bytes()
        self.assertLess(len(serialized), 16_384)

        path.write_bytes(serialized + (b" " * (16_384 - len(serialized))))
        path.chmod(0o600)
        self.assertEqual(load_agent_credential("proj_1")["project_id"], "proj_1")

        with path.open("ab") as handle:
            handle.write(b" ")
        with self.assertRaises(MemoLensError) as raised:
            load_agent_credential("proj_1")
        self.assertEqual(raised.exception.code, "agent_credential_invalid")

    def test_credential_load_rejects_final_symlink_and_open_identity_swap(self) -> None:
        secret = "ef" * 32
        credential = {
            "object": "memolens.agent_project_credential",
            "schema_version": "1",
            "base_url": "http://127.0.0.1:5519",
            "project_id": "proj_1",
            "pairing_id": "pair_1",
            "capability_id": None,
            "subject_id": "agent_1",
            "claimed_client_label": "Codex claim",
            "proof_secret": secret,
            "database_uuid": "db-1",
            "actions": ["blueprint.commit_proposal"],
            "created_at": "2026-08-22T12:00:00+00:00",
            "expires_at": "2026-08-22T12:15:00+00:00",
            "status": "pending",
        }
        save_agent_credential(credential)
        directory = self.state_dir / "agent-credentials"
        digest = hashlib.sha256(b"proj_1").hexdigest()[:32]
        target = directory / f"project-{digest}.json"
        original = directory / "original.json"
        target.replace(original)
        target.symlink_to(original.name)
        with self.assertRaises(MemoLensError) as symlink_error:
            load_agent_credential("proj_1")
        self.assertEqual(symlink_error.exception.code, "agent_credential_insecure")

        target.unlink()
        original.replace(target)
        rogue = directory / "rogue.json"
        rogue.write_text(json.dumps(credential), encoding="utf-8")
        rogue.chmod(0o600)
        real_open = os.open

        def swap_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
            return real_open(rogue, flags, *args, **kwargs)

        with mock.patch("memolens_agent_credentials.os.open", side_effect=swap_open):
            with self.assertRaises(MemoLensError) as identity_error:
                load_agent_credential("proj_1")
        self.assertEqual(identity_error.exception.code, "agent_credential_insecure")

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and bool(getattr(os, "O_NONBLOCK", 0)),
        "POSIX FIFO race oracle requires mkfifo and O_NONBLOCK",
    )
    def test_credential_load_fifo_identity_swap_cannot_block(self) -> None:
        save_agent_credential(
            {
                "object": "memolens.agent_project_credential",
                "schema_version": "1",
                "base_url": "http://127.0.0.1:5519",
                "project_id": "proj_1",
                "pairing_id": "pair_1",
                "capability_id": None,
                "subject_id": "agent_1",
                "claimed_client_label": "Codex claim",
                "proof_secret": "23" * 32,
                "database_uuid": "db-1",
                "actions": ["blueprint.commit_proposal"],
                "created_at": "2026-08-22T12:00:00+00:00",
                "expires_at": "2026-08-22T12:15:00+00:00",
                "status": "pending",
            }
        )
        fifo = self.state_dir / "credential-race.fifo"
        os.mkfifo(fifo, mode=0o600)
        real_open = os.open
        observed_flags: list[int] = []

        def swap_to_fifo(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202
            observed_flags.append(flags)
            if not flags & os.O_NONBLOCK:
                raise AssertionError("credential open must be non-blocking before type validation")
            return real_open(fifo, flags, *args, **kwargs)

        with mock.patch("memolens_agent_credentials.os.open", side_effect=swap_to_fifo):
            with self.assertRaises(MemoLensError) as raised:
                load_agent_credential("proj_1")
        self.assertEqual(raised.exception.code, "agent_credential_insecure")
        self.assertEqual(len(observed_flags), 1)
        self.assertTrue(observed_flags[0] & os.O_NONBLOCK)

    def test_pair_status_and_write_keep_proof_below_output_boundary(self) -> None:
        workflow = AgentPairingWorkflow(base_url=None, timeout=1)
        fake = _FakeAgentClient()
        workflow.client = fake
        created = workflow.create(
            project_id="proj_1",
            current_head={"revision": 1, "content_sha256": "a" * 64},
            claimed_client_label="Claude claim",
            ttl_seconds=900,
            max_operations=20,
        )
        secret = fake.last_pair["proof_secret"]
        self.assertEqual(
            fake.last_pair["actions"],
            ["blueprint.commit_proposal", "blueprint.restore_revision"],
        )
        self.assertNotIn(secret, json.dumps(created))
        self.assertEqual(created["status"], "pending")
        refreshed = workflow.status("proj_1")
        self.assertTrue(refreshed["write_ready"])
        self.assertNotIn(secret, json.dumps(refreshed))
        self.assertEqual(fake.last_status["proof_secret"], secret)
        result = workflow.write(
            command="commit",
            project_id="proj_1",
            body={
                "expected_head": {"revision": 1, "content_sha256": "a" * 64},
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": {},
            },
            idempotency_key="write-1",
        )
        self.assertEqual(result["authority"]["state"], "unverified")
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(fake.last_write["credential"]["proof_secret"], secret)

    def test_preview_only_status_is_playback_ready_but_not_write_ready(self) -> None:
        workflow = AgentPairingWorkflow(base_url=None, timeout=1)
        fake = _FakeAgentClient()
        workflow.client = fake
        workflow.create(
            project_id="proj_1",
            current_head={"revision": 1, "content_sha256": "a" * 64},
            claimed_client_label="Preview-only host",
            ttl_seconds=900,
            max_operations=20,
            actions=[TIMELINE_PREVIEW_ACTION],
        )

        refreshed = workflow.status("proj_1")

        self.assertFalse(refreshed["write_ready"])
        self.assertTrue(refreshed["preview_ready"])
        self.assertEqual(fake.last_pair["actions"], [TIMELINE_PREVIEW_ACTION])

    def test_timeline_pairing_is_explicit_and_does_not_submit_a_caller_timeline_head(self) -> None:
        workflow = AgentPairingWorkflow(base_url=None, timeout=1)
        fake = _FakeAgentClient()
        workflow.client = fake
        result = workflow.create(
            project_id="proj_1",
            current_head={"revision": 1, "content_sha256": "a" * 64},
            claimed_client_label="DeepSeek claim",
            ttl_seconds=900,
            max_operations=20,
            actions=["timeline.apply_edit"],
        )
        self.assertEqual(fake.last_pair["actions"], ["timeline.apply_edit"])
        self.assertNotIn("observed_timeline_head", fake.last_pair)
        self.assertEqual(result["actions"], ["timeline.apply_edit"])

    def test_cli_exposes_pairing_and_write_commands_without_a_secret_argument(self) -> None:
        parser = build_parser()
        pairing = parser.parse_args(["agent-pair", "proj_1"])
        self.assertEqual(pairing.command, "agent-pair")
        self.assertEqual(pairing.ttl_seconds, 900)
        self.assertEqual(pairing.max_operations, 20)
        self.assertIsNone(pairing.actions)
        timeline_pairing = parser.parse_args(
            ["agent-pair", "proj_1", "--action", "timeline.apply_edit"]
        )
        self.assertEqual(timeline_pairing.actions, ["timeline.apply_edit"])
        write = parser.parse_args(
            [
                "blueprint-commit",
                "proj_1",
                "--input",
                "command.json",
                "--idempotency-key",
                "write-1",
            ]
        )
        self.assertFalse(hasattr(write, "proof_secret"))
        self.assertFalse(hasattr(write, "capability_token"))


if __name__ == "__main__":
    unittest.main()
