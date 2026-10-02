from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from threading import Barrier
import time
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / ".agents" / "plugins" / "plugins" / "memolens"
PLUGIN_SCRIPTS = PLUGIN_ROOT / "scripts"
CLI_PATH = PLUGIN_SCRIPTS / "memolens_cli.py"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from backend.src.media.blueprint import BlueprintService  # noqa: E402
from core.db import ImageIndexRepository  # noqa: E402
from core.media_db import (  # noqa: E402
    BlueprintCommandResult,
    BlueprintHeadConflictError,
    BlueprintIntegrityError,
    MediaRepository,
    canonical_json,
    utc_now_iso,
)
from memolens_core import MemoLensGateway, TRUST_LOCAL_API_ENV  # noqa: E402
from memolens_mcp import TOOLS, call_tool  # noqa: E402


def semantic_fixture(label: str) -> dict[str, object]:
    return {
        "intent": {
            "goal": f"把本地素材剪成可编辑短片 {label}",
            "stance": "普通生活值得被认真观看",
            "audience": "个人创作者",
            "platform": "short-video",
        },
        "script": {
            "blocks": [
                {
                    "block_id": "opening",
                    "text": f"这些被忘记的画面，其实一直在等待一个故事。{label}",
                }
            ]
        },
        "direction": {
            "theme": "重新发现日常",
            "narrative_arc": "从零散素材到完整表达",
            "emotion": "温暖",
            "tone": "真诚",
            "pace": "先快后慢",
        },
        "output": {"duration_target_ms": 30_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def head_ref(head: dict[str, object]) -> dict[str, object]:
    return {
        "revision": head["revision"],
        "content_sha256": head["content_sha256"],
    }


class BlueprintStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-blueprint-model-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "index.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        project = self.repository.create_project(
            "Blueprint model",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "state-machine-test"},
        )
        self.project_id = str(project["id"])
        brief = self.repository.get_brief(self.project_id, 1)
        assert brief is not None
        self.legacy_ref = {
            "revision": 1,
            "content_sha256": str(brief["content_sha256"]),
        }
        self.service = BlueprintService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _counts(self) -> tuple[int, int, int]:
        with self.repository._connect() as connection:
            revisions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM creative_blueprint_revisions WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0]
            )
            operations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM creative_blueprint_operations WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0]
            )
            receipts = int(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_command_receipts WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0]
            )
        return revisions, operations, receipts

    def _call_recorded(
        self,
        command: tuple[str, dict[str, object], str, BlueprintCommandResult],
    ) -> BlueprintCommandResult:
        kind, payload, key, _original = command
        if kind == "commit":
            return self.service.commit_proposal(
                self.project_id,
                deepcopy(payload),
                idempotency_key=key,
            )
        return self.service.restore_revision(
            self.project_id,
            deepcopy(payload),
            idempotency_key=key,
        )

    def test_seeded_randomized_200_step_commit_restore_replay_conflict_model(self) -> None:
        expected_revisions = 0
        expected_operations = 0
        expected_receipts = 0
        model_head: dict[str, object] | None = None
        model_semantic: dict[str, object] | None = None
        semantics_by_revision: dict[int, dict[str, object]] = {}
        refs_by_revision: dict[int, dict[str, object]] = {}
        operation_ids: list[str] = []
        operation_kinds: list[str] = []
        revision_operation_ids: dict[int, str] = {}
        commands: list[tuple[str, dict[str, object], str, BlueprintCommandResult]] = []

        seed = 0xB10E_200
        randomizer = random.Random(seed)
        tail = (
            ["commit"] * 39
            + ["no_change"] * 40
            + ["replay"] * 40
            + ["conflict"] * 40
            + ["restore"] * 40
        )
        randomizer.shuffle(tail)
        action_plan = ["commit", *tail]
        action_counts = {
            "commit": 0,
            "no_change": 0,
            "replay": 0,
            "conflict": 0,
            "restore": 0,
        }

        for step, action in enumerate(action_plan):
            action_index = action_counts[action]
            action_counts[action] += 1
            if action == "commit":
                proposal = semantic_fixture(f"proposal-{action_index:03d}")
                payload: dict[str, object] = {
                    "expected_head": head_ref(model_head) if model_head is not None else None,
                    "initial_legacy_brief": self.legacy_ref if model_head is None else None,
                    "source_candidate_sha256": hashlib.sha256(
                        f"candidate:{action_index}".encode()
                    ).hexdigest(),
                    "semantic": proposal,
                }
                key = f"state-{step:03d}-commit"
                result = self.service.commit_proposal(
                    self.project_id,
                    deepcopy(payload),
                    idempotency_key=key,
                )
                self.assertFalse(result.replayed)
                self.assertEqual(result.response_status, 201)
                self.assertEqual(result.response["result"]["kind"], "revision_created")
                expected_revisions += 1
                expected_operations += 1
                expected_receipts += 1
                model_head = deepcopy(result.response["result"]["result_head"])
                model_semantic = proposal
                semantics_by_revision[expected_revisions] = deepcopy(proposal)
                refs_by_revision[expected_revisions] = head_ref(model_head)
                revision_operation_ids[expected_revisions] = result.operation_id
                operation_ids.append(result.operation_id)
                operation_kinds.append("revision_created")
                commands.append(("commit", deepcopy(payload), key, result))
            elif action == "no_change":
                assert model_head is not None and model_semantic is not None
                payload = {
                    "expected_head": head_ref(model_head),
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": hashlib.sha256(
                        f"no-change:{action_index}".encode()
                    ).hexdigest(),
                    "semantic": deepcopy(model_semantic),
                }
                key = f"state-{step:03d}-no-change"
                result = self.service.commit_proposal(
                    self.project_id,
                    deepcopy(payload),
                    idempotency_key=key,
                )
                self.assertFalse(result.replayed)
                self.assertEqual(result.response_status, 200)
                self.assertEqual(result.response["result"]["kind"], "no_change")
                self.assertEqual(result.response["result"]["result_head"], model_head)
                expected_operations += 1
                expected_receipts += 1
                operation_ids.append(result.operation_id)
                operation_kinds.append("no_change")
                commands.append(("commit", deepcopy(payload), key, result))
            elif action == "replay":
                original_command = commands[randomizer.randrange(len(commands))]
                original = original_command[3]
                replay = self._call_recorded(original_command)
                self.assertTrue(replay.replayed)
                self.assertEqual(replay.response, original.response)
                self.assertEqual(replay.response_status, original.response_status)
                self.assertEqual(replay.operation_id, original.operation_id)
                self.assertEqual(replay.resource_id, original.resource_id)
            elif action == "conflict":
                assert model_head is not None
                wrong_digest = "0" * 64
                if model_head["content_sha256"] == wrong_digest:
                    wrong_digest = "1" * 64
                stale = {
                    "revision": model_head["revision"],
                    "content_sha256": wrong_digest,
                }
                key = f"state-{step:03d}-conflict"
                with self.assertRaises(BlueprintHeadConflictError) as raised:
                    if action_index % 2 == 0:
                        self.service.commit_proposal(
                            self.project_id,
                            {
                                "expected_head": stale,
                                "initial_legacy_brief": None,
                                "source_candidate_sha256": None,
                                "semantic": deepcopy(model_semantic),
                            },
                            idempotency_key=key,
                        )
                    else:
                        self.service.restore_revision(
                            self.project_id,
                            {
                                "expected_head": stale,
                                "restore_from": deepcopy(refs_by_revision[1]),
                            },
                            idempotency_key=key,
                        )
                self.assertEqual(head_ref(raised.exception.current), head_ref(model_head))
            else:
                assert model_head is not None
                target_revision = randomizer.randint(1, expected_revisions)
                payload = {
                    "expected_head": head_ref(model_head),
                    "restore_from": deepcopy(refs_by_revision[target_revision]),
                }
                key = f"state-{step:03d}-restore"
                result = self.service.restore_revision(
                    self.project_id,
                    deepcopy(payload),
                    idempotency_key=key,
                )
                self.assertFalse(result.replayed)
                self.assertEqual(result.response_status, 201)
                self.assertEqual(result.response["result"]["kind"], "revision_restored")
                expected_revisions += 1
                expected_operations += 1
                expected_receipts += 1
                model_head = deepcopy(result.response["result"]["result_head"])
                model_semantic = deepcopy(semantics_by_revision[target_revision])
                semantics_by_revision[expected_revisions] = deepcopy(model_semantic)
                refs_by_revision[expected_revisions] = head_ref(model_head)
                revision_operation_ids[expected_revisions] = result.operation_id
                operation_ids.append(result.operation_id)
                operation_kinds.append("revision_restored")
                commands.append(("restore", deepcopy(payload), key, result))

            self.assertEqual(
                self._counts(),
                (expected_revisions, expected_operations, expected_receipts),
                f"ledger cardinality diverged after seeded step {step}",
            )

        self.assertEqual(set(action_plan), set(action_counts))
        self.assertEqual(set(action_counts.values()), {40})
        self.assertEqual((expected_revisions, expected_operations, expected_receipts), (80, 120, 120))
        assert model_head is not None
        explicit_head = self.repository.get_blueprint_head(self.project_id)
        assert explicit_head is not None
        self.assertEqual(head_ref(explicit_head), head_ref(model_head))
        self.assertEqual(explicit_head["semantic_sha256"], model_head["semantic_sha256"])
        self.assertEqual(explicit_head["operation_id"], model_head["operation_id"])

        with self.repository._connect() as connection:
            stored_head = connection.execute(
                "SELECT revision,content_sha256,semantic_sha256,operation_id "
                "FROM creative_blueprint_heads WHERE project_id=?",
                (self.project_id,),
            ).fetchone()
            maximum_revision = connection.execute(
                "SELECT MAX(revision) FROM creative_blueprint_revisions WHERE project_id=?",
                (self.project_id,),
            ).fetchone()[0]
        assert stored_head is not None
        self.assertEqual(int(stored_head["revision"]), expected_revisions)
        self.assertEqual(int(maximum_revision), expected_revisions)
        self.assertEqual(dict(stored_head), model_head)

        operations = self.repository.list_blueprint_operations(self.project_id, limit=500)
        self.assertEqual([row["sequence"] for row in operations], list(range(120, 0, -1)))
        self.assertEqual([row["id"] for row in reversed(operations)], operation_ids)
        self.assertEqual([row["result_kind"] for row in reversed(operations)], operation_kinds)
        self.assertEqual(operations[-1]["parent_operation_id"], None)
        for newer, older in zip(operations, operations[1:]):
            self.assertEqual(newer["parent_operation_id"], older["id"])

        previous: dict[str, object] | None = None
        for revision_number in range(1, expected_revisions + 1):
            row = self.repository.get_blueprint_revision(self.project_id, revision_number)
            assert row is not None
            self.assertEqual(row["revision"], revision_number)
            self.assertEqual(row["operation_id"], revision_operation_ids[revision_number])
            self.assertEqual(row["blueprint"]["semantic"], semantics_by_revision[revision_number])
            self.assertEqual(row["authority_state"], "unverified")
            self.assertEqual(row["blueprint"]["authority"]["state"], "unverified")
            if previous is None:
                self.assertIsNone(row["parent_revision"])
                self.assertIsNone(row["parent_content_sha256"])
            else:
                self.assertEqual(row["parent_revision"], previous["revision"])
                self.assertEqual(row["parent_content_sha256"], previous["content_sha256"])
            previous = row

        current_read = self.service.get(self.project_id).response
        exact_first = self.service.get(self.project_id, revision=1).response
        self.assertTrue(current_read["current"])
        self.assertFalse(exact_first["current"])
        self.assertEqual(current_read["revision"], expected_revisions)
        self.assertEqual(current_read["blueprint"]["semantic"], model_semantic)
        self.assertEqual(exact_first["blueprint"]["semantic"], semantics_by_revision[1])
        bounded_history = self.service.history(self.project_id, limit=100).response
        self.assertEqual(len(bounded_history["operations"]), 100)
        self.assertEqual(bounded_history["operations"][0]["sequence"], 120)
        self.assertEqual(bounded_history["operations"][-1]["sequence"], 21)
        self.assertFalse(bounded_history["ledger"]["complete_project_history"])

    def test_b2a_seeded_100_round_agent_commit_vs_ui_restore_cas_oracle(self) -> None:
        """One stale head may linearize once, even across unlike commands.

        This models an Agent proposing a new Blueprint while the UI restores a
        historical revision.  Replaying the winning command immediately after
        its response is "lost" must return the frozen receipt without another
        mutation, even though its expected head is stale by then.
        """

        seed = 0xB2A_CA5
        randomizer = random.Random(seed)
        anchor = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": hashlib.sha256(b"b2a-race-anchor").hexdigest(),
                "semantic": semantic_fixture("b2a-race-anchor"),
            },
            idempotency_key="b2a-race-anchor",
        )
        current_head = deepcopy(anchor.response["result"]["result_head"])
        anchor_ref = head_ref(current_head)
        database_uuid = str(self.service.get(self.project_id).response["database_uuid"])
        winner_counts = {"agent_commit": 0, "ui_restore": 0}
        conflict_count = 0
        losing_keys: list[str] = []

        for round_number in range(100):
            expected = head_ref(current_head)
            agent_payload: dict[str, object] = {
                "expected_head": deepcopy(expected),
                "initial_legacy_brief": None,
                "source_candidate_sha256": hashlib.sha256(
                    f"b2a-agent:{seed}:{round_number}".encode()
                ).hexdigest(),
                "semantic": semantic_fixture(f"b2a-agent-{round_number:03d}"),
            }
            restore_payload: dict[str, object] = {
                "expected_head": deepcopy(expected),
                "restore_from": deepcopy(anchor_ref),
            }
            command_inputs = {
                "agent_commit": (
                    agent_payload,
                    f"b2a-race-{round_number:03d}-agent",
                ),
                "ui_restore": (
                    restore_payload,
                    f"b2a-race-{round_number:03d}-restore",
                ),
            }
            starting_gate = Barrier(2)

            def contender(kind: str) -> dict[str, object]:
                payload, idempotency_key = command_inputs[kind]
                starting_gate.wait(timeout=5)
                try:
                    if kind == "agent_commit":
                        result = self.service.commit_proposal(
                            self.project_id,
                            deepcopy(payload),
                            idempotency_key=idempotency_key,
                        )
                    else:
                        result = self.service.restore_revision(
                            self.project_id,
                            deepcopy(payload),
                            idempotency_key=idempotency_key,
                            expected_database_uuid=database_uuid,
                        )
                    return {
                        "kind": kind,
                        "status": "committed",
                        "result": result,
                        "key": idempotency_key,
                        "payload": payload,
                    }
                except BlueprintHeadConflictError as exc:
                    return {
                        "kind": kind,
                        "status": "conflict",
                        "current": head_ref(exc.current),
                        "key": idempotency_key,
                    }

            submission_order = ["agent_commit", "ui_restore"]
            randomizer.shuffle(submission_order)
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(contender, submission_order))

            self.assertEqual(
                sorted(str(item["status"]) for item in outcomes),
                ["committed", "conflict"],
                f"seed={seed} round={round_number}",
            )
            winner = next(item for item in outcomes if item["status"] == "committed")
            loser = next(item for item in outcomes if item["status"] == "conflict")
            result = winner["result"]
            assert isinstance(result, BlueprintCommandResult)
            self.assertFalse(result.replayed)
            self.assertEqual(result.response_status, 201)
            winner_head = result.response["result"]["result_head"]
            self.assertEqual(int(winner_head["revision"]), int(expected["revision"]) + 1)
            self.assertEqual(loser["current"], head_ref(winner_head))
            winner_counts[str(winner["kind"])] += 1
            conflict_count += 1
            losing_keys.append(str(loser["key"]))

            expected_cardinality = round_number + 2
            self.assertEqual(
                self._counts(),
                (expected_cardinality, expected_cardinality, expected_cardinality),
            )
            persisted = self.repository.get_blueprint_head(self.project_id)
            assert persisted is not None
            self.assertEqual(head_ref(persisted), head_ref(winner_head))

            before_replay = self._counts()
            if winner["kind"] == "agent_commit":
                replay = self.service.commit_proposal(
                    self.project_id,
                    deepcopy(winner["payload"]),
                    idempotency_key=str(winner["key"]),
                )
            else:
                replay = self.service.restore_revision(
                    self.project_id,
                    deepcopy(winner["payload"]),
                    idempotency_key=str(winner["key"]),
                    expected_database_uuid=database_uuid,
                )
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.response_status, result.response_status)
            self.assertEqual(replay.response, result.response)
            self.assertEqual(replay.operation_id, result.operation_id)
            self.assertEqual(self._counts(), before_replay)
            current_head = deepcopy(winner_head)

        self.assertEqual(sum(winner_counts.values()), 100)
        self.assertGreater(winner_counts["agent_commit"], 0)
        self.assertGreater(winner_counts["ui_restore"], 0)
        self.assertEqual(conflict_count, 100)
        self.assertEqual(self._counts(), (101, 101, 101))

        with self.repository._connect() as connection:
            receipt_keys = {
                str(row[0])
                for row in connection.execute(
                    "SELECT idempotency_key FROM blueprint_command_receipts WHERE project_id=?",
                    (self.project_id,),
                ).fetchall()
            }
            maximum_revision = int(
                connection.execute(
                    "SELECT MAX(revision) FROM creative_blueprint_revisions WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0]
            )
        self.assertTrue(set(losing_keys).isdisjoint(receipt_keys))
        self.assertEqual(maximum_revision, 101)

        previous: dict[str, object] | None = None
        for revision_number in range(1, 102):
            revision = self.repository.get_blueprint_revision(
                self.project_id,
                revision_number,
            )
            assert revision is not None
            if previous is None:
                self.assertIsNone(revision["parent_revision"])
                self.assertIsNone(revision["parent_content_sha256"])
            else:
                self.assertEqual(revision["parent_revision"], previous["revision"])
                self.assertEqual(
                    revision["parent_content_sha256"],
                    previous["content_sha256"],
                )
            previous = revision

        operations = self.repository.list_blueprint_operations(self.project_id, limit=500)
        self.assertEqual(
            [operation["sequence"] for operation in operations],
            list(range(101, 0, -1)),
        )
        print(
            "B2A_CAS_PROFILE "
            + json.dumps(
                {
                    "seed": seed,
                    "rounds": 100,
                    "agent_commit_wins": winner_counts["agent_commit"],
                    "ui_restore_wins": winner_counts["ui_restore"],
                    "conflicts": conflict_count,
                    "same_key_replays": 100,
                    "silent_overwrites": 0,
                    "final_revision": 101,
                },
                sort_keys=True,
            )
        )

    def test_workspace_read_uses_one_transaction_scoped_blueprint_attestation(self) -> None:
        self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "d" * 64,
                "semantic": semantic_fixture("workspace-attestation"),
            },
            idempotency_key="workspace-attestation",
        )

        with (
            patch.object(
                MediaRepository,
                "_validate_blueprint_revision_chain",
                wraps=MediaRepository._validate_blueprint_revision_chain,
            ) as revision_audit,
            patch.object(
                MediaRepository,
                "_validate_blueprint_receipt_ledger",
                wraps=MediaRepository._validate_blueprint_receipt_ledger,
            ) as receipt_audit,
            patch.object(
                MediaRepository,
                "_validate_authority_ledger",
                wraps=MediaRepository._validate_authority_ledger,
            ) as authority_audit,
        ):
            response = self.service.project_workspace(self.project_id, limit=100).response
        self.assertEqual(response["project"]["current_blueprint"]["revision"], 1)
        self.assertEqual(revision_audit.call_count, 1)
        self.assertEqual(receipt_audit.call_count, 1)
        self.assertEqual(authority_audit.call_count, 1)

        with self.repository.transaction() as connection:
            snapshot = self.repository.read_blueprint_workspace_ledger_in_transaction(
                connection,
                self.project_id,
                limit=100,
            )
            self.assertIsNotNone(snapshot)
        assert snapshot is not None
        with self.repository.transaction() as other_connection:
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    other_connection,
                    self.project_id,
                    validated_blueprint_ledger=snapshot,
                )

    def test_workspace_attestation_rejects_same_connection_later_transactions(
        self,
    ) -> None:
        self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "e" * 64,
                "semantic": semantic_fixture("transaction-epoch"),
            },
            idempotency_key="workspace-transaction-epoch",
        )
        connection = self.repository._connect()
        try:
            connection.execute("BEGIN")
            rollback_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert rollback_snapshot is not None
            rollback_epoch = rollback_snapshot.transaction_epoch
            connection.rollback()
            connection.execute("BEGIN")
            self.assertGreater(
                connection._blueprint_read_transaction_epoch,
                rollback_epoch,
            )
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=rollback_snapshot,
                )
            connection.rollback()

            connection.execute("BEGIN")
            commit_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert commit_snapshot is not None
            commit_epoch = commit_snapshot.transaction_epoch
            connection.commit()
            connection.execute("BEGIN")
            self.assertGreater(
                connection._blueprint_read_transaction_epoch,
                commit_epoch,
            )
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=commit_snapshot,
                )
            connection.rollback()

            connection.execute("CREATE TEMP TABLE epoch_probe(value INTEGER)")
            connection.execute("BEGIN")
            implicit_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert implicit_snapshot is not None
            connection.execute("ROLLBACK")
            connection.execute("INSERT INTO epoch_probe VALUES(1)")
            self.assertTrue(connection.in_transaction)
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=implicit_snapshot,
                )
            connection.rollback()

            connection.execute("BEGIN")
            script_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert script_snapshot is not None
            connection.executescript("SELECT 1; BEGIN;")
            self.assertTrue(connection.in_transaction)
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=script_snapshot,
                )
            connection.rollback()

            connection.execute("BEGIN")
            cursor_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert cursor_snapshot is not None
            cursor = connection.cursor()
            try:
                cursor.execute("COMMIT")
                cursor.execute("BEGIN")
            finally:
                cursor.close()
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=cursor_snapshot,
                )
            connection.rollback()

            connection.execute("BEGIN")
            executemany_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert executemany_snapshot is not None
            connection.rollback()
            connection.executemany(
                "INSERT INTO epoch_probe VALUES(?)",
                [(2,), (3,)],
            )
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=executemany_snapshot,
                )
            connection.rollback()

            connection.execute("BEGIN")
            context_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert context_snapshot is not None
            with connection:
                pass
            connection.execute("BEGIN")
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=context_snapshot,
                )
            connection.rollback()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

        closed_connection = self.repository._connect()
        try:
            closed_connection.execute("BEGIN")
            closed_snapshot = (
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    closed_connection,
                    self.project_id,
                    limit=100,
                )
            )
            assert closed_snapshot is not None
            closed_connection.close()
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    closed_connection,
                    self.project_id,
                    validated_blueprint_ledger=closed_snapshot,
                )
        finally:
            closed_connection.close()

    def test_workspace_attestation_seals_nested_cache_values(self) -> None:
        expected_semantic = semantic_fixture("sealed-cache")
        committed = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "f" * 64,
                "semantic": expected_semantic,
            },
            idempotency_key="workspace-sealed-cache",
        )
        operation_id = committed.operation_id

        with self.repository.transaction() as connection:
            snapshot = self.repository.read_blueprint_workspace_ledger_in_transaction(
                connection,
                self.project_id,
                limit=100,
            )
            assert snapshot is not None

            forged_head = snapshot.head
            forged_blueprint = forged_head["blueprint"]
            assert isinstance(forged_blueprint, dict)
            forged_semantic = forged_blueprint["semantic"]
            assert isinstance(forged_semantic, dict)
            forged_script = forged_semantic["script"]
            assert isinstance(forged_script, dict)
            forged_script["blocks"] = [
                {"block_id": "forged", "text": "forged authority payload"}
            ]

            forged_operations = snapshot.operations
            self.assertEqual(len(forged_operations), 1)
            forged_operations[0]["typed_diff"] = []

            document_material = snapshot.validation_context.documents_by_revision[1]
            operation_material = snapshot.validation_context.operations_by_id[operation_id]
            self.assertIsInstance(document_material[1], str)
            self.assertIsInstance(operation_material[1], str)
            decoded_document = json.loads(document_material[1])
            decoded_operation = json.loads(operation_material[1])
            decoded_document["semantic"]["script"] = {"blocks": []}
            decoded_operation["typed_diff"] = []
            with self.assertRaises(TypeError):
                snapshot.validation_context.documents_by_revision[1] = document_material
            with self.assertRaises(TypeError):
                snapshot.validation_context.operations_by_id[operation_id] = operation_material
            with self.assertRaises(AttributeError):
                snapshot.validation_context = snapshot.validation_context
            with self.assertRaises(AttributeError):
                snapshot.head_json = canonical_json(forged_head)

            self.assertEqual(
                snapshot.head["blueprint"]["semantic"]["script"],
                expected_semantic["script"],
            )
            self.assertNotEqual(snapshot.operations[0]["typed_diff"], [])
            authority = self.repository._validate_authority_ledger(
                connection,
                self.project_id,
                validated_blueprint_ledger=snapshot,
            )
            self.assertEqual(
                authority.current_payloads["script"],
                expected_semantic["script"],
            )

    def test_workspace_attestation_rejects_replaced_snapshot_fields(self) -> None:
        first = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "1" * 64,
                "semantic": semantic_fixture("replace-first"),
            },
            idempotency_key="workspace-replace-first",
        )
        second = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": head_ref(first.response["result"]["result_head"]),
                "initial_legacy_brief": None,
                "source_candidate_sha256": "2" * 64,
                "semantic": semantic_fixture("replace-second"),
            },
            idempotency_key="workspace-replace-second",
        )
        self.assertEqual(second.response["result"]["result_head"]["revision"], 2)

        with self.repository.transaction() as connection:
            snapshot = self.repository.read_blueprint_workspace_ledger_in_transaction(
                connection,
                self.project_id,
                limit=100,
            )
            assert snapshot is not None
            first_revision = self.repository.get_blueprint_revision_in_transaction(
                connection,
                self.project_id,
                1,
                _validate_ledger=False,
            )
            assert first_revision is not None
            first_revision["head_updated_at"] = snapshot.head["head_updated_at"]
            forged = replace(
                snapshot,
                head_json=canonical_json(first_revision),
                revision_count=1,
                operation_count=1,
                operation_jsons=(snapshot.operation_jsons[-1],),
            )
            forged = replace(
                forged,
                seal_sha256=self.repository._blueprint_read_snapshot_seal_sha256(
                    forged
                ),
            )
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=forged,
                )

    def test_workspace_attestation_rejects_temp_ledger_shadows(self) -> None:
        self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "3" * 64,
                "semantic": semantic_fixture("temp-shadow"),
            },
            idempotency_key="workspace-temp-shadow",
        )
        epoch = "workspace_temp_shadow_epoch"
        presentation = self.repository.build_blueprint_authority_presentation(
            self.project_id,
            authority_operation="confirm",
            decision_units=["script"],
            runtime_authority_epoch=epoch,
        )
        self.repository.confirm_blueprint_decision_units(
            self.project_id,
            runtime_authority_epoch=epoch,
            presentation=presentation["presentation"],
            presentation_sha256=str(presentation["presentation_sha256"]),
            native_gesture_nonce="workspace_temp_shadow_gesture",
            idempotency_key="workspace-temp-shadow-authority",
        )

        with self.repository.transaction() as connection:
            snapshot = self.repository.read_blueprint_workspace_ledger_in_transaction(
                connection,
                self.project_id,
                limit=100,
            )
            assert snapshot is not None
            original = self.repository._validate_authority_ledger(
                connection,
                self.project_id,
                validated_blueprint_ledger=snapshot,
            )
            self.assertEqual(original.current_projection["state"], "partially_confirmed")
            self.assertEqual(original.current_projection["confirmed_decision_unit_count"], 1)
            issued_epoch = snapshot.transaction_epoch
            total_changes = connection.total_changes
            for table in (
                "blueprint_decision_authority_events",
                "blueprint_decision_authority_receipts",
                "blueprint_decision_authority_heads",
            ):
                connection.execute(
                    f"""CREATE TEMP TABLE {table} AS
                         SELECT * FROM main.{table} WHERE 0"""
                )
            self.assertGreater(connection._blueprint_read_transaction_epoch, issued_epoch)
            self.assertEqual(connection.total_changes, total_changes)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_decision_authority_events"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM main.blueprint_decision_authority_events"
                ).fetchone()[0],
                1,
            )
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_read_attestation_invalid",
            ):
                self.repository._validate_authority_ledger(
                    connection,
                    self.project_id,
                    validated_blueprint_ledger=snapshot,
                )

        with self.repository.transaction() as connection:
            for table in (
                "creative_blueprint_heads",
                "creative_blueprint_revisions",
                "creative_blueprint_operations",
            ):
                connection.execute(
                    f"""CREATE TEMP TABLE {table} AS
                         SELECT * FROM main.{table} WHERE 0"""
                )
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_temp_schema_shadow_forbidden",
            ):
                self.repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    self.project_id,
                    limit=100,
                )

    def test_b2a_workspace_100_by_100_by_100_read_profile(self) -> None:
        """Hold the frozen B2A fixture to the local warm-read p95 budget."""

        seed = 0xB2A_100
        fixture_contract = {
            "object": "memolens.b2a_workspace_profile_fixture",
            "schema_version": "1",
            "oracle": "b2a_workspace_100_blueprint_100_authority_100_legacy",
            "seed": seed,
            "blueprint": {
                "revisions": 100,
                "semantic_label_pattern": "workspace-{revision:03d}",
                "all_revision_creating": True,
            },
            "authority": {
                "events": 100,
                "operation_pattern": ["confirm", "revoke"],
                "decision_units": ["script"],
            },
            "legacy": {"briefs": 50, "timelines": 50, "timeline_revision": 1},
            "read": {
                "history_limit": 100,
                "warmups": 3,
                "samples": 25,
                "clock": "time.perf_counter",
                "round_decimals": 3,
            },
        }
        fixture_contract_sha256 = hashlib.sha256(
            canonical_json(fixture_contract).encode()
        ).hexdigest()
        self.assertEqual(
            fixture_contract_sha256,
            "ba231276b0cf32be024eb3dadfb1a2ac51343d17a2b78aa09c142287ee011afa",
        )
        blueprint_contract = fixture_contract["blueprint"]
        authority_contract = fixture_contract["authority"]
        legacy_contract = fixture_contract["legacy"]
        read_contract = fixture_contract["read"]
        assert isinstance(blueprint_contract, dict)
        assert isinstance(authority_contract, dict)
        assert isinstance(legacy_contract, dict)
        assert isinstance(read_contract, dict)
        blueprint_revisions = int(blueprint_contract["revisions"])
        authority_events = int(authority_contract["events"])
        legacy_briefs = int(legacy_contract["briefs"])
        legacy_timelines = int(legacy_contract["timelines"])
        history_limit = int(read_contract["history_limit"])
        warmups = int(read_contract["warmups"])
        sample_count = int(read_contract["samples"])
        round_decimals = int(read_contract["round_decimals"])
        initial = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": hashlib.sha256(
                    f"workspace:{seed}:0".encode()
                ).hexdigest(),
                "semantic": semantic_fixture("workspace-000"),
            },
            idempotency_key="b2a-workspace-000",
        )
        current_head = deepcopy(initial.response["result"]["result_head"])
        for revision_number in range(1, blueprint_revisions):
            committed = self.service.commit_proposal(
                self.project_id,
                {
                    "expected_head": head_ref(current_head),
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": hashlib.sha256(
                        f"workspace:{seed}:{revision_number}".encode()
                    ).hexdigest(),
                    "semantic": semantic_fixture(
                        f"workspace-{revision_number:03d}"
                    ),
                },
                idempotency_key=f"b2a-workspace-{revision_number:03d}",
            )
            current_head = deepcopy(committed.response["result"]["result_head"])

        for event_number in range(authority_events):
            operation = "confirm" if event_number % 2 == 0 else "revoke"
            epoch = f"b2a_workspace_epoch_{event_number:03d}"
            presentation = self.repository.build_blueprint_authority_presentation(
                self.project_id,
                authority_operation=operation,
                decision_units=["script"],
                runtime_authority_epoch=epoch,
            )
            command = (
                self.repository.confirm_blueprint_decision_units
                if operation == "confirm"
                else self.repository.revoke_blueprint_decision_units
            )
            result = command(
                self.project_id,
                runtime_authority_epoch=epoch,
                presentation=presentation["presentation"],
                presentation_sha256=str(presentation["presentation_sha256"]),
                native_gesture_nonce=f"b2a_workspace_gesture_{event_number:03d}",
                idempotency_key=f"b2a-workspace-authority-{event_number:03d}",
            )
            self.assertFalse(result.replayed)

        with self.repository.transaction(immediate=True) as connection:
            for brief_revision in range(2, legacy_briefs + 1):
                brief = {
                    "goal": f"B2A legacy brief {brief_revision:03d}",
                    "candidate_refs": [],
                }
                serialized = canonical_json(brief)
                connection.execute(
                    "INSERT INTO creative_briefs VALUES(?,?,?,?,?,?)",
                    (
                        self.project_id,
                        brief_revision,
                        serialized,
                        hashlib.sha256(serialized.encode()).hexdigest(),
                        canonical_json(
                            {
                                "created_by": "b2a_workspace_profile",
                                "seed": seed,
                            }
                        ),
                        utc_now_iso(),
                    ),
                )
            for timeline_number in range(legacy_timelines):
                timeline_id = f"b2a_workspace_timeline_{timeline_number:03d}"
                self.repository.save_timeline(
                    timeline_id=timeline_id,
                    project_id=self.project_id,
                    revision=1,
                    timeline={
                        "id": timeline_id,
                        "project_id": self.project_id,
                        "revision": 1,
                        "tracks": [],
                    },
                    provenance={
                        "brief_revision": 1,
                        "created_by": "b2a_workspace_profile",
                        "seed": seed,
                    },
                    validation_status="valid",
                    connection=connection,
                )

        for _ in range(warmups):
            self.service.project_workspace(self.project_id, limit=history_limit)
        samples_ms: list[float] = []
        final_workspace: dict[str, object] | None = None
        for _ in range(sample_count):
            started = time.perf_counter()
            response = self.service.project_workspace(
                self.project_id,
                limit=history_limit,
            ).response
            samples_ms.append((time.perf_counter() - started) * 1_000)
            project = response["project"]
            assert isinstance(project, dict)
            final_workspace = project

        assert final_workspace is not None
        history = final_workspace["history"]
        assert isinstance(history, dict)
        blueprint_lane = history["blueprint"]
        authority_lane = history["decision_authority"]
        legacy_lane = history["legacy_artifacts"]
        assert isinstance(blueprint_lane, dict)
        assert isinstance(authority_lane, dict)
        assert isinstance(legacy_lane, dict)
        self.assertEqual(blueprint_lane["available_revision_count"], 100)
        self.assertEqual(blueprint_lane["available_operation_count"], 100)
        self.assertEqual(len(blueprint_lane["operations"]), 100)
        self.assertEqual(authority_lane["available_event_count"], 100)
        self.assertEqual(len(authority_lane["events"]), 100)
        self.assertEqual(legacy_lane["brief_count"], 50)
        self.assertEqual(legacy_lane["timeline_revision_count"], 50)
        self.assertEqual(
            len(legacy_lane["briefs"]) + len(legacy_lane["timelines"]),
            100,
        )

        ordered = sorted(samples_ms)
        rounded_samples = [round(sample, round_decimals) for sample in samples_ms]
        profile = {
            "oracle": fixture_contract["oracle"],
            "seed": seed,
            "samples": len(samples_ms),
            "fixture_contract_sha256": fixture_contract_sha256,
            "raw_samples_ms": rounded_samples,
            "p50_ms": round(
                ordered[math.ceil(len(ordered) * 0.50) - 1],
                round_decimals,
            ),
            "p95_ms": round(
                ordered[math.ceil(len(ordered) * 0.95) - 1],
                round_decimals,
            ),
            "max_ms": round(max(ordered), round_decimals),
        }
        self.assertEqual(len(rounded_samples), 25)
        self.assertLessEqual(profile["p95_ms"], 500.0)
        print(f"B2A_WORKSPACE_PROFILE {json.dumps(profile, sort_keys=True)}")

    def _cli(self, *arguments: str) -> dict[str, object]:
        environment = os.environ.copy()
        environment[TRUST_LOCAL_API_ENV] = "0"
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--db",
                str(self.db_path),
                *arguments,
            ],
            cwd=self.root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stderr, "")
        value = json.loads(completed.stdout)
        assert isinstance(value, dict)
        return value

    def test_real_core_writes_have_gateway_cli_mcp_current_exact_history_parity(self) -> None:
        first_semantic = semantic_fixture("parity-first")
        first_payload = {
            "expected_head": None,
            "initial_legacy_brief": self.legacy_ref,
            "source_candidate_sha256": "a" * 64,
            "semantic": first_semantic,
        }
        first = self.service.commit_proposal(
            self.project_id,
            first_payload,
            idempotency_key="parity-first",
        )
        first_head = deepcopy(first.response["result"]["result_head"])
        self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": head_ref(first_head),
                "initial_legacy_brief": None,
                "source_candidate_sha256": "b" * 64,
                "semantic": deepcopy(first_semantic),
            },
            idempotency_key="parity-no-change",
        )
        second_semantic = semantic_fixture("parity-second")
        second = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": head_ref(first_head),
                "initial_legacy_brief": None,
                "source_candidate_sha256": "c" * 64,
                "semantic": second_semantic,
            },
            idempotency_key="parity-second",
        )
        second_head = deepcopy(second.response["result"]["result_head"])
        restored = self.service.restore_revision(
            self.project_id,
            {
                "expected_head": head_ref(second_head),
                "restore_from": head_ref(first_head),
            },
            idempotency_key="parity-restore",
        )
        self.assertEqual(restored.response["result"]["result_head"]["revision"], 3)
        self.assertEqual(self._counts(), (3, 4, 4))

        with patch.dict(
            os.environ,
            {TRUST_LOCAL_API_ENV: "0", "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0"},
            clear=False,
        ):
            gateway = MemoLensGateway(
                db_path=self.db_path,
                base_url="http://must-never-resolve.invalid:1",
            )

        gateway_current = gateway.blueprint_get(self.project_id)
        gateway_exact = gateway.blueprint_get(self.project_id, revision=2)
        gateway_history = gateway.blueprint_history(self.project_id, limit=50)

        mcp_current = call_tool(
            "memolens_blueprint_get",
            {"project_id": self.project_id},
            gateway,
        )
        mcp_exact = call_tool(
            "memolens_blueprint_get",
            {"project_id": self.project_id, "revision": 2},
            gateway,
        )
        mcp_history = call_tool(
            "memolens_blueprint_history",
            {"project_id": self.project_id, "limit": 50},
            gateway,
        )

        cli_current = self._cli("blueprint-get", self.project_id)
        cli_exact = self._cli("blueprint-get", self.project_id, "--revision", "2")
        cli_history = self._cli("blueprint-history", self.project_id, "--limit", "50")

        self.assertEqual(mcp_current, gateway_current)
        self.assertEqual(mcp_exact, gateway_exact)
        self.assertEqual(mcp_history, gateway_history)
        self.assertEqual(cli_current, gateway_current)
        self.assertEqual(cli_exact, gateway_exact)
        self.assertEqual(cli_history, gateway_history)

        core_current = self.service.get(self.project_id).response
        core_exact = self.service.get(self.project_id, revision=2).response
        self.assertEqual(gateway_current["selection"]["kind"], "current")
        self.assertEqual(gateway_current["selection"]["current_head"]["revision"], 3)
        self.assertEqual(gateway_current["blueprint"]["semantic"], core_current["blueprint"]["semantic"])
        self.assertEqual(gateway_exact["selection"]["kind"], "exact_revision")
        self.assertEqual(gateway_exact["blueprint"]["semantic"], core_exact["blueprint"]["semantic"])
        self.assertFalse(
            gateway_current["blueprint"]["authority_summary"][
                "all_decision_units_confirmed"
            ]
        )
        self.assertEqual(gateway_history["result_count"], 4)
        self.assertEqual(
            [item["result_kind"] for item in gateway_history["operations"]],
            ["revision_restored", "revision_created", "no_change", "revision_created"],
        )
        self.assertEqual(gateway_history["current_head"], gateway_current["selection"]["current_head"])
        self.assertFalse(gateway_history["complete_project_history"])
        self.assertEqual(self._counts(), (3, 4, 4))

    def test_native_decision_authority_projects_through_gateway_cli_mcp_and_resume(self) -> None:
        first_semantic = semantic_fixture("authority-first")
        first = self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "d" * 64,
                "semantic": first_semantic,
            },
            idempotency_key="authority-first",
        )
        first_head = deepcopy(first.response["result"]["result_head"])
        confirmation = self.repository.build_blueprint_authority_presentation(
            self.project_id,
            authority_operation="confirm",
            decision_units=["script", "output"],
            runtime_authority_epoch="plugin_projection_epoch",
        )
        self.repository.confirm_blueprint_decision_units(
            self.project_id,
            runtime_authority_epoch="plugin_projection_epoch",
            presentation=confirmation["presentation"],
            presentation_sha256=str(confirmation["presentation_sha256"]),
            native_gesture_nonce="plugin_projection_confirm",
            idempotency_key="plugin-projection-confirm",
        )

        second_semantic = deepcopy(first_semantic)
        second_semantic["script"]["blocks"][0]["text"] = "changed for revision two"
        self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": head_ref(first_head),
                "initial_legacy_brief": None,
                "source_candidate_sha256": "e" * 64,
                "semantic": second_semantic,
            },
            idempotency_key="authority-second",
        )

        with patch.dict(
            os.environ,
            {TRUST_LOCAL_API_ENV: "0", "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0"},
            clear=False,
        ):
            gateway = MemoLensGateway(
                db_path=self.db_path,
                base_url="http://must-never-resolve.invalid:1",
            )

        current = gateway.blueprint_get(self.project_id)
        historical = call_tool(
            "memolens_blueprint_get",
            {"project_id": self.project_id, "revision": 1},
            gateway,
        )
        history = gateway.blueprint_history(self.project_id, limit=10)
        resume = gateway.project_open(self.project_id)
        cli_current = self._cli("blueprint-get", self.project_id)

        self.assertEqual(cli_current, current)
        self.assertEqual(
            current["blueprint"]["creation_authority"],
            {"state": "unverified", "verified": False},
        )
        current_projection = current["blueprint"]["authority_projection"]
        self.assertEqual(current_projection["state"], "partially_confirmed")
        self.assertEqual(current_projection["confirmed_decision_unit_count"], 1)
        self.assertFalse(current_projection["decision_units"]["script"]["confirmed"])
        self.assertTrue(current_projection["decision_units"]["output"]["confirmed"])
        current_summary = current["blueprint"]["authority_summary"]
        self.assertTrue(current_summary["any_decision_unit_confirmed"])
        self.assertFalse(current_summary["all_decision_units_confirmed"])
        self.assertNotIn("authority_verified", current_summary)
        self.assertNotIn("user_confirmed", current_summary)

        historical_projection = historical["blueprint"]["authority_projection"]
        self.assertEqual(historical_projection["as_of_revision"], 1)
        self.assertEqual(historical_projection["confirmed_decision_unit_count"], 2)
        self.assertTrue(historical_projection["decision_units"]["script"]["confirmed"])
        self.assertTrue(historical_projection["decision_units"]["output"]["confirmed"])

        by_revision = {
            item["result_head"]["revision"]: item["result_head"][
                "authority_projection"
            ]
            for item in history["operations"]
        }
        self.assertEqual(by_revision[1]["confirmed_decision_unit_count"], 2)
        self.assertEqual(by_revision[2]["confirmed_decision_unit_count"], 1)
        self.assertEqual(resume["creative_blueprint_head"]["authority_projection"], current_projection)
        gap_codes = {item["code"] for item in resume["gaps"]}
        self.assertIn("blueprint_user_authority_partial", gap_codes)
        self.assertNotIn("blueprint_user_authority_unverified", gap_codes)

    def test_eight_of_eight_authority_keeps_open_publish_and_compiler_gaps(self) -> None:
        proposal = semantic_fixture("authority-all-units")
        proposal["open_decisions"] = [
            {
                "decision_id": "music",
                "question": "Should the final cut keep the location sound?",
                "scope": "direction",
                "required_before": "render",
            }
        ]
        self.service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": self.legacy_ref,
                "source_candidate_sha256": "f" * 64,
                "semantic": proposal,
            },
            idempotency_key="authority-all-units",
        )
        presentation = self.repository.build_blueprint_authority_presentation(
            self.project_id,
            authority_operation="confirm",
            decision_units=[
                "intent_goal",
                "intent_stance",
                "script",
                "creative_direction",
                "output",
                "material_constraints",
                "references",
                "techniques",
            ],
            runtime_authority_epoch="plugin_projection_epoch",
        )
        self.repository.confirm_blueprint_decision_units(
            self.project_id,
            runtime_authority_epoch="plugin_projection_epoch",
            presentation=presentation["presentation"],
            presentation_sha256=str(presentation["presentation_sha256"]),
            native_gesture_nonce="plugin_projection_all_units",
            idempotency_key="plugin-projection-all-units",
        )
        with patch.dict(
            os.environ,
            {TRUST_LOCAL_API_ENV: "0", "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0"},
            clear=False,
        ):
            gateway = MemoLensGateway(
                db_path=self.db_path,
                base_url="http://must-never-resolve.invalid:1",
            )

        resume = gateway.project_open(self.project_id)
        via_mcp = call_tool(
            "memolens_project_open",
            {"project_id": self.project_id},
            gateway,
        )
        self.assertEqual(via_mcp, resume)
        projection = resume["creative_blueprint_head"]["authority_projection"]
        self.assertEqual(projection["state"], "confirmed")
        self.assertEqual(projection["confirmed_decision_unit_count"], 8)
        gap_codes = {item["code"] for item in resume["gaps"]}
        self.assertIn("blueprint_open_decisions_unresolved", gap_codes)
        self.assertIn("blueprint_publish_authority_unavailable", gap_codes)
        self.assertIn("blueprint_timeline_compiler_unavailable", gap_codes)
        blueprint_read = gateway.blueprint_get(self.project_id)
        authority_summary = blueprint_read["blueprint"]["authority_summary"]
        self.assertTrue(authority_summary["any_decision_unit_confirmed"])
        self.assertTrue(authority_summary["all_decision_units_confirmed"])
        self.assertEqual(
            blueprint_read["blueprint"]["creation_authority"],
            {"state": "unverified", "verified": False},
        )
        self.assertNotIn("authority_verified", authority_summary)
        self.assertNotIn("user_confirmed", authority_summary)
        self.assertTrue(blueprint_read["safety"]["operation_receipts_read"])
        self.assertTrue(blueprint_read["safety"]["operation_receipts_validated"])
        self.assertFalse(
            blueprint_read["safety"]["raw_operation_receipts_returned"]
        )
        project_open = next(tool for tool in TOOLS if tool["name"] == "memolens_project_open")
        action_enum = set(
            project_open["outputSchema"]["properties"]["next_actions"]["items"]["enum"]
        )
        self.assertLessEqual(set(resume["next_actions"]), action_enum)


if __name__ == "__main__":
    unittest.main()
