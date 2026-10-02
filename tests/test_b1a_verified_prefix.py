from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import gc
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading
import time
from typing import Callable
import unittest
import warnings
from unittest.mock import patch

from backend.src.media.blueprint import BlueprintService
from core.db import ImageIndexRepository
from core.media_db import BlueprintIntegrityError, MediaRepository
from core.sqlite_runtime import is_wal_reset_safe


def semantic(label: str = "one") -> dict[str, object]:
    return {
        "intent": {
            "goal": f"turn local footage into story {label}",
            "stance": "ordinary memories deserve careful editing",
            "audience": "individual creators",
            "platform": "short-video",
        },
        "script": {"blocks": [{"block_id": "opening", "text": f"opening {label}"}]},
        "direction": {
            "theme": "rediscovery",
            "narrative_arc": "forgotten to found",
            "emotion": "calm",
            "tone": "restrained",
            "pace": "measured",
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


class VerifiedPrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        # Flush unreachable connections from earlier test classes before this
        # fixture begins. tearDown still collects and rejects every resource
        # leak created by the current VerifiedPrefix test.
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-b1a-prefix-")
        root = Path(self.temporary.name).resolve()
        library = root / "library"
        library.mkdir()
        self.db_path = root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(library)
        project = self.repository.create_project(
            "B1A",
            {"goal": "local footage"},
            {"source": "test"},
        )
        self.project_id = str(project["id"])
        brief = self.repository.get_brief(self.project_id, 1)
        assert brief is not None
        self.initial_legacy = {
            "revision": 1,
            "content_sha256": str(brief["content_sha256"]),
        }
        self.service = BlueprintService(self.repository)
        self.head: dict[str, object] | None = None
        self.current_semantic: dict[str, object] | None = None

    def tearDown(self) -> None:
        self.repository.close()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
        resource_warnings = [
            str(item.message)
            for item in caught
            if issubclass(item.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])
        self.temporary.cleanup()

    def _commit(
        self,
        label: str,
        key: str,
        *,
        paired: bool = False,
    ) -> tuple[object, dict[str, object]]:
        value = semantic(label)
        payload = {
            "expected_head": (
                None
                if self.head is None
                else {
                    "revision": self.head["revision"],
                    "content_sha256": self.head["content_sha256"],
                }
            ),
            "initial_legacy_brief": self.initial_legacy if self.head is None else None,
            "source_candidate_sha256": None,
            "semantic": value,
        }
        arguments = (
            {
                "paired_capability_id": "cap_b1a",
                "runtime_authority_epoch": "epoch_b1a",
                "presented_secret_sha256": "b" * 64,
            }
            if paired
            else {}
        )
        result = self.service.commit_proposal(
            self.project_id,
            payload,
            idempotency_key=key,
            **arguments,
        )
        self.head = dict(result.response["result"]["result_head"])
        self.current_semantic = value
        return result, payload

    def _no_change(
        self,
        key: str,
        *,
        paired: bool = False,
    ) -> tuple[object, dict[str, object]]:
        assert self.head is not None and self.current_semantic is not None
        payload = {
            "expected_head": {
                "revision": self.head["revision"],
                "content_sha256": self.head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": None,
            "semantic": deepcopy(self.current_semantic),
        }
        arguments = (
            {
                "paired_capability_id": "cap_b1a",
                "runtime_authority_epoch": "epoch_b1a",
                "presented_secret_sha256": "b" * 64,
            }
            if paired
            else {}
        )
        result = self.service.commit_proposal(
            self.project_id,
            payload,
            idempotency_key=key,
            **arguments,
        )
        self.head = dict(result.response["result"]["result_head"])
        return result, payload

    def _pair(self) -> None:
        arguments = {
            "pairing_id": "pair_b1a",
            "runtime_authority_epoch": "epoch_b1a",
            "project_id": self.project_id,
            "paired_subject_id": "agent_b1a",
            "claimed_client_label": "Codex (claimed)",
            "actions": ["blueprint.commit_proposal", "blueprint.restore_revision"],
            "ttl_seconds": 900,
            "max_operations": 8,
            "pairing_request_sha256": "a" * 64,
        }
        presentation = self.repository.build_agent_pairing_presentation(**arguments)
        self.repository.issue_agent_project_capability(
            capability_id="cap_b1a",
            secret_sha256="b" * 64,
            presentation=presentation["presentation"],
            presentation_sha256=str(presentation["presentation_sha256"]),
            native_gesture_nonce="pair_gesture_b1a",
            **arguments,
        )

    def _project_ledger_state(self) -> dict[str, object]:
        tables = (
            "creative_blueprint_operations",
            "creative_blueprint_revisions",
            "creative_blueprint_heads",
            "blueprint_command_receipts",
            "blueprint_desktop_receipt_integrity",
            "agent_project_capabilities",
            "agent_pairing_confirmation_receipts",
            "agent_project_command_receipts",
            "agent_project_capability_events",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            counts = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (self.project_id,),
                    ).fetchone()[0]
                )
                for table in tables
            )
            head = connection.execute(
                """SELECT revision,content_sha256,semantic_sha256,operation_id,updated_at
                     FROM creative_blueprint_heads WHERE project_id=?""",
                (self.project_id,),
            ).fetchone()
            project_updated_at = connection.execute(
                "SELECT updated_at FROM creative_projects WHERE id=?",
                (self.project_id,),
            ).fetchone()
        return {
            "counts": counts,
            "head": tuple(head) if head is not None else None,
            "project_updated_at": (
                str(project_updated_at[0]) if project_updated_at is not None else None
            ),
        }

    def _read_trigger_sql(self, trigger_name: str) -> str:
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
        self.assertIsNotNone(row, trigger_name)
        assert row is not None and type(row[0]) is str
        return str(row[0])

    def _assert_physical_schema_valid(self, trigger_name: str, trigger_sql: str) -> None:
        self.assertEqual(self._read_trigger_sql(trigger_name), trigger_sql)
        connection = self.repository._connect()
        try:
            self.repository._verify_blueprint_full_physical_schema(connection)
        finally:
            connection.close()

    def _tamper_with_restored_trigger(
        self,
        trigger_name: str,
        mutate: Callable[[sqlite3.Connection], None],
    ) -> None:
        trigger_sql = self._read_trigger_sql(trigger_name)
        quoted_name = '"' + trigger_name.replace('"', '""') + '"'
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(f"DROP TRIGGER {quoted_name}")
            mutate(connection)
            connection.execute(trigger_sql)
            connection.commit()
        self._assert_physical_schema_valid(trigger_name, trigger_sql)

    def _assert_next_command_cold_fails_before_mutation(self, label: str) -> None:
        before = self._project_ledger_state()
        original_audit = self.repository._full_audit_blueprint_prefix
        with (
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
            patch.object(
                self.service,
                "_commit_mutation",
                side_effect=AssertionError("tampered prefix reached mutation"),
            ) as mutation,
            self.assertRaises(BlueprintIntegrityError),
        ):
            self._commit(label, f"after-tamper-{label}")
        self.assertEqual(audit.call_count, 1)
        mutation.assert_not_called()
        self.assertNotIn(self.project_id, self.repository._blueprint_verified_prefixes)
        self.assertEqual(self._project_ledger_state(), before)
        anchor = self.repository._blueprint_anchor
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertFalse(anchor.in_transaction)

    def _seed_paired_prefix(self) -> None:
        self._commit("desktop", "desktop")
        self._pair()
        self._commit("paired", "paired", paired=True)

    def test_external_head_tamper_restores_schema_then_cold_audit_rejects(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            self._tamper_with_restored_trigger(
                "trg_blueprint_heads_forward_only",
                lambda connection: connection.execute(
                    """UPDATE creative_blueprint_heads SET content_sha256=?
                         WHERE project_id=?""",
                    ("0" * 64, self.project_id),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("head")

    def test_external_revision_tamper_restores_schema_then_cold_audit_rejects(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            self._tamper_with_restored_trigger(
                "trg_blueprint_revisions_no_update",
                lambda connection: connection.execute(
                    """UPDATE creative_blueprint_revisions SET blueprint_json='{}'
                         WHERE project_id=? AND revision=1""",
                    (self.project_id,),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("revision")

    def test_external_desktop_receipt_tamper_restores_schema_then_cold_audit_rejects(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            self._tamper_with_restored_trigger(
                "trg_blueprint_receipts_no_update",
                lambda connection: connection.execute(
                    """UPDATE blueprint_command_receipts SET response_json='{}'
                         WHERE project_id=?""",
                    (self.project_id,),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("desktop-receipt")

    def test_external_sidecar_tamper_restores_schema_then_cold_audit_rejects(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            self._tamper_with_restored_trigger(
                "trg_blueprint_desktop_receipt_integrity_no_update",
                lambda connection: connection.execute(
                    """UPDATE blueprint_desktop_receipt_integrity SET receipt_sha256=?
                         WHERE project_id=?""",
                    ("0" * 64, self.project_id),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("sidecar")

    def test_external_agent_receipt_tamper_restores_schema_then_cold_audit_rejects(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._seed_paired_prefix()
            self._tamper_with_restored_trigger(
                "trg_agent_project_receipts_no_update",
                lambda connection: connection.execute(
                    """UPDATE agent_project_command_receipts SET response_json='{}'
                         WHERE project_id=? AND receipt_schema_version='1'""",
                    (self.project_id,),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("agent-receipt")

    def test_external_capability_fact_tamper_restores_schema_then_cold_audit_rejects(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._seed_paired_prefix()
            self._tamper_with_restored_trigger(
                "trg_agent_capabilities_no_update",
                lambda connection: connection.execute(
                    """UPDATE agent_project_capabilities
                          SET claimed_client_label='forged client' WHERE project_id=?""",
                    (self.project_id,),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("capability-fact")

    def test_external_used_event_tamper_restores_schema_then_cold_audit_rejects(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._seed_paired_prefix()
            self._tamper_with_restored_trigger(
                "trg_agent_capability_events_no_update",
                lambda connection: connection.execute(
                    """UPDATE agent_project_capability_events
                          SET action='blueprint.restore_revision'
                        WHERE project_id=? AND event_type='used'""",
                    (self.project_id,),
                ),
            )
            self._assert_next_command_cold_fails_before_mutation("capability-used-event")

    def test_same_anchor_schema_ddl_rolls_back_and_forces_cold_recovery(self) -> None:
        trigger_name = "trg_creative_blueprint_operations_no_update"
        trigger_sql = self._read_trigger_sql(trigger_name)
        quoted_name = '"' + trigger_name.replace('"', '""') + '"'
        variants = (
            "drop-trigger",
            "drop-recreate-trigger",
            "drop-reset-cookie",
            "create-table",
            "create-temp-table",
        )
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("initial", "initial")
            for index, variant in enumerate(variants, start=1):
                with self.subTest(variant=variant):
                    before = self._project_ledger_state()
                    anchor = self.repository._blueprint_anchor
                    self.assertIsNotNone(anchor)
                    assert anchor is not None
                    before_cookie = int(
                        anchor.execute("PRAGMA schema_version").fetchone()[0]
                    )
                    original_mutation = self.service._commit_mutation

                    def ddl_then_mutate(connection, **kwargs):
                        if variant == "drop-trigger":
                            connection.execute(f"DROP TRIGGER {quoted_name}")
                        elif variant == "drop-recreate-trigger":
                            connection.execute(f"DROP TRIGGER {quoted_name}")
                            connection.execute(trigger_sql)
                        elif variant == "drop-reset-cookie":
                            connection.execute(f"DROP TRIGGER {quoted_name}")
                            connection.execute(
                                f"PRAGMA main.schema_version={before_cookie}"
                            )
                        elif variant == "create-temp-table":
                            connection.execute(
                                "CREATE TEMP TABLE b1a_transient_temp_guard(value TEXT)"
                            )
                        else:
                            connection.execute(
                                "CREATE TABLE b1a_transient_schema_guard(value TEXT)"
                            )
                        return original_mutation(connection, **kwargs)

                    original_audit = self.repository._full_audit_blueprint_prefix
                    with (
                        patch.object(
                            self.repository,
                            "_full_audit_blueprint_prefix",
                            wraps=original_audit,
                        ) as warm_audit,
                        patch.object(
                            self.service,
                            "_commit_mutation",
                            side_effect=ddl_then_mutate,
                        ) as mutation,
                        self.assertRaises(BlueprintIntegrityError),
                    ):
                        self._commit(f"ddl-{index}", f"ddl-{index}")
                    self.assertEqual(warm_audit.call_count, 0)
                    self.assertEqual(mutation.call_count, 1)
                    self.assertNotIn(
                        self.project_id,
                        self.repository._blueprint_verified_prefixes,
                    )
                    self.assertEqual(self._project_ledger_state(), before)
                    self.assertFalse(anchor.in_transaction)
                    self.assertEqual(
                        int(anchor.execute("PRAGMA schema_version").fetchone()[0]),
                        before_cookie,
                    )
                    self._assert_physical_schema_valid(trigger_name, trigger_sql)
                    with closing(sqlite3.connect(self.db_path)) as connection, connection:
                        self.assertIsNone(
                            connection.execute(
                                """SELECT 1 FROM sqlite_schema
                                    WHERE name='b1a_transient_schema_guard'"""
                            ).fetchone()
                        )
                    self.assertIsNone(
                        anchor.execute(
                            """SELECT 1 FROM sqlite_temp_schema
                                WHERE name='b1a_transient_temp_guard'"""
                        ).fetchone()
                    )

                    original_audit = self.repository._full_audit_blueprint_prefix
                    with patch.object(
                        self.repository,
                        "_full_audit_blueprint_prefix",
                        wraps=original_audit,
                    ) as recovery_audit:
                        result, _payload = self._commit(
                            f"recovery-{index}",
                            f"recovery-{index}",
                        )
                    self.assertFalse(result.replayed)
                    self.assertEqual(recovery_audit.call_count, 1)
                    self.assertIn(
                        self.project_id,
                        self.repository._blueprint_verified_prefixes,
                    )

    def test_removed_authorizer_cannot_hide_main_or_temp_schema_mutation(
        self,
    ) -> None:
        trigger_name = "trg_creative_blueprint_operations_no_update"
        trigger_sql = self._read_trigger_sql(trigger_name)
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("initial", "initial")
            for variant in ("main", "temp", "attach"):
                with self.subTest(variant=variant):
                    before = self._project_ledger_state()
                    original_mutation = self.service._commit_mutation

                    def bypass_authorizer_then_mutate(connection, **kwargs):
                        connection.set_authorizer(None)
                        if variant == "main":
                            cookie = int(
                                connection.execute(
                                    "PRAGMA main.schema_version"
                                ).fetchone()[0]
                            )
                            connection.execute(f"DROP TRIGGER {trigger_name}")
                            connection.execute(
                                f"PRAGMA main.schema_version={cookie}"
                            )
                        elif variant == "temp":
                            cookie = int(
                                connection.execute(
                                    "PRAGMA temp.schema_version"
                                ).fetchone()[0]
                            )
                            connection.execute(
                                "CREATE TEMP TABLE b1a_hidden_temp(value TEXT)"
                            )
                            connection.execute(
                                f"PRAGMA temp.schema_version={cookie}"
                            )
                        else:
                            attachment = self.db_path.with_name(
                                "b1a-hidden-attachment.db"
                            )
                            connection.execute(
                                "ATTACH DATABASE ? AS b1a_hidden_attachment",
                                (str(attachment),),
                            )
                        return original_mutation(connection, **kwargs)

                    with (
                        patch.object(
                            self.service,
                            "_commit_mutation",
                            side_effect=bypass_authorizer_then_mutate,
                        ),
                        self.assertRaisesRegex(
                            BlueprintIntegrityError,
                            "blueprint_schema_integrity_error",
                        ),
                    ):
                        self._commit(f"hidden-{variant}", f"hidden-{variant}")

                    self.assertEqual(self._project_ledger_state(), before)
                    self.assertNotIn(
                        self.project_id,
                        self.repository._blueprint_verified_prefixes,
                    )
                    self._assert_physical_schema_valid(trigger_name, trigger_sql)
                    self.assertIsNone(self.repository._blueprint_anchor)
                    verification = self.repository._connect()
                    try:
                        self.assertIsNone(
                            verification.execute(
                            "SELECT 1 FROM sqlite_temp_schema WHERE name='b1a_hidden_temp'"
                            ).fetchone()
                        )
                        self.assertEqual(
                            {
                                str(row[1]).casefold()
                                for row in verification.execute("PRAGMA database_list")
                            },
                            {"main", "temp"},
                        )
                    finally:
                        verification.close()

    def test_commit_trace_schema_mutation_is_post_commit_indeterminate_and_poisoned(
        self,
    ) -> None:
        trigger_name = "trg_creative_blueprint_operations_no_update"
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("initial", "initial")
            original_store = self.repository._store_blueprint_receipt
            trace_fired = False

            def install_commit_trace(connection, **kwargs):
                nonlocal trace_fired

                def mutate_on_commit(statement: str) -> None:
                    nonlocal trace_fired
                    if trace_fired or statement.strip().upper() != "COMMIT":
                        return
                    trace_fired = True
                    connection.set_trace_callback(None)
                    cookie = int(
                        connection.execute("PRAGMA main.schema_version").fetchone()[0]
                    )
                    connection.execute(f"DROP TRIGGER {trigger_name}")
                    connection.execute(f"PRAGMA main.schema_version={cookie}")

                connection.set_trace_callback(mutate_on_commit)
                return original_store(connection, **kwargs)

            with (
                patch.object(
                    self.repository,
                    "_store_blueprint_receipt",
                    side_effect=install_commit_trace,
                ),
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_post_commit_verification_indeterminate",
                ),
            ):
                self._commit("trace-commit", "trace-commit")

        self.assertTrue(trace_fired)
        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (trigger_name,),
                ).fetchone()
            )
            self.assertEqual(
                int(
                    connection.execute(
                        """SELECT COUNT(*) FROM creative_blueprint_operations
                            WHERE project_id=?""",
                        (self.project_id,),
                    ).fetchone()[0]
                ),
                2,
            )

        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_database_binding_indeterminate",
        ):
            self._commit("poisoned", "poisoned")

    def test_callback_cannot_commit_a_partial_blueprint_command(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("initial", "initial")
            before = self._project_ledger_state()
            original_mutation = self.service._commit_mutation

            def commit_then_fail(connection, **kwargs):
                original_mutation(connection, **kwargs)
                connection.set_authorizer(None)
                connection.commit()
                raise RuntimeError("fault after callback commit")

            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=commit_then_fail,
                ),
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_schema_mutation_forbidden",
                ),
            ):
                self._commit("must-be-atomic", "must-be-atomic")

            self.assertEqual(self._project_ledger_state(), before)
            self.assertFalse(self.repository._blueprint_database_binding_failed)

    def test_base_commit_bypass_is_post_commit_indeterminate_and_poisoned(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("initial", "initial")
            original_mutation = self.service._commit_mutation

            def bypass_then_fail(connection, **kwargs):
                original_mutation(connection, **kwargs)
                connection.set_authorizer(None)
                sqlite3.Connection.commit(connection)
                raise RuntimeError("fault after base commit")

            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=bypass_then_fail,
                ),
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_post_commit_verification_indeterminate",
                ),
            ):
                self._commit("indeterminate", "indeterminate")

        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_database_binding_indeterminate",
        ):
            self._commit("poisoned-after-bypass", "poisoned-after-bypass")

    def test_safe_and_fallback_bind_the_actual_sqlite_connection_inode(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("initial", "initial")
        assert self.head is not None
        self.repository._discard_blueprint_anchor()
        clone = self.db_path.with_name("same-uuid-clone.db")
        with closing(sqlite3.connect(self.db_path)) as source, source:
            with closing(sqlite3.connect(clone)) as destination, destination:
                source.backup(destination)

        for safe_runtime in (True, False):
            with self.subTest(safe_runtime=safe_runtime):
                repository = MediaRepository(self.db_path)
                service = BlueprintService(repository)
                original_connect = repository._connect

                def connect_clone(**kwargs):
                    expected_path = repository.db_path
                    repository.db_path = clone
                    try:
                        return original_connect(**kwargs)
                    finally:
                        repository.db_path = expected_path

                payload = {
                    "expected_head": {
                        "revision": self.head["revision"],
                        "content_sha256": self.head["content_sha256"],
                    },
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": None,
                    "semantic": semantic(f"clone-{safe_runtime}"),
                }
                try:
                    with (
                        patch.object(
                            repository,
                            "_verified_prefix_sqlite_is_safe",
                            return_value=safe_runtime,
                        ),
                        patch.object(
                            repository,
                            "_connect",
                            side_effect=connect_clone,
                        ),
                        patch.object(
                            service,
                            "_commit_mutation",
                            side_effect=AssertionError(
                                "unbound SQLite connection reached mutation"
                            ),
                        ) as mutation,
                        self.assertRaisesRegex(
                            BlueprintIntegrityError,
                            "blueprint_database_binding_indeterminate",
                        ),
                    ):
                        service.commit_proposal(
                            self.project_id,
                            payload,
                            idempotency_key=f"clone-{safe_runtime}",
                        )
                    mutation.assert_not_called()
                    self.assertTrue(repository._blueprint_database_binding_failed)
                finally:
                    repository.close()

                for path in (self.db_path, clone):
                    with closing(sqlite3.connect(path)) as connection, connection:
                        revision = connection.execute(
                            """SELECT revision FROM creative_blueprint_heads
                                WHERE project_id=?""",
                            (self.project_id,),
                        ).fetchone()
                    self.assertEqual(revision, (1,))

    def test_post_commit_path_swap_is_indeterminate_and_poisons_repository(
        self,
    ) -> None:
        if not is_wal_reset_safe(sqlite3.sqlite_version_info):
            self.skipTest("requires a WAL-reset-safe SQLite runtime")
        self._commit("initial", "initial")
        old_database = self.db_path.with_name("committed-old-media.db")
        original_identity = self.repository._blueprint_database_file_identity
        identity_calls = 0
        committed_revision: int | None = None

        def swap_before_post_commit_identity() -> tuple[int, int]:
            nonlocal committed_revision, identity_calls
            identity_calls += 1
            anchor = self.repository._blueprint_anchor
            if (
                committed_revision is None
                and anchor is not None
                and not anchor.in_transaction
            ):
                row = anchor.execute(
                    "SELECT revision FROM creative_blueprint_heads WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()
                assert row is not None
                if int(row[0]) == 2:
                    committed_revision = int(row[0])
                    # Preserve a recovery name for the exact inode still held by
                    # SQLite, then atomically replace only the bound pathname.
                    os.link(self.db_path, old_database)
                    replacement = self.db_path.with_name("replacement-empty.db")
                    replacement.touch()
                    os.replace(replacement, self.db_path)
            return original_identity()

        with (
            patch.object(
                self.repository,
                "_blueprint_database_file_identity",
                side_effect=swap_before_post_commit_identity,
            ),
            self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_database_binding_indeterminate",
            ),
        ):
            self._commit("committed-but-detached", "committed-but-detached")

        self.assertGreaterEqual(identity_calls, 3)
        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)
        self.assertTrue(old_database.is_file())
        self.assertEqual(self.db_path.stat().st_size, 0)
        self.assertEqual(committed_revision, 2)

        with (
            patch.object(
                self.repository,
                "_blueprint_anchor_connection",
                side_effect=AssertionError("poisoned repository reopened a database"),
            ) as reconnect,
            self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_database_binding_indeterminate",
            ),
        ):
            self._commit("must-not-rebind", "must-not-rebind")
        reconnect.assert_not_called()

    def test_between_request_valid_snapshot_swap_never_rebinds_repository(
        self,
    ) -> None:
        if not is_wal_reset_safe(sqlite3.sqlite_version_info):
            self.skipTest("requires a WAL-reset-safe SQLite runtime")
        self._commit("snapshot-one", "snapshot-one")
        anchor = self.repository._blueprint_anchor
        self.assertIsNotNone(anchor)
        assert anchor is not None
        old_snapshot = self.db_path.with_name("valid-old-snapshot.db")
        with closing(sqlite3.connect(old_snapshot)) as destination, destination:
            anchor.backup(destination)
        self._commit("snapshot-two", "snapshot-two")

        os.replace(old_snapshot, self.db_path)
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_database_binding_indeterminate",
        ):
            self._commit("must-not-use-old-snapshot", "must-not-use-old-snapshot")

        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    def test_same_inode_valid_snapshot_rollback_violates_attested_floor(self) -> None:
        if not is_wal_reset_safe(sqlite3.sqlite_version_info):
            self.skipTest("requires a WAL-reset-safe SQLite runtime")
        self._commit("snapshot-one", "snapshot-one")
        first_head = deepcopy(self.head)
        first_semantic = deepcopy(self.current_semantic)
        anchor = self.repository._blueprint_anchor
        self.assertIsNotNone(anchor)
        assert anchor is not None
        old_snapshot = self.db_path.with_name("same-inode-old-snapshot.db")
        with closing(sqlite3.connect(old_snapshot)) as destination, destination:
            anchor.backup(destination)
        self._commit("snapshot-two", "snapshot-two")
        bound_identity = self.repository._blueprint_bound_database_identity
        self.assertIsNotNone(bound_identity)

        with closing(sqlite3.connect(old_snapshot)) as source, source:
            source.backup(anchor)
        self.assertEqual(
            self.repository._blueprint_database_file_identity(),
            bound_identity,
        )
        self.head = first_head
        self.current_semantic = first_semantic
        with (
            patch.object(
                self.service,
                "_commit_mutation",
                side_effect=AssertionError("rolled-back prefix reached mutation"),
            ) as mutation,
            self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_database_binding_indeterminate",
            ),
        ):
            self._commit("must-not-fork", "must-not-fork")

        mutation.assert_not_called()
        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    def test_fallback_path_binding_survives_a_to_b_to_a_replacement(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("fallback-a", "fallback-a")
            original_inode = self.db_path.with_name("fallback-original-a.db")
            os.link(self.db_path, original_inode)
            replacement_b = self.db_path.with_name("fallback-replacement-b.db")
            with closing(sqlite3.connect(self.db_path)) as source, source:
                with closing(sqlite3.connect(replacement_b)) as destination, destination:
                    source.backup(destination)
            os.replace(replacement_b, self.db_path)
            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=AssertionError("replacement inode reached mutation"),
                ) as mutation,
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_database_binding_indeterminate",
                ),
            ):
                self._commit("fallback-b", "fallback-b")
            mutation.assert_not_called()

            os.replace(original_inode, self.db_path)
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_database_binding_indeterminate",
            ):
                self._commit("fallback-restored-a", "fallback-restored-a")

        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    def test_fallback_same_inode_rollback_violates_attested_floor(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("fallback-old", "fallback-old")
            first_head = deepcopy(self.head)
            first_semantic = deepcopy(self.current_semantic)
            old_snapshot = self.db_path.with_name("fallback-old-snapshot.db")
            with closing(sqlite3.connect(self.db_path)) as source, source:
                with closing(sqlite3.connect(old_snapshot)) as destination, destination:
                    source.backup(destination)
            self._commit("fallback-new", "fallback-new")
            bound_identity = self.repository._blueprint_bound_database_identity
            with closing(sqlite3.connect(old_snapshot)) as source, source:
                with closing(sqlite3.connect(self.db_path)) as destination, destination:
                    source.backup(destination)
            self.assertEqual(
                self.repository._blueprint_database_file_identity(),
                bound_identity,
            )
            self.head = first_head
            self.current_semantic = first_semantic
            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=AssertionError("fallback rollback reached mutation"),
                ) as mutation,
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_database_binding_indeterminate",
                ),
            ):
                self._commit("fallback-fork", "fallback-fork")

        mutation.assert_not_called()
        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "fork"), "requires fork")
    def test_real_fork_discards_inherited_anchor_and_cold_audits_without_deadlock(
        self,
    ) -> None:
        if not is_wal_reset_safe(sqlite3.sqlite_version_info):
            self.skipTest("requires a WAL-reset-safe SQLite runtime")
        self._commit("parent", "parent")
        parent_pid = os.getpid()
        parent_anchor = self.repository._blueprint_anchor
        self.assertIsNotNone(parent_anchor)
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - assertions are reported over the pipe
            os.close(read_fd)
            try:
                original_audit = self.repository._full_audit_blueprint_prefix
                with patch.object(
                    self.repository,
                    "_full_audit_blueprint_prefix",
                    wraps=original_audit,
                ) as audit:
                    result, _payload = self._commit("child", "child")
                lease = self.repository._blueprint_verified_prefixes[self.project_id]
                child_anchor = self.repository._blueprint_anchor
                valid = bool(
                    not result.replayed
                    and audit.call_count == 1
                    and lease.process_id == os.getpid()
                    and child_anchor is not None
                    and self.repository._blueprint_anchor_pid == os.getpid()
                    and not child_anchor.in_transaction
                )
                os.write(
                    write_fd,
                    f"ok:{int(valid)}:{audit.call_count}:{lease.operation_sequence}".encode(),
                )
                os._exit(0 if valid else 2)
            except BaseException as exc:
                message = f"error:{type(exc).__name__}:{exc}".encode()[:1_024]
                try:
                    os.write(write_fd, message)
                finally:
                    os._exit(1)

        os.close(write_fd)
        deadline = time.monotonic() + 10.0
        status: int | None = None
        while time.monotonic() < deadline:
            waited_pid, candidate_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = candidate_status
                break
            time.sleep(0.01)
        if status is None:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
            os.close(read_fd)
            self.fail("forked Blueprint command exceeded 10 second deadline")
        payload = os.read(read_fd, 2_048).decode()
        os.close(read_fd)
        self.assertTrue(os.WIFEXITED(status), payload)
        self.assertEqual(os.WEXITSTATUS(status), 0, payload)
        self.assertEqual(payload, "ok:1:1:2")
        self.assertEqual(self.repository._blueprint_anchor_pid, parent_pid)
        self.assertIs(self.repository._blueprint_anchor, parent_anchor)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT revision FROM creative_blueprint_heads WHERE project_id=?",
                    (self.project_id,),
                ).fetchone(),
                (2,),
            )

    def test_cross_project_round_trip_is_safely_cold_on_global_change_counter(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("project-a-first", "project-a-first")
            project_b = self.repository.create_project(
                "B1A project B",
                {"goal": "project B"},
                {"source": "test"},
            )
            project_b_id = str(project_b["id"])
            brief_b = self.repository.get_brief(project_b_id, 1)
            assert brief_b is not None
            service_b = BlueprintService(self.repository)
            result_b = service_b.commit_proposal(
                project_b_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": str(brief_b["content_sha256"]),
                    },
                    "source_candidate_sha256": None,
                    "semantic": semantic("project-b"),
                },
                idempotency_key="project-b-first",
            )
            self.assertFalse(result_b.replayed)
            self.assertIn(self.project_id, self.repository._blueprint_verified_prefixes)
            self.assertIn(project_b_id, self.repository._blueprint_verified_prefixes)

            original_audit = self.repository._full_audit_blueprint_prefix
            with patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit:
                result_a, _payload = self._commit(
                    "project-a-second",
                    "project-a-second",
                )
            self.assertFalse(result_a.replayed)
            self.assertEqual(audit.call_count, 1)
            self.assertEqual(
                self.repository._blueprint_verified_prefixes[
                    self.project_id
                ].operation_sequence,
                2,
            )

    def test_sqlite_wal_fix_gate_is_conservative(self) -> None:
        admitted = ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 0))
        rejected = (
            (3, 44, 5),
            (3, 45, 9),
            (3, 47, 1),
            (3, 50, 6),
            (3, 51, 2),
            (3, 52, 0),
            (3, 52, 1),
            (4, 0, 0),
        )
        for version in admitted:
            with self.subTest(version=version):
                self.assertTrue(is_wal_reset_safe(version))
        for version in rejected:
            with self.subTest(version=version):
                self.assertFalse(is_wal_reset_safe(version))
        with patch("core.media_db.is_wal_reset_safe", return_value=True) as gate:
            self.assertTrue(MediaRepository._verified_prefix_sqlite_is_safe())
            gate.assert_called_once_with(sqlite3.sqlite_version_info)

    def test_warm_thousand_commits_audit_once_and_keep_constant_sql_shape(self) -> None:
        original_audit = self.repository._full_audit_blueprint_prefix
        sql_counts: dict[int, int] = {}
        plan_shapes: dict[int, tuple[tuple[str, ...], ...]] = {}
        statements: list[str] = []
        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
        ):
            self._commit("1", "commit-1")
            anchor = self.repository._blueprint_anchor
            assert anchor is not None
            tables = tuple(
                prefix.table
                for prefix in self.repository._blueprint_verified_prefixes[
                    self.project_id
                ].immutable_prefixes
            )
            anchor.set_trace_callback(statements.append)
            for revision in range(2, 1_001):
                statements.clear()
                self._commit(str(revision), f"commit-{revision}")
                if revision in {10, 50, 100, 1_000}:
                    committed_statements = tuple(statements)
                    sql_counts[revision] = len(committed_statements)
                    shape: list[tuple[str, ...]] = []
                    for table in tables:
                        candidates = [
                            statement
                            for statement in committed_statements
                            if f"FROM {table}" in statement
                            and "rowid>" in statement
                            and "ORDER BY rowid LIMIT" in statement
                        ]
                        self.assertEqual(len(candidates), 1, table)
                        shape.append(
                            tuple(
                                str(row[3]).upper()
                                for row in anchor.execute(
                                    f"EXPLAIN QUERY PLAN {candidates[0]}"
                                ).fetchall()
                            )
                        )
                    plan_shapes[revision] = tuple(shape)
            anchor.set_trace_callback(None)
            self.assertEqual(audit.call_count, 1)
        self.assertEqual(sql_counts[10], sql_counts[50])
        self.assertEqual(sql_counts[50], sql_counts[100])
        self.assertEqual(sql_counts[100], sql_counts[1_000])
        self.assertEqual(plan_shapes[10], plan_shapes[50])
        self.assertEqual(plan_shapes[50], plan_shapes[100])
        self.assertEqual(plan_shapes[100], plan_shapes[1_000])
        lease = self.repository._blueprint_verified_prefixes[self.project_id]
        self.assertEqual(lease.operation_sequence, 1_000)
        self.assertEqual(lease.head["revision"], 1_000)

    def test_warm_prefix_queries_use_bounded_rowid_range_plans(self) -> None:
        statements: list[str] = []
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("plan-1", "plan-1")
            anchor = self.repository._blueprint_anchor
            assert anchor is not None
            lease = self.repository._blueprint_verified_prefixes[self.project_id]
            anchor.set_trace_callback(statements.append)
            self._commit("plan-2", "plan-2")
            anchor.set_trace_callback(None)

        for prefix in lease.immutable_prefixes:
            with self.subTest(table=prefix.table):
                candidates = [
                    statement
                    for statement in statements
                    if f"FROM {prefix.table}" in statement
                    and "rowid>" in statement
                    and "ORDER BY rowid LIMIT" in statement
                ]
                self.assertEqual(len(candidates), 1)
                plan = anchor.execute(
                    f"EXPLAIN QUERY PLAN {candidates[0]}"
                ).fetchall()
                details = tuple(str(row[3]).upper() for row in plan)
                self.assertTrue(
                    any(
                        "USING INTEGER PRIMARY KEY" in detail
                        and "ROWID>?" in detail
                        and "ROWID<?" in detail
                        for detail in details
                    ),
                    details,
                )
                self.assertFalse(
                    any("SCAN " in detail or "TEMP B-TREE" in detail for detail in details),
                    details,
                )

    def test_cold_prefix_frontier_skips_other_project_history_on_next_warm(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("project-a-1", "project-a-1")
            project_b = self.repository.create_project(
                "B1A project B",
                {"goal": "other project history"},
                {"source": "test"},
            )
            project_b_id = str(project_b["id"])
            brief_b = self.repository.get_brief(project_b_id, 1)
            assert brief_b is not None
            project_b_semantic = semantic("project-b")
            project_b_result = self.service.commit_proposal(
                project_b_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": str(brief_b["content_sha256"]),
                    },
                    "source_candidate_sha256": None,
                    "semantic": project_b_semantic,
                },
                idempotency_key="project-b-desktop-base",
            )
            project_b_head = dict(
                project_b_result.response["result"]["result_head"]
            )
            pairing = {
                "pairing_id": "pair_b1a_project_b",
                "runtime_authority_epoch": "epoch_b1a_project_b",
                "project_id": project_b_id,
                "paired_subject_id": "agent_b1a_project_b",
                "claimed_client_label": "Codex project B (claimed)",
                "actions": [
                    "blueprint.commit_proposal",
                    "blueprint.restore_revision",
                ],
                "ttl_seconds": 900,
                "max_operations": 32,
                "pairing_request_sha256": "c" * 64,
            }
            presentation = self.repository.build_agent_pairing_presentation(
                **pairing
            )
            self.repository.issue_agent_project_capability(
                capability_id="cap_b1a_project_b",
                secret_sha256="d" * 64,
                presentation=presentation["presentation"],
                presentation_sha256=str(presentation["presentation_sha256"]),
                native_gesture_nonce="pair_gesture_b1a_project_b",
                **pairing,
            )
            for sequence in range(32):
                payload = {
                    "expected_head": {
                        "revision": project_b_head["revision"],
                        "content_sha256": project_b_head["content_sha256"],
                    },
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": None,
                    "semantic": project_b_semantic,
                }
                result = self.service.commit_proposal(
                    project_b_id,
                    payload,
                    idempotency_key=f"project-b-{sequence}",
                    paired_capability_id="cap_b1a_project_b",
                    runtime_authority_epoch="epoch_b1a_project_b",
                    presented_secret_sha256="d" * 64,
                )
                project_b_head = dict(result.response["result"]["result_head"])

            original_audit = self.repository._full_audit_blueprint_prefix
            with patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit:
                self._commit("project-a-2", "project-a-2")
            self.assertEqual(audit.call_count, 1)
            anchor = self.repository._blueprint_anchor
            assert anchor is not None
            lease = self.repository._blueprint_verified_prefixes[self.project_id]
            prefix = next(
                item
                for item in lease.immutable_prefixes
                if item.table == "agent_project_command_receipts"
            )
            global_high_water = int(
                anchor.execute(
                    "SELECT COALESCE(MAX(rowid),0) "
                    "FROM agent_project_command_receipts"
                ).fetchone()[0]
            )
            self.assertEqual(global_high_water, 32)
            self.assertEqual(prefix.max_rowid, global_high_water)

            statements: list[str] = []
            anchor.set_trace_callback(statements.append)
            self._commit("project-a-3", "project-a-3")
            anchor.set_trace_callback(None)
            candidates = [
                " ".join(statement.split())
                for statement in statements
                if "FROM agent_project_command_receipts" in statement
                and "rowid>" in statement
                and "ORDER BY rowid LIMIT" in statement
            ]
            self.assertEqual(len(candidates), 1)
            self.assertIn(f"rowid>{global_high_water}", candidates[0])
            self.assertIn(f"rowid<={global_high_water}", candidates[0])

    def test_desktop_commit_no_change_restore_and_replay_have_exact_dml(self) -> None:
        deltas: list[int] = []
        original_prepare = self.repository._prepare_verified_prefix_successor

        def record_delta(connection, context, **kwargs):
            original_prepare(connection, context, **kwargs)
            assert context is not None
            deltas.append(connection.total_changes - context.starting_total_changes)

        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_prepare_verified_prefix_successor",
                side_effect=record_delta,
            ),
        ):
            first, _ = self._commit("first", "first")
            first_ref = {
                "revision": first.response["result"]["result_head"]["revision"],
                "content_sha256": first.response["result"]["result_head"]["content_sha256"],
            }
            self._commit("second", "second")
            self._no_change("no-change")
            assert self.head is not None
            restore_payload = {
                "expected_head": {
                    "revision": self.head["revision"],
                    "content_sha256": self.head["content_sha256"],
                },
                "restore_from": first_ref,
            }
            restored = self.service.restore_revision(
                self.project_id,
                restore_payload,
                idempotency_key="restore",
            )
            replay = self.service.restore_revision(
                self.project_id,
                restore_payload,
                idempotency_key="restore",
            )
            self.assertFalse(restored.replayed)
            self.assertTrue(replay.replayed)
        self.assertEqual(deltas, [6, 6, 3, 6, 0])

    def test_paired_receipt_and_capability_use_are_in_the_exact_delta(self) -> None:
        deltas: list[int] = []
        original_prepare = self.repository._prepare_verified_prefix_successor

        def record_delta(connection, context, **kwargs):
            original_prepare(connection, context, **kwargs)
            assert context is not None
            deltas.append(connection.total_changes - context.starting_total_changes)

        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("desktop", "desktop")
            self._pair()
            with patch.object(
                self.repository,
                "_prepare_verified_prefix_successor",
                side_effect=record_delta,
            ):
                self._commit("paired", "paired", paired=True)
                _result, payload = self._no_change("paired-no-change", paired=True)
                replay = self.service.commit_proposal(
                    self.project_id,
                    payload,
                    idempotency_key="paired-no-change",
                    paired_capability_id="cap_b1a",
                    runtime_authority_epoch="epoch_b1a",
                    presented_secret_sha256="b" * 64,
                )
                self.assertTrue(replay.replayed)
        self.assertEqual(deltas, [6, 3, 0])
        capability = self.repository.get_agent_project_capability(
            "cap_b1a",
            runtime_authority_epoch="epoch_b1a",
        )
        assert capability is not None
        self.assertEqual(capability["operations_used"], 2)

    def test_external_tamper_invalidates_lease_and_cold_audit_fails_closed(self) -> None:
        original_audit = self.repository._full_audit_blueprint_prefix
        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
        ):
            self._commit("first", "first")
            assert self.head is not None
            with closing(sqlite3.connect(self.db_path)) as external, external:
                trigger = external.execute(
                    """SELECT sql FROM sqlite_schema
                        WHERE name='trg_creative_blueprint_operations_no_update'"""
                ).fetchone()[0]
                external.execute("DROP TRIGGER trg_creative_blueprint_operations_no_update")
                external.execute(
                    """UPDATE creative_blueprint_operations SET created_at='not-a-time'
                        WHERE project_id=? AND sequence=1""",
                    (self.project_id,),
                )
                external.execute(trigger)
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_operation_row_invalid",
            ):
                self._commit("second", "second")
            self.assertEqual(audit.call_count, 2)
            self.assertNotIn(self.project_id, self.repository._blueprint_verified_prefixes)

    def test_legitimate_external_commit_invalidates_then_cold_audit_succeeds(self) -> None:
        original_audit = self.repository._full_audit_blueprint_prefix
        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
        ):
            self._commit("first", "first")
            with closing(sqlite3.connect(self.db_path)) as external, external:
                external.execute(
                    "UPDATE creative_projects SET title=? WHERE id=?",
                    ("B1A externally renamed", self.project_id),
                )
            result, _payload = self._commit("second", "second")
            self.assertFalse(result.replayed)
            self.assertEqual(audit.call_count, 2)
        lease = self.repository._blueprint_verified_prefixes[self.project_id]
        self.assertEqual(lease.operation_sequence, 2)

    def test_legitimate_external_blueprint_extension_advances_attested_floor(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            assert self.head is not None
            external_repository = MediaRepository(self.db_path)
            external_service = BlueprintService(external_repository)
            try:
                external = external_service.commit_proposal(
                    self.project_id,
                    {
                        "expected_head": {
                            "revision": self.head["revision"],
                            "content_sha256": self.head["content_sha256"],
                        },
                        "initial_legacy_brief": None,
                        "source_candidate_sha256": None,
                        "semantic": semantic("external-extension"),
                    },
                    idempotency_key="external-extension",
                )
            finally:
                external_repository.close()
            self.head = dict(external.response["result"]["result_head"])
            self.current_semantic = semantic("external-extension")

            original_audit = self.repository._full_audit_blueprint_prefix
            with patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit:
                result, _payload = self._commit("third", "third")

        self.assertFalse(result.replayed)
        self.assertEqual(audit.call_count, 1)
        floor = self.repository._blueprint_attested_prefix_floors[self.project_id]
        self.assertEqual(floor.operation_sequence, 3)
        self.assertEqual(floor.head["revision"], 3)

    def test_rollback_does_not_publish_and_next_command_cold_audits(self) -> None:
        original_audit = self.repository._full_audit_blueprint_prefix
        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
        ):
            self._commit("first", "first")
            original_store = self.repository._store_blueprint_receipt

            def fail_after_store(*args, **kwargs):
                original_store(*args, **kwargs)
                raise RuntimeError("receipt_fault")

            with (
                patch.object(
                    self.repository,
                    "_store_blueprint_receipt",
                    side_effect=fail_after_store,
                ),
                self.assertRaisesRegex(RuntimeError, "receipt_fault"),
            ):
                self._commit("rolled-back", "rolled-back")
            self.assertNotIn(self.project_id, self.repository._blueprint_verified_prefixes)
            self._commit("second", "second")
            self.assertEqual(audit.call_count, 2)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM creative_blueprint_operations
                        WHERE project_id=?""",
                    (self.project_id,),
                ).fetchone()[0],
                2,
            )

    def test_warm_command_ddl_is_rolled_back_and_never_renews_the_lease(self) -> None:
        original_audit = self.repository._full_audit_blueprint_prefix
        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
        ):
            self._commit("first", "first")
            original_store = self.repository._store_blueprint_receipt

            def drop_trigger_before_receipt(connection, **kwargs):
                connection.execute(
                    "DROP TRIGGER trg_creative_blueprint_operations_no_update"
                )
                return original_store(connection, **kwargs)

            with (
                patch.object(
                    self.repository,
                    "_store_blueprint_receipt",
                    side_effect=drop_trigger_before_receipt,
                ),
            self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_schema_mutation_forbidden",
            ),
            ):
                self._commit("ddl", "ddl")

            self.assertNotIn(
                self.project_id,
                self.repository._blueprint_verified_prefixes,
            )
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                self.assertIsNotNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_schema
                            WHERE type='trigger'
                              AND name='trg_creative_blueprint_operations_no_update'"""
                    ).fetchone()
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT COUNT(*) FROM creative_blueprint_operations
                            WHERE project_id=?""",
                        (self.project_id,),
                    ).fetchone()[0],
                    1,
                )
            self._commit("second", "second")
            self.assertEqual(audit.call_count, 2)

    def test_new_repository_and_pid_mismatch_are_cold(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            real_pid = os.getpid()
            original_audit = self.repository._full_audit_blueprint_prefix
            with (
                patch.object(
                    self.repository,
                    "_full_audit_blueprint_prefix",
                    wraps=original_audit,
                ) as audit,
                patch("core.media_db.os.getpid", return_value=real_pid + 10_000),
            ):
                self._commit("pid-child", "pid-child")
                self.assertEqual(audit.call_count, 1)

        restarted = MediaRepository(self.db_path)
        restarted_service = BlueprintService(restarted)
        held_descriptor: int | None = None
        try:
            assert self.head is not None
            payload = {
                "expected_head": {
                    "revision": self.head["revision"],
                    "content_sha256": self.head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic("restart"),
            }
            original_audit = restarted._full_audit_blueprint_prefix
            with (
                patch.object(
                    restarted,
                    "_verified_prefix_sqlite_is_safe",
                    return_value=True,
                ),
                patch.object(
                    restarted,
                    "_full_audit_blueprint_prefix",
                    wraps=original_audit,
                ) as audit,
            ):
                restarted_service.commit_proposal(
                    self.project_id,
                    payload,
                    idempotency_key="restart",
                )
                self.assertEqual(audit.call_count, 1)
            output_root = restarted.register_preview_root(
                self.db_path.parent.parent / "preview"
            )
            held_descriptor = restarted._output_root_descriptors[str(output_root["id"])]
            restarted._artifact_cache["artifact"] = (1, 1, 1, 1, 1, "digest")
            restarted._source_cache["source"] = (1, 1, 1, 1, 1, "digest")
        finally:
            restarted.close()
            restarted.close()
        assert held_descriptor is not None
        with self.assertRaises(OSError):
            os.fstat(held_descriptor)
        self.assertEqual(restarted._output_root_descriptors, {})
        self.assertEqual(restarted._artifact_cache, {})
        self.assertEqual(restarted._source_cache, {})

    def test_unsafe_runtime_never_opens_anchor_or_publishes_lease(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("first", "first")
            self._commit("second", "second")
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertEqual(self.repository._blueprint_verified_prefixes, {})

    def test_data_only_authorizer_also_guards_non_optimized_transaction(self) -> None:
        original_store = self.repository._store_blueprint_receipt

        def drop_trigger_before_receipt(connection, **kwargs):
            connection.execute(
                "DROP TRIGGER trg_creative_blueprint_operations_no_update"
            )
            return original_store(connection, **kwargs)

        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("first", "first")
            with (
                patch.object(
                    self.repository,
                    "_store_blueprint_receipt",
                    side_effect=drop_trigger_before_receipt,
                ),
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_schema_mutation_forbidden",
                ),
            ):
                self._commit("fallback-ddl", "fallback-ddl")

        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertIsNotNone(
                connection.execute(
                    """SELECT 1 FROM sqlite_schema
                        WHERE type='trigger'
                          AND name='trg_creative_blueprint_operations_no_update'"""
                ).fetchone()
            )

    def test_fallback_cold_audit_rejects_preexisting_physical_schema_damage(
        self,
    ) -> None:
        trigger_name = "trg_creative_blueprint_operations_no_update"
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("fallback-first", "fallback-first")
            before = self._project_ledger_state()
            with closing(sqlite3.connect(self.db_path)) as external, external:
                cookie = int(
                    external.execute("PRAGMA main.schema_version").fetchone()[0]
                )
                external.execute(f"DROP TRIGGER {trigger_name}")
                external.execute(f"PRAGMA main.schema_version={cookie}")

            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=AssertionError("damaged schema reached mutation"),
                ) as mutation,
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_schema_integrity_error",
                ),
            ):
                self._commit("fallback-damaged", "fallback-damaged")

        mutation.assert_not_called()
        self.assertEqual(self._project_ledger_state(), before)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    def test_fallback_rejects_extra_persistent_schema_before_mutation(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("fallback-first", "fallback-first")
            before = self._project_ledger_state()
            with closing(sqlite3.connect(self.db_path)) as external, external:
                external.execute(
                    "CREATE TABLE b1a_unauthorized_effect(value INTEGER NOT NULL)"
                )
                external.execute(
                    "INSERT INTO b1a_unauthorized_effect(value) VALUES(0)"
                )
                external.execute(
                    """CREATE TRIGGER b1a_unauthorized_receipt_effect
                       AFTER INSERT ON blueprint_command_receipts
                       BEGIN
                         UPDATE b1a_unauthorized_effect SET value=value+1;
                       END"""
                )

            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=AssertionError("extra schema reached mutation"),
                ) as mutation,
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_schema_integrity_error",
                ),
            ):
                self._commit("fallback-extra", "fallback-extra")

        mutation.assert_not_called()
        self.assertEqual(self._project_ledger_state(), before)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM b1a_unauthorized_effect"
                ).fetchone(),
                (0,),
            )

    def test_cold_audit_rejects_same_identity_schema_sql_replacements(self) -> None:
        variants = (
            (
                "idx_image_index_taken_at",
                "CREATE INDEX idx_image_index_taken_at ON image_index(taken_at DESC)",
            ),
            (
                "idx_assets_kind_probe",
                "CREATE INDEX idx_assets_kind_probe ON assets(probe_status,kind)",
            ),
            (
                "idx_creator_profiles_created",
                "CREATE INDEX idx_creator_profiles_created "
                "ON creator_profile_revisions(profile_id,created_at ASC)",
            ),
        )
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("schema-sql-first", "schema-sql-first")
            for index, (name, replacement_sql) in enumerate(variants, start=1):
                with self.subTest(name=name):
                    with closing(sqlite3.connect(self.db_path)) as external, external:
                        row = external.execute(
                            "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
                            (name,),
                        ).fetchone()
                        self.assertIsNotNone(row)
                        assert row is not None and type(row[0]) is str
                        original_sql = str(row[0])
                        external.execute(f'DROP INDEX "{name}"')
                        external.execute(replacement_sql)
                    before = self._project_ledger_state()
                    try:
                        with (
                            patch.object(
                                self.service,
                                "_commit_mutation",
                                side_effect=AssertionError(
                                    "same-identity schema replacement reached mutation"
                                ),
                            ) as mutation,
                            self.assertRaisesRegex(
                                BlueprintIntegrityError,
                                "blueprint_schema_integrity_error",
                            ),
                        ):
                            self._commit(
                                f"schema-sql-{index}",
                                f"schema-sql-{index}",
                            )
                        mutation.assert_not_called()
                        self.assertEqual(self._project_ledger_state(), before)
                    finally:
                        with closing(sqlite3.connect(self.db_path)) as external, external:
                            external.execute(f'DROP INDEX "{name}"')
                            external.execute(original_sql)

    def test_fallback_reuses_the_exact_command_delta_validator(self) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=False,
        ):
            self._commit("fallback-first", "fallback-first")
            before = self._project_ledger_state()
            original_mutation = self.service._commit_mutation

            def mutate_unrelated_row(connection, **kwargs):
                result = original_mutation(connection, **kwargs)
                connection.execute(
                    """UPDATE creative_projects SET title=title
                        WHERE id=?""",
                    (self.project_id,),
                )
                return result

            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=mutate_unrelated_row,
                ),
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_command_delta_invalid",
                ),
            ):
                self._commit("fallback-extra-dml", "fallback-extra-dml")

        self.assertEqual(self._project_ledger_state(), before)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    def test_safe_and_fallback_reject_equal_count_mutable_write_substitution(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("mutable-first", "mutable-first")

        def substitute_project_touch(
            connection: sqlite3.Connection,
            project_id: str,
            *,
            updated_at: str | None = None,
        ) -> None:
            del project_id, updated_at
            cursor = connection.execute(
                """UPDATE library_roots SET label=label
                     WHERE id=(SELECT id FROM library_roots ORDER BY id LIMIT 1)"""
            )
            self.assertEqual(cursor.rowcount, 1)

        def corrupt_project_row(
            connection: sqlite3.Connection,
            project_id: str,
            *,
            updated_at: str | None = None,
        ) -> None:
            assert updated_at is not None
            cursor = connection.execute(
                """UPDATE creative_projects
                      SET title=title || ' forged',updated_at=? WHERE id=?""",
                (updated_at, project_id),
            )
            self.assertEqual(cursor.rowcount, 1)

        attacks = (
            ("unrelated-dml", substitute_project_touch),
            ("project-row-corruption", corrupt_project_row),
        )
        for safe, (attack_name, attack) in (
            (safe, attack)
            for safe in (True, False)
            for attack in attacks
        ):
            with self.subTest(safe=safe, attack=attack_name):
                before = self._project_ledger_state()
                with (
                    patch.object(
                        self.repository,
                        "_verified_prefix_sqlite_is_safe",
                        return_value=safe,
                    ),
                    patch.object(
                        self.repository,
                        "touch_creative_project",
                        side_effect=attack,
                    ),
                    self.assertRaisesRegex(
                        BlueprintIntegrityError,
                        "blueprint_command_delta_invalid",
                    ),
                ):
                    self._commit(
                        f"mutable-substitution-{safe}-{attack_name}",
                        f"mutable-substitution-{safe}-{attack_name}",
                    )
                self.assertEqual(self._project_ledger_state(), before)

    def test_attested_floor_commits_every_immutable_prefix_member(self) -> None:
        trigger_name = "trg_creative_blueprint_operations_no_update"
        trigger_sql = self._read_trigger_sql(trigger_name)
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            self._commit("first", "first")
            self._commit("second", "second")
            assert self.head is not None

            with closing(sqlite3.connect(self.db_path)) as external, external:
                external.execute(f"DROP TRIGGER {trigger_name}")
                external.execute(
                    """UPDATE creative_blueprint_operations
                          SET created_at='2030-01-01T00:00:00+00:00'
                        WHERE project_id=? AND sequence=1""",
                    (self.project_id,),
                )
                external.execute(trigger_sql)

            external_repository = MediaRepository(self.db_path)
            external_service = BlueprintService(external_repository)
            try:
                extension = external_service.commit_proposal(
                    self.project_id,
                    {
                        "expected_head": {
                            "revision": self.head["revision"],
                            "content_sha256": self.head["content_sha256"],
                        },
                        "initial_legacy_brief": None,
                        "source_candidate_sha256": None,
                        "semantic": semantic("external-extension-after-rewrite"),
                    },
                    idempotency_key="external-extension-after-rewrite",
                )
            finally:
                external_repository.close()
            self.head = dict(extension.response["result"]["result_head"])
            self.current_semantic = semantic("external-extension-after-rewrite")

            with (
                patch.object(
                    self.service,
                    "_commit_mutation",
                    side_effect=AssertionError("rewritten prefix reached mutation"),
                ) as mutation,
                self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "blueprint_database_binding_indeterminate",
                ),
            ):
                self._commit("must-not-extend-rewrite", "must-not-extend-rewrite")

        mutation.assert_not_called()
        self.assertTrue(self.repository._blueprint_database_binding_failed)
        self.assertIsNone(self.repository._blueprint_anchor)
        self.assertFalse(self.repository._blueprint_verified_prefixes)

    def test_project_leases_are_lru_bounded_to_sixteen(self) -> None:
        fixtures: list[tuple[str, BlueprintService, dict[str, object]]] = []
        for index in range(17):
            project = self.repository.create_project(
                f"LRU {index}",
                {"goal": f"project {index}"},
                {"source": "test"},
            )
            project_id = str(project["id"])
            brief = self.repository.get_brief(project_id, 1)
            assert brief is not None
            fixtures.append(
                (
                    project_id,
                    BlueprintService(self.repository),
                    {
                        "revision": 1,
                        "content_sha256": str(brief["content_sha256"]),
                    },
                )
            )
        with patch.object(
            self.repository,
            "_verified_prefix_sqlite_is_safe",
            return_value=True,
        ):
            for index, (project_id, service, legacy) in enumerate(fixtures):
                service.commit_proposal(
                    project_id,
                    {
                        "expected_head": None,
                        "initial_legacy_brief": legacy,
                        "source_candidate_sha256": None,
                        "semantic": semantic(f"lru-{index}"),
                    },
                    idempotency_key=f"lru-{index}",
                )
        self.assertEqual(len(self.repository._blueprint_verified_prefixes), 16)
        self.assertNotIn(fixtures[0][0], self.repository._blueprint_verified_prefixes)
        self.assertIn(fixtures[-1][0], self.repository._blueprint_verified_prefixes)

    def test_two_real_request_threads_reuse_one_anchor_serially(self) -> None:
        first_done = threading.Event()
        second_done = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def first_request() -> None:
            try:
                self._commit("thread-one", "thread-one")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                first_done.set()
                release.wait(timeout=10)

        def second_request() -> None:
            first_done.wait(timeout=10)
            try:
                self._commit("thread-two", "thread-two")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                second_done.set()
                release.wait(timeout=10)

        original_audit = self.repository._full_audit_blueprint_prefix
        with (
            patch.object(
                self.repository,
                "_verified_prefix_sqlite_is_safe",
                return_value=True,
            ),
            patch.object(
                self.repository,
                "_full_audit_blueprint_prefix",
                wraps=original_audit,
            ) as audit,
        ):
            first = threading.Thread(target=first_request, name="b1a-request-one")
            second = threading.Thread(target=second_request, name="b1a-request-two")
            first.start()
            second.start()
            self.assertTrue(second_done.wait(timeout=10))
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(audit.call_count, 1)
        lease = self.repository._blueprint_verified_prefixes[self.project_id]
        self.assertEqual(lease.operation_sequence, 2)


if __name__ == "__main__":
    unittest.main()
