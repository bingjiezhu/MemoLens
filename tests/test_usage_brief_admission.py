from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from flask import Flask

from backend.src import DESKTOP_TOKEN_HEADER
from backend.src.api import api_blueprint
from backend.src.media.director import CreativeBriefError, CreativeDirector
from backend.src.media.mixed_presenter import present_explicit_matches
from backend.src.media.usage_projection import UsageQueryPolicy, annotate_usage_candidates
from core.db import ImageIndexRepository
from core.media_db import MediaRepository, canonical_json


def _image_candidate(
    identifier: str = "asset-image",
    *,
    analysis_status: str = "unknown",
    analysis_run_id: str | None = None,
    analysis_revision: int | None = None,
) -> dict[str, object]:
    candidate: dict[str, object] = {
        "id": identifier,
        "asset_id": identifier,
        "asset_source_id": f"source-{identifier}",
        "result_type": "image_asset",
        "filename": "memory.jpg",
        "start_ms": None,
        "end_ms": None,
        "summary": "memory",
        "combined_text": "memory",
        "tags": [],
        "analysis_run_id": analysis_run_id,
        "analysis_revision": analysis_revision,
        "analysis_status": analysis_status,
        "source_availability": "available",
        "review": {
            "revision": 0,
            "inbox_state": "inbox",
            "favorite": False,
            "project_ready": False,
        },
        "duration_ms": None,
    }
    if (
        analysis_status == "current"
        and analysis_run_id is not None
        and analysis_revision is not None
    ):
        candidate["canonical_image_observation"] = _image_observation(
            identifier,
            analysis_run_id=analysis_run_id,
            analysis_revision=analysis_revision,
        )
    return candidate


def _image_observation(
    asset_id: str,
    *,
    analysis_run_id: str,
    analysis_revision: int,
) -> dict[str, object]:
    stage = {
        "status": "disabled",
        "provenance": {
            "producer_id": "fixture",
            "producer_version": "1",
            "model_id": None,
            "model_version": None,
            "rule_id": "fixture",
            "rule_version": "1",
        },
        "output": None,
        "reason_code": "fixture_disabled",
    }
    return {
        "object": "memolens.canonical_image_observation",
        "schema_version": "1",
        "status": "current",
        "authority": "canonical_image_analysis",
        "provenance_status": "verified_current",
        "asset_id": asset_id,
        "analysis_binding": {
            "analysis_run_id": analysis_run_id,
            "revision": analysis_revision,
            "content_sha256": "a" * 64,
        },
        "source_binding_sha256": "b" * 64,
        "projection": {
            "status": "current",
            "generation_id": "generation-fixture",
            "processing_generation_id": "generation-fixture",
            "receipt_sha256": "c" * 64,
            "row_sha256": "d" * 64,
            "reason_code": None,
        },
        "stages": {
            name: deepcopy(stage)
            for name in ("metadata", "geocode", "vision", "embedding", "quality")
        },
        "reason_code": None,
    }


def _video_candidate(identifier: str = "segment-video") -> dict[str, object]:
    asset_sha256 = "1" * 64
    if identifier == "segment-video":
        identifier = f"seg_{asset_sha256[:24]}_1_0"
    return {
        "id": identifier,
        "asset_id": f"asset_{asset_sha256[:24]}",
        "asset_sha256": asset_sha256,
        "asset_source_id": f"src_{'2' * 24}",
        "source_binding_sha256": "4" * 64,
        "result_type": "video_segment",
        "filename": "memory.mp4",
        "start_ms": 1_000,
        "end_ms": 7_000,
        "summary": "memory",
        "combined_text": "memory",
        "tags": [],
        "analysis_run_id": f"arun_{'3' * 32}",
        "analysis_revision": 1,
        "input_asset_sha256": asset_sha256,
        "source_availability": "available",
        "review": {
            "revision": 0,
            "inbox_state": "inbox",
            "favorite": False,
            "project_ready": False,
        },
        "duration_ms": 10_000,
    }


