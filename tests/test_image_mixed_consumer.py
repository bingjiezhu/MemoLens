from __future__ import annotations

import hashlib
import unittest

from backend.src.media.director import CreativeBriefError, CreativeDirector
from backend.src.media.inbox import MediaInboxService
from backend.src.media.retrieval import MixedRetrievalService
from core.image_analysis_contract import canonical_sha256
from core.image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    materialize_projected_image_index_values,
)
from core.media_db import canonical_json
from tests import test_image_projection_materialization as materialization_fixture
from tests import test_image_projection_schema_guards as schema_fixture


class ImageMixedConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.materialization = materialization_fixture.ImageProjectionMaterializationTests(
            methodName="runTest"
        )
        self.materialization.setUp()
        self.fixture = self.materialization.fixture

    def tearDown(self) -> None:
        self.materialization.tearDown()

    def _image_candidate(self) -> tuple[dict[str, object], dict[str, str]]:
        candidates, heads = self.fixture.repository.mixed_candidates()
        images = [
            candidate
            for candidate in candidates
            if candidate["result_type"] == "image_asset"
        ]
        self.assertEqual(len(images), 1)
        return images[0], heads

    def _search(self, query: str) -> list[dict[str, object]]:
        return self._search_response(query)["results"]  # type: ignore[return-value]

    def _search_response(self, query: str) -> dict[str, object]:
        return MixedRetrievalService(self.fixture.repository).search(
            {"query": query, "types": ["image"]}
        )

    def _insert_physical_projection(self) -> None:
        values = materialize_projected_image_index_values(
            self.fixture.projected_row_document,
            created_at=schema_fixture.NOW,
            updated_at=schema_fixture.NOW,
        )
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                values,
            )

    def _install_legacy_alias(self, legacy_id: str) -> None:
        identity = {
            "database_uuid": self.fixture.repository.database_uuid,
            "legacy_id": legacy_id,
            "canonical_asset_id": self.fixture.asset_id,
            "library_root_id": self.fixture.root_id,
            "source_id": self.fixture.source_id,
            "alias_scope": "legacy-image-index/v1",
        }
        with self.fixture.repository.transaction(immediate=True) as connection:
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
                    canonical_sha256(identity),
                    schema_fixture.NOW,
                ),
            )

    def test_verified_active_projection_returns_exact_canonical_analysis_binding(
        self,
    ) -> None:
        projected = self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )

        candidate, heads = self._image_candidate()

        self.assertEqual(candidate["id"], self.fixture.asset_id)
        self.assertEqual(candidate["asset_id"], self.fixture.asset_id)
        self.assertEqual(candidate["analysis_status"], "current")
        self.assertEqual(
            candidate["analysis_run_id"],
            self.fixture.analysis_binding["analysis_run_id"],
        )
        self.assertEqual(
            candidate["analysis_revision"],
            self.fixture.analysis_binding["revision"],
        )
        self.assertEqual(
            heads,
            {
                self.fixture.asset_id: self.fixture.analysis_binding[
                    "analysis_run_id"
                ]
            },
        )
        results = self._search("guard")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["analysis_status"], "current")
        self.assertTrue(results[0]["grounded"])
        self.assertEqual(
            results[0]["canonical_image_observation"],
            candidate["canonical_image_observation"],
        )
        self.assertEqual(
            results[0]["canonical_image_observation"]["provenance_status"],
            "verified_current",
        )
        self.assertEqual(
            results[0]["provenance"],
            ["canonical_image_analysis", "verified_image_projection"],
        )
        self.assertEqual(
            projected["generation_id"],
            self.fixture.repository.get_current_image_analysis(
                self.fixture.asset_id
            )["projection"]["generation_id"],  # type: ignore[index]
        )

    def test_published_result_without_projection_is_explicitly_pending(self) -> None:
        candidate, heads = self._image_candidate()

        self.assertEqual(candidate["analysis_status"], "pending")
        self.assertEqual(
            candidate["analysis_run_id"],
            self.fixture.analysis_binding["analysis_run_id"],
        )
        self.assertEqual(
            candidate["analysis_revision"],
            self.fixture.analysis_binding["revision"],
        )
        self.assertEqual(
            heads,
            {self.fixture.asset_id: self.fixture.analysis_binding["analysis_run_id"]},
        )
        response = self._search_response("guard")
        self.assertEqual(response["results"], [])
        audit = response["audit_results"][0]
        self.assertFalse(audit["grounded"])
        self.assertEqual(audit["analysis_status"], "pending")
        self.assertEqual(
            audit["canonical_image_observation"]["provenance_status"],
            "verified_pending",
        )

    def test_inbox_explicitly_reports_pending_current_and_unavailable(self) -> None:
        inbox = MediaInboxService(self.fixture.repository)

        pending = inbox.list_assets(kinds="image").items[0]
        self.assertEqual(pending["analysis_status"], "pending")
        self.assertEqual(
            pending["canonical_image_observation"]["status"],
            "pending",
        )
        self.assertEqual(
            pending["canonical_image_observation"]["analysis_binding"],
            self.fixture.analysis_binding,
        )

        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        current = inbox.list_assets(kinds="image").items[0]
        self.assertEqual(current["analysis_status"], "current")
        self.assertEqual(
            current["canonical_image_observation"]["provenance_status"],
            "verified_current",
        )

        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description='tampered inbox projection' WHERE id=?",
                (self.fixture.asset_id,),
            )
        unavailable = inbox.list_assets(kinds="image").items[0]
        self.assertEqual(unavailable["analysis_status"], "unavailable")
        self.assertEqual(
            unavailable["canonical_image_observation"]["status"],
            "unavailable",
        )
        self.assertEqual(
            unavailable["canonical_image_observation"]["reason_code"],
            "image_analysis_projection_corrupt",
        )

    def test_unknown_image_is_auditable_but_never_grounded(self) -> None:
        relative_path = "audit-unknown.png"
        image_path = self.fixture.library / relative_path
        payload = b"unknown-image-without-analysis"
        image_path.write_bytes(payload)
        observed = image_path.stat()
        asset = self.fixture.repository.upsert_asset_source(
            root_id=self.fixture.root_id,
            relative_path=relative_path,
            filename=relative_path,
            kind="image",
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type="image/png",
            file_size=len(payload),
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        self.fixture.repository.update_image_probe(
            str(asset["id"]),
            width=8,
            height=8,
        )

        response = self._search_response("audit unknown")

        self.assertEqual(response["results"], [])
        unknown = next(
            item
            for item in response["audit_results"]
            if item["asset_id"] == asset["id"]
        )
        self.assertFalse(unknown["grounded"])
        self.assertEqual(unknown["analysis_status"], "unknown")
        self.assertEqual(
            unknown["canonical_image_observation"]["reason_code"],
            "current_image_analysis_unavailable",
        )

    def test_incomplete_generation_never_admits_bare_physical_evidence(self) -> None:
        generation_id = self.fixture._create_generation(with_row=True)
        receipt = self.fixture._projection_receipt(generation_id)
        self.fixture._insert_projection_receipt(generation_id, receipt)
        self._insert_physical_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description='bare unverified evidence' WHERE id=?",
                (self.fixture.asset_id,),
            )

        candidate, heads = self._image_candidate()

        self.assertEqual(candidate["analysis_status"], "pending")
        self.assertEqual(
            candidate["analysis_run_id"],
            self.fixture.analysis_binding["analysis_run_id"],
        )
        self.assertEqual(
            candidate["analysis_revision"],
            self.fixture.analysis_binding["revision"],
        )
        self.assertIsNone(candidate["summary"])
        self.assertEqual(candidate["tags"], [])
        self.assertEqual(
            heads,
            {self.fixture.asset_id: self.fixture.analysis_binding["analysis_run_id"]},
        )
        self.assertEqual(self._search("bare unverified evidence"), [])

    def test_tampered_physical_projection_is_audit_only_unavailable(self) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description='forged consumer evidence' WHERE id=?",
                (self.fixture.asset_id,),
            )

        candidate, heads = self._image_candidate()

        self.assertEqual(candidate["analysis_status"], "unavailable")
        self.assertEqual(
            candidate["analysis_run_id"],
            self.fixture.analysis_binding["analysis_run_id"],
        )
        self.assertEqual(
            candidate["analysis_revision"],
            self.fixture.analysis_binding["revision"],
        )
        self.assertIsNone(candidate["summary"])
        self.assertEqual(candidate["tags"], [])
        self.assertEqual(
            heads,
            {self.fixture.asset_id: self.fixture.analysis_binding["analysis_run_id"]},
        )
        self.assertEqual(self._search("forged consumer evidence"), [])
        response = self._search_response("guard")
        self.assertEqual(response["results"], [])
        fallback = response["audit_results"][0]
        self.assertFalse(fallback["grounded"])
        self.assertEqual(fallback["analysis_status"], "unavailable")
        self.assertEqual(
            fallback["canonical_image_observation"]["reason_code"],
            "image_analysis_projection_corrupt",
        )

    def test_tamper_after_usage_search_rejects_current_image_binding(self) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        retrieval = MixedRetrievalService(self.fixture.repository)
        searched = retrieval.search(
            {
                "query": "guard",
                "types": ["image"],
                "filters": {"allow_reuse": True},
            }
        )
        candidate = searched["results"][0]
        payload = {
            "goal": "guard",
            "duration_ms": 3_000,
            "candidate_refs": [candidate["id"]],
            "candidate_observations": [candidate["canonical_image_observation"]],
            "usage_selection": {
                "policy": "allow_reuse",
                "usage_revision": searched["usage_revision"],
                "derivative_revision": searched["derivative_revision"],
                "candidates": [
                    {
                        "id": candidate["id"],
                        "asset_id": candidate["asset_id"],
                        "asset_source_id": candidate["asset_source_id"],
                        "result_type": candidate["result_type"],
                        "analysis_run_id": candidate["analysis_run_id"],
                        "analysis_revision": candidate["analysis_revision"],
                        "start_ms": candidate["start_ms"],
                        "end_ms": candidate["end_ms"],
                        "usage": candidate["usage"],
                    }
                ],
            },
        }
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description='forged after search' WHERE id=?",
                (self.fixture.asset_id,),
            )

        with self.assertRaises(CreativeBriefError) as raised:
            CreativeDirector(
                self.fixture.repository,
                retrieval,
            ).create_brief_idempotent(
                payload,
                idempotency_scope="test:image-usage",
                idempotency_key="tamper-after-search",
                request_sha256=hashlib.sha256(
                    canonical_json(payload).encode("utf-8")
                ).hexdigest(),
            )

        self.assertEqual(raised.exception.code, "candidate_observation_conflict")
        self.assertEqual(raised.exception.status, 409)
        with self.fixture.repository.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_briefs").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0],
                0,
            )

    def test_revision_n_to_n_plus_one_rejects_create_but_not_review_cas(self) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        retrieval = MixedRetrievalService(self.fixture.repository)
        searched = retrieval.search({"query": "guard", "types": ["image"]})
        candidate = searched["results"][0]
        inbox = MediaInboxService(self.fixture.repository)
        inbox_item = inbox.list_assets(kinds="image").items[0]
        self.assertEqual(
            inbox_item["canonical_image_observation"],
            candidate["canonical_image_observation"],
        )

        self.materialization._publish_revision_two()
        payload = {
            "goal": "guard",
            "duration_ms": 3_000,
            "candidate_refs": [candidate["id"]],
            "candidate_observations": [candidate["canonical_image_observation"]],
        }
        with self.assertRaises(CreativeBriefError) as raised:
            CreativeDirector(self.fixture.repository, retrieval).create_brief_idempotent(
                payload,
                idempotency_scope="test:image-observation",
                idempotency_key="stale-n-to-n-plus-one",
                request_sha256=hashlib.sha256(
                    canonical_json(payload).encode("utf-8")
                ).hexdigest(),
            )
        self.assertEqual(raised.exception.code, "candidate_observation_conflict")
        self.assertEqual(raised.exception.status, 409)
        with self.fixture.repository.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0],
                0,
            )

        review_payload = {"base_revision": 0, "favorite": True}
        review, replayed = inbox.update_review(
            self.fixture.asset_id,
            review_payload,
            idempotency_scope=f"desktop:PUT:/v1/inbox/assets/{self.fixture.asset_id}",
            idempotency_key="review-after-analysis-head-change",
            request_sha256=hashlib.sha256(
                canonical_json(review_payload).encode("utf-8")
            ).hexdigest(),
        )
        self.assertFalse(replayed)
        self.assertEqual(review["revision"], 1)
        self.assertTrue(review["favorite"])

    def test_create_scoped_legacy_alias_normalizes_and_wrong_scope_is_zero_write(
        self,
    ) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        legacy_id = "legacy-scoped-create-reference"
        self._install_legacy_alias(legacy_id)
        retrieval = MixedRetrievalService(self.fixture.repository)
        candidate = retrieval.search({"query": "guard", "types": ["image"]})[
            "results"
        ][0]
        director = CreativeDirector(self.fixture.repository, retrieval)
        payload = {
            "goal": "guard",
            "candidate_refs": [
                {
                    "legacy_id": legacy_id,
                    "library_root_id": self.fixture.root_id,
                }
            ],
            "candidate_observations": [candidate["canonical_image_observation"]],
        }
        created = director.create_brief_idempotent(
            payload,
            idempotency_scope="test:scoped-legacy-create",
            idempotency_key="scoped-legacy-success",
            request_sha256=hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest(),
        )
        self.assertFalse(created.replayed)
        brief = created.response["project"]["brief"]
        self.assertEqual(brief["candidate_ref_ids"], [self.fixture.asset_id])
        self.assertEqual(brief["candidate_refs"][0]["id"], self.fixture.asset_id)
        self.assertNotIn(legacy_id, canonical_json(created.response))

        with self.fixture.repository.transaction() as connection:
            before = (
                connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0],
            )
        missing_scope = {
            "goal": "guard",
            "candidate_refs": [{"legacy_id": legacy_id}],
            "candidate_observations": [candidate["canonical_image_observation"]],
        }
        with self.assertRaisesRegex(ValueError, "closed scoped legacy alias"):
            director.create_brief_idempotent(
                missing_scope,
                idempotency_scope="test:scoped-legacy-create",
                idempotency_key="scoped-legacy-missing-scope",
                request_sha256=hashlib.sha256(
                    canonical_json(missing_scope).encode("utf-8")
                ).hexdigest(),
            )
        wrong_scope = {
            "goal": "guard",
            "candidate_refs": [
                {
                    "legacy_id": legacy_id,
                    "library_root_id": "root_wrong_scope",
                }
            ],
            "candidate_observations": [candidate["canonical_image_observation"]],
        }
        with self.assertRaises(CreativeBriefError) as blocked:
            director.create_brief_idempotent(
                wrong_scope,
                idempotency_scope="test:scoped-legacy-create",
                idempotency_key="scoped-legacy-wrong-scope",
                request_sha256=hashlib.sha256(
                    canonical_json(wrong_scope).encode("utf-8")
                ).hexdigest(),
            )
        self.assertEqual(blocked.exception.code, "candidate_alias_conflict")
        self.assertEqual(blocked.exception.status, 409)
        with self.fixture.repository.transaction() as connection:
            self.assertEqual(
                (
                    connection.execute(
                        "SELECT COUNT(*) FROM creative_projects"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM idempotency_records"
                    ).fetchone()[0],
                ),
                before,
            )


if __name__ == "__main__":
    unittest.main()
