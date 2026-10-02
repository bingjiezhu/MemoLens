"""Definitive Core admission and fd-pinned Timeline preview data plane."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import sqlite3
from typing import Iterator, Mapping

from core.media_db import (
    AgentCapabilityError,
    BlueprintIntegrityError,
    BoundAssetSourceFile,
    CanonicalTimelineIntegrityError,
    CoverageIntegrityError,
    MediaRepository,
    canonical_json,
)
from core.timeline_preview_contract import (
    TimelinePreviewContractError,
    derive_timeline_preview_scope,
    require_timeline_preview_lease_request,
)

from ..agent_authority import AgentPairingBroker, AgentProtocolError, RuntimeCapability
from ..agent_preview import (
    AgentPreviewError,
    PreviewLeaseBroker,
    PreviewReadReservation,
    agent_preview_mint_proof,
)


_FIRST_BLOCK_BYTES = 64 * 1024
_STREAM_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _PreviewAdmission:
    scope: dict[str, object]
    descriptors: list[dict[str, object]]


@dataclass
class PreparedTimelinePreviewRead:
    reservation: PreviewReadReservation
    source: BoundAssetSourceFile
    first_block: bytes
    mime_type: str
    opaque_etag: str

    @property
    def size(self) -> int:
        return self.source.size


def _preview_error_from_contract(exc: TimelinePreviewContractError) -> AgentPreviewError:
    return AgentPreviewError(
        exc.code,
        exc.message,
        status=(409 if exc.code in {"agent_preview_head_changed", "agent_preview_source_changed"} else 403),
    )


def _preview_error_from_capability(
    exc: AgentCapabilityError | AgentProtocolError,
    *,
    reading: bool,
) -> AgentPreviewError:
    code = getattr(exc, "code", "agent_pairing_scope_denied")
    if code == "agent_pairing_proof_invalid":
        return AgentPreviewError(
            "agent_preview_proof_invalid",
            "Timeline preview capability proof is invalid.",
            status=401,
        )
    if reading and code in {
        "agent_pairing_required",
        "agent_pairing_expired",
        "agent_pairing_revoked",
    }:
        return AgentPreviewError(
            "agent_preview_lease_expired",
            "Timeline preview capability is no longer active.",
            status=403,
        )
    return AgentPreviewError(
        "agent_preview_scope_denied",
        "The paired capability does not cover this Timeline preview scope.",
        status=403,
    )


class TimelinePreviewService:
    """Coordinate durable authority, runtime leases, and exact source fds."""

    def __init__(
        self,
        repository: MediaRepository,
        pairing_broker: AgentPairingBroker,
        preview_broker: PreviewLeaseBroker,
    ) -> None:
        self.repository = repository
        self.pairing_broker = pairing_broker
        self.preview_broker = preview_broker

    @staticmethod
    def _descriptor_in_transaction(
        connection: sqlite3.Connection,
        clip: Mapping[str, object],
    ) -> dict[str, object]:
        row = connection.execute(
            """SELECT asset.mime_type,asset.file_size,asset.probe_status,
                      asset.codec_json
                 FROM main.asset_sources source
                 JOIN main.assets asset ON asset.id=source.asset_id
                 JOIN main.library_roots root ON root.id=source.library_root_id
                WHERE source.id=? AND source.asset_id=? AND asset.sha256=?
                  AND asset.kind='video' AND source.availability='available'
                  AND root.status='active'""",
            (
                clip["asset_source_id"],
                clip["asset_id"],
                clip["asset_sha256"],
            ),
        ).fetchone()
        if row is None:
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview source metadata is unavailable.",
                status=409,
            )
        raw_codec = row["codec_json"]
        if (
            type(raw_codec) is not str
            or len(raw_codec.encode("utf-8")) > 64 * 1024
        ):
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview source codec metadata is invalid.",
                status=409,
            )
        try:
            codec = json.loads(raw_codec)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview source codec metadata is invalid.",
                status=409,
            ) from exc
        if (
            type(codec) is not dict
            or canonical_json(codec) != raw_codec
        ):
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview source codec metadata is invalid.",
                status=409,
            )
        video_codec = codec.get("video_codec")
        return {
            "clip_id": clip["clip_id"],
            "mime_type": str(row["mime_type"]).casefold(),
            "size_bytes": int(row["file_size"]),
            "probe_status": str(row["probe_status"]),
            "video_codec": (
                video_codec.casefold() if isinstance(video_codec, str) else ""
            ),
        }

    def _admit_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        runtime_capability: RuntimeCapability,
        project_id: str,
        observed_head: Mapping[str, object],
        reading: bool,
    ) -> _PreviewAdmission:
        try:
            self.repository.require_timeline_preview_capability_in_transaction(
                connection,
                capability_id=runtime_capability.capability_id,
                runtime_authority_epoch=runtime_capability.runtime_authority_epoch,
                database_uuid=runtime_capability.database_uuid,
                project_id=project_id,
                paired_subject_id=runtime_capability.paired_subject_id,
                presented_secret_sha256=runtime_capability.proof_secret_sha256,
            )
        except AgentCapabilityError as exc:
            raise _preview_error_from_capability(exc, reading=reading) from exc
        head = self.repository.get_trusted_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )
        if head is None:
            raise AgentPreviewError(
                "agent_preview_head_changed",
                "Canonical Timeline head is unavailable.",
                status=409,
            )
        timeline = head.get("timeline")
        source_bindings = head.get("source_bindings")
        if type(timeline) is not dict or type(source_bindings) is not list:
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Canonical Timeline source bindings are invalid.",
                status=409,
            )
        if not self.repository.canonical_timeline_source_bindings_current_in_transaction(
            connection,
            source_bindings,
        ):
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Canonical Timeline source bindings are no longer current.",
                status=409,
            )
        try:
            scope = derive_timeline_preview_scope(
                timeline=timeline,
                source_bindings=source_bindings,
                observed_head=observed_head,
            )
        except TimelinePreviewContractError as exc:
            raise _preview_error_from_contract(exc) from exc
        descriptors = [
            self._descriptor_in_transaction(connection, clip)
            for clip in scope["clips"]  # type: ignore[index]
        ]
        return _PreviewAdmission(scope=scope, descriptors=descriptors)

    def _runtime_capability(
        self,
        capability_id: str,
        *,
        database_uuid: str,
        project_id: str,
        reading: bool,
    ) -> RuntimeCapability:
        try:
            return self.pairing_broker.runtime_capability(
                capability_id,
                database_uuid=database_uuid,
                project_id=project_id,
                action="timeline.preview_media",
            )
        except AgentProtocolError as exc:
            raise _preview_error_from_capability(exc, reading=reading) from exc

    def mint_lease(
        self,
        *,
        capability_id: str,
        request_payload: object,
        proof: str,
        canonical_path: str,
        runtime_generation_id: str,
    ) -> dict[str, object]:
        try:
            request_row = require_timeline_preview_lease_request(request_payload)
        except TimelinePreviewContractError as exc:
            raise _preview_error_from_contract(exc) from exc
        runtime_capability = self._runtime_capability(
            capability_id,
            database_uuid=self.repository.database_uuid,
            project_id=str(request_row["project_id"]),
            reading=False,
        )
        expected_proof = agent_preview_mint_proof(
            runtime_capability.proof_secret,
            capability_id=capability_id,
            method="POST",
            canonical_path=canonical_path,
            database_uuid=self.repository.database_uuid,
            body_sha256=hashlib.sha256(
                canonical_json(request_row).encode("utf-8")
            ).hexdigest(),
        )
        if not proof or not hmac.compare_digest(proof, expected_proof):
            raise AgentPreviewError(
                "agent_preview_proof_invalid",
                "Timeline preview mint proof is invalid.",
                status=401,
            )
        mint_nonce_claim = self.preview_broker.claim_mint_nonce(
            capability=runtime_capability,
            request_payload=request_row,
            proof=proof,
            canonical_path=canonical_path,
        )
        lease: dict[str, object] | None = None
        try:
            with self.repository.transaction(immediate=True) as connection:
                admission = self._admit_in_transaction(
                    connection,
                    runtime_capability=runtime_capability,
                    project_id=str(request_row["project_id"]),
                    observed_head=request_row["observed_head"],  # type: ignore[arg-type]
                    reading=False,
                )
                lease = self.preview_broker.mint_lease(
                    capability=runtime_capability,
                    request_payload=request_row,
                    proof=proof,
                    canonical_path=canonical_path,
                    runtime_generation_id=runtime_generation_id,
                    scope=admission.scope,
                    media_descriptors=admission.descriptors,
                    mint_nonce_claim=mint_nonce_claim,
                )
            return lease
        except Exception:
            if lease is not None:
                self.preview_broker.drop_lease(str(lease["lease_id"]))
            raise
        finally:
            self.preview_broker.abandon_mint_nonce_claim(mint_nonce_claim)

    @staticmethod
    def _current_clip(
        admission: _PreviewAdmission,
        clip_id: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        clips = admission.scope["clips"]
        assert isinstance(clips, list)
        clip = next(
            (item for item in clips if item.get("clip_id") == clip_id),
            None,
        )
        descriptor = next(
            (item for item in admission.descriptors if item.get("clip_id") == clip_id),
            None,
        )
        if not isinstance(clip, dict) or not isinstance(descriptor, dict):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview clip is outside the current project scope.",
                status=403,
            )
        return clip, descriptor

    @staticmethod
    def _require_grant_current(
        reservation: PreviewReadReservation,
        clip: Mapping[str, object],
        descriptor: Mapping[str, object],
    ) -> None:
        current = {**clip, **descriptor}
        compared = set(clip) | set(descriptor)
        if any(reservation.grant.get(field) != current.get(field) for field in compared):
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview clip or source metadata changed after lease mint.",
                status=409,
            )

    def prepare_read(
        self,
        *,
        lease_id: str,
        request_nonce: str,
        proof: str,
        method: str,
        canonical_path: str,
        range_header: str | None,
        runtime_generation_id: str,
    ) -> PreparedTimelinePreviewRead:
        authority = self.preview_broker.lease_authority(lease_id)
        runtime_capability = self._runtime_capability(
            authority.capability_id,
            database_uuid=authority.database_uuid,
            project_id=authority.project_id,
            reading=True,
        )
        reservation: PreviewReadReservation | None = None
        source: BoundAssetSourceFile | None = None
        try:
            # Phase 1 pins the exact row and fd in a short WAL read snapshot and
            # atomically spends the proof nonce/budgets before any full-file hash.
            with self.repository.transaction() as connection:
                admission = self._admit_in_transaction(
                    connection,
                    runtime_capability=runtime_capability,
                    project_id=authority.project_id,
                    observed_head=authority.observed_head,
                    reading=True,
                )
                reservation = self.preview_broker.begin_read(
                    lease_id=lease_id,
                    request_nonce=request_nonce,
                    proof=proof,
                    method=method,
                    canonical_path=canonical_path,
                    range_header=range_header,
                    current_database_uuid=self.repository.database_uuid,
                    current_runtime_generation_id=runtime_generation_id,
                    current_capability_id=runtime_capability.capability_id,
                    current_paired_subject_id=runtime_capability.paired_subject_id,
                    current_project_id=authority.project_id,
                    current_head=admission.scope["observed_head"],  # type: ignore[arg-type]
                    current_source_bindings_sha256=str(
                        admission.scope["source_bindings_sha256"]
                    ),
                )
                clip, descriptor = self._current_clip(admission, reservation.clip_id)
                self._require_grant_current(reservation, clip, descriptor)
                source = self.repository.open_bound_asset_source_file(
                    str(clip["asset_source_id"]),
                    str(clip["asset_id"]),
                    str(clip["asset_sha256"]),
                    int(descriptor["size_bytes"]),
                    connection=connection,
                    mark_changed=False,
                    verify_content=False,
                )
                if source is None:
                    raise AgentPreviewError(
                        "agent_preview_source_changed",
                        "Timeline preview source identity changed before admission.",
                        status=409,
                    )

            assert reservation is not None and source is not None
            self.preview_broker.require_active_read(reservation.reservation_id)
            if not self.repository.verify_bound_asset_source_content(
                source,
                expected_sha256=str(reservation.grant["asset_sha256"]),
                require_active=lambda: self.preview_broker.require_active_read(
                    reservation.reservation_id
                ),
            ):
                raise AgentPreviewError(
                    "agent_preview_source_changed",
                    "Timeline preview source bytes changed after import.",
                    status=409,
                )
            self.preview_broker.require_active_read(reservation.reservation_id)

            # Phase 2 is the definitive short serialization point with Timeline
            # writes.  It performs no full-file hashing.
            with self.repository.transaction(immediate=True) as connection:
                runtime_capability = self._runtime_capability(
                    authority.capability_id,
                    database_uuid=authority.database_uuid,
                    project_id=authority.project_id,
                    reading=True,
                )
                admission = self._admit_in_transaction(
                    connection,
                    runtime_capability=runtime_capability,
                    project_id=authority.project_id,
                    observed_head=authority.observed_head,
                    reading=True,
                )
                clip, descriptor = self._current_clip(admission, reservation.clip_id)
                self._require_grant_current(reservation, clip, descriptor)
                if not self.repository.bound_asset_source_file_current_in_transaction(
                    connection,
                    source,
                    asset_source_id=str(clip["asset_source_id"]),
                    expected_asset_id=str(clip["asset_id"]),
                    expected_sha256=str(clip["asset_sha256"]),
                    expected_size=int(descriptor["size_bytes"]),
                ):
                    raise AgentPreviewError(
                        "agent_preview_source_changed",
                        "Timeline preview source changed before the first byte.",
                        status=409,
                    )
                self.preview_broker.require_active_read(reservation.reservation_id)
                byte_range = reservation.byte_range
                source.handle.seek(byte_range.start)
                first_block = (
                    b""
                    if reservation.method == "HEAD"
                    else source.handle.read(
                        min(_FIRST_BLOCK_BYTES, byte_range.length)
                    )
                )
                if reservation.method == "GET" and not first_block:
                    raise AgentPreviewError(
                        "agent_preview_source_changed",
                        "Timeline preview source became unreadable before the first byte.",
                        status=409,
                    )
                source.verify_current_identity()
                self.preview_broker.require_active_read(reservation.reservation_id)

            etag = str(reservation.grant.get("opaque_etag") or "")
            if len(etag) != 64 or any(character not in "0123456789abcdef" for character in etag):
                raise AgentPreviewError(
                    "agent_preview_source_changed",
                    "Timeline preview response identity is invalid.",
                    status=409,
                )
            return PreparedTimelinePreviewRead(
                reservation=reservation,
                source=source,
                first_block=first_block,
                mime_type=str(descriptor["mime_type"]),
                opaque_etag=etag,
            )
        except (BlueprintIntegrityError, CanonicalTimelineIntegrityError, CoverageIntegrityError) as exc:
            if source is not None:
                source.close()
            if reservation is not None:
                self.preview_broker.finish_read(reservation.reservation_id)
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Canonical Timeline preview authority failed integrity validation.",
                status=409,
            ) from exc
        except Exception:
            if source is not None:
                source.close()
            if reservation is not None:
                self.preview_broker.finish_read(reservation.reservation_id)
            raise

    def finish_read(self, prepared: PreparedTimelinePreviewRead) -> None:
        try:
            prepared.source.close()
        finally:
            self.preview_broker.finish_read(prepared.reservation.reservation_id)

    def iter_read(self, prepared: PreparedTimelinePreviewRead) -> Iterator[bytes]:
        remaining = prepared.reservation.byte_range.length
        try:
            self.preview_broker.require_active_read(
                prepared.reservation.reservation_id
            )
            prepared.source.verify_current_identity()
            if prepared.first_block:
                remaining -= len(prepared.first_block)
                yield prepared.first_block
            while remaining > 0:
                self.preview_broker.require_active_read(
                    prepared.reservation.reservation_id
                )
                prepared.source.verify_current_identity()
                chunk = prepared.source.handle.read(
                    min(_STREAM_BLOCK_BYTES, remaining)
                )
                if not chunk:
                    raise AgentPreviewError(
                        "agent_preview_source_changed",
                        "Timeline preview source ended before the reserved range.",
                        status=409,
                    )
                remaining -= len(chunk)
                yield chunk
        finally:
            self.finish_read(prepared)


__all__ = [
    "PreparedTimelinePreviewRead",
    "TimelinePreviewService",
]