def _video_occurrence(*, start_ms: int = 2_000, end_ms: int = 4_000) -> dict[str, object]:
    return {
        "project_id": "prior-project",
        "export_revision": 1,
        "asset_id": f"asset_{'1' * 24}",
        "media_kind": "video",
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
    }


def _image_occurrence(asset_id: str = "asset-image") -> dict[str, object]:
    return {
        "project_id": "prior-project",
        "export_revision": 1,
        "asset_id": asset_id,
        "media_kind": "image",
        "source_start_ms": None,
        "source_end_ms": None,
    }


class _AdmissionRepository(MediaRepository):
    def __init__(self, db_path: Path):
        super().__init__(db_path)
        self.current_candidates: list[dict[str, object]] = []
        self.current_occurrences: list[dict[str, object]] = []
        self.admission_connections: list[sqlite3.Connection] = []

    def mixed_candidates_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        include_archived: bool = False,
    ) -> tuple[list[dict[str, object]], dict[str, str]]:
        self.admission_connections.append(connection)
        if not connection.in_transaction:
            raise AssertionError("candidate admission escaped the create transaction")
        heads = {
            str(value["asset_id"]): str(value["analysis_run_id"])
            for value in self.current_candidates
            if value.get("analysis_run_id") is not None
        }
        return deepcopy(self.current_candidates), heads

    def canonical_material_search_facts_in_transaction(
        self,
        connection: sqlite3.Connection,
        asset_ids: list[str],
    ) -> dict[str, object]:
        self.admission_connections.append(connection)
        if not connection.in_transaction:
            raise AssertionError("Usage admission escaped the create transaction")
        selected = set(asset_ids)
        occurrences = deepcopy(
            [value for value in self.current_occurrences if str(value["asset_id"]) in selected]
        )
        derivative_facts: list[dict[str, object]] = []
        return {
            "usage_occurrences": occurrences,
            "derivative_facts": derivative_facts,
            "derivative_revision": hashlib.sha256(
                canonical_json(derivative_facts).encode("utf-8")
            ).hexdigest(),
        }


