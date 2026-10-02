from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from core.provider_egress_contract import (
    canonical_sha256,
    seal_provider_egress_grant_intent,
    seal_provider_payload_manifest_plan,
)
from core.provider_egress_persistence import (
    ProviderEgressPersistenceError,
    ProviderEgressPersistenceMixin,
)
from core.provider_egress_schema import V16_SCHEMA_STATEMENTS


DATABASE_UUID = "11111111-1111-4111-8111-111111111111"
JOB_ID = f"job_{'2' * 32}"
ASSET_ID = f"asset_{'3' * 24}"
SOURCE_ID = f"src_{'4' * 24}"
ASSET_SHA256 = hashlib.sha256(b"canonical-image-source").hexdigest()
SOURCE_BINDING_SHA256 = hashlib.sha256(b"canonical-source-binding").hexdigest()
REQUEST_SHA256 = hashlib.sha256(b"canonical-image-request").hexdigest()
PROFILE_SHA256 = hashlib.sha256(b"canonical-image-profile").hexdigest()
PROVIDER_PROFILE_SHA256 = hashlib.sha256(b"provider-profile").hexdigest()
RUNTIME_GENERATION = f"runtime_generation_{'5' * 64}"
ATTEMPT_AUTHORITY_SHA256 = hashlib.sha256(b"attempt-authority").hexdigest()
WIRE_PAYLOAD = b'{"image_digest":"exact-wire-body"}'
WIRE_PAYLOAD_SHA256 = hashlib.sha256(WIRE_PAYLOAD).hexdigest()
TOKEN = "grant-token-that-is-never-persisted-0000000000000001"
RAW_PRIVATE_PATH = "/Users/private/secret/image.png"


SCHEMA = (
    """CREATE TABLE database_meta(
           singleton INTEGER PRIMARY KEY CHECK(singleton=1),
           database_uuid TEXT NOT NULL UNIQUE)""",
    """CREATE TABLE current_image_context(
           job_id TEXT PRIMARY KEY,database_uuid TEXT NOT NULL,
           database_device INTEGER NOT NULL,database_inode INTEGER NOT NULL,
           request_sha256 TEXT NOT NULL,asset_id TEXT NOT NULL,
           source_id TEXT NOT NULL,input_asset_sha256 TEXT NOT NULL,
           source_binding_sha256 TEXT NOT NULL,
           analysis_profile_id TEXT NOT NULL,
           analysis_profile_version TEXT NOT NULL,
           analysis_profile_sha256 TEXT NOT NULL,
           runtime_generation TEXT NOT NULL,job_attempt INTEGER NOT NULL,
           attempt_authority_sha256 TEXT NOT NULL,
           job_kind TEXT NOT NULL,job_status TEXT NOT NULL,
           job_stage TEXT NOT NULL,job_cancel_requested INTEGER NOT NULL,
           attempt_state TEXT NOT NULL,source_current INTEGER NOT NULL)""",
    *V16_SCHEMA_STATEMENTS,
)


