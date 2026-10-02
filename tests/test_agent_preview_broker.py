from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from backend.src.agent_authority import RuntimeCapability, proof_secret_sha256
from backend.src.agent_preview import (
    AgentPreviewError,
    PreviewLeaseBroker,
    agent_preview_mint_proof,
    agent_preview_read_proof,
)
from core.timeline_lowering_contract import canonical_timeline_content_sha256
from core.timeline_preview_contract import derive_timeline_preview_scope
from tests.test_timeline_lowering_contract import compile_result


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class AgentPreviewBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.epoch = "epoch_preview_fixture"
        self.generation = "runtime_generation_fixture"
        self.secret = "ab" * 32
        self.capability = RuntimeCapability(
            capability_id="cap_preview_fixture",
            pairing_id="pair_preview_fixture",
            project_id="project_timeline",
            database_uuid="database_preview_fixture",
            runtime_authority_epoch=self.epoch,
            paired_subject_id="codex_fixture",
            claimed_client_label="Codex fixture",
            actions=("timeline.preview_media",),
            proof_secret=self.secret,
            proof_secret_sha256=proof_secret_sha256(self.secret),
            issued_at=self.clock.now(),
            expires_at=self.clock.now() + timedelta(minutes=15),
            max_operations=20,
        )
        compiled, _blueprint, _coverage = compile_result()
        self.timeline = compiled["timeline"]
        self.sources = compiled["source_bindings"]
        self.head = {
            "revision": self.timeline["revision"],
            "content_sha256": canonical_timeline_content_sha256(self.timeline),
        }
        self.scope = derive_timeline_preview_scope(
            timeline=self.timeline,
            source_bindings=self.sources,
            observed_head=self.head,
        )
        self.clip_id = str(self.scope["clips"][0]["clip_id"])
        self.descriptors = [
            {
                "clip_id": self.clip_id,
                "mime_type": "video/mp4",
                "size_bytes": 1024,
                "probe_status": "ready",
                "video_codec": "h264",
            }
        ]
        self.request = {
            "object": "memolens.timeline_preview_lease_request",
            "schema_version": "1",
            "project_id": "project_timeline",
            "observed_head": self.head,
            "request_nonce": "m" * 32,
        }
        self.mint_path = (
            "/v1/agent/creative/projects/project_timeline/timeline/preview-leases"
        )
        self.broker = PreviewLeaseBroker(self.epoch, now=self.clock.now)

    def _mint(self, *, request: dict[str, object] | None = None, proof: str | None = None):
        payload = request or self.request
        exact_proof = proof or agent_preview_mint_proof(
            self.secret,
            capability_id=self.capability.capability_id,
            method="POST",
            canonical_path=self.mint_path,
            database_uuid=self.capability.database_uuid,
            body_sha256=hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )
        return self.broker.mint_lease(
            capability=self.capability,
            request_payload=payload,
            proof=exact_proof,
            canonical_path=self.mint_path,
            runtime_generation_id=self.generation,
            scope=self.scope,
            media_descriptors=self.descriptors,
        )

    def _begin(
        self,
        lease: dict[str, object],
        *,
        nonce: str,
        range_header: str | None = "bytes=0-99",
        method: str = "GET",
    ):
        path = (
            f"/v1/agent/preview-leases/{lease['lease_id']}/clips/"
            f"{self.clip_id}/media"
        )
        proof = agent_preview_read_proof(
            str(lease["lease_secret"]),
            lease_id=str(lease["lease_id"]),
            request_nonce=nonce,
            method=method,
            canonical_path=path,
            range_header=range_header,
        )
        return self.broker.begin_read(
            lease_id=str(lease["lease_id"]),
            request_nonce=nonce,
            proof=proof,
            method=method,
            canonical_path=path,
            range_header=range_header,
            current_database_uuid=self.capability.database_uuid,
            current_runtime_generation_id=self.generation,
            current_capability_id=self.capability.capability_id,
            current_paired_subject_id=self.capability.paired_subject_id,
            current_project_id=self.capability.project_id,
            current_head=self.head,
            current_source_bindings_sha256=str(
                self.scope["source_bindings_sha256"]
            ),
        )

    def test_mint_is_preview_only_server_state_and_response_is_path_free(self) -> None:
        lease = self._mint()
        self.assertEqual(lease["project_id"], self.capability.project_id)
        self.assertEqual(lease["clips"][0]["status"], "playable")
        self.assertEqual(lease["budgets"]["max_concurrency"], 2)
        serialized = json.dumps(lease, sort_keys=True)
        for forbidden in (
            "asset_id",
            "asset_sha256",
            "asset_source_id",
            "source_binding_sha256",
            "/Users/",
            "backend_url",
            self.secret,
        ):
            self.assertNotIn(forbidden, serialized)

    def test_mint_proof_scope_and_nonce_replay_fail_closed(self) -> None:
        with self.assertRaises(AgentPreviewError) as raised:
            self._mint(proof="0" * 64)
        self.assertEqual(raised.exception.code, "agent_preview_proof_invalid")

        self._mint()
        with self.assertRaises(AgentPreviewError) as raised:
            self._mint()
        self.assertEqual(raised.exception.code, "agent_preview_nonce_invalid")

        no_preview = RuntimeCapability(
            **{
                **self.capability.__dict__,
                "actions": ("timeline.apply_edit",),
            }
        )
        proof = agent_preview_mint_proof(
            self.secret,
            capability_id=no_preview.capability_id,
            method="POST",
            canonical_path=self.mint_path,
            database_uuid=no_preview.database_uuid,
            body_sha256=hashlib.sha256(
                json.dumps(self.request, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        other = PreviewLeaseBroker(self.epoch, now=self.clock.now)
        with self.assertRaises(AgentPreviewError) as raised:
            other.mint_lease(
                capability=no_preview,
                request_payload=self.request,
                proof=proof,
                canonical_path=self.mint_path,
                runtime_generation_id=self.generation,
                scope=self.scope,
                media_descriptors=self.descriptors,
            )
        self.assertEqual(raised.exception.code, "agent_preview_scope_denied")

    def test_spent_mint_nonce_store_is_hard_bounded_and_recovers_after_ttl(self) -> None:
        bounded = PreviewLeaseBroker(
            self.epoch,
            now=self.clock.now,
            max_leases=1,
        )

        def claim(index: int) -> str:
            request = {
                **self.request,
                "request_nonce": f"s{index:031d}",
            }
            proof = agent_preview_mint_proof(
                self.secret,
                capability_id=self.capability.capability_id,
                method="POST",
                canonical_path=self.mint_path,
                database_uuid=self.capability.database_uuid,
                body_sha256=hashlib.sha256(
                    json.dumps(
                        request,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
            )
            return bounded.claim_mint_nonce(
                capability=self.capability,
                request_payload=request,
                proof=proof,
                canonical_path=self.mint_path,
            )

        self.assertEqual(bounded._max_mint_nonces, 4)
        for index in range(bounded._max_mint_nonces):
            bounded.abandon_mint_nonce_claim(claim(index))
        self.assertEqual(len(bounded._mint_nonces), bounded._max_mint_nonces)
        self.assertEqual(len(bounded._mint_nonce_claims), 0)

        with self.assertRaises(AgentPreviewError) as raised:
            claim(bounded._max_mint_nonces)
        self.assertEqual(raised.exception.code, "agent_preview_rate_limited")
        self.assertEqual(len(bounded._mint_nonces), bounded._max_mint_nonces)
        self.assertEqual(len(bounded._mint_nonce_claims), 0)

        with self.assertRaises(AgentPreviewError) as raised:
            claim(0)
        self.assertEqual(raised.exception.code, "agent_preview_nonce_invalid")
        self.assertEqual(len(bounded._mint_nonces), bounded._max_mint_nonces)

        self.clock.advance(91)
        recovered_claim = claim(bounded._max_mint_nonces)
        bounded.abandon_mint_nonce_claim(recovered_claim)
        self.assertEqual(len(bounded._mint_nonces), 1)
        self.assertEqual(len(bounded._mint_nonce_claims), 0)

    def test_read_proof_nonce_range_and_concurrency_are_atomic(self) -> None:
        lease = self._mint()
        first = self._begin(lease, nonce="a" * 32)
        second = self._begin(lease, nonce="b" * 32)
        self.broker.require_active_read(first.reservation_id)
        self.assertEqual(first.byte_range.length, 100)
        with self.assertRaises(AgentPreviewError) as raised:
            self._begin(lease, nonce="c" * 32)
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")
        self.broker.finish_read(first.reservation_id)
        third = self._begin(lease, nonce="d" * 32, range_header="bytes=-8")
        self.assertEqual(third.byte_range.length, 8)
        self.broker.finish_read(second.reservation_id)
        self.broker.finish_read(third.reservation_id)
        with self.assertRaises(AgentPreviewError) as raised:
            self.broker.require_active_read(first.reservation_id)
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")

        with self.assertRaises(AgentPreviewError) as raised:
            self._begin(lease, nonce="a" * 32)
        self.assertEqual(raised.exception.code, "agent_preview_nonce_invalid")
        with self.assertRaises(AgentPreviewError) as raised:
            self._begin(lease, nonce="e" * 32, range_header="bytes=0-1,4-5")
        self.assertEqual(raised.exception.code, "agent_preview_range_invalid")
        self.assertEqual(raised.exception.details["source_size"], 1024)
        self.assertEqual(raised.exception.details["mime_type"], "video/mp4")
        self.assertRegex(raised.exception.details["opaque_etag"], r"^[0-9a-f]{64}$")

        substituted_path = (
            f"/v1/agent/preview-leases/preview_{'0' * 32}/clips/"
            f"{self.clip_id}/media"
        )
        proof = agent_preview_read_proof(
            str(lease["lease_secret"]),
            lease_id=str(lease["lease_id"]),
            request_nonce="q" * 32,
            method="GET",
            canonical_path=substituted_path,
            range_header="bytes=0-1",
        )
        with self.assertRaises(AgentPreviewError) as raised:
            self.broker.begin_read(
                lease_id=str(lease["lease_id"]),
                request_nonce="q" * 32,
                proof=proof,
                method="GET",
                canonical_path=substituted_path,
                range_header="bytes=0-1",
                current_database_uuid=self.capability.database_uuid,
                current_runtime_generation_id=self.generation,
                current_capability_id=self.capability.capability_id,
                current_paired_subject_id=self.capability.paired_subject_id,
                current_project_id=self.capability.project_id,
                current_head=self.head,
                current_source_bindings_sha256=str(
                    self.scope["source_bindings_sha256"]
                ),
            )
        self.assertEqual(raised.exception.code, "agent_preview_scope_denied")

    def test_runtime_head_revoke_expiry_and_restart_invalidate(self) -> None:
        lease = self._mint()
        def attempt(nonce: str, **changes: object) -> AgentPreviewError:
            path = (
                f"/v1/agent/preview-leases/{lease['lease_id']}/clips/"
                f"{self.clip_id}/media"
            )
            proof = agent_preview_read_proof(
                str(lease["lease_secret"]),
                lease_id=str(lease["lease_id"]),
                request_nonce=nonce,
                method="GET",
                canonical_path=path,
                range_header="bytes=0-1",
            )
            arguments = {
                "lease_id": str(lease["lease_id"]),
                "request_nonce": nonce,
                "proof": proof,
                "method": "GET",
                "canonical_path": path,
                "range_header": "bytes=0-1",
                "current_database_uuid": self.capability.database_uuid,
                "current_runtime_generation_id": self.generation,
                "current_capability_id": self.capability.capability_id,
                "current_paired_subject_id": self.capability.paired_subject_id,
                "current_project_id": self.capability.project_id,
                "current_head": self.head,
                "current_source_bindings_sha256": str(
                    self.scope["source_bindings_sha256"]
                ),
            }
            arguments.update(changes)
            with self.assertRaises(AgentPreviewError) as raised:
                self.broker.begin_read(**arguments)
            return raised.exception

        self.assertEqual(
            attempt("f" * 32, current_runtime_generation_id="runtime_generation_other").code,
            "agent_preview_lease_expired",
        )
        self.assertEqual(
            attempt("d" * 32, current_database_uuid="database_preview_other").code,
            "agent_preview_lease_expired",
        )
        self.assertEqual(
            attempt("c" * 32, current_capability_id="cap_preview_other").code,
            "agent_preview_lease_expired",
        )
        self.assertEqual(
            attempt("s" * 32, current_paired_subject_id="subject_other").code,
            "agent_preview_lease_expired",
        )
        self.assertEqual(
            attempt(
                "b" * 32,
                current_source_bindings_sha256="0" * 64,
            ).code,
            "agent_preview_source_changed",
        )
        self.assertEqual(
            attempt("p" * 32, current_project_id="project_preview_other").code,
            "agent_preview_scope_denied",
        )
        stale = dict(self.head)
        stale["content_sha256"] = "0" * 64
        self.assertEqual(
            attempt("g" * 32, current_head=stale).code,
            "agent_preview_head_changed",
        )

        self.broker.revoke_capability(self.capability.capability_id)
        self.assertEqual(attempt("h" * 32).code, "agent_preview_lease_expired")

        replacement = PreviewLeaseBroker(self.epoch, now=self.clock.now)
        self.broker = replacement
        self.assertEqual(attempt("i" * 32).code, "agent_preview_lease_expired")

        self.broker = PreviewLeaseBroker(self.epoch, now=self.clock.now)
        self.request["request_nonce"] = "z" * 32
        lease = self._mint()
        self.clock.advance(91)
        self.assertEqual(attempt("j" * 32).code, "agent_preview_lease_expired")

    def test_drop_or_revoke_stops_an_active_read(self) -> None:
        lease = self._mint()
        reservation = self._begin(lease, nonce="x" * 32)
        self.broker.drop_lease(str(lease["lease_id"]))
        with self.assertRaises(AgentPreviewError) as raised:
            self.broker.require_active_read(reservation.reservation_id)
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")

        self.request["request_nonce"] = "w" * 32
        lease = self._mint()
        reservation = self._begin(lease, nonce="w" * 32)
        self.broker.drop_all()
        with self.assertRaises(AgentPreviewError) as raised:
            self.broker.require_active_read(reservation.reservation_id)
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")

        self.request["request_nonce"] = "y" * 32
        lease = self._mint()
        reservation = self._begin(lease, nonce="y" * 32)
        self.broker.revoke_capability(self.capability.capability_id)
        with self.assertRaises(AgentPreviewError) as raised:
            self.broker.require_active_read(reservation.reservation_id)
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")

    def test_large_open_ended_reads_are_sequential_bounded_windows(self) -> None:
        self.descriptors[0]["size_bytes"] = (16 * 1024 * 1024) + 257
        lease = self._mint()
        first = self._begin(lease, nonce="o" * 32, range_header="bytes=0-")
        self.assertEqual(
            (first.byte_range.start, first.byte_range.end, first.byte_range.length),
            (0, (8 * 1024 * 1024) - 1, 8 * 1024 * 1024),
        )
        self.broker.finish_read(first.reservation_id)
        second = self._begin(
            lease,
            nonce="p" * 32,
            range_header=f"bytes={8 * 1024 * 1024}-",
        )
        self.assertEqual(
            (second.byte_range.start, second.byte_range.end, second.byte_range.length),
            (8 * 1024 * 1024, (16 * 1024 * 1024) - 1, 8 * 1024 * 1024),
        )
        self.broker.finish_read(second.reservation_id)

        head = self._begin(
            lease,
            nonce="h" * 32,
            range_header=None,
            method="HEAD",
        )
        self.assertEqual(head.byte_range.status_code, 200)
        self.assertEqual(head.byte_range.length, (16 * 1024 * 1024) + 257)
        self.broker.finish_read(head.reservation_id)

    def test_request_and_aggregate_budgets_do_not_use_write_operations(self) -> None:
        self.descriptors[0]["size_bytes"] = 8 * 1024 * 1024
        lease = self._mint()
        for index in range(8):
            reservation = self._begin(
                lease,
                nonce=f"{index:032d}",
                range_header="bytes=0-8388607",
            )
            self.broker.finish_read(reservation.reservation_id)
        with self.assertRaises(AgentPreviewError) as raised:
            self._begin(
                lease,
                nonce=f"{8:032d}",
                range_header="bytes=0-8388607",
            )
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")
        self.assertEqual(self.capability.max_operations, 20)

        self.broker = PreviewLeaseBroker(self.epoch, now=self.clock.now)
        self.descriptors[0]["size_bytes"] = 1024
        self.request["request_nonce"] = "r" * 32
        lease = self._mint()
        for index in range(64):
            reservation = self._begin(
                lease,
                nonce=f"r{index:031d}",
                range_header="bytes=0-0",
            )
            self.broker.finish_read(reservation.reservation_id)
        with self.assertRaises(AgentPreviewError) as raised:
            self._begin(
                lease,
                nonce=f"r{64:031d}",
                range_header="bytes=0-0",
            )
        self.assertEqual(raised.exception.code, "agent_preview_lease_expired")

    def test_unsupported_codec_never_creates_playable_success(self) -> None:
        self.descriptors[0]["video_codec"] = "hevc"
        lease = self._mint()
        self.assertEqual(lease["clips"][0]["status"], "unsupported")
        with self.assertRaises(AgentPreviewError) as raised:
            self._begin(lease, nonce="k" * 32)
        self.assertEqual(raised.exception.code, "agent_preview_media_unsupported")


if __name__ == "__main__":
    unittest.main()
