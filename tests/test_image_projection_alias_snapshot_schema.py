from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
import unittest

from core.image_analysis_contract import (
    build_projection_manifest,
    canonical_projection_manifest_json,
    seal_image_analysis_result,
    seal_projection_receipt,
)
from core.image_analysis_schema import V14_SCHEMA_OBJECTS
from core.media_db import content_sha256
from tests import test_image_projection_schema_guards as schema_guards


class ImageProjectionAliasSnapshotSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(methodName="runTest")
        self.fixture.setUp()
        self.repository = self.fixture.repository

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _insert_alias(self, legacy_id: str) -> dict[str, str]:
        identity = {
            "database_uuid": self.repository.database_uuid,
            "legacy_id": legacy_id,
            "canonical_asset_id": self.fixture.asset_id,
            "library_root_id": self.fixture.root_id,
            "source_id": self.fixture.source_id,
            "alias_scope": "legacy-image-index/v1",
        }
        alias_sha256 = content_sha256(identity)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO legacy_image_aliases(
                       database_uuid,legacy_id,canonical_asset_id,library_root_id,
                       source_id,alias_scope,alias_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    identity["database_uuid"],
                    identity["legacy_id"],
                    identity["canonical_asset_id"],
                    identity["library_root_id"],
                    identity["source_id"],
                    identity["alias_scope"],
                    alias_sha256,
                    schema_guards.NOW,
                ),
            )
        return {"legacy_id": legacy_id, "alias_sha256": alias_sha256}

    def _snapshot_entries(self, generation_id: str) -> list[dict[str, str]]:
        with self.repository.transaction() as connection:
            rows = connection.execute(
                """SELECT legacy_id,alias_sha256
                     FROM image_projection_aliases
                    WHERE generation_id=? ORDER BY legacy_id""",
                (generation_id,),
            ).fetchall()
        return [
            {
                "legacy_id": str(row["legacy_id"]),
                "alias_sha256": str(row["alias_sha256"]),
            }
            for row in rows
        ]

    def _snapshot_binding(self, generation_id: str) -> tuple[int, str]:
        entries = self._snapshot_entries(generation_id)
        return len(entries), content_sha256(entries)

    def _manifest(
        self,
        *,
        alias_count: int,
        alias_set_sha256: str,
    ) -> dict[str, object]:
        return build_projection_manifest(
            projector_version=schema_guards.PROJECTOR_VERSION,
            compiler_version=schema_guards.COMPILER_ID,
            database_uuid=self.repository.database_uuid,
            canonical_high_water_mark=self.fixture.change_position,
            entries=[],
            alias_count=alias_count,
            alias_set_sha256=alias_set_sha256,
        )

    def _seal_generation(self, generation_id: str) -> dict[str, object]:
        alias_count, alias_set_sha256 = self._snapshot_binding(generation_id)
        manifest = self._manifest(
            alias_count=alias_count,
            alias_set_sha256=alias_set_sha256,
        )
        self.fixture._insert_manifest(generation_id, manifest)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=? WHERE id=?""",
                (schema_guards.NOW, generation_id),
            )
        return manifest

    def test_sidecar_is_managed_by_v14_physical_and_blueprint_schema_guards(self) -> None:
        declared = {name for _object_type, name in V14_SCHEMA_OBJECTS}
        self.assertTrue(
            {
                "image_projection_aliases",
                "trg_image_projection_aliases_binding_insert",
                "trg_image_projection_aliases_no_update",
                "trg_image_projection_aliases_no_delete",
                "trg_image_projection_aliases_snapshot_generation",
            }.issubset(declared)
        )
        connection = self.repository._connect()
        try:
            self.repository._verify_v14_physical_schema(
                connection,
                allow_v17_activation_trigger=True,
            )
            self.repository._verify_v17_physical_schema(connection)
            self.repository._verify_blueprint_full_physical_schema(connection)
        finally:
            connection.close()

    def test_building_generation_snapshots_current_aliases_and_then_freezes(self) -> None:
        first_alias = self._insert_alias("legacy-a")
        generation_id = self.fixture._create_generation(with_row=False)

        self.assertEqual(self._snapshot_entries(generation_id), [first_alias])
        self._seal_generation(generation_id)
        later_alias = self._insert_alias("legacy-b")

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image projection aliases require building generation",
        ):
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    """INSERT INTO image_projection_aliases(
                           generation_id,legacy_id,canonical_asset_id,
                           library_root_id,source_id,alias_scope,alias_sha256,
                           snapshotted_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        later_alias["legacy_id"],
                        self.fixture.asset_id,
                        self.fixture.root_id,
                        self.fixture.source_id,
                        "legacy-image-index/v1",
                        later_alias["alias_sha256"],
                        schema_guards.NOW,
                    ),
                )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image_projection_aliases are immutable",
        ):
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    """UPDATE image_projection_aliases SET snapshotted_at=?
                         WHERE generation_id=? AND legacy_id=?""",
                    ("2026-08-30T00:00:00+00:00", generation_id, "legacy-a"),
                )

        self.assertEqual(self._snapshot_entries(generation_id), [first_alias])

    def test_later_generation_keeps_old_snapshot_independently_verifiable(self) -> None:
        first_alias = self._insert_alias("legacy-a")
        first_generation = self.fixture._create_generation(with_row=False)
        first_manifest = self._seal_generation(first_generation)

        second_alias = self._insert_alias("legacy-b")
        second_generation = self.fixture._create_generation(with_row=False)
        second_manifest = self._seal_generation(second_generation)

        self.assertEqual(self._snapshot_entries(first_generation), [first_alias])
        self.assertEqual(
            self._snapshot_entries(second_generation),
            [first_alias, second_alias],
        )
        self.assertEqual(
            self._snapshot_binding(first_generation),
            (
                first_manifest["counts"]["aliases"],
                first_manifest["alias_set_sha256"],
            ),
        )
        self.assertEqual(
            self._snapshot_binding(second_generation),
            (
                second_manifest["counts"]["aliases"],
                second_manifest["alias_set_sha256"],
            ),
        )
        self.assertNotEqual(
            first_manifest["alias_set_sha256"],
            second_manifest["alias_set_sha256"],
        )

    def test_manifest_cannot_rebind_old_generation_to_later_global_alias_set(self) -> None:
        self._insert_alias("legacy-a")
        generation_id = self.fixture._create_generation(with_row=False)
        self._insert_alias("legacy-b")
        with self.repository.transaction() as connection:
            global_rows = connection.execute(
                """SELECT legacy_id,alias_sha256 FROM legacy_image_aliases
                    WHERE database_uuid=? ORDER BY legacy_id""",
                (self.repository.database_uuid,),
            ).fetchall()
        global_entries = [
            {
                "legacy_id": str(row["legacy_id"]),
                "alias_sha256": str(row["alias_sha256"]),
            }
            for row in global_rows
        ]
        forged = self._manifest(
            alias_count=len(global_entries),
            alias_set_sha256=content_sha256(global_entries),
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image projection manifest binding mismatch",
        ):
            self.fixture._insert_manifest(generation_id, forged)

        self.assertEqual(len(self._snapshot_entries(generation_id)), 1)


class SupersededImageProjectionTerminalGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(methodName="runTest")
        self.fixture.setUp()
        self.repository = self.fixture.repository

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _complete_and_activate_generation(
        self,
        generation_id: str,
        manifest: dict[str, object],
    ) -> None:
        counts = manifest["counts"]
        assert isinstance(counts, dict)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=? WHERE id=?""",
                (schema_guards.NOW, generation_id),
            )
            previous = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE database_uuid=?
                      AND projection_contract=?
                      AND is_active=1""",
                (
                    self.repository.database_uuid,
                    schema_guards.PROJECTION_CONTRACT,
                ),
            ).fetchone()
            connection.execute(
                """INSERT INTO image_projection_read_manifests(
                       generation_id,previous_generation_id,database_uuid,
                       canonical_high_water_position,compiler_id,
                       projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,
                       mismatched_count,blocked_count,alias_set_sha256,
                       row_set_sha256,manifest_json,manifest_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    None if previous is None else str(previous["id"]),
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
                    canonical_projection_manifest_json(manifest),
                    manifest["manifest_sha256"],
                    schema_guards.NOW,
                ),
            )
            connection.execute(
                """UPDATE image_projection_generations
                      SET is_active=1 WHERE id=?""",
                (generation_id,),
            )

    def _enqueue_successor(self) -> str:
        job = self.repository.enqueue_image_analysis(
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            database_file_identity=(
                int(self.fixture.binding["database_device"]),
                int(self.fixture.binding["database_inode"]),
            ),
            asset_id=self.fixture.asset_id,
            source_id=self.fixture.source_id,
            analysis_profile=schema_guards.PROFILE,
            expected_head=self.fixture.analysis_binding,
            enqueue_scope="schema-guard/image-analysis",
            idempotency_key="schema-guard-successor",
        )
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        successor_payload = deepcopy(self.fixture._sealed_result())
        successor_payload.pop("content_sha256")
        successor_payload["analysis_run_id"] = binding["analysis_run_id"]
        successor_payload["revision"] = binding["intended_revision"]
        successor = seal_image_analysis_result(successor_payload)
        self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
            result=successor,
            artifacts=self.fixture._artifacts(),
        )
        return job_id

    def test_superseded_job_can_finish_only_against_strictly_newer_active_head(self) -> None:
        successor_job_id = self._enqueue_successor()
        projected = self.repository.project_image_analysis_change(
            job_id=successor_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        with self.repository.transaction() as connection:
            row = connection.execute(
                """SELECT receipt_json FROM image_projection_receipts
                    WHERE change_position=?""",
                (self.fixture.change_position,),
            ).fetchone()
            newer = connection.execute(
                """SELECT head.revision AS head_revision,
                          projected.revision AS projected_revision
                     FROM image_analysis_heads head
                     JOIN image_projection_rows projected
                       ON projected.asset_id=head.asset_id
                      AND projected.analysis_run_id=head.analysis_run_id
                      AND projected.revision=head.revision
                    WHERE projected.generation_id=? AND head.asset_id=?""",
                (projected["generation_id"], self.fixture.asset_id),
            ).fetchone()
        assert row is not None and newer is not None
        self.assertGreater(
            int(newer["head_revision"]),
            self.fixture.analysis_binding["revision"],
        )
        self.assertEqual(newer["head_revision"], newer["projected_revision"])
        superseded_receipt = json.loads(str(row["receipt_json"]))
        self.assertEqual(superseded_receipt["outcome"], "superseded")

        completed = self.repository.get_media_job(self.fixture.job_id)
        assert completed is not None
        self.assertEqual((completed["status"], completed["stage"]), ("succeeded", "completed"))
        self.assertEqual(
            completed["checkpoint"]["stage_outcomes"][-1],
            {
                "stage": "shadow_projection",
                "outcome": "partial",
                "outcome_sha256": content_sha256(superseded_receipt),
                "reason_code": "newer_head_published",
            },
        )

    def test_equal_revision_cannot_satisfy_superseded_terminal_guard(self) -> None:
        generation_id = self.fixture._create_generation(with_row=True)
        applied = self.fixture._projection_receipt(generation_id)
        superseded_payload = {key: value for key, value in applied.items() if key != "receipt_sha256"}
        superseded_payload["outcome"] = "superseded"
        superseded_payload["reason_code"] = "newer_head_published"
        superseded = seal_projection_receipt(superseded_payload)
        self.fixture._insert_projection_receipt(generation_id, superseded)
        manifest = self.fixture._manifest()
        self.fixture._insert_manifest(generation_id, manifest)
        self._complete_and_activate_generation(generation_id, manifest)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image_analysis success requires publication and projection",
        ):
            with self.repository.transaction(immediate=True) as connection:
                self.repository._finalize_image_projection_job_in_transaction(
                    connection,
                    job_id=self.fixture.job_id,
                    receipt=superseded,
                    now=schema_guards.NOW,
                )

        job = self.repository.get_media_job(self.fixture.job_id)
        assert job is not None
        self.assertEqual((job["status"], job["stage"]), ("running", "shadow_projection"))


if __name__ == "__main__":
    unittest.main()