class _ProviderEgressRepository(ProviderEgressPersistenceMixin):
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._bound_database_identity: tuple[int, int] | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False):  # type: ignore[no-untyped-def]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _admit_image_database_identity(
        self,
        expected: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        observed = os.stat(self.db_path, follow_symlinks=False)
        current = (int(observed.st_dev), int(observed.st_ino))
        if expected is not None and expected != current:
            raise RuntimeError("image_database_scope_changed")
        if self._bound_database_identity is None:
            self._bound_database_identity = current
        elif self._bound_database_identity != current:
            raise RuntimeError("image_database_scope_changed")
        return current

    @staticmethod
    def _image_meta_in_transaction(connection: sqlite3.Connection) -> dict[str, object]:
        row = connection.execute(
            "SELECT database_uuid FROM database_meta WHERE singleton=1"
        ).fetchone()
        assert row is not None
        return {"database_uuid": str(row["database_uuid"])}

    @staticmethod
    def _provider_egress_current_image_context_in_transaction(
        connection: sqlite3.Connection,
        job_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT * FROM current_image_context WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _provider_egress_require_current_source_in_transaction(
        self,
        connection: sqlite3.Connection,
        context: dict[str, object],
    ) -> None:
        del connection
        if context.get("source_current") != 1:
            raise ProviderEgressPersistenceError("provider_egress_source_scope_changed")


class ProviderEgressPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-provider-egress-")
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "media.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO database_meta(singleton,database_uuid) VALUES(1,?)",
                (DATABASE_UUID,),
            )
            connection.commit()
        finally:
            connection.close()
        self.repository = _ProviderEgressRepository(self.db_path)
        self.database_identity = self.repository._admit_image_database_identity()
        with self.repository.transaction(immediate=True) as transaction:
            transaction.execute(
                """INSERT INTO current_image_context(
                       job_id,database_uuid,database_device,database_inode,
                       request_sha256,asset_id,source_id,input_asset_sha256,
                       source_binding_sha256,analysis_profile_id,
                       analysis_profile_version,analysis_profile_sha256,
                       runtime_generation,job_attempt,attempt_authority_sha256,
                       job_kind,job_status,job_stage,job_cancel_requested,
                       attempt_state,source_current)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                          'image_analysis','running','vision',0,'claimed',1)""",
                (
                    JOB_ID,
                    DATABASE_UUID,
                    self.database_identity[0],
                    self.database_identity[1],
                    REQUEST_SHA256,
                    ASSET_ID,
                    SOURCE_ID,
                    ASSET_SHA256,
                    SOURCE_BINDING_SHA256,
                    "canonical-image-local-v1",
                    "1",
                    PROFILE_SHA256,
                    RUNTIME_GENERATION,
                    1,
                    ATTEMPT_AUTHORITY_SHA256,
                ),
            )
        self.issued_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        self.now = self._iso(self.issued_at + timedelta(seconds=1))
        self.expires_at = self._iso(self.issued_at + timedelta(minutes=5))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def _grant_intent(
        self,
        *,
        suffix: str = "6",
        wire_payload_sha256: str = WIRE_PAYLOAD_SHA256,
        wire_payload_bytes: int = len(WIRE_PAYLOAD),
    ) -> dict[str, object]:
        operation = {
            "capability": "image_vision",
            "database_binding": {
                "database_uuid": DATABASE_UUID,
                "database_device": self.database_identity[0],
                "database_inode": self.database_identity[1],
            },
            "job_id": JOB_ID,
            "request_sha256": REQUEST_SHA256,
            "asset_id": ASSET_ID,
            "source_id": SOURCE_ID,
            "input_asset_sha256": ASSET_SHA256,
            "source_binding_sha256": SOURCE_BINDING_SHA256,
            "analysis_profile": {
                "profile_id": "canonical-image-local-v1",
                "profile_version": "1",
                "profile_sha256": PROFILE_SHA256,
            },
            "provider_profile": {
                "profile_id": "canonical-image-provider-v1",
                "profile_version": "1",
                "profile_sha256": PROVIDER_PROFILE_SHA256,
            },
            "provider": "example-provider",
            "model": "example-vision-v1",
            "purpose": "canonical_image_analysis",
            "payload_class": "derived_image_bytes",
            "wire_payload_sha256": wire_payload_sha256,
            "wire_payload_bytes": wire_payload_bytes,
            "adapter_contract": {
                "adapter_id": "memolens.provider.image-vision",
                "adapter_version": "1",
                "contract_sha256": hashlib.sha256(b"image-vision-adapter-v1").hexdigest(),
            },
        }
        value = {
            "object": "memolens.provider_egress_grant_intent",
            "schema_version": "1",
            "grant_id": f"pgrant_{suffix * 32}",
            **operation,
            "issued_at": self._iso(self.issued_at),
            "expires_at": self.expires_at,
            "native_confirmation": {
                "confirmation_id": f"confirm_{suffix * 32}",
                "operation": "provider_egress.image_vision",
            },
        }
        confirmation_material = deepcopy(value)
        native_confirmation = value["native_confirmation"]
        assert isinstance(native_confirmation, dict)
        native_confirmation["operation_sha256"] = canonical_sha256(confirmation_material)
        return seal_provider_egress_grant_intent(value)

    def _manifest_plan(
        self,
        intent: dict[str, object],
        *,
        suffix: str = "7",
        match_grant: bool = True,
    ) -> tuple[dict[str, object], str]:
        permit = f"permit-that-is-never-persisted-{suffix * 40}"
        permit_sha256 = hashlib.sha256(permit.encode()).hexdigest()
        value = {
            "object": "memolens.provider_payload_manifest_plan",
            "schema_version": "1",
            "manifest_id": f"pmanifest_{suffix * 32}",
            "grant_id": intent["grant_id"],
            "grant_intent_sha256": intent["grant_intent_sha256"],
            "database_binding": deepcopy(intent["database_binding"]),
            "job_id": intent["job_id"],
            "request_sha256": intent["request_sha256"],
            "asset_id": intent["asset_id"],
            "source_id": intent["source_id"],
            "input_asset_sha256": intent["input_asset_sha256"],
            "source_binding_sha256": intent["source_binding_sha256"],
            "analysis_profile": deepcopy(intent["analysis_profile"]),
            "provider_profile": deepcopy(intent["provider_profile"]),
            "provider": intent["provider"],
            "model": intent["model"],
            "capability": intent["capability"],
            "purpose": intent["purpose"],
            "payload_class": intent["payload_class"],
            "wire_payload_sha256": intent["wire_payload_sha256"],
            "wire_payload_bytes": intent["wire_payload_bytes"],
            "adapter_contract": intent["adapter_contract"],
            "runtime_generation": RUNTIME_GENERATION,
            "attempt": 1,
            "attempt_authority_sha256": ATTEMPT_AUTHORITY_SHA256,
        }
        kwargs: dict[str, object] = {"permit_sha256": permit_sha256}
        if match_grant:
            kwargs["grant_intent"] = intent
        return seal_provider_payload_manifest_plan(value, **kwargs), permit_sha256

    def _issue(
        self,
        *,
        intent: dict[str, object] | None = None,
        token: str = TOKEN,
    ) -> dict[str, object]:
        return self.repository.issue_provider_egress_grant(
            grant_intent=intent or self._grant_intent(),
            grant_token=token,
            now=self.now,
        )

    def _issue_and_reserve(
        self,
        *,
        intent: dict[str, object] | None = None,
        token: str = TOKEN,
        manifest_suffix: str = "7",
    ) -> tuple[dict[str, object], dict[str, object], str]:
        exact_intent = intent or self._grant_intent()
        grant = self._issue(intent=exact_intent, token=token)
        plan, permit_sha256 = self._manifest_plan(
            exact_intent,
            suffix=manifest_suffix,
        )
        manifest = self.repository.reserve_provider_egress(
            grant_token=token,
            manifest_plan=plan,
            now=self.now,
        )
        return grant, manifest, permit_sha256

    def _fetch_one(self, table: str) -> dict[str, object]:
        if table not in {"provider_egress_grants", "provider_egress_manifests"}:
            raise AssertionError("unsafe test table")
        with self.repository.transaction() as connection:
            row = connection.execute(f"SELECT * FROM {table}").fetchone()
        assert row is not None
        return dict(row)

    def test_happy_path_consumes_once_and_records_exact_sent_bytes(self) -> None:
        grant, manifest, permit_sha256 = self._issue_and_reserve()
        self.assertEqual(grant["status"], "active")
        self.assertEqual(manifest["status"], "planned")

        sending = self.repository.mark_provider_egress_send_started(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
            now=self.now,
        )
        self.assertEqual(sending["status"], "sending")
        finished = self.repository.finish_provider_egress_send(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            outcome="sent",
            bytes_sent=len(WIRE_PAYLOAD),
            now=self.now,
        )
        self.assertEqual(finished["status"], "sent")
        self.assertEqual(finished["bytes_sent"], len(WIRE_PAYLOAD))
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "consumed")

        plan, _ = self._manifest_plan(self._grant_intent(), suffix="8")
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_grant_unavailable",
        ):
            self.repository.reserve_provider_egress(
                grant_token=TOKEN,
                manifest_plan=plan,
                now=self.now,
            )

    def test_before_send_failure_reactivates_grant_for_exact_new_plan(self) -> None:
        intent = self._grant_intent()
        _, manifest, permit_sha256 = self._issue_and_reserve(intent=intent)
        failed = self.repository.fail_provider_egress_before_send(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            failure_code="provider_unavailable",
            now=self.now,
        )
        self.assertEqual(failed["status"], "failed_before_send")
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "active")

        retry_plan, _ = self._manifest_plan(intent, suffix="8")
        retried = self.repository.reserve_provider_egress(
            grant_token=TOKEN,
            manifest_plan=retry_plan,
            now=self.now,
        )
        self.assertEqual(retried["status"], "planned")
        with self.repository.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_egress_manifests"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_issue_replay_is_exact_and_wrong_token_cannot_rebind_grant(self) -> None:
        intent = self._grant_intent()
        first = self._issue(intent=intent)
        replay = self._issue(intent=intent)
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_grant_conflict",
        ):
            self._issue(
                intent=intent,
                token="different-token-never-persisted-0000000000000003",
            )
        with self.repository.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_egress_grants"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_cancel_and_revoke_never_consume_grant(self) -> None:
        grant, manifest, permit_sha256 = self._issue_and_reserve()
        cancelled = self.repository.fail_provider_egress_before_send(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            failure_code="user_cancelled",
            cancelled=True,
            now=self.now,
        )
        self.assertEqual(cancelled["status"], "cancelled")
        revoked = self.repository.revoke_provider_egress_grant(
            str(grant["id"]),
            now=self.now,
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertIsNone(revoked["consumed_at"])

    def test_revoke_reserved_atomically_cancels_planned_manifest(self) -> None:
        grant, _, _ = self._issue_and_reserve()
        revoked = self.repository.revoke_provider_egress_grant(
            str(grant["id"]),
            now=self.now,
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertIsNone(revoked["consumed_at"])
        self.assertEqual(self._fetch_one("provider_egress_manifests")["status"], "cancelled")

    def test_ambiguous_after_start_is_terminal_and_never_reusable(self) -> None:
        intent = self._grant_intent()
        _, manifest, permit_sha256 = self._issue_and_reserve(intent=intent)
        self.repository.mark_provider_egress_send_started(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
            now=self.now,
        )
        ambiguous = self.repository.finish_provider_egress_send(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            outcome="ambiguous",
            bytes_sent=None,
            failure_code="transport_outcome_unknown",
            now=self.now,
        )
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "ambiguous")

        plan, _ = self._manifest_plan(intent, suffix="8")
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_grant_unavailable",
        ):
            self.repository.reserve_provider_egress(
                grant_token=TOKEN,
                manifest_plan=plan,
                now=self.now,
            )

    def test_known_failure_after_send_stays_consumed_and_is_not_retried(self) -> None:
        intent = self._grant_intent()
        _, manifest, permit_sha256 = self._issue_and_reserve(intent=intent)
        self.repository.mark_provider_egress_send_started(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
            now=self.now,
        )
        failed = self.repository.finish_provider_egress_send(
            manifest_id=str(manifest["id"]),
            permit_sha256=permit_sha256,
            outcome="failed_after_send",
            bytes_sent=1,
            failure_code="provider_rejected_after_send",
            now=self.now,
        )
        self.assertEqual(failed["status"], "failed_after_send")
        grant = self._fetch_one("provider_egress_grants")
        self.assertEqual(grant["status"], "consumed")
        self.assertIsNotNone(grant["terminal_at"])

    def test_wrong_permit_cannot_start_send_or_mutate_reservation(self) -> None:
        _, manifest, _ = self._issue_and_reserve()
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_manifest_missing",
        ):
            self.repository.mark_provider_egress_send_started(
                manifest_id=str(manifest["id"]),
                permit_sha256="f" * 64,
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
                now=self.now,
            )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "reserved")
        self.assertEqual(self._fetch_one("provider_egress_manifests")["status"], "planned")

    def test_startup_recovery_releases_planned_but_ambiguates_sending(self) -> None:
        first_intent = self._grant_intent(suffix="6")
        _, first_manifest, _ = self._issue_and_reserve(
            intent=first_intent,
            token=TOKEN,
            manifest_suffix="7",
        )
        second_token = "second-grant-token-never-persisted-0000000000000002"
        second_intent = self._grant_intent(suffix="8")
        _, second_manifest, second_permit = self._issue_and_reserve(
            intent=second_intent,
            token=second_token,
            manifest_suffix="9",
        )
        self.repository.mark_provider_egress_send_started(
            manifest_id=str(second_manifest["id"]),
            permit_sha256=second_permit,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
            now=self.now,
        )

        recovered = self.repository.recover_provider_egress_state(now=self.now)
        self.assertEqual(recovered, {"failed_before_send": 1, "ambiguous": 1})
        self.assertEqual(
            self.repository.recover_provider_egress_state(now=self.now),
            {"failed_before_send": 0, "ambiguous": 0},
        )
        with self.repository.transaction() as connection:
            states = {
                row["id"]: row["status"]
                for row in connection.execute(
                    "SELECT id,status FROM provider_egress_manifests"
                ).fetchall()
            }
            grants = {
                row["id"]: row["status"]
                for row in connection.execute(
                    "SELECT id,status FROM provider_egress_grants"
                ).fetchall()
            }
        self.assertEqual(states[first_manifest["id"]], "failed_before_send")
        self.assertEqual(states[second_manifest["id"]], "ambiguous")
        self.assertEqual(grants[first_intent["grant_id"]], "active")
        self.assertEqual(grants[second_intent["grant_id"]], "ambiguous")

    def test_reserve_and_send_start_revalidate_current_attempt_and_source(self) -> None:
        intent = self._grant_intent()
        self._issue(intent=intent)
        plan, permit_sha256 = self._manifest_plan(intent)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE current_image_context SET source_current=0 WHERE job_id=?",
                (JOB_ID,),
            )
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_source_scope_changed",
        ):
            self.repository.reserve_provider_egress(
                grant_token=TOKEN,
                manifest_plan=plan,
                now=self.now,
            )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "active")
        with self.repository.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_egress_manifests"
            ).fetchone()[0]
        self.assertEqual(count, 0)

        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE current_image_context SET source_current=1 WHERE job_id=?",
                (JOB_ID,),
            )
        manifest = self.repository.reserve_provider_egress(
            grant_token=TOKEN,
            manifest_plan=plan,
            now=self.now,
        )
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE current_image_context
                      SET runtime_generation=? WHERE job_id=?""",
                (f"runtime_generation_{'a' * 64}", JOB_ID),
            )
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_job_scope_changed",
        ):
            self.repository.mark_provider_egress_send_started(
                manifest_id=str(manifest["id"]),
                permit_sha256=permit_sha256,
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
                now=self.now,
            )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "reserved")
        self.assertEqual(self._fetch_one("provider_egress_manifests")["status"], "planned")

    def test_reserve_rejects_every_changed_job_request_asset_source_and_profile_field(self) -> None:
        intent = self._grant_intent()
        self._issue(intent=intent)
        plan, _ = self._manifest_plan(intent)
        mutations: tuple[tuple[str, object, object], ...] = (
            ("request_sha256", "a" * 64, REQUEST_SHA256),
            ("asset_id", f"asset_{'a' * 24}", ASSET_ID),
            ("source_id", f"src_{'b' * 24}", SOURCE_ID),
            ("input_asset_sha256", "b" * 64, ASSET_SHA256),
            ("source_binding_sha256", "c" * 64, SOURCE_BINDING_SHA256),
            ("analysis_profile_id", "other-image-profile", "canonical-image-local-v1"),
            ("analysis_profile_version", "2", "1"),
            ("analysis_profile_sha256", "d" * 64, PROFILE_SHA256),
            ("runtime_generation", f"runtime_generation_{'e' * 64}", RUNTIME_GENERATION),
            ("job_attempt", 2, 1),
            ("attempt_authority_sha256", "f" * 64, ATTEMPT_AUTHORITY_SHA256),
        )
        for column, replacement, original in mutations:
            with self.subTest(column=column):
                with self.repository.transaction(immediate=True) as connection:
                    connection.execute(
                        f"UPDATE current_image_context SET {column}=? WHERE job_id=?",
                        (replacement, JOB_ID),
                    )
                with self.assertRaisesRegex(
                    ProviderEgressPersistenceError,
                    "provider_egress_job_scope_changed",
                ):
                    self.repository.reserve_provider_egress(
                        grant_token=TOKEN,
                        manifest_plan=plan,
                        now=self.now,
                    )
                with self.repository.transaction(immediate=True) as connection:
                    connection.execute(
                        f"UPDATE current_image_context SET {column}=? WHERE job_id=?",
                        (original, JOB_ID),
                    )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "active")
        with self.repository.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_egress_manifests"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_grant_manifest_provider_and_payload_scope_are_exact(self) -> None:
        intent = self._grant_intent()
        self._issue(intent=intent)
        for key, replacement in (
            ("provider", "other-provider"),
            ("model", "other-model"),
            ("wire_payload_sha256", "f" * 64),
            ("wire_payload_bytes", len(WIRE_PAYLOAD) + 1),
        ):
            with self.subTest(key=key):
                forged = deepcopy(intent)
                forged[key] = replacement
                plan, _ = self._manifest_plan(
                    forged,
                    suffix="a",
                    match_grant=False,
                )
                with self.assertRaisesRegex(
                    ProviderEgressPersistenceError,
                    "provider_egress_grant_scope_mismatch",
                ):
                    self.repository.reserve_provider_egress(
                        grant_token=TOKEN,
                        manifest_plan=plan,
                        now=self.now,
                    )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "active")
        with self.repository.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_egress_manifests"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_only_one_of_two_concurrent_reservations_wins(self) -> None:
        intent = self._grant_intent()
        self._issue(intent=intent)
        plans = [self._manifest_plan(intent, suffix=suffix)[0] for suffix in ("7", "8")]
        barrier = threading.Barrier(2)

        def reserve(plan: dict[str, object]) -> str:
            barrier.wait()
            try:
                result = self.repository.reserve_provider_egress(
                    grant_token=TOKEN,
                    manifest_plan=plan,
                    now=self.now,
                )
            except ProviderEgressPersistenceError as exc:
                return exc.code
            return str(result["id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, plans))
        self.assertEqual(sum(value.startswith("pmanifest_") for value in outcomes), 1)
        self.assertIn("provider_egress_grant_unavailable", outcomes)
        with self.repository.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM provider_egress_manifests"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_expired_and_revoked_grants_fail_before_manifest_write(self) -> None:
        expired_intent = self._grant_intent(suffix="6")
        self._issue(intent=expired_intent)
        expired_at = self._iso(self.issued_at + timedelta(minutes=6))
        result = self.repository.expire_provider_egress_grants(now=expired_at)
        self.assertEqual(result, {"active": 1, "reserved": 0})
        plan, _ = self._manifest_plan(expired_intent)
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_grant_expired",
        ):
            self.repository.reserve_provider_egress(
                grant_token=TOKEN,
                manifest_plan=plan,
                now=expired_at,
            )

    def test_grant_expiring_after_reservation_cannot_start_send(self) -> None:
        _, manifest, permit_sha256 = self._issue_and_reserve()
        after_expiry = self._iso(self.issued_at + timedelta(minutes=6))
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_grant_expired",
        ):
            self.repository.mark_provider_egress_send_started(
                manifest_id=str(manifest["id"]),
                permit_sha256=permit_sha256,
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
                now=after_expiry,
            )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "reserved")
        self.assertEqual(self._fetch_one("provider_egress_manifests")["status"], "planned")

        expired = self.repository.expire_provider_egress_grants(now=after_expiry)
        self.assertEqual(expired, {"active": 0, "reserved": 1})
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "expired")
        self.assertEqual(self._fetch_one("provider_egress_manifests")["status"], "cancelled")

    def test_token_permit_path_and_raw_wire_body_are_never_persisted(self) -> None:
        _, _, permit_sha256 = self._issue_and_reserve()
        with self.repository.transaction() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(TOKEN, dump)
        self.assertNotIn(RAW_PRIVATE_PATH, dump)
        self.assertNotIn(WIRE_PAYLOAD.decode(), dump)
        self.assertNotIn("permit-that-is-never-persisted", dump)
        self.assertIn(hashlib.sha256(TOKEN.encode()).hexdigest(), dump)
        self.assertIn(permit_sha256, dump)

    def test_canonical_document_tamper_blocks_transition_without_state_change(self) -> None:
        _, manifest, permit_sha256 = self._issue_and_reserve()
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "DROP TRIGGER trg_provider_egress_manifests_identity_immutable"
            )
            connection.execute(
                "DROP TRIGGER trg_provider_egress_manifests_state_transition"
            )
            connection.execute(
                """UPDATE provider_egress_manifests
                      SET manifest_plan_json=replace(
                        manifest_plan_json,'example-provider','evil-provider')
                    WHERE id=?""",
                (manifest["id"],),
            )
        with self.assertRaisesRegex(
            ProviderEgressPersistenceError,
            "provider_egress_manifest_corrupt",
        ):
            self.repository.mark_provider_egress_send_started(
                manifest_id=str(manifest["id"]),
                permit_sha256=permit_sha256,
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                attempt_authority_sha256=ATTEMPT_AUTHORITY_SHA256,
                now=self.now,
            )
        self.assertEqual(self._fetch_one("provider_egress_grants")["status"], "reserved")


if __name__ == "__main__":
    unittest.main()
