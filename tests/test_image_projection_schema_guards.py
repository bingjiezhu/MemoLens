from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image

from core.db import ImageIndexRepository
from core.image_analysis_contract import (
    build_projection_manifest,
    canonical_projection_manifest_json,
    canonical_projection_receipt_json,
    seal_image_analysis_result,
    seal_projection_receipt,
)
from core.image_projection_renderer import (
    IMAGE_PROJECTOR_VERSION,
    projected_image_index_row_sha256,
    render_projected_image_index_row,
)
from core.media_db import MediaRepository, canonical_json


RUNTIME_GENERATION = f"runtime_generation_{'a' * 64}"
PROFILE_SHA256 = hashlib.sha256(b"canonical-image-local-v1").hexdigest()
PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": PROFILE_SHA256,
}
PROJECTION_CONTRACT = "legacy-image-index-shadow/v1"
PROJECTOR_VERSION = IMAGE_PROJECTOR_VERSION
COMPILER_ID = "memolens.image-manifest/v1"
NOW = "2026-08-29T00:00:00+00:00"
METADATA_ARTIFACT = b"metadata"
EMBEDDING_ARTIFACT = b"embedding"
IMAGE_VECTOR_ARTIFACT = bytes(range(12))


def _provenance(rule_id: str) -> dict[str, object]:
    return {
        "producer_id": "memolens.image-worker",
        "producer_version": "1",
        "model_id": None,
        "model_version": None,
        "rule_id": rule_id,
        "rule_version": "1",
    }


def _disabled_stage(rule_id: str, reason_code: str) -> dict[str, object]:
    return {
        "status": "disabled",
        "provenance": _provenance(rule_id),
        "output": None,
        "artifact_sha256": None,
        "reason_code": reason_code,
    }


class ImageProjectionSchemaGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-image-schema-guards-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)

        self.root_id = str(self.repository.library_roots()[0]["id"])
        image_path = self.library / "guard.png"
        Image.new("RGB", (24, 12), "purple").save(image_path)
        payload = image_path.read_bytes()
        self.asset_sha256 = hashlib.sha256(payload).hexdigest()
        observed = image_path.stat()
        asset = self.repository.upsert_asset_source(
            root_id=self.root_id,
            relative_path=image_path.name,
            filename=image_path.name,
            kind="image",
            sha256=self.asset_sha256,
            mime_type="image/png",
            file_size=len(payload),
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        self.asset_id = str(asset["id"])
        self.source_id = str(asset["asset_source_id"])
        self.repository.update_image_probe(self.asset_id, width=24, height=12)

        database_stat = os.stat(self.db_path, follow_symlinks=False)
        job = self.repository.enqueue_image_analysis(
            runtime_generation=RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=self.asset_id,
            source_id=self.source_id,
            analysis_profile=PROFILE,
            expected_head=None,
            enqueue_scope="schema-guard/image-analysis",
            idempotency_key="schema-guard-publication",
        )
        self.job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(self.job_id)
        assert binding is not None
        self.binding = binding
        result = self._sealed_result()
        publication = self.repository.publish_image_analysis(
            job_id=self.job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self._artifacts(),
        )
        self.change_position = int(publication["change_position"])
        self.analysis_binding = {
            "analysis_run_id": str(binding["analysis_run_id"]),
            "revision": int(binding["intended_revision"]),
            "content_sha256": str(result["content_sha256"]),
        }
        self.source_binding_sha256 = str(binding["source_binding_sha256"])
        self.alias_set_sha256 = hashlib.sha256(b"[]").hexdigest()
        self.projected_row_document = render_projected_image_index_row(
            result=result,
            source_projection={
                "filename": image_path.name,
                "relative_path": image_path.name,
            },
            artifacts=self._artifacts(),
            projector_version=PROJECTOR_VERSION,
        )
        self.assertEqual(
            self.projected_row_document["source_binding_sha256"],
            self.source_binding_sha256,
        )
        self.row_sha256 = str(self.projected_row_document["row_sha256"])
        self._generation_sequence = 0

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _sealed_result(self) -> dict[str, object]:
        return seal_image_analysis_result(
            {
                "object": "memolens.image_analysis_result",
                "schema_version": "1",
                "asset_id": self.asset_id,
                "asset_sha256": self.asset_sha256,
                "analysis_run_id": self.binding["analysis_run_id"],
                "revision": self.binding["intended_revision"],
                "source_binding": {
                    "source_id": self.source_id,
                    "library_root_id": self.root_id,
                    "observed_size": self.binding["observed_size"],
                    "observed_mtime_ns": self.binding["observed_mtime_ns"],
                    "file_identity_sha256": self.binding["file_identity_sha256"],
                },
                "analysis_profile": {
                    "id": PROFILE["profile_id"],
                    "version": PROFILE["profile_version"],
                    "content_sha256": PROFILE["profile_sha256"],
                },
                "stages": {
                    "metadata": {
                        "status": "succeeded",
                        "provenance": _provenance("pillow-probe"),
                        "output": {
                            "mime_type": "image/png",
                            "file_size": self.binding["observed_size"],
                            "width": 24,
                            "height": 12,
                            "taken_at": None,
                            "latitude": None,
                            "longitude": None,
                            "altitude": None,
                        },
                        "artifact_sha256": hashlib.sha256(b"metadata").hexdigest(),
                        "reason_code": None,
                    },
                    "geocode": _disabled_stage(
                        "network-policy",
                        "network_not_authorized",
                    ),
                    "vision": _disabled_stage(
                        "provider-policy",
                        "provider_not_authorized",
                    ),
                    "embedding": {
                        "status": "succeeded",
                        "provenance": _provenance("embedding-router"),
                        "output": {
                            "combined_text_sha256": None,
                            "vectors": [
                                {
                                    "purpose": "image_search",
                                    "signal": "visual",
                                    "model_id": "schema-guard-visual-v1",
                                    "dimensions": 3,
                                    "artifact_sha256": hashlib.sha256(IMAGE_VECTOR_ARTIFACT).hexdigest(),
                                }
                            ],
                        },
                        "artifact_sha256": hashlib.sha256(EMBEDDING_ARTIFACT).hexdigest(),
                        "reason_code": None,
                    },
                    "quality": _disabled_stage(
                        "quality-policy",
                        "quality_not_configured",
                    ),
                },
            }
        )

    @staticmethod
    def _artifacts() -> list[dict[str, object]]:
        return [
            {
                "stage": "metadata",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(METADATA_ARTIFACT).hexdigest(),
                "bytes": METADATA_ARTIFACT,
            },
            {
                "stage": "embedding",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(EMBEDDING_ARTIFACT).hexdigest(),
                "bytes": EMBEDDING_ARTIFACT,
            },
            {
                "stage": "embedding",
                "name": "vector:image_search",
                "media_type": "application/vnd.memolens.float32-vector",
                "signal": "visual",
                "model_id": "schema-guard-visual-v1",
                "dimensions": 3,
                "artifact_sha256": hashlib.sha256(IMAGE_VECTOR_ARTIFACT).hexdigest(),
                "bytes": IMAGE_VECTOR_ARTIFACT,
            },
        ]

    def _create_generation(self, *, with_row: bool) -> str:
        self._generation_sequence += 1
        generation_id = f"image_projection_generation_{self._generation_sequence}"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO image_projection_generations(
                       id,database_uuid,projection_contract,
                       canonical_high_water_position,compiler_id,projector_version,
                       status,is_active,created_at,completed_at)
                   VALUES(?,?,?,?,?,?,'building',0,?,NULL)""",
                (
                    generation_id,
                    self.repository.database_uuid,
                    PROJECTION_CONTRACT,
                    self.change_position,
                    COMPILER_ID,
                    PROJECTOR_VERSION,
                    NOW,
                ),
            )
            if with_row:
                connection.execute(
                    """INSERT INTO image_projection_rows(
                           generation_id,asset_id,analysis_run_id,revision,
                           content_sha256,source_id,source_binding_sha256,row_json,
                           row_sha256,alias_set_sha256,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        self.asset_id,
                        self.analysis_binding["analysis_run_id"],
                        self.analysis_binding["revision"],
                        self.analysis_binding["content_sha256"],
                        self.source_id,
                        self.source_binding_sha256,
                        canonical_json(self.projected_row_document),
                        self.row_sha256,
                        self.alias_set_sha256,
                        NOW,
                    ),
                )
        return generation_id

    def _projection_receipt(self, generation_id: str) -> dict[str, object]:
        return seal_projection_receipt(
            {
                "object": "memolens.image_projection_receipt",
                "schema_version": "1",
                "projection_contract": PROJECTION_CONTRACT,
                "projector_version": PROJECTOR_VERSION,
                "database_uuid": self.repository.database_uuid,
                "change_position": self.change_position,
                "asset_id": self.asset_id,
                "analysis_binding": self.analysis_binding,
                "source_binding_sha256": self.source_binding_sha256,
                "projected_row_sha256": self.row_sha256,
                "alias_set_sha256": self.alias_set_sha256,
                "outcome": "applied",
                "reason_code": None,
            }
        )

    def _insert_projection_receipt(
        self,
        generation_id: str,
        receipt: dict[str, object],
        *,
        receipt_json: str | None = None,
    ) -> None:
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO image_projection_receipts(
                       change_position,generation_id,projection_contract,
                       projector_version,database_uuid,asset_id,analysis_run_id,
                       revision,content_sha256,source_binding_sha256,
                       projected_row_sha256,alias_set_sha256,outcome,reason_code,
                       receipt_json,receipt_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.change_position,
                    generation_id,
                    receipt["projection_contract"],
                    receipt["projector_version"],
                    receipt["database_uuid"],
                    receipt["asset_id"],
                    self.analysis_binding["analysis_run_id"],
                    self.analysis_binding["revision"],
                    self.analysis_binding["content_sha256"],
                    receipt["source_binding_sha256"],
                    receipt["projected_row_sha256"],
                    receipt["alias_set_sha256"],
                    receipt["outcome"],
                    receipt["reason_code"],
                    receipt_json or canonical_projection_receipt_json(receipt),
                    receipt["receipt_sha256"],
                    NOW,
                ),
            )

    def _manifest(self) -> dict[str, object]:
        return build_projection_manifest(
            projector_version=PROJECTOR_VERSION,
            compiler_version=COMPILER_ID,
            database_uuid=self.repository.database_uuid,
            canonical_high_water_mark=self.change_position,
            entries=[
                {
                    "asset_id": self.asset_id,
                    "analysis_binding": self.analysis_binding,
                    "expected_row_sha256": self.row_sha256,
                    "actual_row_sha256": self.row_sha256,
                    "status": "matched",
                    "reason_code": None,
                }
            ],
            alias_count=0,
            alias_set_sha256=self.alias_set_sha256,
        )

    def _insert_manifest(
        self,
        generation_id: str,
        manifest: dict[str, object],
        *,
        manifest_json: str | None = None,
    ) -> None:
        counts = manifest["counts"]
        assert isinstance(counts, dict)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO image_projection_manifests(
                       generation_id,database_uuid,canonical_high_water_position,
                       compiler_id,projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,mismatched_count,
                       blocked_count,alias_set_sha256,row_set_sha256,manifest_json,
                       manifest_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    manifest["database_uuid"],
                    manifest["canonical_high_water_mark"],
                    manifest["compiler_version"],
                    manifest["projector_version"],
                    counts["eligible"],
                    counts["projected"],
                    counts["aliases"],
                    counts["missing"],
                    counts["unexpected"],
                    counts["mismatched"],
                    counts["blocked"],
                    manifest["alias_set_sha256"],
                    manifest["row_set_sha256"],
                    manifest_json or canonical_projection_manifest_json(manifest),
                    manifest["manifest_sha256"],
                    NOW,
                ),
            )

    def test_forged_projection_receipt_self_digest_is_rejected(self) -> None:
        generation_id = self._create_generation(with_row=True)
        receipt = self._projection_receipt(generation_id)
        forged = deepcopy(receipt)
        forged["receipt_sha256"] = "0" * 64

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image projection receipt binding mismatch",
        ):
            self._insert_projection_receipt(
                generation_id,
                forged,
                receipt_json=canonical_json(forged),
            )

        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_projection_receipts").fetchone()[0],
                0,
            )

    def test_stale_checkpoint_cannot_mark_job_succeeded(self) -> None:
        generation_id = self._create_generation(with_row=True)
        receipt = self._projection_receipt(generation_id)
        self._insert_projection_receipt(generation_id, receipt)
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM media_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
        assert row is not None
        checkpoint = json.loads(str(row["checkpoint_json"]))
        checkpoint["current_stage"] = "completed"
        checkpoint["completed_stages"].append("shadow_projection")
        checkpoint["publish_binding"]["analysis_run_id"] = f"arun_{'f' * 32}"
        checkpoint["publish_binding"]["projection_receipt_sha256"] = receipt["receipt_sha256"]

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image_analysis success requires publication and projection",
        ):
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    """UPDATE media_jobs SET status='succeeded',stage='completed',
                              progress=1,checkpoint_json=?,finished_at=?
                         WHERE id=?""",
                    (canonical_json(checkpoint), NOW, self.job_id),
                )

        with self.repository.transaction() as connection:
            state = connection.execute(
                "SELECT status,stage FROM media_jobs WHERE id=?",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("running", "shadow_projection"))

    def test_forged_manifest_self_digest_is_rejected(self) -> None:
        generation_id = self._create_generation(with_row=True)
        manifest = self._manifest()
        forged = deepcopy(manifest)
        forged["manifest_sha256"] = "0" * 64

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image projection manifest binding mismatch",
        ):
            self._insert_manifest(
                generation_id,
                forged,
                manifest_json=canonical_json(forged),
            )

        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_projection_manifests").fetchone()[0],
                0,
            )

    def test_projection_row_cannot_mutate_after_manifest_publication(self) -> None:
        generation_id = self._create_generation(with_row=True)
        self._insert_manifest(generation_id, self._manifest())
        mutated_document = deepcopy(self.projected_row_document)
        mutated_row = mutated_document["row"]
        assert isinstance(mutated_row, dict)
        mutated_row["filename"] = "renamed.png"
        mutated_row["relative_path"] = "renamed.png"
        mutated_document["row_sha256"] = projected_image_index_row_sha256(mutated_row)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "complete image projection rows are immutable",
        ):
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    """UPDATE image_projection_rows SET row_json=?,row_sha256=?
                         WHERE generation_id=? AND asset_id=?""",
                    (
                        canonical_json(mutated_document),
                        mutated_document["row_sha256"],
                        generation_id,
                        self.asset_id,
                    ),
                )

        with self.repository.transaction() as connection:
            stored = connection.execute(
                """SELECT row_sha256 FROM image_projection_rows
                     WHERE generation_id=? AND asset_id=?""",
                (generation_id, self.asset_id),
            ).fetchone()
        self.assertEqual(stored["row_sha256"], self.row_sha256)

    def test_projection_row_cannot_move_between_generations(self) -> None:
        source_generation = self._create_generation(with_row=True)
        target_generation = self._create_generation(with_row=False)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "complete image projection rows are immutable",
        ):
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    """UPDATE image_projection_rows SET generation_id=?
                         WHERE generation_id=? AND asset_id=?""",
                    (target_generation, source_generation, self.asset_id),
                )

        with self.repository.transaction() as connection:
            locations = tuple(
                str(row["generation_id"])
                for row in connection.execute(
                    """SELECT generation_id FROM image_projection_rows
                         WHERE asset_id=? ORDER BY generation_id""",
                    (self.asset_id,),
                )
            )
        self.assertEqual(locations, (source_generation,))


if __name__ == "__main__":
    unittest.main()
