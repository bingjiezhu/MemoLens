from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from backend.src import (
    DESKTOP_TOKEN_HEADER,
    MAIN_AUTHORITY_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from backend.src.agent_authority import (
    AGENT_CAPABILITY_HEADER,
    AGENT_NONCE_HEADER,
    AGENT_PROOF_HEADER,
    AGENT_STATUS_PROOF_HEADER,
    AgentPairingBroker,
    agent_request_proof,
    agent_status_proof,
)
from backend.src.api.routes import MAIN_AUTHORITY_ENDPOINTS
from core.config import Settings
from core.media_db import canonical_json, content_sha256
from core.production_surface_inventory import load_production_surface_inventory
from tests.test_coverage_persistence import semantic
from tests import test_timeline_lowering_integration as timeline_integration


ROOT = Path(__file__).resolve().parents[1]
HIGH_IMPACT_EFFECTS = frozenset(
    {"egress", "filesystem-read", "filesystem-write", "mutation", "process-start"}
)


class MainBusinessDispatchBeforeAuthorityDenial(AssertionError):
    """Raised when an untrusted caller reaches a main-only Flask view."""


def _converter_probe(converter: object) -> object:
    converter_name = type(converter).__name__
    if converter_name == "IntegerConverter":
        return 1
    if converter_name == "FloatConverter":
        return 1.0
    if converter_name == "UUIDConverter":
        return UUID("00000000-0000-4000-8000-000000000001")
    if converter_name == "AnyConverter":
        items = tuple(getattr(converter, "items", ()))
        if not items:
            raise AssertionError("AnyConverter has no closed probe candidate.")
        return items[0]
    return "inventory-probe"


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            rows.append((relative, "symlink", os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            rows.append(
                (relative, "file", hashlib.sha256(path.read_bytes()).hexdigest())
            )
        elif stat.S_ISDIR(metadata.st_mode):
            rows.append((relative, "directory", ""))
        else:
            rows.append((relative, "other", str(stat.S_IFMT(metadata.st_mode))))
    return tuple(rows)


class BackendProtocolAuthorityInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-protocol-inventory-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.export_root = self.root / "exports"
        self.export_root.mkdir()
        self.db_path = self.root / "state" / "inventory.db"
        self.desktop_token = "protocol-inventory-desktop-token"
        self.main_token = "protocol-inventory-main-token"
        self.epoch = "protocol_inventory_epoch"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
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
        self.addCleanup(self.environment.stop)
        self.app = create_app(Settings.from_env())
        self.app.testing = True
        self.addCleanup(shutdown_runtime_extensions, self.app.extensions)
        self.client = self.app.test_client()
        self.repository = self.app.extensions["media_repository"]
        self.blueprints = self.app.extensions["blueprint_service"]
        self.coverage = self.app.extensions["coverage_service"]
        self.timelines = self.app.extensions["timeline_lowering_service"]
        self._sequence = 0

    def _database_snapshot(self) -> str:
        with closing(self.repository._connect()) as connection:
            dump = "\n".join(connection.iterdump())
        return hashlib.sha256(dump.encode("utf-8")).hexdigest()

    def _state_snapshot(self) -> tuple[str, tuple[tuple[str, str, str], ...]]:
        return self._database_snapshot(), _tree_snapshot(self.root)

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_integration.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            content,
        )

    def _build_timeline_project(
        self,
        label: str,
    ) -> tuple[str, dict[str, object]]:
        opening = self._register_image(
            f"{label}-opening.jpg", f"{label}-opening".encode()
        )
        ending = self._register_image(
            f"{label}-ending.jpg", f"{label}-ending".encode()
        )
        opening_ref = f"memolens://evidence/asset/{opening['id']}"
        ending_ref = f"memolens://evidence/asset/{ending['id']}"
        proposed = semantic(f"{label} story", evidence_ref=opening_ref)
        proposed["material_hints"].append(
            {
                "hint_id": f"{label}_ending",
                "evidence_ref": ending_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact ending image.",
            }
        )
        project = self.repository.create_project(
            f"Protocol inventory {label}",
            {
                "goal": f"{label} story",
                "duration_ms": 3_000,
                "aspect_ratio": "9:16",
                "candidate_refs": [
                    {
                        "result_type": "image",
                        "id": opening["id"],
                        "asset_id": opening["id"],
                    }
                ],
            },
            {"created_by": "protocol-inventory-test"},
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
                "semantic": proposed,
            },
            idempotency_key=f"{label}-blueprint",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    field: blueprint[field]
                    for field in ("revision", "content_sha256", "semantic_sha256")
                },
                "expected_plan_head": None,
            },
            idempotency_key=f"{label}-coverage",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        materialize = {
            "expected_blueprint": {
                field: blueprint[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "semantic_sha256",
                    "operation_id",
                )
            },
            "expected_coverage": {
                field: coverage[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": None,
        }
        result = self.timelines.materialize_first_cut(
            project_id,
            materialize,
            idempotency_key=f"{label}-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(result.response_status, 201)
        workspace = self.timelines.read(project_id)
        head = workspace["head"]
        timeline = workspace["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        clip_id = str(timeline["tracks"][0]["clips"][0]["clip_id"])
        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": materialize["expected_blueprint"],
                "expected_coverage": materialize["expected_coverage"],
                "expected_timeline_head": head,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip_id,
                    "duration_ms": 1_375,
                },
            },
            idempotency_key=f"{label}-edit",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.repository.get_canonical_timeline_head(project_id)
        target = self.repository.get_canonical_timeline_revision(project_id, 1)
        assert current is not None and target is not None
        restore = {
            "expected_blueprint": copy.deepcopy(current["blueprint_binding"]),
            "expected_coverage": copy.deepcopy(current["coverage_binding"]),
            "expected_timeline_head": (
                self.repository._canonical_timeline_head_projection(current)
            ),
            "restore_from": (
                self.repository._canonical_timeline_head_projection(target)
            ),
        }
        return project_id, restore

    def _pairing_body(
        self,
        project_id: str,
        *,
        actions: list[str],
        secret: str,
    ) -> dict[str, object]:
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        self._sequence += 1
        return {
            "object": "memolens.agent_pairing_request",
            "schema_version": "1",
            "project_id": project_id,
            "observed_head": {
                "revision": blueprint["revision"],
                "content_sha256": blueprint["content_sha256"],
            },
            "subject_id": f"protocol_inventory_subject_{self._sequence}",
            "claimed_client_label": "Protocol inventory local agent",
            "proof_secret": secret,
            "actions": actions,
            "ttl_seconds": 900,
            "max_operations": 32,
        }

    def _create_pending_pairing(
        self,
        project_id: str,
        *,
        actions: list[str],
        secret: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        body = self._pairing_body(project_id, actions=actions, secret=secret)
        response = self.client.post("/v1/agent/pairings", json=body)
        self.assertEqual(response.status_code, 201, response.json)
        presentation = self.client.get(
            f"/v1/main/agent/pairings/{response.json['pairing_id']}/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        return response.json, presentation.json

    def _approve_pairing(
        self,
        pairing: dict[str, object],
        presentation: dict[str, object],
    ) -> str:
        self._sequence += 1
        approved = self.client.post(
            f"/v1/main/agent/pairings/{pairing['pairing_id']}/approve",
            json={
                "presentation_sha256": presentation["presentation_sha256"],
                "native_gesture_nonce": f"protocol_pairing_setup_{self._sequence}",
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(approved.status_code, 201, approved.json)
        return str(approved.json["capability_id"])

    def _issue_nonce(self, capability_id: str, action: str, secret: str):
        return self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    secret,
                    kind="nonce",
                    identifier=f"{capability_id}:{action}",
                )
            },
        )

    def _signed_agent_post(
        self,
        *,
        capability_id: str,
        secret: str,
        action: str,
        path: str,
        project_id: str,
        body: dict[str, object],
        key: str,
        signed_key: str | None = None,
        signed_database_uuid: str | None = None,
        signed_project_id: str | None = None,
        nonce_action: str | None = None,
        include_key: bool = True,
    ):
        nonce_response = self._issue_nonce(
            capability_id,
            nonce_action or action,
            secret,
        )
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        nonce = str(nonce_response.json["nonce"])
        proof = agent_request_proof(
            secret,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=signed_database_uuid or self.repository.database_uuid,
            project_id=signed_project_id or project_id,
            body_sha256=hashlib.sha256(canonical_json(body).encode()).hexdigest(),
            idempotency_key=key if signed_key is None else signed_key,
        )
        headers = {
            AGENT_CAPABILITY_HEADER: capability_id,
            AGENT_NONCE_HEADER: nonce,
            AGENT_PROOF_HEADER: proof,
        }
        if include_key:
            headers["Idempotency-Key"] = key
        return self.client.post(path, json=body, headers=headers)

    def test_every_high_impact_main_action_denies_before_business_dispatch(
        self,
    ) -> None:
        inventory = load_production_surface_inventory()
        target_action_ids = frozenset(
            str(action["id"])
            for action in inventory["actions"]
            if action["surface"] == "flask_http"
            and "electron-main" in action["authority"]
            and HIGH_IMPACT_EFFECTS.intersection(action["effects"])
        )
        self.assertTrue(target_action_ids)
        runtime_main_endpoints = frozenset(
            rule.endpoint.rsplit(".", 1)[-1]
            for rule in self.app.url_map.iter_rules()
            if rule.rule.startswith("/v1/main/")
        )
        self.assertEqual(
            runtime_main_endpoints,
            MAIN_AUTHORITY_ENDPOINTS,
            "Every and only /v1/main runtime view must use pre-dispatch admission.",
        )

        cases: dict[str, tuple[object, str, str]] = {}
        for rule in self.app.url_map.iter_rules():
            for method in sorted(set(rule.methods or ()) - {"HEAD", "OPTIONS"}):
                action_id = f"flask_http.{method}:{rule.rule}"
                if action_id not in target_action_ids:
                    continue
                values = {
                    argument: _converter_probe(rule._converters[argument])
                    for argument in rule.arguments
                }
                built = rule.build(values, append_unknown=False)
                self.assertIsNotNone(built)
                assert built is not None
                _subdomain, path = built
                endpoint = rule.endpoint.rsplit(".", 1)[-1]
                self.assertIn(
                    endpoint,
                    MAIN_AUTHORITY_ENDPOINTS,
                    f"{action_id} lacks pre-dispatch main admission.",
                )
                self.assertNotIn(action_id, cases)
                cases[action_id] = (rule, method, path)

        self.assertEqual(frozenset(cases), target_action_ids)
        self.assertEqual(len(cases), len(target_action_ids))

        original_views: dict[str, object] = {}

        def dispatch_sentinel(**_values: object) -> None:
            raise MainBusinessDispatchBeforeAuthorityDenial(
                "main-only Flask endpoint dispatch"
            )

        for rule, _method, _path in cases.values():
            if rule.endpoint not in original_views:
                original_views[rule.endpoint] = self.app.view_functions[rule.endpoint]
                self.app.view_functions[rule.endpoint] = dispatch_sentinel

        attempted: set[tuple[str, str]] = set()
        try:
            with patch(
                "flask.wrappers.Request.get_json",
                side_effect=MainBusinessDispatchBeforeAuthorityDenial(
                    "main-only JSON decode"
                ),
            ):
                for action_id in sorted(target_action_ids):
                    _rule, method, path = cases[action_id]
                    variants = {
                        "missing-main": {},
                        "desktop-impersonation": {
                            DESKTOP_TOKEN_HEADER: self.desktop_token
                        },
                        "browser-origin": {
                            MAIN_AUTHORITY_HEADER: self.main_token,
                            "Origin": "http://127.0.0.1:5173",
                        },
                    }
                    for variant, headers in variants.items():
                        with self.subTest(
                            action_id=action_id,
                            variant=variant,
                            path=path,
                        ):
                            before = self._state_snapshot()
                            attempted.add((action_id, variant))
                            response = self.client.open(
                                path,
                                method=method,
                                data=b'{"',
                                content_type="application/json",
                                headers=headers,
                            )
                            self.assertEqual(response.status_code, 403, response.json)
                            self.assertEqual(
                                response.json["code"],
                                "native_confirmation_required",
                            )
                            self.assertEqual(self._state_snapshot(), before)
        finally:
            for endpoint, original_view in original_views.items():
                self.app.view_functions[endpoint] = original_view

        self.assertEqual(
            attempted,
            {
                (action_id, variant)
                for action_id in target_action_ids
                for variant in (
                    "missing-main",
                    "desktop-impersonation",
                    "browser-origin",
                )
            },
        )

    def test_agent_routes_deny_pairing_idempotency_cas_and_scope_substitution_without_writes(
        self,
    ) -> None:
        blueprint_project, _unused_restore = self._build_timeline_project(
            "agent-blueprint"
        )
        timeline_project, timeline_restore = self._build_timeline_project(
            "agent-timeline"
        )
        other_project, _other_restore = self._build_timeline_project("agent-other")

        blueprint_target = self.repository.get_blueprint_head(blueprint_project)
        assert blueprint_target is not None
        advanced = self.blueprints.commit_proposal(
            blueprint_project,
            {
                "expected_head": {
                    "revision": blueprint_target["revision"],
                    "content_sha256": blueprint_target["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": "b" * 64,
                "semantic": semantic("agent blueprint revision two"),
            },
            idempotency_key="agent-blueprint-revision-two",
        )
        self.assertEqual(advanced.response_status, 201)
        blueprint_current = self.repository.get_blueprint_head(blueprint_project)
        assert blueprint_current is not None
        commit_body = {
            "expected_head": {
                "revision": blueprint_current["revision"],
                "content_sha256": blueprint_current["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "c" * 64,
            "semantic": semantic("agent blueprint candidate"),
        }
        blueprint_restore_body = {
            "expected_head": copy.deepcopy(commit_body["expected_head"]),
            "restore_from": {
                "revision": blueprint_target["revision"],
                "content_sha256": blueprint_target["content_sha256"],
            },
        }

        blueprint_secret = "a1" * 32
        blueprint_pair, blueprint_presentation = self._create_pending_pairing(
            blueprint_project,
            actions=["blueprint.commit_proposal", "blueprint.restore_revision"],
            secret=blueprint_secret,
        )
        blueprint_capability = self._approve_pairing(
            blueprint_pair,
            blueprint_presentation,
        )
        timeline_secret = "b2" * 32
        timeline_pair, timeline_presentation = self._create_pending_pairing(
            timeline_project,
            actions=["timeline.restore_revision"],
            secret=timeline_secret,
        )
        timeline_capability = self._approve_pairing(
            timeline_pair,
            timeline_presentation,
        )

        commit_only_secret = "c3" * 32
        commit_only_pair, commit_only_presentation = self._create_pending_pairing(
            blueprint_project,
            actions=["blueprint.commit_proposal"],
            secret=commit_only_secret,
        )
        commit_only_capability = self._approve_pairing(
            commit_only_pair,
            commit_only_presentation,
        )
        edit_only_secret = "d4" * 32
        edit_only_pair, edit_only_presentation = self._create_pending_pairing(
            timeline_project,
            actions=["timeline.apply_edit"],
            secret=edit_only_secret,
        )
        edit_only_capability = self._approve_pairing(
            edit_only_pair,
            edit_only_presentation,
        )

        def assert_unchanged(response, before, statuses: set[int]) -> None:
            self.assertIn(response.status_code, statuses, response.json)
            self.assertEqual(self._state_snapshot(), before)

        # Pairing intake binds the active database implicitly, an existing
        # project, and the exact current Blueprint head before any pending
        # runtime record or durable row can be created.
        injected_database = self._pairing_body(
            blueprint_project,
            actions=["blueprint.commit_proposal"],
            secret="e5" * 32,
        )
        injected_database["database_uuid"] = self.repository.database_uuid
        before = self._state_snapshot()
        assert_unchanged(
            self.client.post("/v1/agent/pairings", json=injected_database),
            before,
            {400},
        )

        missing_project = self._pairing_body(
            blueprint_project,
            actions=["blueprint.commit_proposal"],
            secret="e6" * 32,
        )
        missing_project["project_id"] = "missing_project_protocol_inventory"
        missing_project["observed_head"] = {
            "revision": 1,
            "content_sha256": "1" * 64,
        }
        before = self._state_snapshot()
        assert_unchanged(
            self.client.post("/v1/agent/pairings", json=missing_project),
            before,
            {404},
        )

        stale_pairing = self._pairing_body(
            blueprint_project,
            actions=["blueprint.commit_proposal"],
            secret="e7" * 32,
        )
        stale_pairing["observed_head"] = {
            **stale_pairing["observed_head"],
            "content_sha256": "f" * 64,
        }
        before = self._state_snapshot()
        stale_response = self.client.post("/v1/agent/pairings", json=stale_pairing)
        assert_unchanged(stale_response, before, {409})
        self.assertEqual(stale_response.json["code"], "blueprint_head_conflict")

        command_cases = (
            (
                "blueprint.commit_proposal",
                f"/v1/agent/creative/projects/{blueprint_project}/blueprint/commit",
                blueprint_project,
                commit_body,
                blueprint_capability,
                blueprint_secret,
            ),
            (
                "blueprint.restore_revision",
                f"/v1/agent/creative/projects/{blueprint_project}/blueprint/restore",
                blueprint_project,
                blueprint_restore_body,
                blueprint_capability,
                blueprint_secret,
            ),
            (
                "timeline.restore_revision",
                f"/v1/agent/creative/projects/{timeline_project}/timeline/restore",
                timeline_project,
                timeline_restore,
                timeline_capability,
                timeline_secret,
            ),
        )

        # Missing durable/runtime pairing proof cannot cross the route into a
        # service, regardless of which command body happens to be supplied.
        for action, path, _project_id, body, _capability, _secret in command_cases:
            with self.subTest(action=action, denial="missing-pairing"):
                before = self._state_snapshot()
                response = self.client.post(path, json=body)
                assert_unchanged(response, before, {401})
                self.assertEqual(response.json["code"], "agent_pairing_required")

        # Missing keys are signed as the actual empty header value so the proof
        # is otherwise genuine; key rebinding changes only the transmitted key.
        for action, path, project_id, body, capability, secret in command_cases:
            with self.subTest(action=action, denial="missing-idempotency"):
                before = self._state_snapshot()
                response = self._signed_agent_post(
                    capability_id=capability,
                    secret=secret,
                    action=action,
                    path=path,
                    project_id=project_id,
                    body=body,
                    key="",
                    include_key=False,
                )
                assert_unchanged(response, before, {400})
            with self.subTest(action=action, denial="rebound-idempotency"):
                before = self._state_snapshot()
                response = self._signed_agent_post(
                    capability_id=capability,
                    secret=secret,
                    action=action,
                    path=path,
                    project_id=project_id,
                    body=body,
                    key=f"{action.replace('.', '-')}-wire-key",
                    signed_key=f"{action.replace('.', '-')}-signed-key",
                )
                assert_unchanged(response, before, {401})
                self.assertEqual(response.json["code"], "agent_pairing_proof_invalid")

            with self.subTest(action=action, denial="database-binding"):
                before = self._state_snapshot()
                response = self._signed_agent_post(
                    capability_id=capability,
                    secret=secret,
                    action=action,
                    path=path,
                    project_id=project_id,
                    body=body,
                    key=f"{action.replace('.', '-')}-wrong-database",
                    signed_database_uuid="00000000-0000-4000-8000-000000000000",
                )
                assert_unchanged(response, before, {401})
                self.assertEqual(response.json["code"], "agent_pairing_proof_invalid")

        # Project scope is checked by the runtime capability before the command
        # nonce or canonical service can mutate anything.
        for action, _path, _project_id, body, capability, secret in command_cases:
            suffix = (
                "blueprint/commit"
                if action == "blueprint.commit_proposal"
                else "blueprint/restore"
                if action == "blueprint.restore_revision"
                else "timeline/restore"
            )
            cross_path = f"/v1/agent/creative/projects/{other_project}/{suffix}"
            with self.subTest(action=action, denial="cross-project"):
                before = self._state_snapshot()
                response = self._signed_agent_post(
                    capability_id=capability,
                    secret=secret,
                    action=action,
                    path=cross_path,
                    project_id=other_project,
                    body=body,
                    key=f"{action.replace('.', '-')}-cross-project",
                )
                assert_unchanged(response, before, {403})
                self.assertEqual(response.json["code"], "agent_pairing_scope_denied")

        # A capability for the adjacent action cannot be upgraded by choosing a
        # different route; the nonce itself remains bound to the admitted action.
        cross_action_cases = (
            (
                "blueprint.restore_revision",
                "blueprint.commit_proposal",
                commit_only_capability,
                commit_only_secret,
                f"/v1/agent/creative/projects/{blueprint_project}/blueprint/restore",
                blueprint_project,
                blueprint_restore_body,
            ),
            (
                "timeline.restore_revision",
                "timeline.apply_edit",
                edit_only_capability,
                edit_only_secret,
                f"/v1/agent/creative/projects/{timeline_project}/timeline/restore",
                timeline_project,
                timeline_restore,
            ),
        )
        for action, nonce_action, capability, secret, path, project_id, body in cross_action_cases:
            with self.subTest(action=action, denial="cross-action"):
                before = self._state_snapshot()
                response = self._signed_agent_post(
                    capability_id=capability,
                    secret=secret,
                    action=action,
                    nonce_action=nonce_action,
                    path=path,
                    project_id=project_id,
                    body=body,
                    key=f"{action.replace('.', '-')}-cross-action",
                )
                assert_unchanged(response, before, {403})
                self.assertEqual(response.json["code"], "agent_pairing_scope_denied")

        # Each CAS/resource binding is substituted independently under a valid
        # pairing, nonce, request proof, and idempotency key.
        stale_commit = copy.deepcopy(commit_body)
        stale_commit["expected_head"]["content_sha256"] = "f" * 64
        before = self._state_snapshot()
        response = self._signed_agent_post(
            capability_id=blueprint_capability,
            secret=blueprint_secret,
            action="blueprint.commit_proposal",
            path=f"/v1/agent/creative/projects/{blueprint_project}/blueprint/commit",
            project_id=blueprint_project,
            body=stale_commit,
            key="blueprint-commit-stale-head",
        )
        assert_unchanged(response, before, {409})

        stale_restore = copy.deepcopy(blueprint_restore_body)
        stale_restore["expected_head"]["content_sha256"] = "e" * 64
        before = self._state_snapshot()
        response = self._signed_agent_post(
            capability_id=blueprint_capability,
            secret=blueprint_secret,
            action="blueprint.restore_revision",
            path=f"/v1/agent/creative/projects/{blueprint_project}/blueprint/restore",
            project_id=blueprint_project,
            body=stale_restore,
            key="blueprint-restore-stale-head",
        )
        assert_unchanged(response, before, {409})

        wrong_blueprint_revision = copy.deepcopy(blueprint_restore_body)
        wrong_blueprint_revision["restore_from"]["content_sha256"] = "d" * 64
        before = self._state_snapshot()
        response = self._signed_agent_post(
            capability_id=blueprint_capability,
            secret=blueprint_secret,
            action="blueprint.restore_revision",
            path=f"/v1/agent/creative/projects/{blueprint_project}/blueprint/restore",
            project_id=blueprint_project,
            body=wrong_blueprint_revision,
            key="blueprint-restore-wrong-revision",
        )
        assert_unchanged(response, before, {409})

        timeline_variants = {
            "blueprint-head": ("expected_blueprint", "content_sha256"),
            "coverage-head": ("expected_coverage", "content_sha256"),
            "canonical-timeline-head": (
                "expected_timeline_head",
                "revision_sha256",
            ),
            "canonical-timeline-revision": ("restore_from", "revision_sha256"),
        }
        for scope, (section, field) in timeline_variants.items():
            stale_timeline_restore = copy.deepcopy(timeline_restore)
            stale_timeline_restore[section][field] = "c" * 64
            with self.subTest(action="timeline.restore_revision", scope=scope):
                before = self._state_snapshot()
                response = self._signed_agent_post(
                    capability_id=timeline_capability,
                    secret=timeline_secret,
                    action="timeline.restore_revision",
                    path=(
                        f"/v1/agent/creative/projects/{timeline_project}/timeline/restore"
                    ),
                    project_id=timeline_project,
                    body=stale_timeline_restore,
                    key=f"timeline-restore-{scope}",
                )
                assert_unchanged(response, before, {409})

        # Nonce issuance has its own closed capability, database, and action
        # scope.  The wrong-database case uses a real broker capability bound to
        # another database while Core still holds the current durable row.
        before = self._state_snapshot()
        unknown_nonce = self._issue_nonce(
            "capability_unknown_inventory",
            "timeline.restore_revision",
            timeline_secret,
        )
        assert_unchanged(unknown_nonce, before, {401})

        before = self._state_snapshot()
        wrong_action_nonce = self._issue_nonce(
            timeline_capability,
            "timeline.apply_edit",
            timeline_secret,
        )
        assert_unchanged(wrong_action_nonce, before, {403})
        self.assertEqual(wrong_action_nonce.json["code"], "agent_pairing_scope_denied")

        current_broker = self.app.extensions["agent_pairing_broker"]
        mismatched_broker = AgentPairingBroker(self.epoch)
        mismatched_body = self._pairing_body(
            timeline_project,
            actions=["timeline.restore_revision"],
            secret=timeline_secret,
        )
        blueprint_head = self.repository.get_blueprint_head(timeline_project)
        assert blueprint_head is not None
        mismatched_pending = mismatched_broker.create_pairing(
            mismatched_body,
            database_uuid="wrong_database_scope",
            observed_head=blueprint_head,
        )
        now = datetime.now(timezone.utc)
        mismatched_broker.activate_capability(
            str(mismatched_pending["pairing_id"]),
            capability_id=timeline_capability,
            issued_at=now,
            expires_at=now + timedelta(minutes=15),
            max_operations=32,
        )
        self.app.extensions["agent_pairing_broker"] = mismatched_broker
        try:
            before = self._state_snapshot()
            wrong_database_nonce = self._issue_nonce(
                timeline_capability,
                "timeline.restore_revision",
                timeline_secret,
            )
            assert_unchanged(wrong_database_nonce, before, {403})
            self.assertEqual(
                wrong_database_nonce.json["code"],
                "agent_pairing_scope_denied",
            )
        finally:
            self.app.extensions["agent_pairing_broker"] = current_broker

        # Durable revocation is evaluated before runtime proof or canonical
        # service dispatch for both Blueprint and Timeline commands.
        revoked_cases = []
        for project_id, actions, secret, action, path, body in (
            (
                blueprint_project,
                ["blueprint.commit_proposal", "blueprint.restore_revision"],
                "f6" * 32,
                "blueprint.commit_proposal",
                f"/v1/agent/creative/projects/{blueprint_project}/blueprint/commit",
                commit_body,
            ),
            (
                timeline_project,
                ["timeline.restore_revision"],
                "f7" * 32,
                "timeline.restore_revision",
                f"/v1/agent/creative/projects/{timeline_project}/timeline/restore",
                timeline_restore,
            ),
        ):
            pending, presentation = self._create_pending_pairing(
                project_id,
                actions=actions,
                secret=secret,
            )
            capability = self._approve_pairing(pending, presentation)
            revoke_presentation = self.client.get(
                f"/v1/main/agent/capabilities/{capability}/revoke/presentation",
                headers={MAIN_AUTHORITY_HEADER: self.main_token},
            )
            self.assertEqual(revoke_presentation.status_code, 200)
            self._sequence += 1
            revoked = self.client.post(
                f"/v1/main/agent/capabilities/{capability}/revoke",
                json={
                    "presentation": revoke_presentation.json["presentation"],
                    "presentation_sha256": revoke_presentation.json[
                        "presentation_sha256"
                    ],
                    "native_gesture_nonce": f"protocol_revoke_setup_{self._sequence}",
                },
                headers={MAIN_AUTHORITY_HEADER: self.main_token},
            )
            self.assertEqual(revoked.status_code, 200, revoked.json)
            revoked_cases.append((action, path, body, capability))

        for action, path, body, capability in revoked_cases:
            with self.subTest(action=action, denial="revoked"):
                before = self._state_snapshot()
                response = self.client.post(
                    path,
                    json=body,
                    headers={AGENT_CAPABILITY_HEADER: capability},
                )
                assert_unchanged(response, before, {403})
                self.assertEqual(response.json["code"], "agent_pairing_revoked")

    def test_main_mutations_deny_cross_target_presentation_project_and_head_without_writes(
        self,
    ) -> None:
        project_a, _restore_a = self._build_timeline_project("main-project-a")
        project_b, _restore_b = self._build_timeline_project("main-project-b")
        actions = [
            "blueprint.commit_proposal",
            "blueprint.restore_revision",
            "timeline.apply_edit",
            "timeline.restore_revision",
        ]
        secret_a = "71" * 32
        secret_b = "72" * 32
        pairing_a, pairing_presentation_a = self._create_pending_pairing(
            project_a,
            actions=actions,
            secret=secret_a,
        )
        pairing_b, pairing_presentation_b = self._create_pending_pairing(
            project_b,
            actions=actions,
            secret=secret_b,
        )

        def assert_unchanged(response, before, statuses: set[int]) -> None:
            self.assertIn(response.status_code, statuses, response.json)
            self.assertEqual(self._state_snapshot(), before)

        # Pairing approval/rejection accepts only the digest rebuilt for the
        # route target's database, project, Blueprint head, Timeline head, and
        # pairing facts.  A different valid presentation remains insufficient.
        for operation in ("approve", "reject"):
            path = (
                f"/v1/main/agent/pairings/{pairing_a['pairing_id']}/{operation}"
            )
            for label, digest in (
                (
                    "cross-project-head-presentation",
                    pairing_presentation_b["presentation_sha256"],
                ),
                ("forged-presentation", "0" * 64),
            ):
                with self.subTest(action=f"pairing-{operation}", denial=label):
                    before = self._state_snapshot()
                    response = self.client.post(
                        path,
                        json={
                            "presentation_sha256": digest,
                            "native_gesture_nonce": (
                                f"main_pairing_{operation}_{label.replace('-', '_')}"
                            ),
                        },
                        headers={MAIN_AUTHORITY_HEADER: self.main_token},
                    )
                    assert_unchanged(response, before, {409})

        broker = self.app.extensions["agent_pairing_broker"]
        wrong_database_body = self._pairing_body(
            project_a,
            actions=actions,
            secret="73" * 32,
        )
        head_a = self.repository.get_blueprint_head(project_a)
        assert head_a is not None
        wrong_database_pairing = broker.create_pairing(
            wrong_database_body,
            database_uuid="different_database_scope",
            observed_head=head_a,
        )
        for operation in ("approve", "reject"):
            with self.subTest(action=f"pairing-{operation}", denial="active-database"):
                before = self._state_snapshot()
                response = self.client.post(
                    (
                        "/v1/main/agent/pairings/"
                        f"{wrong_database_pairing['pairing_id']}/{operation}"
                    ),
                    json={
                        "presentation_sha256": "0" * 64,
                        "native_gesture_nonce": (
                            f"main_pairing_wrong_database_{operation}"
                        ),
                    },
                    headers={MAIN_AUTHORITY_HEADER: self.main_token},
                )
                assert_unchanged(response, before, {403})
                self.assertEqual(response.json["code"], "agent_pairing_scope_denied")

        capability_a = self._approve_pairing(pairing_a, pairing_presentation_a)
        capability_b = self._approve_pairing(pairing_b, pairing_presentation_b)
        revoke_a = self.client.get(
            f"/v1/main/agent/capabilities/{capability_a}/revoke/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        revoke_b = self.client.get(
            f"/v1/main/agent/capabilities/{capability_b}/revoke/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoke_a.status_code, 200, revoke_a.json)
        self.assertEqual(revoke_b.status_code, 200, revoke_b.json)

        capability_revoke_cases: list[tuple[str, str, dict[str, object], str]] = []
        capability_revoke_cases.append(
            (
                "cross-capability-presentation",
                capability_a,
                copy.deepcopy(revoke_b.json["presentation"]),
                str(revoke_b.json["presentation_sha256"]),
            )
        )
        capability_revoke_cases.append(
            (
                "cross-target",
                capability_b,
                copy.deepcopy(revoke_a.json["presentation"]),
                str(revoke_a.json["presentation_sha256"]),
            )
        )
        wrong_database_revoke = copy.deepcopy(revoke_a.json["presentation"])
        wrong_database_revoke["database_uuid"] = (
            "00000000-0000-4000-8000-000000000000"
        )
        capability_revoke_cases.append(
            (
                "active-database",
                capability_a,
                wrong_database_revoke,
                content_sha256(wrong_database_revoke),
            )
        )
        for label, capability_id, presentation, digest in capability_revoke_cases:
            with self.subTest(action="capability-revoke", denial=label):
                before = self._state_snapshot()
                response = self.client.post(
                    f"/v1/main/agent/capabilities/{capability_id}/revoke",
                    json={
                        "presentation": presentation,
                        "presentation_sha256": digest,
                        "native_gesture_nonce": (
                            f"main_capability_revoke_{label.replace('-', '_')}"
                        ),
                    },
                    headers={MAIN_AUTHORITY_HEADER: self.main_token},
                )
                assert_unchanged(response, before, {403, 409})

        confirm_presentation_path = (
            f"/v1/main/creative/projects/{project_a}/blueprint/authority/presentation"
        )
        confirm_path = (
            f"/v1/main/creative/projects/{project_a}/blueprint/authority/confirm"
        )
        confirm_presentation = self.client.post(
            confirm_presentation_path,
            json={"operation": "confirm", "decision_units": ["script"]},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(confirm_presentation.status_code, 200, confirm_presentation.json)

        def authority_body(envelope) -> dict[str, object]:
            self._sequence += 1
            return {
                "presentation": copy.deepcopy(envelope["presentation"]),
                "presentation_sha256": envelope["presentation_sha256"],
                "native_gesture_nonce": f"main_authority_case_{self._sequence}",
            }

        before = self._state_snapshot()
        missing_confirm_key = self.client.post(
            confirm_path,
            json=authority_body(confirm_presentation.json),
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        assert_unchanged(missing_confirm_key, before, {400})
        self.assertEqual(missing_confirm_key.json["code"], "invalid_idempotency_key")

        cross_project_confirm = authority_body(confirm_presentation.json)
        before = self._state_snapshot()
        response = self.client.post(
            f"/v1/main/creative/projects/{project_b}/blueprint/authority/confirm",
            json=cross_project_confirm,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-confirm-cross-project",
            },
        )
        assert_unchanged(response, before, {409})

        wrong_database_confirm = authority_body(confirm_presentation.json)
        wrong_database_confirm["presentation"]["database_uuid"] = (
            "00000000-0000-4000-8000-000000000000"
        )
        wrong_database_confirm["presentation_sha256"] = content_sha256(
            wrong_database_confirm["presentation"]
        )
        before = self._state_snapshot()
        response = self.client.post(
            confirm_path,
            json=wrong_database_confirm,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-confirm-wrong-database",
            },
        )
        assert_unchanged(response, before, {409, 422})

        wrong_unit_confirm = authority_body(confirm_presentation.json)
        wrong_unit_confirm["presentation"]["decision_units"][0][
            "semantic_sha256"
        ] = "f" * 64
        wrong_unit_confirm["presentation_sha256"] = content_sha256(
            wrong_unit_confirm["presentation"]
        )
        before = self._state_snapshot()
        response = self.client.post(
            confirm_path,
            json=wrong_unit_confirm,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-confirm-wrong-unit",
            },
        )
        assert_unchanged(response, before, {409, 422})

        old_confirm = authority_body(confirm_presentation.json)
        current = self.repository.get_blueprint_head(project_a)
        assert current is not None
        self.blueprints.commit_proposal(
            project_a,
            {
                "expected_head": {
                    "revision": current["revision"],
                    "content_sha256": current["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": "8" * 64,
                "semantic": semantic("main authority advanced once"),
            },
            idempotency_key="main-authority-advance-once",
        )
        before = self._state_snapshot()
        response = self.client.post(
            confirm_path,
            json=old_confirm,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-confirm-stale-head",
            },
        )
        assert_unchanged(response, before, {409})

        fresh_confirm_presentation = self.client.post(
            confirm_presentation_path,
            json={"operation": "confirm", "decision_units": ["script"]},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(fresh_confirm_presentation.status_code, 200)
        confirmed = self.client.post(
            confirm_path,
            json=authority_body(fresh_confirm_presentation.json),
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-confirm-setup-for-revoke",
            },
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.json)

        revoke_presentation_path = confirm_presentation_path
        revoke_path = (
            f"/v1/main/creative/projects/{project_a}/blueprint/authority/revoke"
        )
        blueprint_revoke_presentation = self.client.post(
            revoke_presentation_path,
            json={"operation": "revoke", "decision_units": ["script"]},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(
            blueprint_revoke_presentation.status_code,
            200,
            blueprint_revoke_presentation.json,
        )

        before = self._state_snapshot()
        missing_revoke_key = self.client.post(
            revoke_path,
            json=authority_body(blueprint_revoke_presentation.json),
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        assert_unchanged(missing_revoke_key, before, {400})

        wrong_database_blueprint_revoke = authority_body(
            blueprint_revoke_presentation.json
        )
        wrong_database_blueprint_revoke["presentation"]["database_uuid"] = (
            "00000000-0000-4000-8000-000000000000"
        )
        wrong_database_blueprint_revoke["presentation_sha256"] = content_sha256(
            wrong_database_blueprint_revoke["presentation"]
        )
        before = self._state_snapshot()
        response = self.client.post(
            revoke_path,
            json=wrong_database_blueprint_revoke,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-revoke-wrong-database",
            },
        )
        assert_unchanged(response, before, {409, 422})

        cross_project_revoke = authority_body(blueprint_revoke_presentation.json)
        before = self._state_snapshot()
        response = self.client.post(
            f"/v1/main/creative/projects/{project_b}/blueprint/authority/revoke",
            json=cross_project_revoke,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-revoke-cross-project",
            },
        )
        assert_unchanged(response, before, {409})

        old_revoke = authority_body(blueprint_revoke_presentation.json)
        current = self.repository.get_blueprint_head(project_a)
        assert current is not None
        self.blueprints.commit_proposal(
            project_a,
            {
                "expected_head": {
                    "revision": current["revision"],
                    "content_sha256": current["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": "9" * 64,
                "semantic": semantic("main authority advanced twice"),
            },
            idempotency_key="main-authority-advance-twice",
        )
        before = self._state_snapshot()
        response = self.client.post(
            revoke_path,
            json=old_revoke,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-revoke-stale-head",
            },
        )
        assert_unchanged(response, before, {409})

        # Project B remains fully current and provides a real canonical Export
        # presentation.  Each field substitution is rejected before any Export
        # operation/job/receipt or destination materialization can commit.
        export_presentation = self.client.post(
            f"/v1/main/creative/projects/{project_b}/exports/presentation",
            json={},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(export_presentation.status_code, 200, export_presentation.json)
        export_identity = self.export_root.stat()

        def export_body(envelope) -> dict[str, object]:
            self._sequence += 1
            return {
                "presentation": copy.deepcopy(envelope["presentation"]),
                "presentation_sha256": envelope["presentation_sha256"],
                "native_gesture_nonce": f"main_export_case_{self._sequence}",
                "destination": {
                    "canonical_path": str(self.export_root),
                    "package_basename": f"protocol-{self._sequence}",
                    "selection_identity": {
                        "device": str(export_identity.st_dev),
                        "inode": str(export_identity.st_ino),
                    },
                },
            }

        export_path = f"/v1/main/creative/projects/{project_b}/exports"
        before = self._state_snapshot()
        missing_export_key = self.client.post(
            export_path,
            json=export_body(export_presentation.json),
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        assert_unchanged(missing_export_key, before, {400})

        wrong_destination = export_body(export_presentation.json)
        wrong_destination["destination"]["selection_identity"]["inode"] = str(
            export_identity.st_ino + 1
        )
        before = self._state_snapshot()
        response = self.client.post(
            export_path,
            json=wrong_destination,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-export-wrong-destination",
            },
        )
        assert_unchanged(response, before, {400})

        export_variants: dict[str, dict[str, object]] = {}
        wrong_export_database = export_body(export_presentation.json)
        wrong_export_database["presentation"]["database_uuid"] = (
            "00000000-0000-4000-8000-000000000000"
        )
        wrong_export_database["presentation_sha256"] = content_sha256(
            wrong_export_database["presentation"]
        )
        export_variants["active-database"] = wrong_export_database

        wrong_export_head = export_body(export_presentation.json)
        wrong_export_head["presentation"]["timeline_binding"][
            "revision_sha256"
        ] = "a" * 64
        wrong_export_head["presentation_sha256"] = content_sha256(
            wrong_export_head["presentation"]
        )
        export_variants["canonical-timeline-head"] = wrong_export_head

        wrong_export_sources = export_body(export_presentation.json)
        wrong_export_sources["presentation"]["timeline_binding"][
            "source_bindings_sha256"
        ] = "b" * 64
        wrong_export_sources["presentation_sha256"] = content_sha256(
            wrong_export_sources["presentation"]
        )
        export_variants["canonical-source-set"] = wrong_export_sources

        for scope, body in export_variants.items():
            with self.subTest(action="canonical-export", scope=scope):
                before = self._state_snapshot()
                response = self.client.post(
                    export_path,
                    json=body,
                    headers={
                        MAIN_AUTHORITY_HEADER: self.main_token,
                        "Idempotency-Key": f"main-export-{scope}",
                    },
                )
                assert_unchanged(response, before, {400, 409})
                self.assertEqual(self.repository._prepared_export_roots, {})

        cross_project_export = export_body(export_presentation.json)
        before = self._state_snapshot()
        response = self.client.post(
            f"/v1/main/creative/projects/{project_a}/exports",
            json=cross_project_export,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "main-export-cross-project",
            },
        )
        assert_unchanged(response, before, {400, 409})
        self.assertEqual(self.repository._prepared_export_roots, {})