class _Retrieval:
    def __init__(self, repository: _AdmissionRepository):
        self.repository = repository

    def search(
        self,
        _payload: dict[str, object],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        if connection is None or not connection.in_transaction:
            raise AssertionError("search escaped the create transaction")
        results = present_explicit_matches(
            self.repository.current_candidates,
            [str(value["id"]) for value in self.repository.current_candidates],
        )
        return {
            "object": "mixed.search",
            "schema_version": "1",
            "results": results,
            "search_revision": "fixture-search",
            "analysis_heads": {},
        }

    def resolve_matches(
        self,
        match_ids: list[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        if connection is None or not connection.in_transaction:
            raise AssertionError("resolution escaped the create transaction")
        return present_explicit_matches(self.repository.current_candidates, match_ids)

    def constraint_conflicts(
        self,
        _match_ids: list[str],
        *,
        required_terms: list[str],
        excluded_terms: list[str],
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        if connection is None or not connection.in_transaction:
            raise AssertionError("constraint admission escaped the create transaction")
        return []


class _RejectingDirector:
    def __init__(self, error: CreativeBriefError):
        self.error = error

    def create_brief_idempotent(self, *_args: object, **_kwargs: object):
        raise self.error


def _usage_selection(
    candidates: list[dict[str, object]],
    occurrences: list[dict[str, object]],
    *,
    policy: str,
) -> dict[str, object]:
    projected = annotate_usage_candidates(
        candidates,
        occurrences,
        UsageQueryPolicy(requested=True, allow_reuse=True),
    )
    return {
        "policy": policy,
        "usage_revision": hashlib.sha256(canonical_json(occurrences).encode("utf-8")).hexdigest(),
        "derivative_revision": hashlib.sha256(canonical_json([]).encode("utf-8")).hexdigest(),
        "candidates": [
            {
                "id": value["id"],
                "asset_id": value["asset_id"],
                "asset_source_id": value["asset_source_id"],
                "result_type": value["result_type"],
                "analysis_run_id": value.get("analysis_run_id"),
                "analysis_revision": value.get("analysis_revision"),
                "start_ms": value.get("start_ms"),
                "end_ms": value.get("end_ms"),
                "usage": value["canonical_usage"],
            }
            for value in projected
        ],
    }


class UsageBriefAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-usage-brief-")
        root = Path(self.temporary.name)
        self.db_path = root / "state" / "media.db"
        self.db_path.parent.mkdir()
        library = root / "library"
        library.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = _AdmissionRepository(self.db_path)
        self.repository.ensure_schema(library)
        self.director = CreativeDirector(
            self.repository,
            _Retrieval(self.repository),  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _payload(
        self,
        candidates: list[dict[str, object]],
        occurrences: list[dict[str, object]],
        *,
        policy: str,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "goal": "memory",
            "duration_ms": 3_000,
            "candidate_refs": [str(value["id"]) for value in candidates],
            "usage_selection": _usage_selection(candidates, occurrences, policy=policy),
        }
        observations = [
            deepcopy(value["canonical_image_observation"])
            for value in candidates
            if value.get("result_type") == "image_asset"
            and isinstance(value.get("canonical_image_observation"), dict)
        ]
        if observations:
            payload["candidate_observations"] = observations
        return payload

    def _create_idempotent(self, payload: dict[str, object], key: str = "usage-brief"):
        return self.director.create_brief_idempotent(
            payload,
            idempotency_scope="desktop:POST:/v1/creative/briefs",
            idempotency_key=key,
            request_sha256=hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        )

    def _assert_no_partial_write(self) -> None:
        with closing(self.repository._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM creative_briefs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0], 0)

    def test_fresh_unused_only_admission_is_atomic_and_replays_exactly(self) -> None:
        candidate = _video_candidate()
        self.repository.current_candidates = [candidate]
        payload = self._payload([candidate], [], policy="unused_only")

        first = self._create_idempotent(payload)
        replay = self._create_idempotent(payload)

        self.assertEqual(first.response_status, 201)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(len({id(value) for value in self.repository.admission_connections}), 1)
        brief = first.response["project"]["brief"]
        self.assertEqual(brief["usage_selection"], payload["usage_selection"])
        project_id = str(first.response["project"]["id"])
        stored = self.repository.get_brief(project_id, 1)
        assert stored is not None
        self.assertEqual(stored["provenance"]["usage_selection"]["policy"], "unused_only")
        self.assertRegex(
            str(stored["provenance"]["usage_selection"]["candidate_projection_sha256"]),
            r"^[0-9a-f]{64}$",
        )
        with closing(self.repository._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0], 1)

    def test_usage_selection_contract_is_closed_and_requires_exact_candidates(self) -> None:
        candidate = _image_candidate(
            analysis_status="current",
            analysis_run_id="analysis-image-1",
            analysis_revision=1,
        )
        self.repository.current_candidates = [candidate]
        base = self._payload([candidate], [], policy="unused_only")
        cases: list[tuple[dict[str, object], str]] = []

        missing_candidates = deepcopy(base)
        missing_candidates["candidate_refs"] = []
        cases.append((missing_candidates, "usage_selection_requires_candidates"))
        unknown_policy = deepcopy(base)
        unknown_policy["usage_selection"]["policy"] = "sometimes"
        cases.append((unknown_policy, "usage_selection_policy_invalid"))
        extra_field = deepcopy(base)
        extra_field["usage_selection"]["unexpected"] = True
        cases.append((extra_field, "usage_selection_invalid"))
        presentation_status = deepcopy(base)
        presentation_status["usage_selection"]["candidates"][0]["analysis_status"] = "unknown"
        cases.append((presentation_status, "usage_selection_invalid"))
        incomplete_image_binding = deepcopy(base)
        incomplete_image_binding["usage_selection"]["candidates"][0]["analysis_run_id"] = None
        cases.append((incomplete_image_binding, "usage_selection_invalid"))
        wrong_order = deepcopy(base)
        wrong_order["candidate_refs"] = ["different"]
        cases.append((wrong_order, "usage_selection_requires_candidates"))

        for index, (payload, expected_code) in enumerate(cases):
            with self.subTest(expected_code=expected_code), self.assertRaises(CreativeBriefError) as raised:
                self._create_idempotent(payload, key=f"contract-{index}")
            self.assertEqual(raised.exception.code, expected_code)
            self.assertEqual(raised.exception.status, 400)
        self._assert_no_partial_write()

    def test_current_image_binding_is_preserved_in_closed_selection_and_receipt_hash(self) -> None:
        candidate = _image_candidate(
            analysis_status="current",
            analysis_run_id="analysis-image-1",
            analysis_revision=3,
        )
        self.repository.current_candidates = [candidate]
        payload = self._payload([candidate], [], policy="unused_only")

        result = self._create_idempotent(payload, key="current-image-binding")

        selected = result.response["project"]["brief"]["usage_selection"]["candidates"][0]
        self.assertEqual(selected["analysis_run_id"], "analysis-image-1")
        self.assertEqual(selected["analysis_revision"], 3)
        self.assertNotIn("analysis_status", selected)
        project_id = str(result.response["project"]["id"])
        stored = self.repository.get_brief(project_id, 1)
        assert stored is not None
        expected = hashlib.sha256(
            canonical_json(payload["usage_selection"]["candidates"]).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            stored["provenance"]["usage_selection"]["candidate_projection_sha256"],
            expected,
        )

    def test_pending_and_unknown_images_are_not_creative_evidence(self) -> None:
        for index, status in enumerate(("pending", "unknown")):
            with self.subTest(status=status):
                candidate = _image_candidate(analysis_status=status)
                self.repository.current_candidates = [candidate]
                payload = {
                    "goal": "memory",
                    "duration_ms": 3_000,
                    "candidate_refs": [str(candidate["id"])],
                }

                with self.assertRaises(CreativeBriefError) as raised:
                    self._create_idempotent(payload, key=f"unbound-image-{index}")

                self.assertEqual(raised.exception.code, "candidate_observation_conflict")
                self.assertEqual(raised.exception.status, 409)
        self._assert_no_partial_write()

    def test_image_analysis_downgrade_or_inconsistent_status_rejects_atomically(self) -> None:
        searched = _image_candidate(
            analysis_status="current",
            analysis_run_id="analysis-image-1",
            analysis_revision=1,
        )
        payload = self._payload([searched], [], policy="unused_only")
        cases = [
            _image_candidate(analysis_status="unknown"),
            _image_candidate(
                analysis_status="pending",
                analysis_run_id="analysis-image-1",
                analysis_revision=1,
            ),
            _image_candidate(analysis_status="current"),
        ]
        for index, current in enumerate(cases):
            with self.subTest(current=current), self.assertRaises(CreativeBriefError) as raised:
                self.repository.current_candidates = [current]
                self._create_idempotent(payload, key=f"image-binding-conflict-{index}")
            self.assertEqual(raised.exception.code, "candidate_observation_conflict")
            self.assertEqual(raised.exception.status, 409)
        self._assert_no_partial_write()

    def test_export_usage_added_after_search_rejects_revision_and_rolls_back(self) -> None:
        candidate = _video_candidate()
        self.repository.current_candidates = [candidate]
        payload = self._payload([candidate], [], policy="unused_only")
        self.repository.current_occurrences = [_video_occurrence()]

        with self.assertRaises(CreativeBriefError) as raised:
            self._create_idempotent(payload, key="stale-usage")

        self.assertEqual(raised.exception.code, "usage_revision_conflict")
        self.assertEqual(raised.exception.status, 409)
        self._assert_no_partial_write()

    def test_candidate_head_or_domain_change_rejects_exact_projection(self) -> None:
        candidate = _video_candidate()
        self.repository.current_candidates = [candidate]
        payload = self._payload([candidate], [], policy="allow_reuse")
        self.repository.current_candidates[0]["analysis_run_id"] = "analysis-video-2"
        self.repository.current_candidates[0]["analysis_revision"] = 2

        with self.assertRaises(CreativeBriefError) as raised:
            self._create_idempotent(payload, key="changed-candidate")

        self.assertEqual(raised.exception.code, "usage_candidate_conflict")
        self.assertEqual(raised.exception.status, 409)
        self._assert_no_partial_write()

    def test_partial_used_video_and_used_image_are_rejected_by_unused_only(self) -> None:
        cases = [
            ([_video_candidate()], [_video_occurrence()]),
            (
                [
                    _image_candidate(
                        analysis_status="current",
                        analysis_run_id="analysis-image-1",
                        analysis_revision=1,
                    )
                ],
                [_image_occurrence()],
            ),
        ]
        for index, (candidates, occurrences) in enumerate(cases):
            with self.subTest(kind=candidates[0]["result_type"]):
                self.repository.current_candidates = candidates
                self.repository.current_occurrences = occurrences
                payload = self._payload(candidates, occurrences, policy="unused_only")
                with self.assertRaises(CreativeBriefError) as raised:
                    self._create_idempotent(payload, key=f"already-used-{index}")
                self.assertEqual(raised.exception.code, "usage_candidate_conflict")
                self.assertEqual(raised.exception.status, 409)
        self._assert_no_partial_write()

    def test_prefer_unused_and_allow_reuse_preserve_auditable_provenance(self) -> None:
        candidate = _video_candidate()
        occurrences = [_video_occurrence()]
        self.repository.current_candidates = [candidate]
        self.repository.current_occurrences = occurrences

        for policy in ("prefer_unused", "allow_reuse"):
            with self.subTest(policy=policy):
                payload = self._payload([candidate], occurrences, policy=policy)
                result = self._create_idempotent(payload, key=f"provenance-{policy}")
                brief = result.response["project"]["brief"]
                self.assertEqual(brief["usage_selection"], payload["usage_selection"])
                project_id = str(result.response["project"]["id"])
                stored = self.repository.get_brief(project_id, 1)
                assert stored is not None
                binding = stored["provenance"]["usage_selection"]
                self.assertEqual(binding["policy"], policy)
                self.assertEqual(binding["usage_revision"], payload["usage_selection"]["usage_revision"])
                self.assertEqual(
                    binding["derivative_revision"],
                    payload["usage_selection"]["derivative_revision"],
                )

    def test_api_preserves_usage_contract_and_conflict_status_codes(self) -> None:
        app = Flask("usage-brief-admission-route")
        app.config["DESKTOP_SESSION_TOKEN"] = "usage-route-token"
        app.extensions["media_repository"] = self.repository
        app.extensions["creative_director"] = _RejectingDirector(
            CreativeBriefError(
                "usage_selection_invalid",
                "Usage selection is malformed.",
            )
        )
        app.register_blueprint(api_blueprint)
        client = app.test_client()
        headers = {
            DESKTOP_TOKEN_HEADER: "usage-route-token",
            "Idempotency-Key": "usage-route-oracle",
        }
        payload = {"db_path": str(self.db_path), "goal": "memory"}

        malformed = client.post("/v1/creative/briefs", json=payload, headers=headers)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json["code"], "usage_selection_invalid")
        self.assertNotEqual(malformed.json["code"], "invalid_brief")

        app.extensions["creative_director"] = _RejectingDirector(
            CreativeBriefError(
                "usage_revision_conflict",
                "Canonical Usage changed.",
                status=409,
            )
        )
        conflict = client.post("/v1/creative/briefs", json=payload, headers=headers)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["code"], "usage_revision_conflict")
        self.assertNotEqual(conflict.json["code"], "invalid_brief")


if __name__ == "__main__":
    unittest.main()
