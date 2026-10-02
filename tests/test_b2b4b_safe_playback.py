from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src import DESKTOP_TOKEN_HEADER, MAIN_AUTHORITY_HEADER
from backend.src.agent_authority import (
    AGENT_CAPABILITY_HEADER,
    AGENT_NONCE_HEADER,
    AGENT_PROOF_HEADER,
    AGENT_STATUS_PROOF_HEADER,
    AgentPairingBroker,
    agent_request_proof,
    agent_status_proof,
    proof_secret_sha256,
)
from backend.src.agent_preview import (
    AgentPreviewError,
    PREVIEW_LEASE_HEADER,
    PREVIEW_NONCE_HEADER,
    PREVIEW_PROOF_HEADER,
    PreviewLeaseBroker,
    agent_preview_mint_proof,
    agent_preview_read_proof,
)
from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from backend.src.media.timeline_preview import TimelinePreviewService
from backend.src.runtime import RuntimeBundle, RuntimeManager
from core.db import ImageIndexRepository
from core.media_db import (
    AgentCapabilityError,
    BoundAssetSourceFile,
    MediaRepository,
    canonical_json,
    utc_now_iso,
)
from core.timeline_preview_contract import TIMELINE_PREVIEW_MAX_RESPONSE_BYTES
from tests.test_coverage_persistence import semantic
from tests import test_timeline_lowering_integration as timeline_integration


_VIDEO_SIZE = (TIMELINE_PREVIEW_MAX_RESPONSE_BYTES * 2) + 257
_LEDGER_TABLES = (
    "canonical_timeline_operations",
    "canonical_timeline_revisions",
    "canonical_timeline_heads",
    "canonical_timeline_receipts",
    "canonical_usage_occurrences",
    "export_grants",
    "canonical_export_operations",
    "canonical_export_jobs",
    "canonical_export_revisions",
    "canonical_export_receipts",
    "agent_project_capabilities",
    "agent_pairing_confirmation_receipts",
    "agent_project_command_receipts",
    "agent_project_capability_events",
)


class ExactBoundSourceOpenerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-bound-source-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _import_video(self, name: str, content: bytes) -> dict[str, object]:
        path = self.library / name
        path.write_bytes(content)
        observed = path.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        return self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=name,
            filename=name,
            kind="video",
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )

    def test_exact_source_never_follows_preferred_and_mismatch_is_read_pure(self) -> None:
        content = (b"exact-source-" * 4096) + b"end"
        asset = self._import_video("original.mp4", content)
        asset_id = str(asset["id"])
        asset_sha256 = hashlib.sha256(content).hexdigest()
        source_id = str(
            self.repository.available_sources(asset_id)[0]["asset_source_id"]
        )

        duplicate = self._import_video("preferred.mp4", content)
        duplicate_source_id = str(
            next(
                row["asset_source_id"]
                for row in self.repository.available_sources(asset_id)
                if row["asset_source_id"] != source_id
            )
        )
        self.assertEqual(duplicate["id"], asset_id)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE asset_sources SET is_preferred=0 WHERE asset_id=?",
                (asset_id,),
            )
            connection.execute(
                "UPDATE asset_sources SET is_preferred=1 WHERE id=?",
                (duplicate_source_id,),
            )
        (self.library / "preferred.mp4").write_bytes(b"x" * len(content))

        bound = self.repository.open_bound_asset_source_file(
            source_id,
            asset_id,
            asset_sha256,
            len(content),
        )
        self.assertIsNotNone(bound)
        assert bound is not None
        try:
            self.assertEqual(bound.asset["asset_source_id"], source_id)
            self.assertEqual(bound.handle.read(), content)
        finally:
            bound.close()

        replacement = self.library / "replacement.mp4"
        replacement.write_bytes(b"r" * len(content))
        os.replace(replacement, self.library / "original.mp4")
        self.assertIsNone(
            self.repository.open_bound_asset_source_file(
                source_id,
                asset_id,
                asset_sha256,
                len(content),
                mark_changed=False,
            )
        )
        with self.repository.transaction() as connection:
            availability = connection.execute(
                "SELECT availability FROM asset_sources WHERE id=?",
                (source_id,),
            ).fetchone()[0]
        self.assertEqual(availability, "available")


class SafePlaybackApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-safe-playback-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self.epoch = "epoch_safe_playback"
        self.main_token = "main-safe-playback-token"
        self.desktop_token = "desktop-safe-playback-token"
        self.secret = "d4" * 32
        self.pairing_broker = AgentPairingBroker(self.epoch)
        self.preview_broker = PreviewLeaseBroker(self.epoch)
        self._nonce_sequence = 0

        self.project_id = self._build_video_project()
        workspace = self.timelines.read(self.project_id)
        raw_timeline_head = workspace["head"]
        self.timeline_head = {
            "revision": raw_timeline_head["revision"],
            "content_sha256": raw_timeline_head["timeline_content_sha256"],
        }
        timeline = workspace["timeline"]
        self.video_clip = next(
            clip
            for clip in timeline["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        )
        self.clip_id = str(self.video_clip["clip_id"])
        with self.repository.transaction() as connection:
            trusted = self.repository.get_trusted_canonical_timeline_head_in_transaction(
                connection,
                self.project_id,
            )
        assert trusted is not None
        self.source_bindings = trusted["source_bindings"]
        self.video_binding = next(
            binding
            for clip, binding in zip(
                trusted["timeline"]["tracks"][0]["clips"],
                self.source_bindings,
                strict=True,
            )
            if clip["clip_id"] == self.clip_id
        )
        self.video_source_id = str(self.video_binding["asset_source_id"])
        self.video_asset_id = str(self.video_binding["asset_id"])
        self.video_asset_sha256 = str(self.video_binding["asset_sha256"])

        self.app = Flask("safe-playback-api-test", static_folder=None)
        self.app.config.update(
            TESTING=True,
            MAIN_AUTHORITY_TOKEN=self.main_token,
            DESKTOP_SESSION_TOKEN=self.desktop_token,
            RUNTIME_AUTHORITY_EPOCH=self.epoch,
        )
        runtime_extensions: dict[str, object] = {
            "media_repository": self.repository,
            "blueprint_service": self.blueprints,
            "coverage_service": self.coverage,
            "timeline_lowering_service": self.timelines,
        }
        self.manager = RuntimeManager(lambda _bundle: None)
        bundle = RuntimeBundle.freeze(
            SimpleNamespace(
                db_path=self.db_path,
                image_library_dir=self.library,
            ),
            runtime_extensions,
        )
        self.manager.swap(bundle)
        self.app.extensions.update(runtime_extensions)
        self.app.extensions.update(
            {
                "runtime_manager": self.manager,
                "agent_pairing_broker": self.pairing_broker,
                "agent_preview_broker": self.preview_broker,
            }
        )
        self.app.register_blueprint(api_blueprint)
        self.client = self.app.test_client()
        self.capability_id = self._pair_preview_capability()

    def tearDown(self) -> None:
        self.preview_broker.drop_all()
        self.repository.close()
        self.temporary.cleanup()

    def _next_nonce(self, prefix: str) -> str:
        self._nonce_sequence += 1
        return f"{prefix}{self._nonce_sequence:031d}"

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_integration.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            content,
        )

    def _register_video_span(self) -> tuple[str, bytes]:
        pattern = b"0123456789abcdef"
        content = (pattern * ((_VIDEO_SIZE + len(pattern) - 1) // len(pattern)))[
            :_VIDEO_SIZE
        ]
        path = self.library / "preview.mp4"
        path.write_bytes(content)
        observed = path.stat()
        digest = hashlib.sha256(content).hexdigest()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=path.name,
            filename=path.name,
            kind="video",
            sha256=digest,
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        self.repository.update_asset_probe(
            asset_id,
            {
                "duration_ms": 20_000,
                "width": 1920,
                "height": 1080,
                "rotation_degrees": 0,
                "codec": {
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "audio_streams": 1,
                    "sample_rate_hz": 48_000,
                    "channels": 2,
                },
            },
        )
        analysis_run_id = f"arun_{'d' * 32}"
        segment_id = f"seg_{asset_id.removeprefix('asset_')}_1_0"
        now = utc_now_iso()
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,
                     analysis_profile_json,input_asset_sha256,status,
                     transcript_status,visual_status,created_at)
                   VALUES(?,?,1,'initial','safe-playback-test','{}',?,'succeeded',
                          'available','ready',?)""",
                (analysis_run_id, asset_id, digest, now),
            )
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                     boundary_reason,semantic_json,combined_text,visual_status,
                     transcript_status,created_at)
                   VALUES(?,?,?,0,1000,11000,'safe-playback-test','{}','',
                          'ready','available',?)""",
                (segment_id, asset_id, analysis_run_id, now),
            )
            connection.execute(
                """INSERT INTO asset_analysis_heads(
                     asset_id,analysis_run_id,updated_at) VALUES(?,?,?)""",
                (asset_id, analysis_run_id, now),
            )
        return f"memolens://evidence/span/{segment_id}", content

    def _build_video_project(self) -> str:
        opening = self._register_image("opening.jpg", b"safe-playback-opening")
        span_ref, self.video_bytes = self._register_video_span()
        proposal = semantic(
            "safe-playback",
            evidence_ref=f"memolens://evidence/asset/{opening['id']}",
        )
        proposal["material_hints"].append(
            {
                "hint_id": "ending_video",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact verified H.264 source span.",
            }
        )
        project = self.repository.create_project(
            "Safe playback fixture",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "safe-playback-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposal,
            },
            idempotency_key="safe-playback-blueprint",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint[key]
                    for key in ("revision", "content_sha256", "semantic_sha256")
                },
                "expected_plan_head": None,
            },
            idempotency_key="safe-playback-coverage",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        self.timelines.materialize_first_cut(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                        "operation_id",
                    )
                },
                "expected_coverage": {
                    key: coverage[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "evidence_manifest_sha256",
                        "operation_id",
                    )
                },
                "expected_timeline_head": None,
            },
            idempotency_key="safe-playback-timeline",
            expected_database_uuid=self.repository.database_uuid,
        )
        return project_id

    def _pair_capability(
        self,
        *,
        actions: list[str],
        secret: str,
        subject: str,
        native_nonce: str,
    ) -> str:
        blueprint = self.repository.get_blueprint_head(self.project_id)
        assert blueprint is not None
        pairing = self.client.post(
            "/v1/agent/pairings",
            json={
                "object": "memolens.agent_pairing_request",
                "schema_version": "1",
                "project_id": self.project_id,
                "observed_head": {
                    "revision": blueprint["revision"],
                    "content_sha256": blueprint["content_sha256"],
                },
                "subject_id": subject,
                "claimed_client_label": "Safe playback editor",
                "proof_secret": secret,
                "actions": actions,
                "ttl_seconds": 900,
                "max_operations": 4,
            },
        )
        self.assertEqual(pairing.status_code, 201, pairing.json)
        pairing_id = str(pairing.json["pairing_id"])
        presentation = self.client.get(
            f"/v1/main/agent/pairings/{pairing_id}/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        approved = self.client.post(
            f"/v1/main/agent/pairings/{pairing_id}/approve",
            json={
                "presentation_sha256": presentation.json["presentation_sha256"],
                "native_gesture_nonce": native_nonce,
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(approved.status_code, 201, approved.json)
        return str(approved.json["capability_id"])

    def _pair_preview_capability(self) -> str:
        return self._pair_capability(
            actions=["timeline.preview_media"],
            secret=self.secret,
            subject="safe_playback_subject",
            native_nonce="native_safe_playback_pairing",
        )

    def _mint(
        self,
        *,
        nonce: str | None = None,
        observed_head: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        body = {
            "object": "memolens.timeline_preview_lease_request",
            "schema_version": "1",
            "project_id": self.project_id,
            "observed_head": observed_head or self.timeline_head,
            "request_nonce": nonce or self._next_nonce("m"),
        }
        path = (
            f"/v1/agent/creative/projects/{self.project_id}/timeline/preview-leases"
        )
        headers = {
            AGENT_CAPABILITY_HEADER: self.capability_id,
            PREVIEW_PROOF_HEADER: agent_preview_mint_proof(
                self.secret,
                capability_id=self.capability_id,
                method="POST",
                canonical_path=path,
                database_uuid=self.repository.database_uuid,
                body_sha256=hashlib.sha256(
                    canonical_json(body).encode("utf-8")
                ).hexdigest(),
            ),
        }
        headers.update(extra_headers or {})
        return self.client.post(path, json=body, headers=headers)

    def _read(
        self,
        lease: dict[str, object],
        *,
        range_header: str | None,
        method: str = "GET",
        nonce: str | None = None,
        clip_id: str | None = None,
        proof_method: str | None = None,
    ):
        exact_nonce = nonce or self._next_nonce("r")
        exact_clip = clip_id or self.clip_id
        path = (
            f"/v1/agent/preview-leases/{lease['lease_id']}/clips/"
            f"{exact_clip}/media"
        )
        headers = {
            PREVIEW_LEASE_HEADER: str(lease["lease_id"]),
            PREVIEW_NONCE_HEADER: exact_nonce,
            PREVIEW_PROOF_HEADER: agent_preview_read_proof(
                str(lease["lease_secret"]),
                lease_id=str(lease["lease_id"]),
                request_nonce=exact_nonce,
                method=proof_method or method,
                canonical_path=path,
                range_header=range_header,
            ),
        }
        if range_header is not None:
            headers["Range"] = range_header
        return self.client.open(path, method=method, headers=headers)

    def _prepare_active_stream(self):
        minted = self._mint()
        self.assertEqual(minted.status_code, 201, minted.json)
        lease = minted.json
        nonce = self._next_nonce("i")
        path = (
            f"/v1/agent/preview-leases/{lease['lease_id']}/clips/"
            f"{self.clip_id}/media"
        )
        service = TimelinePreviewService(
            self.repository,
            self.pairing_broker,
            self.preview_broker,
        )
        prepared = service.prepare_read(
            lease_id=str(lease["lease_id"]),
            request_nonce=nonce,
            proof=agent_preview_read_proof(
                str(lease["lease_secret"]),
                lease_id=str(lease["lease_id"]),
                request_nonce=nonce,
                method="GET",
                canonical_path=path,
                range_header="bytes=0-2097151",
            ),
            method="GET",
            canonical_path=path,
            range_header="bytes=0-2097151",
            runtime_generation_id=self.manager.current_generation_id,
        )
        iterator = service.iter_read(prepared)
        self.assertEqual(len(next(iterator)), 64 * 1024)
        return iterator

    def _ledger_snapshot(self) -> dict[str, tuple[tuple[object, ...], ...]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                )
                for table in _LEDGER_TABLES
            }

    def _make_other_source_preferred(self) -> None:
        alternate = self.library / "alternate.mp4"
        alternate.write_bytes(self.video_bytes)
        observed = alternate.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        imported = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=alternate.name,
            filename=alternate.name,
            kind="video",
            sha256=self.video_asset_sha256,
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        self.assertEqual(imported["id"], self.video_asset_id)
        alternate_source = next(
            row
            for row in self.repository.available_sources(self.video_asset_id)
            if row["asset_source_id"] != self.video_source_id
        )
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE asset_sources SET is_preferred=0 WHERE asset_id=?",
                (self.video_asset_id,),
            )
            connection.execute(
                "UPDATE asset_sources SET is_preferred=1 WHERE id=?",
                (alternate_source["asset_source_id"],),
            )
        alternate.write_bytes(b"z" * len(self.video_bytes))

    def _assert_definitive_core_capability_gate_is_exact(self) -> None:
        exact = {
            "capability_id": self.capability_id,
            "runtime_authority_epoch": self.epoch,
            "database_uuid": self.repository.database_uuid,
            "project_id": self.project_id,
            "paired_subject_id": "safe_playback_subject",
            "presented_secret_sha256": proof_secret_sha256(self.secret),
        }
        with self.repository.transaction() as connection:
            admitted = (
                self.repository.require_timeline_preview_capability_in_transaction(
                    connection,
                    **exact,
                )
            )
            self.assertEqual(admitted["id"], self.capability_id)
            substitutions = (
                ("capability_id", "cap_safe_playback_other"),
                ("runtime_authority_epoch", "epoch_safe_playback_other"),
                ("database_uuid", "database_safe_playback_other"),
                ("project_id", "project_safe_playback_other"),
                ("paired_subject_id", "safe_playback_subject_other"),
                ("presented_secret_sha256", "0" * 64),
            )
            for field, value in substitutions:
                with self.subTest(core_capability_substitution=field):
                    with self.assertRaises(AgentCapabilityError):
                        self.repository.require_timeline_preview_capability_in_transaction(
                            connection,
                            **{**exact, field: value},
                        )

    def test_large_media_route_is_bounded_closed_and_read_pure(self) -> None:
        self._assert_definitive_core_capability_gate_is_exact()
        self._make_other_source_preferred()
        baseline = self._ledger_snapshot()

        stale_nonce = self._next_nonce("s")
        stale_head = {**self.timeline_head, "content_sha256": "0" * 64}
        stale = self._mint(nonce=stale_nonce, observed_head=stale_head)
        self.assertEqual(stale.status_code, 409, stale.json)
        self.assertEqual(stale.json["code"], "agent_preview_head_changed")
        with patch.object(
            self.repository,
            "get_trusted_canonical_timeline_head_in_transaction",
            wraps=(
                self.repository.get_trusted_canonical_timeline_head_in_transaction
            ),
        ) as durable_admission:
            replayed_stale = self._mint(
                nonce=stale_nonce,
                observed_head=stale_head,
            )
        self.assertEqual(replayed_stale.status_code, 403, replayed_stale.json)
        self.assertEqual(
            replayed_stale.json["code"],
            "agent_preview_nonce_invalid",
        )
        durable_admission.assert_not_called()

        forbidden_headers = (
            (MAIN_AUTHORITY_HEADER, self.main_token),
            ("X-MemoLens-Desktop-Token", "desktop-high-authority-token"),
            (AGENT_NONCE_HEADER, "write_nonce_forbidden"),
            (AGENT_PROOF_HEADER, "0" * 64),
            (AGENT_STATUS_PROOF_HEADER, "1" * 64),
        )
        for header, value in forbidden_headers:
            with self.subTest(forbidden_mint_header=header):
                forbidden_mint = self._mint(extra_headers={header: value})
                self.assertEqual(
                    forbidden_mint.status_code,
                    403,
                    forbidden_mint.json,
                )
        forbidden_nonce = self.client.post(
            f"/v1/agent/capabilities/{self.capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": "timeline.preview_media",
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    self.secret,
                    kind="nonce",
                    identifier=(
                        f"{self.capability_id}:timeline.preview_media"
                    ),
                )
            },
        )
        self.assertEqual(forbidden_nonce.status_code, 403, forbidden_nonce.json)

        minted = self._mint()
        self.assertEqual(minted.status_code, 201, minted.json)
        lease = minted.json
        playable = next(
            row for row in lease["clips"] if row["clip_id"] == self.clip_id
        )
        self.assertEqual(playable["status"], "playable")
        self.assertIsNone(playable["reason_code"])
        self.assertEqual(
            set(lease["clips"][0]),
            {
                "clip_id",
                "timeline_start_ms",
                "timeline_end_ms",
                "source_in_ms",
                "source_out_ms",
                "status",
                "reason_code",
            },
        )
        serialized_lease = canonical_json(lease)
        for forbidden in (
            "asset_id",
            "asset_sha256",
            "asset_source_id",
            "source_binding_sha256",
            str(self.library),
        ):
            self.assertNotIn(forbidden, serialized_lease)

        with self.repository._source_cache_lock:
            self.repository._source_cache.clear()
        original_transaction = self.repository.transaction
        original_verify = self.repository.verify_bound_asset_source_content
        original_iter = TimelinePreviewService.iter_read
        transaction_depth = 0
        hash_depths: list[int] = []
        stream_observations: list[tuple[int, int]] = []

        @contextmanager
        def tracked_transaction(*, immediate: bool = False):
            nonlocal transaction_depth
            with original_transaction(immediate=immediate) as connection:
                transaction_depth += 1
                try:
                    yield connection
                finally:
                    transaction_depth -= 1

        def tracked_verify(bound, **arguments):
            hash_depths.append(transaction_depth)
            return original_verify(bound, **arguments)

        def tracked_iter(service, prepared):
            current = self.manager._current
            stream_observations.append(
                (transaction_depth, -1 if current is None else current.references)
            )
            yield from original_iter(service, prepared)

        with (
            patch.object(self.repository, "transaction", tracked_transaction),
            patch.object(
                self.repository,
                "verify_bound_asset_source_content",
                tracked_verify,
            ),
            patch.object(TimelinePreviewService, "iter_read", tracked_iter),
        ):
            first = self._read(lease, range_header="bytes=0-")
        self.assertEqual(first.status_code, 206, first.data[:200])
        self.assertEqual(len(first.data), TIMELINE_PREVIEW_MAX_RESPONSE_BYTES)
        self.assertEqual(
            first.data,
            self.video_bytes[:TIMELINE_PREVIEW_MAX_RESPONSE_BYTES],
        )
        self.assertEqual(
            first.headers["Content-Range"],
            f"bytes 0-{TIMELINE_PREVIEW_MAX_RESPONSE_BYTES - 1}/{_VIDEO_SIZE}",
        )
        self.assertEqual(hash_depths, [0])
        self.assertEqual(stream_observations, [(0, 0)])

        second_start = TIMELINE_PREVIEW_MAX_RESPONSE_BYTES
        second = self._read(lease, range_header=f"bytes={second_start}-")
        self.assertEqual(second.status_code, 206, second.data[:200])
        self.assertEqual(len(second.data), TIMELINE_PREVIEW_MAX_RESPONSE_BYTES)
        self.assertEqual(
            second.data,
            self.video_bytes[
                second_start : second_start + TIMELINE_PREVIEW_MAX_RESPONSE_BYTES
            ],
        )
        self.assertEqual(
            second.headers["Content-Range"],
            (
                f"bytes {second_start}-{(second_start * 2) - 1}/"
                f"{_VIDEO_SIZE}"
            ),
        )

        head = self._read(lease, range_header=None, method="HEAD")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.data, b"")
        self.assertEqual(int(head.headers["Content-Length"]), _VIDEO_SIZE)

        invalid = self._read(
            lease,
            range_header=f"bytes=0-{TIMELINE_PREVIEW_MAX_RESPONSE_BYTES}",
        )
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid.data, b"")
        self.assertEqual(invalid.headers["Content-Type"], "video/mp4")
        self.assertEqual(invalid.headers["Content-Length"], "0")
        self.assertEqual(invalid.headers["Accept-Ranges"], "bytes")
        self.assertEqual(invalid.headers["Content-Range"], f"bytes */{_VIDEO_SIZE}")
        self.assertEqual(invalid.headers["Cache-Control"], "private, no-store")
        self.assertEqual(invalid.headers["Pragma"], "no-cache")
        self.assertEqual(invalid.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(invalid.headers["ETag"], first.headers["ETag"])

        etag = first.headers["ETag"].strip('"')
        self.assertRegex(etag, r"^[0-9a-f]{64}$")
        self.assertNotEqual(etag, self.video_asset_sha256)
        self.assertNotIn(self.video_asset_sha256, str(first.headers))
        self.assertNotIn(str(self.video_binding["asset_source_id"]), str(first.headers))
        self.assertNotIn("Content-Disposition", first.headers)

        huge_range = "bytes=" + ("9" * 5000) + "-"
        huge = self._read(lease, range_header=huge_range)
        self.assertEqual(huge.status_code, 416)
        self.assertEqual(huge.data, b"")
        self.assertEqual(huge.headers["ETag"], first.headers["ETag"])

        replay_nonce = self._next_nonce("q")
        replay = self._read(
            lease,
            range_header="bytes=0-0",
            nonce=replay_nonce,
        )
        self.assertEqual(replay.status_code, 206)
        replayed = self._read(
            lease,
            range_header="bytes=0-0",
            nonce=replay_nonce,
        )
        self.assertEqual(replayed.status_code, 403, replayed.json)
        self.assertEqual(replayed.json["code"], "agent_preview_nonce_invalid")

        substituted = self._read(
            lease,
            range_header="bytes=0-0",
            method="GET",
            proof_method="HEAD",
        )
        self.assertEqual(substituted.status_code, 401, substituted.json)
        self.assertEqual(substituted.json["code"], "agent_preview_proof_invalid")

        unknown = self._read(
            lease,
            range_header="bytes=0-0",
            clip_id="clip_unknown_safe_playback",
        )
        self.assertEqual(unknown.status_code, 403, unknown.json)
        self.assertEqual(unknown.json["code"], "agent_preview_scope_denied")
        self.assertEqual(self._ledger_snapshot(), baseline)

    def test_hash_revocation_and_source_replacement_never_start_streaming(self) -> None:
        baseline = self._ledger_snapshot()
        minted = self._mint()
        self.assertEqual(minted.status_code, 201, minted.json)
        lease = minted.json

        active_nonce = self._next_nonce("a")
        active_path = (
            f"/v1/agent/preview-leases/{lease['lease_id']}/clips/"
            f"{self.clip_id}/media"
        )
        active_service = TimelinePreviewService(
            self.repository,
            self.pairing_broker,
            self.preview_broker,
        )
        prepared = active_service.prepare_read(
            lease_id=str(lease["lease_id"]),
            request_nonce=active_nonce,
            proof=agent_preview_read_proof(
                str(lease["lease_secret"]),
                lease_id=str(lease["lease_id"]),
                request_nonce=active_nonce,
                method="GET",
                canonical_path=active_path,
                range_header="bytes=0-1048575",
            ),
            method="GET",
            canonical_path=active_path,
            range_header="bytes=0-1048575",
            runtime_generation_id=self.manager.current_generation_id,
        )
        iterator = active_service.iter_read(prepared)
        self.assertEqual(len(next(iterator)), 64 * 1024)
        self.preview_broker.drop_lease(str(lease["lease_id"]))
        with self.assertRaises(AgentPreviewError) as raised:
            next(iterator)
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")

        minted = self._mint()
        self.assertEqual(minted.status_code, 201, minted.json)
        lease = minted.json
        with self.repository._source_cache_lock:
            self.repository._source_cache.clear()
        stream_started = False

        def forbidden_stream(_service, _prepared):
            nonlocal stream_started
            stream_started = True
            raise AssertionError("Timeline preview stream started after failed admission")
            yield b""  # pragma: no cover

        def interrupted_hash(bound, *, require_active=None):
            self.assertIsNotNone(require_active)
            bound.handle.seek(0)
            self.assertEqual(len(bound.handle.read(1024 * 1024)), 1024 * 1024)
            self.preview_broker.drop_lease(str(lease["lease_id"]))
            assert require_active is not None
            require_active()
            raise AssertionError("revoked hash callback returned")

        with (
            patch.object(
                BoundAssetSourceFile,
                "content_sha256",
                interrupted_hash,
            ),
            patch.object(TimelinePreviewService, "iter_read", forbidden_stream),
        ):
            interrupted = self._read(lease, range_header="bytes=0-")
        self.assertEqual(interrupted.status_code, 403, interrupted.json)
        self.assertEqual(interrupted.json["code"], "agent_preview_lease_expired")
        self.assertFalse(stream_started)

        minted = self._mint()
        self.assertEqual(minted.status_code, 201, minted.json)
        lease = minted.json
        replacement = self.library / "replacement.mp4"
        replacement.write_bytes(b"r" * len(self.video_bytes))
        os.replace(replacement, self.library / "preview.mp4")
        with patch.object(TimelinePreviewService, "iter_read", forbidden_stream):
            changed = self._read(lease, range_header="bytes=0-")
        self.assertEqual(changed.status_code, 409, changed.json)
        self.assertEqual(changed.json["code"], "agent_preview_source_changed")
        self.assertEqual(changed.headers["Content-Type"], "application/json")
        self.assertNotIn("Content-Range", changed.headers)
        self.assertNotIn("video/mp4", changed.headers["Content-Type"])
        changed_body = changed.get_data(as_text=True)
        for forbidden in (
            str(self.library),
            self.video_asset_sha256,
            str(lease["lease_secret"]),
        ):
            self.assertNotIn(forbidden, changed_body)
        self.assertFalse(stream_started)
        with self.repository.transaction() as connection:
            availability = connection.execute(
                "SELECT availability FROM asset_sources WHERE id=?",
                (self.video_source_id,),
            ).fetchone()[0]
        self.assertEqual(availability, "available")
        self.assertEqual(self._ledger_snapshot(), baseline)

    def test_real_split_delete_expires_old_child_lease_and_preserves_new_scope(
        self,
    ) -> None:
        command_secret = "a7" * 32
        command_capability = self._pair_capability(
            actions=["timeline.apply_structural_edit"],
            secret=command_secret,
            subject="safe_playback_real_structural_subject",
            native_nonce="native_safe_playback_real_structural_pairing",
        )
        action = "timeline.apply_structural_edit"
        path = (
            f"/v1/agent/creative/projects/{self.project_id}"
            "/timeline/structural-edit"
        )

        def post_structural(body: dict[str, object], *, key: str):
            nonce_response = self.client.post(
                f"/v1/agent/capabilities/{command_capability}/nonce",
                json={
                    "object": "memolens.agent_operation_nonce_request",
                    "schema_version": "1",
                    "action": action,
                },
                headers={
                    AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                        command_secret,
                        kind="nonce",
                        identifier=f"{command_capability}:{action}",
                    )
                },
            )
            self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
            nonce = str(nonce_response.json["nonce"])
            proof = agent_request_proof(
                command_secret,
                capability_id=command_capability,
                nonce=nonce,
                method="POST",
                canonical_path=path,
                database_uuid=self.repository.database_uuid,
                project_id=self.project_id,
                body_sha256=hashlib.sha256(
                    canonical_json(body).encode("utf-8")
                ).hexdigest(),
                idempotency_key=key,
            )
            return self.client.post(
                path,
                json=body,
                headers={
                    AGENT_CAPABILITY_HEADER: command_capability,
                    AGENT_NONCE_HEADER: nonce,
                    AGENT_PROOF_HEADER: proof,
                    "Idempotency-Key": key,
                },
            )

        def structural_body(
            workspace: dict[str, object],
            edit: dict[str, object],
        ) -> dict[str, object]:
            head = workspace["head"]
            assert isinstance(head, dict)
            return {
                "expected_blueprint": head["blueprint_binding"],
                "expected_coverage": head["coverage_binding"],
                "expected_timeline_head": head,
                "structural_edit": edit,
            }

        initial = self.timelines.read(self.project_id)
        initial_timeline = initial["timeline"]
        assert isinstance(initial_timeline, dict)
        parent_video = next(
            clip
            for clip in initial_timeline["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        )
        split_edit = {
            "op": "split_clip",
            "clip_id": parent_video["clip_id"],
            "source_split_ms": (
                int(parent_video["source_in_ms"])
                + int(parent_video["source_out_ms"])
            )
            // 2,
        }
        old_child_lease_id: str | None = None
        dispatched_edits: list[dict[str, object]] = []
        original_dispatch = self.timelines.apply_paired_structural_edit

        def real_dispatch(
            project_id: str,
            payload: object,
            **arguments: object,
        ):
            self.assertEqual(project_id, self.project_id)
            self.assertIsInstance(payload, dict)
            assert isinstance(payload, dict)
            edit = payload.get("structural_edit")
            self.assertIsInstance(edit, dict)
            assert isinstance(edit, dict)
            dispatched_edits.append(dict(edit))
            if edit.get("op") == "delete_clip":
                self.assertIsNotNone(old_child_lease_id)
                assert old_child_lease_id is not None
                with self.assertRaises(AgentPreviewError) as revoked_before_dispatch:
                    self.preview_broker.lease_authority(old_child_lease_id)
                self.assertEqual(
                    revoked_before_dispatch.exception.code,
                    "agent_preview_lease_expired",
                )
            # The spy delegates to the original bound service; no result or
            # canonical mutation is mocked.
            return original_dispatch(project_id, payload, **arguments)

        with patch.object(
            self.timelines,
            "apply_paired_structural_edit",
            side_effect=real_dispatch,
        ):
            split_response = post_structural(
                structural_body(initial, split_edit),
                key="safe-playback-real-split",
            )
            self.assertEqual(split_response.status_code, 201, split_response.json)
            self.assertEqual(
                split_response.json["result"]["structural_edit"],
                split_edit,
            )
            after_split = self.timelines.read(self.project_id)
            split_head = after_split["head"]
            split_timeline = after_split["timeline"]
            assert isinstance(split_head, dict) and isinstance(split_timeline, dict)
            split_children = [
                clip
                for clip in split_timeline["tracks"][0]["clips"]
                if clip["media_kind"] == "video"
            ]
            self.assertEqual(len(split_children), 2)
            self.assertEqual(split_head["revision"], 2)
            split_lease_response = self._mint(
                observed_head={
                    "revision": split_head["revision"],
                    "content_sha256": split_head["timeline_content_sha256"],
                }
            )
            self.assertEqual(
                split_lease_response.status_code,
                201,
                split_lease_response.json,
            )
            split_lease = split_lease_response.json
            old_child_lease_id = str(split_lease["lease_id"])
            leased_clip_ids = {
                str(clip["clip_id"])
                for clip in split_lease["clips"]
            }
            child_ids = {str(child["clip_id"]) for child in split_children}
            self.assertTrue(child_ids <= leased_clip_ids)
            deleted_child_id = str(split_children[0]["clip_id"])
            surviving_child_id = str(split_children[1]["clip_id"])
            live_child_head = self._read(
                split_lease,
                range_header=None,
                method="HEAD",
                clip_id=deleted_child_id,
            )
            self.assertEqual(live_child_head.status_code, 200)

            delete_edit = {
                "op": "delete_clip",
                "clip_id": deleted_child_id,
            }
            delete_response = post_structural(
                structural_body(after_split, delete_edit),
                key="safe-playback-real-delete",
            )
            self.assertEqual(delete_response.status_code, 201, delete_response.json)
            self.assertEqual(
                delete_response.json["result"]["structural_edit"],
                delete_edit,
            )

        self.assertEqual(dispatched_edits, [split_edit, delete_edit])
        final_workspace = self.timelines.read(self.project_id)
        final_head = final_workspace["head"]
        final_timeline = final_workspace["timeline"]
        final_bindings = final_workspace["source_bindings"]
        assert (
            isinstance(final_head, dict)
            and isinstance(final_timeline, dict)
            and isinstance(final_bindings, list)
        )
        final_clip_ids = {
            str(clip["clip_id"])
            for clip in final_timeline["tracks"][0]["clips"]
        }
        self.assertEqual(final_head["revision"], 3)
        self.assertNotEqual(final_head["operation_id"], split_head["operation_id"])
        self.assertNotIn(deleted_child_id, final_clip_ids)
        self.assertIn(surviving_child_id, final_clip_ids)
        self.assertEqual(
            {str(binding["clip_id"]) for binding in final_bindings},
            final_clip_ids,
        )

        with closing(self.repository._connect()) as connection:
            structural_rows = connection.execute(
                "SELECT result_json FROM canonical_timeline_operations "
                "WHERE project_id=? AND command_type=? ORDER BY sequence",
                (self.project_id, action),
            ).fetchall()
        self.assertEqual(
            [canonical_json(row) for row in dispatched_edits],
            [
                canonical_json(
                    json.loads(str(row[0]))["structural_edit"]
                )
                for row in structural_rows
            ],
        )

        cold = MediaRepository(self.db_path)
        try:
            cold_head = cold.get_canonical_timeline_head(self.project_id)
        finally:
            cold.close()
        assert cold_head is not None
        self.assertEqual(cold_head["timeline"], final_timeline)
        self.assertEqual(cold_head["source_bindings"], final_bindings)

        for method, range_header in (("GET", "bytes=0-0"), ("HEAD", None)):
            with self.subTest(old_deleted_child_lease=method):
                denied = self._read(
                    split_lease,
                    range_header=range_header,
                    method=method,
                    clip_id=deleted_child_id,
                )
                self.assertEqual(
                    denied.status_code,
                    403,
                    denied.get_data(as_text=True),
                )
                if method == "GET":
                    self.assertEqual(
                        denied.json["code"],
                        "agent_preview_lease_expired",
                    )
                else:
                    self.assertEqual(denied.data, b"")
                    self.assertEqual(denied.headers["Content-Type"], "application/json")
                    self.assertGreater(int(denied.headers["Content-Length"]), 0)

        new_lease_response = self._mint(
            observed_head={
                "revision": final_head["revision"],
                "content_sha256": final_head["timeline_content_sha256"],
            }
        )
        self.assertEqual(new_lease_response.status_code, 201, new_lease_response.json)
        new_lease = new_lease_response.json
        new_clip_ids = {
            str(clip["clip_id"])
            for clip in new_lease["clips"]
        }
        self.assertNotIn(deleted_child_id, new_clip_ids)
        self.assertIn(surviving_child_id, new_clip_ids)
        for method, range_header in (("GET", "bytes=0-0"), ("HEAD", None)):
            with self.subTest(new_head_deleted_child_scope=method):
                denied = self._read(
                    new_lease,
                    range_header=range_header,
                    method=method,
                    clip_id=deleted_child_id,
                )
                self.assertEqual(
                    denied.status_code,
                    403,
                    denied.get_data(as_text=True),
                )
                if method == "GET":
                    self.assertEqual(
                        denied.json["code"],
                        "agent_preview_scope_denied",
                    )
                else:
                    self.assertEqual(denied.data, b"")
                    self.assertEqual(denied.headers["Content-Type"], "application/json")
                    self.assertGreater(int(denied.headers["Content-Length"]), 0)

        surviving_get = self._read(
            new_lease,
            range_header="bytes=0-0",
            clip_id=surviving_child_id,
        )
        self.assertEqual(surviving_get.status_code, 206, surviving_get.data[:200])
        self.assertEqual(len(surviving_get.data), 1)
        surviving_head = self._read(
            new_lease,
            range_header=None,
            method="HEAD",
            clip_id=surviving_child_id,
        )
        self.assertEqual(surviving_head.status_code, 200)
        self.assertEqual(surviving_head.data, b"")
        self.assertEqual(self.timelines.read(self.project_id), final_workspace)

    def test_timeline_mutation_routes_drop_stream_before_service_dispatch(self) -> None:
        command_secret = "e5" * 32
        command_capability = self._pair_capability(
            actions=[
                "timeline.apply_edit",
                "timeline.apply_structural_edit",
                "timeline.restore_revision",
            ],
            secret=command_secret,
            subject="safe_playback_command_subject",
            native_nonce="native_safe_playback_command_pairing",
        )
        fake_result = SimpleNamespace(
            response={"object": "timeline.barrier.fixture"},
            response_status=200,
            replayed=False,
        )
        barrier_entries: list[str] = []

        def operation_nonce(action: str) -> str:
            response = self.client.post(
                f"/v1/agent/capabilities/{command_capability}/nonce",
                json={
                    "object": "memolens.agent_operation_nonce_request",
                    "schema_version": "1",
                    "action": action,
                },
                headers={
                    AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                        command_secret,
                        kind="nonce",
                        identifier=f"{command_capability}:{action}",
                    )
                },
            )
            self.assertEqual(response.status_code, 201, response.json)
            return str(response.json["nonce"])

        def agent_request(
            action: str,
            *,
            nonce: str,
            proof: str,
            idempotency_key: str,
        ):
            suffix = {
                "timeline.apply_edit": "edit",
                "timeline.apply_structural_edit": "structural-edit",
                "timeline.restore_revision": "restore",
            }[action]
            return self.client.post(
                f"/v1/agent/creative/projects/{self.project_id}/timeline/{suffix}",
                json={},
                headers={
                    AGENT_CAPABILITY_HEADER: command_capability,
                    AGENT_NONCE_HEADER: nonce,
                    AGENT_PROOF_HEADER: proof,
                    "Idempotency-Key": idempotency_key,
                },
            )

        def exact_agent_proof(
            action: str,
            *,
            nonce: str,
            idempotency_key: str,
        ) -> str:
            suffix = {
                "timeline.apply_edit": "edit",
                "timeline.apply_structural_edit": "structural-edit",
                "timeline.restore_revision": "restore",
            }[action]
            path = (
                f"/v1/agent/creative/projects/{self.project_id}/timeline/{suffix}"
            )
            return agent_request_proof(
                command_secret,
                capability_id=command_capability,
                nonce=nonce,
                method="POST",
                canonical_path=path,
                database_uuid=self.repository.database_uuid,
                project_id=self.project_id,
                body_sha256=hashlib.sha256(canonical_json({}).encode()).hexdigest(),
                idempotency_key=idempotency_key,
            )

        def barrier(method_name: str, iterator):
            def dispatch(*_args, **_kwargs):
                barrier_entries.append(method_name)
                with self.assertRaises(AgentPreviewError) as raised:
                    next(iterator)
                self.assertEqual(
                    raised.exception.code,
                    "agent_preview_lease_expired",
                )
                return fake_result

            return dispatch

        iterator = self._prepare_active_stream()
        action = "timeline.apply_edit"
        nonce = operation_nonce(action)
        key = "agent-edit-preview-barrier"
        denied = agent_request(
            action,
            nonce=nonce,
            proof="0" * 64,
            idempotency_key=key,
        )
        self.assertEqual(denied.status_code, 401, denied.json)
        self.assertEqual(len(next(iterator)), 1024 * 1024)
        with patch.object(
            self.timelines,
            "apply_paired_edit",
            side_effect=barrier("apply_paired_edit", iterator),
        ):
            accepted = agent_request(
                action,
                nonce=nonce,
                proof=exact_agent_proof(
                    action,
                    nonce=nonce,
                    idempotency_key=key,
                ),
                idempotency_key=key,
            )
        self.assertEqual(accepted.status_code, 200, accepted.json)

        iterator = self._prepare_active_stream()
        action = "timeline.apply_structural_edit"
        nonce = operation_nonce(action)
        key = "agent-structural-edit-preview-barrier"
        with patch.object(
            self.timelines,
            "apply_paired_structural_edit",
            side_effect=barrier("apply_paired_structural_edit", iterator),
        ):
            accepted = agent_request(
                action,
                nonce=nonce,
                proof=exact_agent_proof(
                    action,
                    nonce=nonce,
                    idempotency_key=key,
                ),
                idempotency_key=key,
            )
        self.assertEqual(accepted.status_code, 200, accepted.json)

        iterator = self._prepare_active_stream()
        action = "timeline.restore_revision"
        nonce = operation_nonce(action)
        key = "agent-restore-preview-barrier"
        with patch.object(
            self.timelines,
            "restore_paired_revision",
            side_effect=barrier("restore_paired_revision", iterator),
        ):
            accepted = agent_request(
                action,
                nonce=nonce,
                proof=exact_agent_proof(
                    action,
                    nonce=nonce,
                    idempotency_key=key,
                ),
                idempotency_key=key,
            )
        self.assertEqual(accepted.status_code, 200, accepted.json)

        desktop_routes = (
            ("materialize", "materialize_first_cut"),
            ("reconcile", "reconcile_from_coverage"),
            ("edit", "apply_edit"),
            ("structural-edit", "apply_structural_edit"),
            ("restore", "restore_revision"),
        )
        for suffix, method_name in desktop_routes:
            with self.subTest(desktop_timeline_route=suffix):
                iterator = self._prepare_active_stream()
                with patch.object(
                    self.timelines,
                    method_name,
                    side_effect=barrier(method_name, iterator),
                ):
                    response = self.client.post(
                        (
                            f"/v1/creative/projects/{self.project_id}/timeline/"
                            f"{suffix}"
                        ),
                        query_string={
                            "db_path": str(self.db_path),
                            "expected_database_uuid": self.repository.database_uuid,
                        },
                        json={},
                        headers={
                            DESKTOP_TOKEN_HEADER: self.desktop_token,
                            "Idempotency-Key": f"desktop-{suffix}-preview-barrier",
                        },
                    )
                self.assertEqual(response.status_code, 200, response.json)

        self.assertEqual(
            barrier_entries,
            [
                "apply_paired_edit",
                "apply_paired_structural_edit",
                "restore_paired_revision",
                "materialize_first_cut",
                "reconcile_from_coverage",
                "apply_edit",
                "apply_structural_edit",
                "restore_revision",
            ],
        )


if __name__ == "__main__":
    unittest.main()
