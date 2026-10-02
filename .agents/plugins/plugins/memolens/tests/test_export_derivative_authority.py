from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
TESTS = REPOSITORY_ROOT / "tests"
for search_path in (REPOSITORY_ROOT, TESTS, SCRIPTS):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

from core.media_db import (  # noqa: E402
    CanonicalExportIntegrityError,
    MediaRepository,
    utc_now_iso,
)
from core.residual_identity_contract import (  # noqa: E402
    require_valid_residual_binding as require_core_residual_binding,
)
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_core import MemoLensGateway  # noqa: E402
from memolens_creative_blueprint import canonical_json, canonical_sha256  # noqa: E402
from memolens_export_authority import (  # noqa: E402
    project_validated_derivative_facts,
)
import memolens_export_authority as export_authority  # noqa: E402
from memolens_sqlite import ReadOnlyDatabase  # noqa: E402
from memolens_mcp import TOOLS, call_tool  # noqa: E402
from memolens_residual_authority import (  # noqa: E402
    UsagePolicy,
    UsageSelection,
    annotate_and_materialize,
    build_residual_binding,
)
import test_canonical_export_persistence as export_fixture  # noqa: E402


SUCCESS_SHA = "a" * 64
FAILED_SHA = "b" * 64
RAW_SHA = "c" * 64


class StandaloneDerivativeAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = export_fixture.CanonicalExportPersistenceTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _complete_export(
        self,
        *,
        key: str = "export-one",
        nonce: str = "nonce-one",
    ) -> tuple[str, dict[str, object]]:
        project_id, presented, _envelope, command = self.fixture._prepared_export(
            key=key,
            nonce=nonce,
        )
        self.fixture._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self.fixture._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        revision = self.fixture.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        return project_id, revision

    def _fail_attested_export(self) -> str:
        project_id, presented, _envelope, command = self.fixture._prepared_export()
        self.fixture._advance_commit_pending(command.resource_id)
        self.fixture._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        self.fixture.repository.update_canonical_export_job(
            job_id=command.resource_id,
            status="failed",
            stage="failed",
            progress=0.8,
            error={
                "code": "render_failed",
                "message": "Export failed after attestation.",
                "retryable": False,
            },
            expected_status="commit_pending",
        )
        return project_id

    def _complete_video_export(self) -> tuple[str, str]:
        opening = self.fixture._register_image(
            "residual-opening.jpg",
            b"residual-opening",
        )
        span_ref = self.fixture._register_video_span()
        semantic = export_fixture.timeline_integration.semantic(
            "plugin-residual",
            evidence_ref=f"memolens://evidence/asset/{opening['id']}",
        )
        semantic["material_hints"].append(
            {
                "hint_id": "residual_video_span",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact current video span.",
            }
        )
        project_id, _blueprint = self.fixture._materialize_semantic_project(
            semantic,
            key="plugin-residual",
        )
        presented, command = self.fixture._present_existing_project(
            project_id,
            key="plugin-residual-export",
            nonce="plugin-residual-nonce",
            package_basename="plugin-residual-film",
        )
        self.fixture._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self.fixture._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        self.fixture.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        video = next(item for item in occurrences if item["media_kind"] == "video")
        asset_id = str(video["asset_id"])
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE assets SET duration_ms=60000 WHERE id=?",
                (asset_id,),
            )
            parent_id = str(
                connection.execute(
                    "SELECT id FROM video_segments WHERE asset_id=? ORDER BY ordinal LIMIT 1",
                    (asset_id,),
                ).fetchone()[0]
            )
        return asset_id, parent_id

    def _insert_searchable_video(
        self,
        *,
        suffix: str,
        sha256: str,
        text: str,
        copy_source: bool = False,
        canonical_identity: bool = False,
    ) -> str:
        identity_digest = hashlib.sha256(suffix.encode("utf-8")).hexdigest()
        asset_id = (
            f"asset_{sha256[:24]}"
            if canonical_identity
            else f"asset_plugin_{suffix}"
        )
        source_id = (
            f"src_{identity_digest[:24]}"
            if canonical_identity
            else f"source_plugin_{suffix}"
        )
        run_id = (
            f"arun_{identity_digest[:32]}"
            if canonical_identity
            else f"arun_plugin_{suffix}"
        )
        segment_id = (
            f"seg_{sha256[:24]}_1_0"
            if canonical_identity
            else f"seg_plugin_{suffix}"
        )
        now = utc_now_iso()
        with self.fixture.repository.transaction(immediate=True) as connection:
            root_id = str(
                connection.execute(
                    "SELECT id FROM library_roots WHERE status='active' ORDER BY id LIMIT 1"
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO assets(
                       id,kind,sha256,mime_type,file_size,duration_ms,width,height,
                       rotation_degrees,codec_json,captured_at,probe_status,error_code,
                       created_at,updated_at)
                   VALUES(?, 'video', ?, 'video/mp4', 128, 10000, 1920, 1080,
                          0, '{}', NULL, 'ready', NULL, ?, ?)""",
                (asset_id, sha256, now, now),
            )
            connection.execute(
                """INSERT INTO asset_sources(
                       id,asset_id,library_root_id,relative_path,display_filename,
                       observed_size,observed_mtime_ns,source_file_id,availability,
                       is_preferred,last_verified_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,128,1,?,'available',1,?,?,?)""",
                (
                    source_id,
                    asset_id,
                    root_id,
                    f"renamed/{suffix}.mp4",
                    f"renamed-{suffix}.mp4",
                    f"inode-{suffix}",
                    now,
                    now,
                    now,
                ),
            )
            if copy_source:
                connection.execute(
                    """INSERT INTO asset_sources(
                           id,asset_id,library_root_id,relative_path,display_filename,
                           observed_size,observed_mtime_ns,source_file_id,availability,
                           is_preferred,last_verified_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,128,2,?,'available',0,?,?,?)""",
                    (
                        f"{source_id}_copy",
                        asset_id,
                        root_id,
                        f"copied/{suffix}-copy.mp4",
                        f"unrelated-final-name-{suffix}.mp4",
                        f"inode-{suffix}-copy",
                        now,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """INSERT INTO analysis_runs(
                       id,asset_id,revision,run_kind,parent_run_id,
                       analysis_profile_id,analysis_profile_json,input_asset_sha256,
                       status,transcript_status,visual_status,error_json,
                       created_at,started_at,finished_at)
                   VALUES(?,?,1,'initial',NULL,'plugin-test','{}',?,'succeeded',
                          'available','ready',NULL,?,?,?)""",
                (run_id, asset_id, sha256, now, now, now),
            )
            connection.execute(
                """INSERT INTO video_segments(
                       id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                       boundary_reason,summary,semantic_json,visible_text,
                       combined_text,text_embedding_model,text_embedding,
                       visual_status,transcript_status,confidence,created_at)
                   VALUES(?,?,?,0,0,10000,'plugin-test',?,'{}',?, ?,NULL,NULL,
                          'ready','available',1.0,?)""",
                (segment_id, asset_id, run_id, text, text, text, now),
            )
            connection.execute(
                """INSERT INTO asset_analysis_heads(
                       asset_id,analysis_run_id,updated_at) VALUES(?,?,?)""",
                (asset_id, run_id, now),
            )
        return segment_id

    def _gateway(self) -> MemoLensGateway:
        return MemoLensGateway(
            db_path=self.fixture.db_path,
            library_dir=self.fixture.library,
        )

    @staticmethod
    def _blueprint_candidate(
        *,
        material_ref: str | None = None,
        material_proof: dict[str, object] | None = None,
        reference_ref: str | None = None,
    ) -> dict[str, object]:
        material_hints: list[dict[str, object]] = []
        if material_ref is not None:
            material_hints.append(
                {
                    "hint_id": "selected_material",
                    "evidence_ref": material_ref,
                    "script_block_ids": ["opening"],
                    "reason": "Use this exact current evidence as executable footage.",
                    **(
                        {"proof": deepcopy(material_proof)}
                        if material_proof is not None
                        else {}
                    ),
                }
            )
        reference_refs: list[dict[str, object]] = []
        if reference_ref is not None:
            reference_refs.append(
                {
                    "reference_id": "visual_reference",
                    "kind": "evidence",
                    "locator": reference_ref,
                    "note": "Reference only; never select as executable footage.",
                }
            )
        return {
            "object": "memolens.creative_blueprint_candidate",
            "schema_version": "1",
            "project_id": None,
            "base": None,
            "intent": {
                "goal": "Cut one bounded local story.",
                "stance": "Original source material remains authoritative.",
                "audience": "individual creators",
                "platform": "short-video",
                "declared_source": "agent_proposal",
            },
            "script": {
                "declared_source": "agent_proposal",
                "blocks": [{"block_id": "opening", "text": "Open on the source."}],
            },
            "direction": {
                "theme": "source integrity",
                "narrative_arc": "source to story",
                "emotion": "calm",
                "tone": "restrained",
                "pace": "measured",
                "declared_source": "agent_proposal",
            },
            "output": {"duration_target_ms": 10_000, "aspect_ratio": "16:9"},
            "constraints": {"must_include": [], "must_exclude": []},
            "material_hints": material_hints,
            "reference_refs": reference_refs,
            "technique_refs": [],
            "bindings": {"creator_context": None, "wiki_generation": None},
            "assumptions": [],
            "missing_evidence": [],
            "open_decisions": [],
        }

    def _database_file_digests(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.fixture.db_path}{suffix}")
            if path.is_file():
                result[suffix or "db"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def _deepseek_mcp_blueprint_validate(
        self, candidate: dict[str, object]
    ) -> dict[str, object]:
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memolens_blueprint_validate",
                "arguments": {"candidate": candidate},
            },
        }
        environment = {
            **os.environ,
            "MEMOLENS_DB_PATH": str(self.fixture.db_path),
            "MEMOLENS_LIBRARY_DIR": str(self.fixture.library),
            "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "memolens_mcp.py")],
            input=(canonical_json(frame) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.fixture.root,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"")
        response = json.loads(completed.stdout.decode("utf-8"))
        self.assertNotIn("error", response)
        self.assertFalse(response["result"].get("isError", False))
        return response["result"]["structuredContent"]

    def _deepseek_mcp_error(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        environment = {
            **os.environ,
            "MEMOLENS_DB_PATH": str(self.fixture.db_path),
            "MEMOLENS_LIBRARY_DIR": str(self.fixture.library),
            "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "memolens_mcp.py")],
            input=(canonical_json(frame) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.fixture.root,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"")
        response = json.loads(completed.stdout.decode("utf-8"))
        self.assertNotIn("error", response)
        self.assertTrue(response["result"].get("isError", False))
        return json.loads(response["result"]["content"][0]["text"])

    @staticmethod
    def _drop_trigger(connection: sqlite3.Connection, name: str) -> str:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        connection.execute(f"DROP TRIGGER {name}")
        return str(row[0])

    def test_valid_projection_exactly_matches_current_core_facts(self) -> None:
        self._complete_export()
        with self.fixture.repository.transaction() as connection:
            core = MediaRepository.canonical_material_search_facts_in_transaction(
                connection,
                [],
            )
        with closing(
            ReadOnlyDatabase(self.fixture.db_path).connection()
        ) as connection:
            plugin = project_validated_derivative_facts(connection)
        self.assertEqual(plugin["derivative_facts"], core["derivative_facts"])
        self.assertEqual(
            plugin["derivative_revision"],
            core["derivative_revision"],
        )
        self.assertEqual(plugin["video_sha256"], frozenset({SUCCESS_SHA}))

    def test_mixed_search_uses_one_snapshot_excludes_same_sha_and_replenishes(self) -> None:
        self._complete_export()
        derivative_segment = self._insert_searchable_video(
            suffix="derivative",
            sha256=SUCCESS_SHA,
            text="exact derivative needle",
            copy_source=True,
        )
        raw_segment = self._insert_searchable_video(
            suffix="raw",
            sha256=RAW_SHA,
            text="lower score needle",
        )
        gateway = self._gateway()
        original = gateway._store.database.connection
        with mock.patch.object(
            gateway._store.database,
            "connection",
            wraps=original,
        ) as opened:
            result = gateway.mixed_search("needle", limit=1)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(
            [item["segment_id"] for item in result["results"]],
            [raw_segment],
        )
        self.assertNotIn(
            derivative_segment,
            {item.get("segment_id") for item in result["results"]},
        )
        self.assertEqual(result["derivative_revision"], result["derivative_exclusion"]["revision"])
        self.assertEqual(result["derivative_exclusion"]["excluded_candidate_count"], 1)

    def test_blueprint_material_admission_rejects_derivative_with_shared_mcp_parity(
        self,
    ) -> None:
        self._complete_export()
        segment_id = self._insert_searchable_video(
            suffix="blueprint-derivative",
            sha256=SUCCESS_SHA,
            text="derivative blueprint material",
            copy_source=True,
        )
        evidence_ref = f"memolens://evidence/span/{segment_id}"
        candidate = self._blueprint_candidate(material_ref=evidence_ref)
        gateway = self._gateway()
        before = self._database_file_digests()
        original = gateway._store.database.connection
        with mock.patch.object(
            gateway._store.database,
            "connection",
            wraps=original,
        ) as opened:
            direct = gateway.blueprint_validate(candidate)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(direct["schema_valid"])
        self.assertEqual(direct["status"], "completed")
        self.assertEqual(direct["planning_readiness"]["status"], "blocked")
        self.assertIn(
            "canonical_derivative_executable_material_forbidden",
            direct["planning_readiness"]["blockers"],
        )
        self.assertEqual(
            direct["reference_resolution"]["issues"],
            [
                {
                    "path": "/material_hints/0/evidence_ref",
                    "code": "canonical_derivative_executable_material_forbidden",
                }
            ],
        )
        codex_tool = call_tool(
            "memolens_blueprint_validate",
            {"candidate": candidate},
            gateway,
        )
        deepseek_mcp = self._deepseek_mcp_blueprint_validate(candidate)
        self.assertEqual(codex_tool, direct)
        self.assertEqual(deepseek_mcp, direct)
        self.assertEqual(self._database_file_digests(), before)

    def test_blueprint_reference_only_derivative_remains_available(self) -> None:
        self._complete_export()
        segment_id = self._insert_searchable_video(
            suffix="blueprint-reference",
            sha256=SUCCESS_SHA,
            text="derivative reference only",
        )
        candidate = self._blueprint_candidate(
            reference_ref=f"memolens://evidence/span/{segment_id}"
        )
        gateway = self._gateway()
        before = self._database_file_digests()
        direct = gateway.blueprint_validate(candidate)
        self.assertEqual(direct["status"], "completed")
        self.assertEqual(direct["reference_resolution"]["status"], "verified")
        self.assertEqual(direct["reference_resolution"]["issues"], [])
        self.assertNotIn(
            "canonical_derivative_executable_material_forbidden",
            direct["planning_readiness"]["blockers"],
        )
        self.assertEqual(
            call_tool(
                "memolens_blueprint_validate",
                {"candidate": candidate},
                gateway,
            ),
            direct,
        )
        self.assertEqual(self._deepseek_mcp_blueprint_validate(candidate), direct)
        self.assertEqual(self._database_file_digests(), before)

    def test_blueprint_asset_material_uses_the_same_exact_derivative_digest(self) -> None:
        self._complete_export()
        self._insert_searchable_video(
            suffix="blueprint-asset-derivative",
            sha256=SUCCESS_SHA,
            text="whole asset derivative material",
        )
        candidate = self._blueprint_candidate(
            material_ref=(
                "memolens://evidence/asset/asset_plugin_blueprint-asset-derivative"
            )
        )
        result = self._gateway().blueprint_validate(candidate)
        self.assertEqual(result["planning_readiness"]["status"], "blocked")
        self.assertIn(
            "canonical_derivative_executable_material_forbidden",
            result["planning_readiness"]["blockers"],
        )

    def test_residual_material_hint_closed_union_and_shared_mcp_admission(
        self,
    ) -> None:
        asset_id, _parent_id = self._complete_video_export()
        gateway = self._gateway()
        search = gateway.mixed_search(
            "ending",
            limit=12,
            filters={"unused_only": True},
        )
        residual = next(
            item
            for item in search["results"]
            if str(item.get("id", "")).startswith("rseg_")
        )
        evidence_ref = f"memolens://evidence/span/{residual['id']}"
        proof = {
            "kind": "residual_span",
            "residual_binding": residual["residual_binding"],
        }
        candidate = self._blueprint_candidate(
            material_ref=evidence_ref,
            material_proof=proof,
        )
        before = self._database_file_digests()
        original = gateway._store.database.connection
        with mock.patch.object(
            gateway._store.database,
            "connection",
            wraps=original,
        ) as opened:
            direct = gateway.blueprint_validate(candidate)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(direct["schema_valid"], direct["errors"])
        self.assertEqual(direct["status"], "completed")
        self.assertEqual(direct["reference_resolution"]["status"], "verified")
        self.assertEqual(direct["reference_resolution"]["issues"], [])
        self.assertEqual(direct["planning_readiness"]["status"], "ready_for_coverage")
        self.assertEqual(
            call_tool(
                "memolens_blueprint_validate",
                {"candidate": candidate},
                gateway,
            ),
            direct,
        )
        self.assertEqual(self._deepseek_mcp_blueprint_validate(candidate), direct)

        missing = self._blueprint_candidate(material_ref=evidence_ref)
        missing_result = gateway.blueprint_validate(missing)
        self.assertFalse(missing_result["schema_valid"])
        self.assertIn(
            "schema_union",
            {item["code"] for item in missing_result["errors"]},
        )
        extra = deepcopy(candidate)
        extra["material_hints"][0]["proof"]["extra"] = True
        self.assertFalse(gateway.blueprint_validate(extra)["schema_valid"])

        binding = dict(residual["residual_binding"])
        tampered = build_residual_binding(
            **{
                key: value
                for key, value in binding.items()
                if key not in {"object", "contract_version", "residual_id"}
            }
            | {"usage_revision": "0" * 64}
        )
        stale_candidate = self._blueprint_candidate(
            material_ref=(
                f"memolens://evidence/span/{tampered['residual_id']}"
            ),
            material_proof={
                "kind": "residual_span",
                "residual_binding": tampered,
            },
        )
        stale = gateway.blueprint_validate(stale_candidate)
        self.assertTrue(stale["schema_valid"], stale["errors"])
        self.assertIn(
            "blueprint_residual_evidence_stale",
            stale["planning_readiness"]["blockers"],
        )
        self.assertEqual(self._database_file_digests(), before)

        with closing(ReadOnlyDatabase(self.fixture.db_path).connection()) as connection:
            project_id = str(
                connection.execute(
                    """SELECT project_id FROM canonical_usage_occurrences
                         WHERE asset_id=? ORDER BY project_id, export_revision LIMIT 1""",
                    (asset_id,),
                ).fetchone()[0]
            )
        presented, command = self.fixture._present_existing_project(
            project_id,
            key="plugin-residual-derivative-export",
            nonce="plugin-residual-derivative-nonce",
            package_basename="plugin-residual-derivative-film",
        )
        self.fixture._advance_commit_pending(command.resource_id)
        artifact, occurrences, runtime = self.fixture._completion_inputs(
            project_id,
            command.resource_id,
            presented,
            video_sha256=str(binding["asset_sha256"]),
        )
        self.fixture.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=artifact,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        derivative_before = self._database_file_digests()
        derivative_residual = gateway.blueprint_validate(candidate)
        self.assertIn(
            "blueprint_residual_evidence_stale",
            derivative_residual["planning_readiness"]["blockers"],
        )
        self.assertNotIn(
            "canonical_derivative_executable_material_forbidden",
            derivative_residual["planning_readiness"]["blockers"],
        )
        self.assertEqual(
            call_tool(
                "memolens_blueprint_validate",
                {"candidate": candidate},
                gateway,
            ),
            derivative_residual,
        )
        self.assertEqual(
            self._deepseek_mcp_blueprint_validate(candidate),
            derivative_residual,
        )
        self.assertEqual(self._database_file_digests(), derivative_before)

    def test_blueprint_material_validator_unavailable_cannot_fall_through(self) -> None:
        self._complete_export()
        segment_id = self._insert_searchable_video(
            suffix="blueprint-validator-unavailable",
            sha256=RAW_SHA,
            text="otherwise safe source material",
        )
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            self._drop_trigger(
                connection,
                "trg_output_roots_identity_immutable",
            )
            connection.execute(
                """CREATE TRIGGER trg_output_roots_identity_immutable
                     BEFORE UPDATE ON output_roots
                     WHEN NEW.kind IS NOT OLD.kind
                     BEGIN SELECT RAISE(ABORT,'output_roots identity is immutable'); END"""
            )
        candidate = self._blueprint_candidate(
            material_ref=f"memolens://evidence/span/{segment_id}"
        )
        result = self._gateway().blueprint_validate(candidate)
        self.assertEqual(result["status"], "capability_unavailable")
        self.assertEqual(result["planning_readiness"]["status"], "blocked")
        self.assertIn(
            "executable_material_authority_unavailable",
            result["planning_readiness"]["blockers"],
        )
        self.assertEqual(result["reference_resolution"]["status"], "unresolved")
        self.assertFalse(
            result["reference_resolution"]["checked_in_single_snapshot"]
        )

    def test_persisted_current_resume_rejects_derivative_but_history_stays_readable(
        self,
    ) -> None:
        segment_id = self._insert_searchable_video(
            suffix="persisted-blueprint-derivative",
            sha256=SUCCESS_SHA,
            text="persisted derivative material",
            canonical_identity=True,
        )
        evidence_ref = f"memolens://evidence/span/{segment_id}"
        semantic = export_fixture.timeline_integration.semantic(
            "persisted-plugin-derivative",
            evidence_ref=evidence_ref,
        )
        project = self.fixture.repository.create_project(
            "Persisted derivative admission",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "plugin-derivative-test"},
        )
        project_id = str(project["id"])
        brief = self.fixture.repository.get_brief(project_id, 1)
        assert brief is not None
        self.fixture.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": semantic,
            },
            idempotency_key="persisted-plugin-derivative",
        )
        self._complete_export()
        gateway = self._gateway()
        before = self._database_file_digests()
        with self.assertRaises(MemoLensError) as rejected:
            gateway.blueprint_get(project_id)
        self.assertEqual(
            rejected.exception.code,
            "canonical_derivative_executable_material_forbidden",
        )
        with self.assertRaises(MemoLensError) as codex_tool:
            call_tool(
                "memolens_blueprint_get",
                {"project_id": project_id},
                gateway,
            )
        self.assertEqual(codex_tool.exception.code, rejected.exception.code)
        deepseek_error = self._deepseek_mcp_error(
            "memolens_blueprint_get",
            {"project_id": project_id},
        )
        self.assertEqual(
            deepseek_error["error"]["code"],
            "canonical_derivative_executable_material_forbidden",
        )
        exact = gateway.blueprint_get(project_id, revision=1)
        history = gateway.blueprint_history(project_id)
        self.assertEqual(exact["status"], "completed")
        self.assertEqual(exact["blueprint"]["revision"], 1)
        self.assertEqual(history["status"], "completed")
        self.assertEqual(history["current_head"]["revision"], 1)
        self.assertEqual(self._database_file_digests(), before)

    def test_restored_and_no_change_residual_head_revalidates_on_current_read(
        self,
    ) -> None:
        _asset_id, _parent_id = self._complete_video_export()
        gateway = self._gateway()
        search = gateway.mixed_search(
            "ending",
            limit=12,
            filters={"unused_only": True},
        )
        residual = next(
            item
            for item in search["results"]
            if str(item.get("id", "")).startswith("rseg_")
        )
        residual_ref = f"memolens://evidence/span/{residual['id']}"
        residual_proof = {
            "kind": "residual_span",
            "residual_binding": residual["residual_binding"],
        }

        first_opening = self.fixture._register_image(
            "restored-residual-opening.jpg",
            b"restored-residual-opening",
        )
        restored_semantic = export_fixture.timeline_integration.semantic(
            "restored-residual",
            evidence_ref=f"memolens://evidence/asset/{first_opening['id']}",
        )
        restored_semantic["material_hints"].append(
            {
                "hint_id": "restored_residual_ending",
                "evidence_ref": residual_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact residual for the ending.",
                "proof": residual_proof,
            }
        )
        project = self.fixture.repository.create_project(
            "Restored residual admission",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "plugin-residual-restore-test"},
        )
        project_id = str(project["id"])
        brief = self.fixture.repository.get_brief(project_id, 1)
        assert brief is not None
        first = self.fixture.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": restored_semantic,
            },
            idempotency_key="plugin-residual-restore-first",
        )
        first_head = dict(first.response["result"]["result_head"])
        second_semantic = deepcopy(restored_semantic)
        second_semantic["script"]["blocks"][0]["text"] += " revised"
        second = self.fixture.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": first_head["revision"],
                    "content_sha256": first_head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": second_semantic,
            },
            idempotency_key="plugin-residual-restore-second",
        )
        second_head = dict(second.response["result"]["result_head"])
        restored = self.fixture.blueprints.restore_revision(
            project_id,
            {
                "expected_head": {
                    "revision": second_head["revision"],
                    "content_sha256": second_head["content_sha256"],
                },
                "restore_from": {
                    "revision": first_head["revision"],
                    "content_sha256": first_head["content_sha256"],
                },
            },
            idempotency_key="plugin-residual-restore-third",
        )
        restored_head = dict(restored.response["result"]["result_head"])
        self.assertEqual(restored.response["result"]["kind"], "revision_restored")
        self.assertEqual(restored_head["revision"], 3)
        self.assertEqual(gateway.blueprint_get(project_id)["status"], "completed")

        no_change = self.fixture.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": restored_head["revision"],
                    "content_sha256": restored_head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": restored_semantic,
            },
            idempotency_key="plugin-residual-current-no-change",
        )
        self.assertEqual(no_change.response["result"]["kind"], "no_change")

        second_opening = self.fixture._register_image(
            "stale-residual-opening.jpg",
            b"stale-residual-opening",
        )
        exporter_semantic = deepcopy(restored_semantic)
        exporter_semantic["material_hints"][0]["evidence_ref"] = (
            f"memolens://evidence/asset/{second_opening['id']}"
        )
        exporter_project, _blueprint = self.fixture._materialize_semantic_project(
            exporter_semantic,
            key="plugin-residual-staler",
        )
        presented, command = self.fixture._present_existing_project(
            exporter_project,
            key="plugin-residual-staler-export",
            nonce="plugin-residual-staler-nonce",
            package_basename="plugin-residual-staler-film",
        )
        self.fixture._advance_commit_pending(command.resource_id)
        artifact, occurrences, runtime = self.fixture._completion_inputs(
            exporter_project,
            command.resource_id,
            presented,
        )
        self.fixture.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=artifact,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )

        before = self._database_file_digests()
        with self.assertRaises(MemoLensError) as direct:
            gateway.blueprint_get(project_id)
        self.assertEqual(
            direct.exception.code,
            "blueprint_residual_evidence_stale",
        )
        with self.assertRaises(MemoLensError) as codex_tool:
            call_tool(
                "memolens_blueprint_get",
                {"project_id": project_id},
                gateway,
            )
        self.assertEqual(codex_tool.exception.code, direct.exception.code)
        deepseek_error = self._deepseek_mcp_error(
            "memolens_blueprint_get",
            {"project_id": project_id},
        )
        self.assertEqual(
            deepseek_error["error"]["code"],
            "blueprint_residual_evidence_stale",
        )
        explicit_first = gateway.blueprint_get(project_id, revision=1)
        explicit_restored = gateway.blueprint_get(project_id, revision=3)
        history = gateway.blueprint_history(project_id)
        self.assertEqual(explicit_first["status"], "completed")
        self.assertEqual(explicit_restored["status"], "completed")
        self.assertEqual(history["current_head"]["revision"], 3)
        self.assertEqual(self._database_file_digests(), before)

    def test_failed_attested_export_does_not_contribute_exclusion(self) -> None:
        self._fail_attested_export()
        candidate = self._insert_searchable_video(
            suffix="failed",
            sha256=SUCCESS_SHA,
            text="failed output needle",
        )
        result = self._gateway().mixed_search("needle", limit=5)
        self.assertIn(candidate, {item.get("segment_id") for item in result["results"]})
        self.assertEqual(result["derivative_exclusion"]["validated_fact_count"], 0)

    def test_derivative_revision_hashes_all_facts_not_only_sha_set(self) -> None:
        project_id, _revision = self._complete_export()
        with closing(ReadOnlyDatabase(self.fixture.db_path).connection()) as connection:
            first = project_validated_derivative_facts(connection)
        first_search = self._gateway().mixed_search("revision needle")
        presented, command = self.fixture._present_existing_project(
            project_id,
            key="export-two",
            nonce="nonce-two",
            package_basename="memory-film-two",
        )
        self.fixture._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self.fixture._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        self.fixture.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        with closing(ReadOnlyDatabase(self.fixture.db_path).connection()) as connection:
            second = project_validated_derivative_facts(connection)
        second_search = self._gateway().mixed_search("revision needle")
        self.assertEqual(first["video_sha256"], second["video_sha256"])
        self.assertEqual(len(first["derivative_facts"]), 1)
        self.assertEqual(len(second["derivative_facts"]), 2)
        self.assertNotEqual(first["derivative_revision"], second["derivative_revision"])
        self.assertNotIn("usage_revision", first_search)
        self.assertNotIn("usage_revision", second_search)
        self.assertNotEqual(
            first_search["derivative_revision"],
            second_search["derivative_revision"],
        )
        self.assertNotEqual(
            first_search["search_revision"],
            second_search["search_revision"],
        )

    def test_resigned_operation_result_semantic_tamper_fails_closed(self) -> None:
        project_id, _revision = self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            operation_trigger = self._drop_trigger(
                connection,
                "trg_canonical_export_operations_no_update",
            )
            receipt_trigger = self._drop_trigger(
                connection,
                "trg_canonical_export_receipts_no_update",
            )
            operation = connection.execute(
                "SELECT * FROM canonical_export_operations WHERE project_id=?",
                (project_id,),
            ).fetchone()
            receipt = connection.execute(
                "SELECT * FROM canonical_export_receipts WHERE project_id=?",
                (project_id,),
            ).fetchone()
            assert operation is not None and receipt is not None
            forged = json.loads(str(operation["result_json"]))
            forged["job"]["progress"] = 0.5
            serialized = canonical_json(forged)
            response_sha256 = canonical_sha256(forged)
            receipt_material = {
                "database_uuid": receipt["database_uuid"],
                "authenticated_principal": receipt["authenticated_principal"],
                "project_id": receipt["project_id"],
                "command_type": receipt["command_type"],
                "command_version": receipt["command_version"],
                "idempotency_key": receipt["idempotency_key"],
                "request_sha256": receipt["request_sha256"],
                "operation_id": receipt["operation_id"],
                "response_status": receipt["response_status"],
                "response_sha256": response_sha256,
                "resource_id": receipt["resource_id"],
                "created_at": receipt["created_at"],
            }
            connection.execute(
                "UPDATE canonical_export_operations SET result_json=? WHERE id=?",
                (serialized, operation["id"]),
            )
            connection.execute(
                """UPDATE canonical_export_receipts
                      SET response_json=?,response_sha256=?,receipt_sha256=?
                    WHERE operation_id=?""",
                (
                    serialized,
                    response_sha256,
                    canonical_sha256(receipt_material),
                    operation["id"],
                ),
            )
            connection.execute(operation_trigger)
            connection.execute(receipt_trigger)
        with self.assertRaises(CanonicalExportIntegrityError):
            self.fixture.repository.validate_canonical_export_ledger(project_id)
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_resigned_revision_video_tamper_fails_closed_like_core(self) -> None:
        project_id, revision = self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            trigger = self._drop_trigger(
                connection,
                "trg_canonical_export_revisions_no_update",
            )
            connection.execute(
                """UPDATE canonical_export_revisions SET video_sha256=?
                     WHERE project_id=? AND revision=?""",
                (FAILED_SHA, project_id, revision["revision"]),
            )
            connection.execute(trigger)
        with self.assertRaises(CanonicalExportIntegrityError):
            self.fixture.repository.validate_canonical_export_ledger(project_id)
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_historical_timeline_tamper_fails_closed_like_core(self) -> None:
        project_id, _revision = self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            trigger = self._drop_trigger(
                connection,
                "trg_canonical_timeline_revisions_no_update",
            )
            row = connection.execute(
                """SELECT revision,timeline_json
                     FROM canonical_timeline_revisions
                    WHERE project_id=? ORDER BY revision DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            assert row is not None
            timeline = json.loads(str(row["timeline_json"]))
            timeline["output"]["duration_ms"] += 1
            connection.execute(
                """UPDATE canonical_timeline_revisions SET timeline_json=?
                    WHERE project_id=? AND revision=?""",
                (canonical_json(timeline), project_id, row["revision"]),
            )
            connection.execute(trigger)
        with self.assertRaises(CanonicalExportIntegrityError):
            self.fixture.repository.validate_canonical_export_ledger(project_id)
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_fully_resigned_script_facts_are_recomputed_from_blueprint(self) -> None:
        project_id, revision = self._complete_export()
        forged_bytes = b"forged standalone script\n"
        forged_script = {
            "sha256": hashlib.sha256(forged_bytes).hexdigest(),
            "size_bytes": len(forged_bytes),
        }
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            revision_trigger = self._drop_trigger(
                connection,
                "trg_canonical_export_revisions_no_update",
            )
            job_terminal_trigger = self._drop_trigger(
                connection,
                "trg_canonical_export_jobs_terminal_immutable",
            )
            job_attestation_trigger = self._drop_trigger(
                connection,
                "trg_canonical_export_jobs_attestation_once",
            )
            row = connection.execute(
                """SELECT * FROM canonical_export_revisions
                     WHERE project_id=? AND revision=?""",
                (project_id, revision["revision"]),
            ).fetchone()
            job = connection.execute(
                "SELECT * FROM canonical_export_jobs WHERE project_id=?",
                (project_id,),
            ).fetchone()
            assert row is not None and job is not None
            manifest = json.loads(str(row["package_manifest_json"]))
            manifest["script"] = dict(forged_script)
            physical = MediaRepository._canonical_export_physical_artifact_bindings(
                manifest
            )
            attestation = json.loads(str(job["commit_attestation_json"]))
            proof = attestation["artifact_proof"]
            proof["script_sha256"] = forged_script["sha256"]
            proof["script_size_bytes"] = forged_script["size_bytes"]
            proof["package_manifest"] = manifest
            for field in (
                "package_manifest_sha256",
                "human_usage_sha256",
                "completion_marker_sha256",
            ):
                proof[field] = physical[field]
            connection.execute(
                """UPDATE canonical_export_jobs
                      SET commit_attestation_json=?,commit_attestation_sha256=?
                    WHERE id=?""",
                (
                    canonical_json(attestation),
                    canonical_sha256(attestation),
                    job["id"],
                ),
            )
            timeline = manifest["timeline_binding"]
            revision_material = {
                "project_id": row["project_id"],
                "revision": row["revision"],
                "job_id": row["job_id"],
                "operation_id": row["operation_id"],
                "timeline_binding": timeline,
                "output_binding": {
                    "output_root_id": row["output_root_id"],
                    "permission_fingerprint": row["permission_fingerprint"],
                    "package_basename": row["package_basename"],
                    "profile": row["profile"],
                },
                "video": {
                    "sha256": row["video_sha256"],
                    "size_bytes": row["video_size_bytes"],
                    "duration_ms": row["video_duration_ms"],
                },
                "script": forged_script,
                "package_manifest_sha256": physical["package_manifest_sha256"],
                "usage_sha256": row["usage_sha256"],
                "runtime_manifest_sha256": row["runtime_manifest_sha256"],
                "human_usage_sha256": physical["human_usage_sha256"],
                "completion_marker_sha256": physical[
                    "completion_marker_sha256"
                ],
                "completed_at": row["completed_at"],
            }
            connection.execute(
                """UPDATE canonical_export_revisions
                      SET script_sha256=?,script_size_bytes=?,
                          package_manifest_json=?,package_manifest_sha256=?,
                          human_usage_sha256=?,completion_marker_sha256=?,
                          revision_sha256=?
                    WHERE project_id=? AND revision=?""",
                (
                    forged_script["sha256"],
                    forged_script["size_bytes"],
                    canonical_json(manifest),
                    physical["package_manifest_sha256"],
                    physical["human_usage_sha256"],
                    physical["completion_marker_sha256"],
                    canonical_sha256(revision_material),
                    project_id,
                    row["revision"],
                ),
            )
            connection.execute(revision_trigger)
            connection.execute(job_terminal_trigger)
            connection.execute(job_attestation_trigger)

        with self.assertRaises(CanonicalExportIntegrityError):
            self.fixture.repository.validate_canonical_export_ledger(project_id)
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )
        with mock.patch.object(
            export_authority,
            "_script_projection",
            return_value=forged_script,
        ):
            bypassed = self._gateway().mixed_search("anything")
        self.assertEqual(
            bypassed["derivative_exclusion"]["validated_fact_count"],
            1,
        )

    def test_five_table_union_detects_occurrence_only_orphan_project(self) -> None:
        self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            trigger = self._drop_trigger(
                connection,
                "trg_canonical_usage_occurrences_no_update",
            )
            row = connection.execute(
                "SELECT * FROM canonical_usage_occurrences ORDER BY ordinal LIMIT 1"
            ).fetchone()
            assert row is not None
            columns = [
                str(info[1])
                for info in connection.execute(
                    "PRAGMA table_info(canonical_usage_occurrences)"
                )
            ]
            values = list(row)
            values[columns.index("project_id")] = "proj_orphan_plugin"
            connection.execute(
                f"INSERT INTO canonical_usage_occurrences({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                values,
            )
            connection.execute(trigger)
            connection.commit()
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_export_integrity_failure_is_outside_branch_error_fallback(self) -> None:
        self._complete_export()
        error = MemoLensError(
            "Derivative authority is unavailable.",
            code="derivative_authority_unavailable",
        )
        with mock.patch(
            "memolens_read_store.project_validated_derivative_facts",
            side_effect=error,
        ):
            with self.assertRaises(MemoLensError) as raised:
                self._gateway().mixed_search("anything")
        self.assertIs(raised.exception, error)

    def test_deleted_export_authority_tables_cannot_fail_open_as_empty(self) -> None:
        self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "canonical_usage_occurrences",
                "canonical_export_revisions",
                "canonical_export_receipts",
                "canonical_export_jobs",
                "canonical_export_operations",
            ):
                connection.execute(f"DROP TABLE {table}")
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_unavailable",
        )

    def test_restored_schema_cannot_hide_all_five_ledger_rows_deleted(self) -> None:
        self._complete_export()
        trigger_names = (
            "trg_canonical_usage_occurrences_no_delete",
            "trg_canonical_export_revisions_no_delete",
            "trg_canonical_export_receipts_no_delete",
            "trg_canonical_export_jobs_no_delete",
            "trg_canonical_export_operations_no_delete",
        )
        tables = (
            "canonical_usage_occurrences",
            "canonical_export_revisions",
            "canonical_export_receipts",
            "canonical_export_jobs",
            "canonical_export_operations",
        )
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            trigger_sql = [
                self._drop_trigger(connection, name) for name in trigger_names
            ]
            for table in tables:
                connection.execute(f"DELETE FROM {table}")
            for statement in trigger_sql:
                connection.execute(statement)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM output_roots WHERE kind='user_export'"
                ).fetchone()[0],
                1,
            )
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_mutable_export_root_refresh_preserves_core_plugin_parity(self) -> None:
        self._complete_export()
        with self.fixture.repository.transaction() as connection:
            core_before = MediaRepository.canonical_material_search_facts_in_transaction(
                connection,
                [],
            )
        with closing(
            ReadOnlyDatabase(self.fixture.db_path).connection()
        ) as connection:
            plugin_before = project_validated_derivative_facts(connection)

        refreshed_at = "2040-01-02T00:00:00+00:00"
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            root = connection.execute(
                """SELECT root.id,root.permission_fingerprint,
                          job.permission_fingerprint
                     FROM output_roots AS root
                     JOIN canonical_export_jobs AS job ON job.output_root_id=root.id
                    LIMIT 1"""
            ).fetchone()
            assert root is not None
            root_id = str(root[0])
            historical_fingerprint = str(root[2])
            self.assertEqual(str(root[1]), historical_fingerprint)
            connection.execute(
                """UPDATE output_roots
                      SET permission_fingerprint=?,status='unavailable',updated_at=?
                    WHERE id=?""",
                ("permission-refreshed", refreshed_at, root_id),
            )
            refreshed = connection.execute(
                """SELECT permission_fingerprint,status,updated_at
                     FROM output_roots WHERE id=?""",
                (root_id,),
            ).fetchone()
            self.assertEqual(
                refreshed,
                ("permission-refreshed", "unavailable", refreshed_at),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT permission_fingerprint FROM canonical_export_jobs LIMIT 1"
                ).fetchone()[0],
                historical_fingerprint,
            )

        with self.fixture.repository.transaction() as connection:
            core_after = MediaRepository.canonical_material_search_facts_in_transaction(
                connection,
                [],
            )
        with closing(
            ReadOnlyDatabase(self.fixture.db_path).connection()
        ) as connection:
            plugin_after = project_validated_derivative_facts(connection)

        self.assertEqual(core_after, core_before)
        self.assertEqual(plugin_after["derivative_facts"], core_after["derivative_facts"])
        self.assertEqual(
            plugin_after["derivative_revision"],
            core_after["derivative_revision"],
        )
        self.assertEqual(plugin_after, plugin_before)

    def test_export_job_root_kind_is_cold_audited(self) -> None:
        self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            root = connection.execute(
                """SELECT root.id
                     FROM output_roots AS root
                     JOIN canonical_export_jobs AS job ON job.output_root_id=root.id
                    LIMIT 1"""
            ).fetchone()
            assert root is not None
            root_id = str(root[0])
            identity_trigger = self._drop_trigger(
                connection,
                "trg_output_roots_identity_immutable",
            )
            connection.execute(
                "UPDATE output_roots SET kind='app_preview' WHERE id=?",
                (root_id,),
            )
            connection.execute(identity_trigger)
        with self.assertRaises(MemoLensError) as kind_error:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            kind_error.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_export_job_root_identity_rebind_is_cold_audited(self) -> None:
        self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            root_id = str(
                connection.execute(
                    "SELECT id FROM output_roots WHERE kind='user_export'"
                ).fetchone()[0]
            )
            identity_trigger = self._drop_trigger(
                connection,
                "trg_output_roots_identity_immutable",
            )
            connection.execute(
                "UPDATE output_roots SET id=? WHERE id=?",
                (f"out_{'f' * 24}", root_id),
            )
            connection.execute(identity_trigger)
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_export_job_root_missing_is_cold_audited(self) -> None:
        self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            deletion_trigger = self._drop_trigger(
                connection,
                "trg_user_export_output_roots_no_delete",
            )
            connection.execute("DELETE FROM output_roots WHERE kind='user_export'")
            connection.execute(deletion_trigger)
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_v19_root_anchor_blocks_kind_rebind_before_five_table_wipe(
        self,
    ) -> None:
        self._complete_export()
        trigger_names = (
            "trg_canonical_usage_occurrences_no_delete",
            "trg_canonical_export_revisions_no_delete",
            "trg_canonical_export_receipts_no_delete",
            "trg_canonical_export_jobs_no_delete",
            "trg_canonical_export_operations_no_delete",
        )
        tables = (
            "canonical_usage_occurrences",
            "canonical_export_revisions",
            "canonical_export_receipts",
            "canonical_export_jobs",
            "canonical_export_operations",
        )
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            root_id = str(
                connection.execute(
                    "SELECT id FROM output_roots WHERE kind='user_export'"
                ).fetchone()[0]
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "output_roots identity is immutable",
            ):
                connection.execute(
                    "UPDATE output_roots SET kind='app_preview' WHERE id=?",
                    (root_id,),
                )
            trigger_sql = [
                self._drop_trigger(connection, name) for name in trigger_names
            ]
            for table in tables:
                connection.execute(f"DELETE FROM {table}")
            for statement in trigger_sql:
                connection.execute(statement)
            self.assertEqual(
                connection.execute(
                    "SELECT kind FROM output_roots WHERE id=?",
                    (root_id,),
                ).fetchone()[0],
                "user_export",
            )
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_integrity_error",
        )

    def test_v19_output_root_anchor_trigger_sql_is_exact(self) -> None:
        self._complete_export()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            self._drop_trigger(
                connection,
                "trg_output_roots_identity_immutable",
            )
            connection.execute(
                """CREATE TRIGGER trg_output_roots_identity_immutable
                     BEFORE UPDATE ON output_roots
                     WHEN NEW.kind IS NOT OLD.kind
                     BEGIN SELECT RAISE(ABORT,'output_roots identity is immutable'); END"""
            )
        with self.assertRaises(MemoLensError) as raised:
            self._gateway().mixed_search("anything")
        self.assertEqual(
            raised.exception.code,
            "derivative_authority_unavailable",
        )

    def test_persisted_residual_id_fails_closed_at_candidate_and_read_boundaries(
        self,
    ) -> None:
        asset_id, parent_id = self._complete_video_export()
        forged_id = f"rseg_{'d' * 64}"
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE video_segments SET id=? WHERE id=?",
                (forged_id, parent_id),
            )
        gateway = self._gateway()
        for read in (
            lambda: gateway.mixed_search("ending", limit=12),
            lambda: gateway.video_search("ending", limit=12),
            lambda: gateway.media_get(asset_id),
        ):
            with self.subTest(read=read):
                with self.assertRaises(MemoLensError) as raised:
                    read()
                self.assertEqual(
                    raised.exception.code,
                    "residual_authority_integrity_error",
                )

    def test_residual_modes_share_one_codex_deepseek_mcp_oracle(self) -> None:
        asset_id, parent_id = self._complete_video_export()
        gateway = self._gateway()

        default = gateway.mixed_search("ending", limit=12)
        self.assertNotIn("usage_revision", default)
        self.assertIn(
            parent_id,
            {item.get("id") for item in default["results"]},
        )

        filters = {"unused_only": True}
        unused = gateway.mixed_search("ending", limit=12, filters=filters)
        with self.fixture.repository.transaction() as connection:
            current_candidates, _heads = (
                self.fixture.repository.mixed_candidates_in_transaction(connection)
            )
            current_asset_ids = sorted(
                {
                    str(item["asset_id"])
                    for item in current_candidates
                    if item.get("asset_id") is not None
                }
            )
            core_facts = MediaRepository.canonical_material_search_facts_in_transaction(
                connection,
                current_asset_ids,
            )
        with closing(ReadOnlyDatabase(self.fixture.db_path).connection()) as connection:
            plugin = project_validated_derivative_facts(connection)
        self.assertEqual(
            unused["usage_revision"],
            canonical_sha256(core_facts["usage_occurrences"]),
        )
        self.assertEqual(
            core_facts["usage_occurrences"],
            [
                item
                for item in plugin["usage_occurrences"]
                if item["asset_id"] in set(current_asset_ids)
            ],
        )
        video_only_occurrences = [
            item
            for item in core_facts["usage_occurrences"]
            if item.get("asset_id") == asset_id
        ]
        self.assertTrue(
            any(
                item.get("media_kind") == "image"
                for item in core_facts["usage_occurrences"]
            )
        )
        self.assertNotEqual(
            unused["usage_revision"],
            canonical_sha256(video_only_occurrences),
        )
        self.assertRegex(unused["usage_revision"], r"^[0-9a-f]{64}$")
        self.assertRegex(unused["derivative_revision"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            unused["usage_revision"],
            unused["derivative_revision"],
        )
        residuals = [
            item
            for item in unused["results"]
            if item.get("result_type") == "video_segment"
        ]
        self.assertGreaterEqual(len(residuals), 1)
        self.assertNotIn(parent_id, {item["id"] for item in residuals})
        for item in residuals:
            binding = require_core_residual_binding(item["residual_binding"])
            self.assertEqual(binding["residual_id"], item["id"])
            self.assertEqual(binding["usage_revision"], unused["usage_revision"])
            self.assertEqual(binding["parent_segment_id"], parent_id)
            self.assertEqual(
                item["thumbnail_url"],
                f"/v1/video-segments/{parent_id}/thumbnail",
            )
            self.assertEqual(item["usage"]["used_intervals"], [])
            with self.fixture.repository.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM video_segments WHERE id=?",
                        (item["id"],),
                    ).fetchone()[0],
                    0,
                )

        residual_of = gateway.mixed_search(
            "ending",
            limit=12,
            filters={"residual_of": asset_id},
        )
        self.assertEqual(
            {item["id"] for item in residuals},
            {item["id"] for item in residual_of["results"]},
        )

        no_reuse = gateway.mixed_search(
            "ending",
            limit=12,
            filters={"allow_reuse": False},
        )
        self.assertEqual(
            {item["id"] for item in residuals},
            {
                item["id"]
                for item in no_reuse["results"]
                if item.get("result_type") == "video_segment"
            },
        )
        self.assertNotIn(
            parent_id,
            {item.get("id") for item in no_reuse["results"]},
        )

        preferred = gateway.mixed_search(
            "ending",
            limit=12,
            filters={"prefer_unused": True, "allow_reuse": True},
        )
        preferred_video_ids = [
            item["id"]
            for item in preferred["results"]
            if item.get("result_type") == "video_segment"
        ]
        self.assertIn(parent_id, preferred_video_ids)
        self.assertTrue(preferred_video_ids[0].startswith("rseg_"))

        # DeepSeek Harness mounts this same MCP script; a direct MCP dispatch
        # must therefore freeze the same identities and both revisions.
        deepseek_path = call_tool(
            "memolens_mixed_search",
            {"query": "ending", "limit": 12, "filters": filters},
            gateway,
        )
        self.assertEqual(deepseek_path, unused)

    def test_used_in_scope_cannot_mint_global_residual_identity(self) -> None:
        asset_id, parent_id = self._complete_video_export()
        with self.fixture.repository.transaction() as connection:
            project_id = str(
                connection.execute(
                    """SELECT project_id FROM canonical_usage_occurrences
                         WHERE asset_id=? ORDER BY project_id, export_revision LIMIT 1""",
                    (asset_id,),
                ).fetchone()[0]
            )
        gateway = self._gateway()
        mixed_tool = next(
            tool for tool in TOOLS if tool["name"] == "memolens_mixed_search"
        )
        used_in_schema = mixed_tool["inputSchema"]["properties"]["filters"][
            "properties"
        ]["used_in"]
        self.assertEqual(len(used_in_schema["oneOf"]), 2)

        scoped = gateway.mixed_search(
            "ending",
            limit=12,
            filters={"used_in": {"project_id": project_id}},
        )
        self.assertIn(
            parent_id,
            {item.get("id") for item in scoped["results"]},
        )
        self.assertEqual(
            scoped["usage_policy"]["used_in"],
            {
                "project_id": project_id,
                "export_revision": None,
            },
        )
        self.assertFalse(
            any(str(item.get("id", "")).startswith("rseg_") for item in scoped["results"])
        )

        for filters in (
            {"used_in": project_id, "unused_only": True},
            {"used_in": project_id, "prefer_unused": True},
            {"used_in": project_id, "residual_of": asset_id},
            {"used_in": project_id, "allow_reuse": False},
            {"used_in": {"project_id": project_id, "extra": 1}},
        ):
            with self.subTest(filters=filters), self.assertRaises(MemoLensError) as raised:
                gateway.mixed_search("ending", limit=12, filters=filters)
            self.assertEqual(raised.exception.code, "usage_filter_invalid")

        with self.assertRaisesRegex(
            ValueError,
            "Scoped Usage cannot authorize",
        ):
            annotate_and_materialize(
                [],
                [],
                usage_revision="f" * 64,
                policy=UsagePolicy(
                    requested=True,
                    prefer_unused=True,
                    used_in=UsageSelection(project_id),
                ),
            )


if __name__ == "__main__":
    unittest.main()
