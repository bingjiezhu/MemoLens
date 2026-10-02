"""Fail-closed read validation for B1 Blueprint command receipts.

The desktop writer stores one command receipt for every persisted Blueprint
operation.  B1 adds a second, mutually exclusive receipt path for paired
Agents.  This module validates both paths from a read-only SQLite snapshot and
returns only aggregate counts; capability secrets and receipt bodies never
leave the validator.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import re
import sqlite3
from typing import Any, Mapping

from memolens_blueprint_authority import (
    CURRENT_MANAGED_SCHEMA_VERSION,
    validate_exact_managed_schema_manifest,
    validate_v13_agent_project_receipt_census,
)
from memolens_contracts import MemoLensError
from memolens_creative_blueprint import canonical_json, canonical_sha256
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json


AGENT_CAPABILITY_COLUMNS = {
    "id",
    "database_uuid",
    "runtime_authority_epoch",
    "project_id",
    "paired_subject_id",
    "claimed_client_label",
    "actions_json",
    "secret_sha256",
    "issued_at",
    "expires_at",
    "max_operations",
    "pairing_receipt_id",
    "facts_sha256",
}
AGENT_PAIRING_RECEIPT_COLUMNS = {
    "id",
    "database_uuid",
    "runtime_authority_epoch",
    "pairing_id",
    "capability_id",
    "project_id",
    "pairing_request_sha256",
    "observed_revision",
    "observed_content_sha256",
    "presentation_json",
    "presentation_sha256",
    "native_gesture_nonce",
    "receipt_sha256",
    "created_at",
}
AGENT_COMMAND_RECEIPT_COLUMNS = {
    "id",
    "database_uuid",
    "capability_id",
    "paired_subject_id",
    "project_id",
    "command_type",
    "command_version",
    "idempotency_key",
    "request_sha256",
    "operation_id",
    "response_status",
    "response_json",
    "response_sha256",
    "receipt_sha256",
    "resource_type",
    "resource_id",
    "created_at",
}
AGENT_PROJECT_COMMAND_RECEIPT_COLUMNS = {
    "id",
    "database_uuid",
    "authenticated_principal",
    "capability_id",
    "paired_subject_id",
    "claimed_client_label",
    "project_id",
    "command_type",
    "command_version",
    "idempotency_key",
    "request_json",
    "request_sha256",
    "actor_json",
    "origin_json",
    "operation_id",
    "response_status",
    "response_json",
    "response_sha256",
    "receipt_sha256",
    "resource_type",
    "resource_id",
    "created_at",
    "receipt_schema_version",
    "request_capture",
}
AGENT_CAPABILITY_EVENT_COLUMNS = {
    "id",
    "capability_id",
    "project_id",
    "sequence",
    "parent_event_id",
    "parent_event_sha256",
    "event_schema_version",
    "event_type",
    "action",
    "operation_id",
    "agent_command_receipt_id",
    "agent_project_command_receipt_id",
    "reason_code",
    "presentation_sha256",
    "native_gesture_nonce",
    "event_sha256",
    "created_at",
}
AGENT_RECEIPT_RELATIONS = {
    "agent_project_capabilities": AGENT_CAPABILITY_COLUMNS,
    "agent_pairing_confirmation_receipts": AGENT_PAIRING_RECEIPT_COLUMNS,
    "agent_project_command_receipts": AGENT_PROJECT_COMMAND_RECEIPT_COLUMNS,
    "agent_project_capability_events": AGENT_CAPABILITY_EVENT_COLUMNS,
}

DESKTOP_RECEIPT_COLUMNS = {
    "database_uuid",
    "authenticated_principal",
    "project_id",
    "command_type",
    "command_version",
    "idempotency_key",
    "request_sha256",
    "operation_id",
    "response_status",
    "response_json",
    "response_sha256",
    "resource_type",
    "resource_id",
    "created_at",
}
DESKTOP_RECEIPT_INTEGRITY_COLUMNS = {
    "project_id",
    "operation_id",
    "receipt_sha256",
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$")
_BLUEPRINT_COMMAND_TYPES = (
    "blueprint.commit_proposal",
    "blueprint.restore_revision",
)
_TIMELINE_COMMAND_TYPES = (
    "timeline.apply_edit",
    "timeline.apply_structural_edit",
    "timeline.restore_revision",
)
_TIMELINE_BINDING_ACTIONS = (*_TIMELINE_COMMAND_TYPES, "timeline.preview_media")
_CAPABILITY_ACTIONS = (*_BLUEPRINT_COMMAND_TYPES, *_TIMELINE_BINDING_ACTIONS)
_JSON_LIMITS = StrictJsonLimits(
    max_bytes=262_144,
    max_depth=16,
    max_nodes=20_000,
    max_object_items=512,
    max_array_items=512,
    max_string_chars=12_000,
)
_RECEIPT_JSON_LIMITS = StrictJsonLimits(
    max_bytes=1_048_576,
    max_depth=16,
    max_nodes=20_000,
    max_object_items=512,
    max_array_items=512,
    max_string_chars=12_000,
)
_TIMELINE_REPLAY_JSON_LIMITS = StrictJsonLimits(
    max_bytes=16 * 1_048_576,
    max_depth=32,
    max_nodes=500_000,
    max_object_items=2_048,
    max_array_items=200_000,
    max_string_chars=2_000_000,
)
_MAX_OPERATIONS = 1_000_000
_MAX_CAPABILITIES = 1_000_000
_MAX_EVENTS_PER_CAPABILITY = 34


def _corrupt() -> MemoLensError:
    return MemoLensError(
        "Persisted Creative Blueprint command receipts failed integrity validation.",
        code="blueprint_receipt_integrity_error",
    )


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _valid_revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= 1_000_000


def _timestamp(value: object) -> datetime:
    if not (isinstance(value, str) and value.isascii() and _UTC_TIMESTAMP.fullmatch(value) is not None):
        raise _corrupt()
    try:
        parsed = datetime.fromisoformat(value)
    except (OverflowError, ValueError) as exc:
        raise _corrupt() from exc
    if parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise _corrupt()
    return parsed


def _strict_json_with_limits(
    raw: object,
    expected_type: type[Any],
    *,
    limits: StrictJsonLimits,
) -> Any:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limits.max_bytes:
        raise _corrupt()
    try:
        value = decode_strict_json(
            raw.encode("utf-8"),
            require_object=expected_type is dict,
            limits=limits,
        )
        normalized = canonical_json(value)
    except (
        UnicodeError,
        StrictJsonError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise _corrupt() from exc
    if type(value) is not expected_type or normalized != raw:
        raise _corrupt()
    return value


def _strict_json(raw: object, expected_type: type[Any]) -> Any:
    return _strict_json_with_limits(raw, expected_type, limits=_JSON_LIMITS)


def _strict_receipt_json(raw: object) -> dict[str, Any]:
    return _strict_json_with_limits(raw, dict, limits=_RECEIPT_JSON_LIMITS)


def _canonical_sha256(value: object) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise _corrupt() from exc


def _database_identity(connection: sqlite3.Connection) -> tuple[str, int]:
    try:
        rows = connection.execute("SELECT database_uuid,schema_version FROM database_meta WHERE singleton=1").fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if (
        len(rows) != 1
        or not _valid_identifier(rows[0]["database_uuid"])
        or type(rows[0]["schema_version"]) is not int
        or not 1 <= int(rows[0]["schema_version"]) <= 1_000_000
    ):
        raise _corrupt()
    return str(rows[0]["database_uuid"]), int(rows[0]["schema_version"])


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error as exc:
        raise _corrupt() from exc


def _preflight_json_budget(
    connection: sqlite3.Connection,
    *,
    table: str,
    project_id: str,
    columns: tuple[str, ...],
    expected_count: int,
    max_bytes: int = _JSON_LIMITS.max_bytes,
    additional_predicate: str = "",
    additional_parameters: tuple[object, ...] = (),
) -> None:
    """Bound each stored JSON value before sqlite3 materializes its body."""

    length_terms = [f"length(CAST({column} AS BLOB))" for column in columns]
    select = ["COUNT(*) AS row_count"]
    select.extend(f"MAX({term}) AS max_{index}" for index, term in enumerate(length_terms))
    select.extend(
        "COALESCE(SUM(CASE WHEN "
        f"typeof({column})='text' THEN 0 ELSE 1 END),0) AS non_text_{index}"
        for index, column in enumerate(columns)
    )
    try:
        row = connection.execute(
            f"SELECT {','.join(select)} FROM {table} "
            f"WHERE project_id=?{additional_predicate}",
            (project_id, *additional_parameters),
        ).fetchone()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if not (
        row is not None
        and row["row_count"] == expected_count
        and all(row[f"non_text_{index}"] == 0 for index in range(len(columns)))
        and all(
            row[f"max_{index}"] is None or (type(row[f"max_{index}"]) is int and 0 <= row[f"max_{index}"] <= max_bytes)
            for index in range(len(columns))
        )
    ):
        raise _corrupt()


def agent_receipt_schema_available(connection: sqlite3.Connection) -> bool:
    """Require the exact managed V13 typed receipt union."""

    _database_uuid, schema_version = _database_identity(connection)
    observed = {table: _columns(connection, table) for table in AGENT_RECEIPT_RELATIONS}
    present = {table for table, columns in observed.items() if columns}
    if present != set(AGENT_RECEIPT_RELATIONS):
        raise _corrupt()
    for table, required in AGENT_RECEIPT_RELATIONS.items():
        if not required <= observed[table]:
            raise _corrupt()
    if schema_version != CURRENT_MANAGED_SCHEMA_VERSION:
        raise _corrupt()
    try:
        validate_exact_managed_schema_manifest(connection)
    except MemoLensError as exc:
        raise _corrupt() from exc
    if not DESKTOP_RECEIPT_COLUMNS <= _columns(connection, "blueprint_command_receipts"):
        raise _corrupt()
    if not DESKTOP_RECEIPT_INTEGRITY_COLUMNS <= _columns(connection, "blueprint_desktop_receipt_integrity"):
        raise _corrupt()
    return True


def _operation_principal(actor: Any, origin: Any) -> str:
    if actor == {
        "identity_verified": True,
        "kind": "desktop_app_command",
        "user_authority_verified": False,
    } and origin == {
        "principal": "desktop_app",
        "surface": "desktop_authenticated_api",
    }:
        return "desktop_app"
    if not (
        type(actor) is dict
        and set(actor)
        == {
            "capability_id",
            "claimed_client_label",
            "identity_verified",
            "kind",
            "paired_subject_id",
            "user_authority_verified",
            "vendor_identity_verified",
        }
        and actor.get("kind") == "paired_agent_command"
        and actor.get("identity_verified") is True
        and actor.get("vendor_identity_verified") is False
        and actor.get("user_authority_verified") is False
        and _valid_identifier(actor.get("capability_id"))
        and _valid_identifier(actor.get("paired_subject_id"))
        and _valid_label(actor.get("claimed_client_label"))
        and origin == {"principal": "paired_agent", "surface": "agent_cli"}
    ):
        raise _corrupt()
    return "paired_agent"


def _valid_label(value: object) -> bool:
    return bool(
        type(value) is str
        and 1 <= len(value) <= 200
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _valid_timeline_idempotency_key(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value[0].isascii()
        and value[0].isalnum()
        and all(
            character.isascii()
            and (character.isalnum() or character in "._:-")
            for character in value
        )
    )


def _valid_structural_edit(value: object) -> bool:
    """Mirror the closed V18 split/delete intent without importing Core."""

    if type(value) is not dict:
        return False
    op = value.get("op")
    if op == "split_clip":
        return bool(
            set(value) == {"op", "clip_id", "source_split_ms"}
            and _valid_identifier(value.get("clip_id"))
            and type(value.get("source_split_ms")) is int
            and 0 <= value["source_split_ms"] <= 9_007_199_254_740_991
        )
    if op == "delete_clip":
        return set(value) == {"op", "clip_id"} and _valid_identifier(
            value.get("clip_id")
        )
    return False


_OPERATION_READ_COLUMNS = (
    "id",
    "project_id",
    "sequence",
    "command_type",
    "command_version",
    "actor_json",
    "origin_json",
    "input_sha256",
    "output_sha256",
    "result_kind",
    "result_revision",
    "result_content_sha256",
    "result_json",
    "created_at",
)


def _prefixed_row(row: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    return {column: row[f"{prefix}{column}"] for column in _OPERATION_READ_COLUMNS}


def _operation_select(alias: str, prefix: str) -> str:
    return ",".join(f"{alias}.{column} AS {prefix}{column}" for column in _OPERATION_READ_COLUMNS)


def _operation_from_row(
    row: Mapping[str, Any],
    *,
    project_id: str,
    expected_sequence: int | None = None,
) -> dict[str, Any]:
    actor = _strict_json(row["actor_json"], dict)
    origin = _strict_json(row["origin_json"], dict)
    result = _strict_json(row["result_json"], dict)
    operation_id = row["id"]
    result_head = result.get("result_head")
    if not (
        _valid_identifier(operation_id)
        and row["project_id"] == project_id
        and _valid_revision(row["sequence"])
        and (expected_sequence is None or row["sequence"] == expected_sequence)
        and row["command_type"] in _BLUEPRINT_COMMAND_TYPES
        and row["command_version"] == "1"
        and _valid_digest(row["input_sha256"])
        and _valid_digest(row["output_sha256"])
        and row["result_kind"] in {"revision_created", "no_change", "revision_restored"}
        and _valid_revision(row["result_revision"])
        and _valid_digest(row["result_content_sha256"])
        and type(result_head) is dict
        and set(result) == {"kind", "result_head", "changed_sections", "restore_from"}
        and set(result_head)
        == {
            "revision",
            "content_sha256",
            "semantic_sha256",
            "operation_id",
        }
        and result.get("kind") == row["result_kind"]
        and result_head.get("revision") == row["result_revision"]
        and result_head.get("content_sha256") == row["result_content_sha256"]
        and result_head.get("content_sha256") == row["output_sha256"]
        and _valid_digest(result_head.get("semantic_sha256"))
        and _valid_identifier(result_head.get("operation_id"))
    ):
        raise _corrupt()
    return {
        "id": operation_id,
        "project_id": project_id,
        "command_type": row["command_type"],
        "command_version": "1",
        "input_sha256": row["input_sha256"],
        "result_kind": row["result_kind"],
        "result_revision": row["result_revision"],
        "result": result,
        "actor": actor,
        "principal": _operation_principal(actor, origin),
        "created_at": _timestamp(row["created_at"]),
    }


def _expected_response(operation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "object": "creative_blueprint.command_result",
        "schema_version": "1",
        "project_id": operation["project_id"],
        "command_type": operation["command_type"],
        "operation_id": operation["id"],
        "result": operation["result"],
        "authority": {"state": "unverified", "verified": False},
        "ledger": {
            "coverage_scope": "creative_blueprint",
            "complete_project_history": False,
        },
    }


def _validate_receipt_envelope(
    receipt: Mapping[str, Any],
    operation: Mapping[str, Any],
    *,
    database_uuid: str,
) -> datetime:
    response = _strict_receipt_json(receipt.get("response_json"))
    expected_status = 200 if operation["result_kind"] == "no_change" else 201
    if not (
        receipt.get("database_uuid") == database_uuid
        and receipt.get("project_id") == operation["project_id"]
        and receipt.get("command_type") == operation["command_type"]
        and receipt.get("command_version") == operation["command_version"]
        and type(receipt.get("idempotency_key")) is str
        and 1 <= len(receipt["idempotency_key"]) <= 256
        and receipt.get("request_sha256") == operation["input_sha256"]
        and receipt.get("operation_id") == operation["id"]
        and receipt.get("response_status") == expected_status
        and receipt.get("resource_type") == "creative_blueprint"
        and receipt.get("resource_id") == f"{operation['project_id']}:{operation['result_revision']}"
        and response == _expected_response(operation)
        and _valid_digest(receipt.get("response_sha256"))
        and hashlib.sha256(receipt["response_json"].encode()).hexdigest() == receipt["response_sha256"]
    ):
        raise _corrupt()
    created_at = _timestamp(receipt.get("created_at"))
    return created_at


_DESKTOP_RECEIPT_READ_COLUMNS = tuple(sorted(DESKTOP_RECEIPT_COLUMNS))
_AGENT_RECEIPT_READ_COLUMNS = tuple(sorted(AGENT_PROJECT_COMMAND_RECEIPT_COLUMNS))


def _relation_select(alias: str, prefix: str, columns: tuple[str, ...]) -> str:
    return ",".join(f"{alias}.{column} AS {prefix}{column}" for column in columns)


def _relation_from_row(
    row: Mapping[str, Any],
    prefix: str,
    columns: tuple[str, ...],
) -> dict[str, Any]:
    return {column: row[f"{prefix}{column}"] for column in columns}


def _receipt_counts_and_preflight(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    agent_available: bool,
) -> tuple[int, int]:
    try:
        desktop_count = connection.execute(
            "SELECT COUNT(*) FROM blueprint_command_receipts WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        agent_count = (
            connection.execute(
                """SELECT COUNT(*) FROM agent_project_command_receipts
                    WHERE project_id=?
                      AND receipt_schema_version='1'
                      AND request_capture='digest_only_legacy'
                      AND resource_type='creative_blueprint'""",
                (project_id,),
            ).fetchone()[0]
            if agent_available
            else 0
        )
        desktop_integrity_count = connection.execute(
            """SELECT COUNT(*) FROM blueprint_desktop_receipt_integrity
                WHERE project_id=?""",
            (project_id,),
        ).fetchone()[0]
        desktop_integrity_mismatch = connection.execute(
            """SELECT 1 FROM blueprint_command_receipts receipt
                 WHERE receipt.project_id=?
                   AND (SELECT COUNT(*)
                          FROM blueprint_desktop_receipt_integrity integrity
                         WHERE integrity.project_id=receipt.project_id
                           AND integrity.operation_id=receipt.operation_id) != 1
                UNION ALL
               SELECT 1 FROM blueprint_desktop_receipt_integrity integrity
                 WHERE integrity.project_id=?
                   AND NOT EXISTS (
                     SELECT 1 FROM blueprint_command_receipts receipt
                      WHERE receipt.project_id=integrity.project_id
                        AND receipt.operation_id=integrity.operation_id)
                LIMIT 1""",
            (project_id, project_id),
        ).fetchone()
        if (
            type(desktop_count) is not int
            or type(agent_count) is not int
            or type(desktop_integrity_count) is not int
            or not 0 <= desktop_count <= _MAX_OPERATIONS
            or not 0 <= agent_count <= _MAX_OPERATIONS
            or desktop_integrity_count != desktop_count
            or desktop_integrity_mismatch is not None
        ):
            raise _corrupt()
        _preflight_json_budget(
            connection,
            table="blueprint_command_receipts",
            project_id=project_id,
            columns=("response_json",),
            expected_count=desktop_count,
            max_bytes=_RECEIPT_JSON_LIMITS.max_bytes,
        )
        if agent_available:
            _preflight_json_budget(
                connection,
                table="agent_project_command_receipts",
                project_id=project_id,
                columns=("response_json",),
                expected_count=agent_count,
                max_bytes=_RECEIPT_JSON_LIMITS.max_bytes,
                additional_predicate=(
                    " AND receipt_schema_version='1'"
                    " AND request_capture='digest_only_legacy'"
                    " AND resource_type='creative_blueprint'"
                ),
            )
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    return desktop_count, agent_count


def _validate_desktop_receipt(
    receipt: Mapping[str, Any],
    operation: Mapping[str, Any],
    *,
    database_uuid: str,
    receipt_sha256: object,
) -> None:
    if operation["principal"] != "desktop_app" or receipt.get("authenticated_principal") != "desktop_app":
        raise _corrupt()
    _validate_receipt_envelope(receipt, operation, database_uuid=database_uuid)
    receipt_material = {
        "database_uuid": receipt.get("database_uuid"),
        "authenticated_principal": receipt.get("authenticated_principal"),
        "project_id": receipt.get("project_id"),
        "command_type": receipt.get("command_type"),
        "command_version": receipt.get("command_version"),
        "idempotency_key": receipt.get("idempotency_key"),
        "request_sha256": receipt.get("request_sha256"),
        "operation_id": receipt.get("operation_id"),
        "response_status": receipt.get("response_status"),
        "response_sha256": receipt.get("response_sha256"),
        "resource_type": receipt.get("resource_type"),
        "resource_id": receipt.get("resource_id"),
        "created_at": receipt.get("created_at"),
    }
    if not (_valid_digest(receipt_sha256) and _canonical_sha256(receipt_material) == receipt_sha256):
        raise _corrupt()


def _validate_operation_receipt_stream(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    database_uuid: str,
    expected_desktop_receipts: int,
    expected_agent_receipts: int,
) -> tuple[int, int, int]:
    try:
        operation_count = connection.execute(
            "SELECT COUNT(*) FROM creative_blueprint_operations WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        if type(operation_count) is not int or not 0 <= operation_count <= _MAX_OPERATIONS:
            raise _corrupt()
        _preflight_json_budget(
            connection,
            table="creative_blueprint_operations",
            project_id=project_id,
            columns=("actor_json", "origin_json", "result_json"),
            expected_count=operation_count,
        )
        cursor = connection.execute(
            f"""SELECT {_operation_select("o", "o_")},
                       {_relation_select("d", "d_", _DESKTOP_RECEIPT_READ_COLUMNS)},
                       di.receipt_sha256 AS desktop_receipt_sha256,
                       a.id AS agent_receipt_id
                  FROM creative_blueprint_operations o
                  LEFT JOIN blueprint_command_receipts d
                    ON d.project_id=o.project_id AND d.operation_id=o.id
                  LEFT JOIN blueprint_desktop_receipt_integrity di
                    ON di.project_id=d.project_id AND di.operation_id=d.operation_id
                  LEFT JOIN agent_project_command_receipts a
                    ON a.project_id=o.project_id AND a.operation_id=o.id
                   AND a.receipt_schema_version='1'
                   AND a.request_capture='digest_only_legacy'
                   AND a.resource_type='creative_blueprint'
                 WHERE o.project_id=? ORDER BY o.sequence""",
            (project_id,),
        )
        observed = 0
        desktop_count = 0
        agent_count = 0
        for sequence, row in enumerate(cursor, start=1):
            observed += 1
            operation = _operation_from_row(
                _prefixed_row(row, "o_"),
                project_id=project_id,
                expected_sequence=sequence,
            )
            has_desktop = row["d_operation_id"] is not None
            has_agent = row["agent_receipt_id"] is not None
            if has_desktop == has_agent:
                raise _corrupt()
            if has_desktop:
                if operation["principal"] != "desktop_app":
                    raise _corrupt()
                receipt = _relation_from_row(
                    row,
                    "d_",
                    _DESKTOP_RECEIPT_READ_COLUMNS,
                )
                _validate_desktop_receipt(
                    receipt,
                    operation,
                    database_uuid=database_uuid,
                    receipt_sha256=row["desktop_receipt_sha256"],
                )
                desktop_count += 1
            else:
                if operation["principal"] != "paired_agent":
                    raise _corrupt()
                agent_count += 1
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if (
        observed != operation_count
        or desktop_count != expected_desktop_receipts
        or agent_count != expected_agent_receipts
        or desktop_count + agent_count != operation_count
    ):
        raise _corrupt()
    return operation_count, desktop_count, agent_count


def _capability_facts(capability: Mapping[str, Any], actions: list[str]) -> dict[str, Any]:
    return {
        "id": capability["id"],
        "database_uuid": capability["database_uuid"],
        "runtime_authority_epoch": capability["runtime_authority_epoch"],
        "project_id": capability["project_id"],
        "paired_subject_id": capability["paired_subject_id"],
        "claimed_client_label": capability["claimed_client_label"],
        "actions": actions,
        "secret_sha256": capability["secret_sha256"],
        "issued_at": capability["issued_at"],
        "expires_at": capability["expires_at"],
        "max_operations": capability["max_operations"],
        "pairing_receipt_id": capability["pairing_receipt_id"],
    }


def _validate_capability(
    capability: Mapping[str, Any],
    *,
    database_uuid: str,
    project_id: str,
) -> tuple[list[str], datetime, datetime]:
    actions = _strict_json(capability.get("actions_json"), list)
    expected_actions = [action for action in _CAPABILITY_ACTIONS if action in actions]
    if not (
        actions
        and all(type(action) is str for action in actions)
        and actions == expected_actions
        and len(actions) == len(set(actions))
        and _valid_identifier(capability.get("id"))
        and capability.get("database_uuid") == database_uuid
        and _valid_identifier(capability.get("runtime_authority_epoch"))
        and capability.get("project_id") == project_id
        and _valid_identifier(capability.get("paired_subject_id"))
        and _valid_label(capability.get("claimed_client_label"))
        and _valid_digest(capability.get("secret_sha256"))
        and type(capability.get("max_operations")) is int
        and 1 <= capability["max_operations"] <= 32
        and _valid_identifier(capability.get("pairing_receipt_id"))
        and _valid_digest(capability.get("facts_sha256"))
        and _canonical_sha256(_capability_facts(capability, actions)) == capability["facts_sha256"]
    ):
        raise _corrupt()
    issued_at = _timestamp(capability.get("issued_at"))
    expires_at = _timestamp(capability.get("expires_at"))
    ttl = (expires_at - issued_at).total_seconds()
    if not ttl.is_integer() or not 60 <= int(ttl) <= 1_800:
        raise _corrupt()
    return actions, issued_at, expires_at


def _pairing_material(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": receipt["id"],
        "database_uuid": receipt["database_uuid"],
        "runtime_authority_epoch": receipt["runtime_authority_epoch"],
        "pairing_id": receipt["pairing_id"],
        "capability_id": receipt["capability_id"],
        "project_id": receipt["project_id"],
        "pairing_request_sha256": receipt["pairing_request_sha256"],
        "observed_revision": receipt["observed_revision"],
        "observed_content_sha256": receipt["observed_content_sha256"],
        "presentation_sha256": receipt["presentation_sha256"],
        "native_gesture_nonce": receipt["native_gesture_nonce"],
        "created_at": receipt["created_at"],
    }


def _validate_pairing_receipt(
    connection: sqlite3.Connection,
    receipt: Mapping[str, Any],
    capability: Mapping[str, Any],
    actions: list[str],
    issued_at: datetime,
    expires_at: datetime,
    observed: Mapping[str, Any] | None,
    *,
    database_uuid: str,
    project_id: str,
) -> None:
    presentation = _strict_json(receipt.get("presentation_json"), dict)
    observed_revision = receipt.get("observed_revision")
    ttl_seconds = int((expires_at - issued_at).total_seconds())
    expected_presentation = {
        "object": "memolens.agent_pairing.presentation",
        "schema_version": "1",
        "database_uuid": database_uuid,
        "runtime_authority_epoch": capability["runtime_authority_epoch"],
        "pairing_id": receipt.get("pairing_id"),
        "project_id": project_id,
        "paired_subject_id": capability["paired_subject_id"],
        "claimed_client_label": capability["claimed_client_label"],
        "actions": actions,
        "ttl_seconds": ttl_seconds,
        "max_operations": capability["max_operations"],
        "pairing_request_sha256": receipt.get("pairing_request_sha256"),
        "observed_head": dict(observed) if observed is not None else None,
    }
    if any(action in actions for action in _TIMELINE_BINDING_ACTIONS):
        observed_timeline_head = presentation.get("observed_timeline_head")
        revision = (
            observed_timeline_head.get("revision")
            if isinstance(observed_timeline_head, Mapping)
            else None
        )
        if not _valid_revision(revision):
            raise _corrupt()
        try:
            timeline_rows = connection.execute(
                """SELECT revision,revision_sha256,timeline_id,
                          timeline_content_sha256,blueprint_revision,
                          blueprint_content_sha256,blueprint_semantic_sha256,
                          blueprint_operation_id,coverage_revision,
                          coverage_content_sha256,
                          coverage_evidence_manifest_sha256,
                          coverage_operation_id,operation_id
                     FROM canonical_timeline_revisions
                    WHERE project_id=? AND revision=? LIMIT 2""",
                (project_id, revision),
            ).fetchall()
        except sqlite3.Error as exc:
            raise _corrupt() from exc
        if len(timeline_rows) != 1:
            raise _corrupt()
        timeline_row = timeline_rows[0]
        expected_timeline_head = {
            "revision": timeline_row["revision"],
            "revision_sha256": timeline_row["revision_sha256"],
            "timeline_id": timeline_row["timeline_id"],
            "timeline_content_sha256": timeline_row["timeline_content_sha256"],
            "blueprint_binding": {
                "revision": timeline_row["blueprint_revision"],
                "content_sha256": timeline_row["blueprint_content_sha256"],
                "semantic_sha256": timeline_row["blueprint_semantic_sha256"],
                "operation_id": timeline_row["blueprint_operation_id"],
            },
            "coverage_binding": {
                "revision": timeline_row["coverage_revision"],
                "content_sha256": timeline_row["coverage_content_sha256"],
                "evidence_manifest_sha256": timeline_row[
                    "coverage_evidence_manifest_sha256"
                ],
                "operation_id": timeline_row["coverage_operation_id"],
            },
            "operation_id": timeline_row["operation_id"],
        }
        if observed_timeline_head != expected_timeline_head:
            raise _corrupt()
        expected_presentation["observed_timeline_head"] = expected_timeline_head
    if not (
        receipt.get("id") == capability["pairing_receipt_id"]
        and _valid_identifier(receipt.get("id"))
        and receipt.get("database_uuid") == database_uuid
        and receipt.get("runtime_authority_epoch") == capability["runtime_authority_epoch"]
        and _valid_identifier(receipt.get("pairing_id"))
        and receipt.get("capability_id") == capability["id"]
        and receipt.get("project_id") == project_id
        and _valid_digest(receipt.get("pairing_request_sha256"))
        and _valid_revision(observed_revision)
        and observed is not None
        and receipt.get("observed_content_sha256") == observed["content_sha256"]
        and _valid_digest(receipt.get("presentation_sha256"))
        and _canonical_sha256(presentation) == receipt["presentation_sha256"]
        and presentation == expected_presentation
        and _valid_identifier(receipt.get("native_gesture_nonce"))
        and _valid_digest(receipt.get("receipt_sha256"))
        and _canonical_sha256(_pairing_material(receipt)) == receipt["receipt_sha256"]
        and _timestamp(receipt.get("created_at")) == issued_at
    ):
        raise _corrupt()


def _event_material(event: Mapping[str, Any]) -> dict[str, Any]:
    material = {
        "id": event["id"],
        "capability_id": event["capability_id"],
        "project_id": event["project_id"],
        "sequence": event["sequence"],
        "parent_event_id": event["parent_event_id"],
        "parent_event_sha256": event["parent_event_sha256"],
        "event_type": event["event_type"],
        "action": event["action"],
        "operation_id": event["operation_id"],
        "agent_command_receipt_id": event["agent_command_receipt_id"],
        "reason_code": event["reason_code"],
        "presentation_sha256": event["presentation_sha256"],
        "native_gesture_nonce": event["native_gesture_nonce"],
        "created_at": event["created_at"],
    }
    schema_version = event.get("event_schema_version")
    if schema_version == "1":
        if event.get("agent_project_command_receipt_id") is not None:
            raise _corrupt()
        return material
    if schema_version != "2" or event.get("agent_command_receipt_id") is not None:
        raise _corrupt()
    return {
        **material,
        "event_schema_version": "2",
        "agent_project_command_receipt_id": event.get(
            "agent_project_command_receipt_id"
        ),
    }


def _public_capability_before_revoke(
    capability: Mapping[str, Any],
    actions: list[str],
    *,
    operations_used: int,
) -> dict[str, Any]:
    max_operations = capability["max_operations"]
    return {
        "id": capability["id"],
        "database_uuid": capability["database_uuid"],
        "runtime_authority_epoch": capability["runtime_authority_epoch"],
        "project_id": capability["project_id"],
        "paired_subject_id": capability["paired_subject_id"],
        "claimed_client_label": capability["claimed_client_label"],
        "vendor_identity_verified": False,
        "user_authority_verified": False,
        "actions": actions,
        "issued_at": capability["issued_at"],
        "expires_at": capability["expires_at"],
        "max_operations": max_operations,
        "operations_used": operations_used,
        "remaining_operations": max(0, max_operations - operations_used),
        "status": "exhausted" if operations_used >= max_operations else "active",
    }


def _validate_agent_receipt(
    receipt: Mapping[str, Any],
    operation: Mapping[str, Any],
    capability: Mapping[str, Any],
    actions: list[str],
    *,
    database_uuid: str,
) -> datetime:
    actor = operation["actor"]
    receipt_material = {
        "id": receipt.get("id"),
        "database_uuid": receipt.get("database_uuid"),
        "capability_id": receipt.get("capability_id"),
        "paired_subject_id": receipt.get("paired_subject_id"),
        "project_id": receipt.get("project_id"),
        "command_type": receipt.get("command_type"),
        "command_version": receipt.get("command_version"),
        "idempotency_key": receipt.get("idempotency_key"),
        "request_sha256": receipt.get("request_sha256"),
        "operation_id": receipt.get("operation_id"),
        "response_status": receipt.get("response_status"),
        "response_sha256": receipt.get("response_sha256"),
        "resource_type": receipt.get("resource_type"),
        "resource_id": receipt.get("resource_id"),
        "created_at": receipt.get("created_at"),
    }
    if not (
        _valid_identifier(receipt.get("id"))
        and receipt.get("receipt_schema_version") == "1"
        and receipt.get("request_capture") == "digest_only_legacy"
        and receipt.get("authenticated_principal") is None
        and receipt.get("claimed_client_label") is None
        and receipt.get("request_json") is None
        and receipt.get("actor_json") is None
        and receipt.get("origin_json") is None
        and _valid_digest(receipt.get("receipt_sha256"))
        and _canonical_sha256(receipt_material) == receipt["receipt_sha256"]
        and receipt.get("capability_id") == capability["id"]
        and receipt.get("paired_subject_id") == capability["paired_subject_id"]
        and actor.get("capability_id") == capability["id"]
        and actor.get("paired_subject_id") == capability["paired_subject_id"]
        and actor.get("claimed_client_label") == capability["claimed_client_label"]
        and operation["command_type"] in actions
    ):
        raise _corrupt()
    return _validate_receipt_envelope(
        receipt,
        operation,
        database_uuid=database_uuid,
    )


def _timeline_revision_evidence(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    revision: object,
) -> dict[str, Any]:
    if not _valid_revision(revision):
        raise _corrupt()
    try:
        rows = connection.execute(
            """SELECT revision,parent_revision,parent_revision_sha256,
                      revision_sha256,timeline_id,timeline_content_sha256,
                      schema_version,
                      blueprint_revision,blueprint_content_sha256,
                      blueprint_semantic_sha256,blueprint_operation_id,
                      coverage_revision,coverage_content_sha256,
                      coverage_evidence_manifest_sha256,coverage_operation_id,
                      source_bindings_sha256,operation_id,
                      typeof(timeline_json) AS timeline_storage,
                      length(CAST(timeline_json AS BLOB)) AS timeline_bytes,
                      typeof(source_bindings_json) AS source_storage,
                      length(CAST(source_bindings_json AS BLOB)) AS source_bytes
                 FROM canonical_timeline_revisions
                WHERE project_id=? AND revision=? LIMIT 2""",
            (project_id, revision),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(rows) != 1:
        raise _corrupt()
    row = rows[0]
    if not (
        row["timeline_storage"] == "text"
        and type(row["timeline_bytes"]) is int
        and 0 <= row["timeline_bytes"] <= _TIMELINE_REPLAY_JSON_LIMITS.max_bytes
        and row["source_storage"] == "text"
        and type(row["source_bytes"]) is int
        and 0 <= row["source_bytes"] <= _TIMELINE_REPLAY_JSON_LIMITS.max_bytes
    ):
        raise _corrupt()
    try:
        document_rows = connection.execute(
            """SELECT timeline_json,source_bindings_json
                 FROM canonical_timeline_revisions
                WHERE project_id=? AND revision=? LIMIT 2""",
            (project_id, revision),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(document_rows) != 1:
        raise _corrupt()
    timeline = _strict_json_with_limits(
        document_rows[0]["timeline_json"],
        dict,
        limits=_TIMELINE_REPLAY_JSON_LIMITS,
    )
    source_bindings = _strict_json_with_limits(
        document_rows[0]["source_bindings_json"],
        list,
        limits=_TIMELINE_REPLAY_JSON_LIMITS,
    )
    head = {
        "revision": row["revision"],
        "revision_sha256": row["revision_sha256"],
        "timeline_id": row["timeline_id"],
        "timeline_content_sha256": row["timeline_content_sha256"],
        "blueprint_binding": {
            "revision": row["blueprint_revision"],
            "content_sha256": row["blueprint_content_sha256"],
            "semantic_sha256": row["blueprint_semantic_sha256"],
            "operation_id": row["blueprint_operation_id"],
        },
        "coverage_binding": {
            "revision": row["coverage_revision"],
            "content_sha256": row["coverage_content_sha256"],
            "evidence_manifest_sha256": row[
                "coverage_evidence_manifest_sha256"
            ],
            "operation_id": row["coverage_operation_id"],
        },
        "operation_id": row["operation_id"],
    }
    if not (
        row["schema_version"] in {"1", "2"}
        and timeline.get("schema_version") == row["schema_version"]
        and _valid_identifier(head["timeline_id"])
        and _valid_identifier(head["operation_id"])
        and _valid_digest(head["revision_sha256"])
        and _valid_digest(head["timeline_content_sha256"])
        and _valid_digest(row["source_bindings_sha256"])
        and _canonical_sha256(timeline) == head["timeline_content_sha256"]
        and _canonical_sha256(source_bindings) == row["source_bindings_sha256"]
        and timeline.get("project_id") == project_id
        and timeline.get("revision") == head["revision"]
        and timeline.get("timeline_id") == head["timeline_id"]
        and timeline.get("blueprint_binding") == head["blueprint_binding"]
        and timeline.get("coverage_binding") == head["coverage_binding"]
    ):
        raise _corrupt()
    return {
        "head": head,
        "parent_revision": row["parent_revision"],
        "parent_revision_sha256": row["parent_revision_sha256"],
        "source_bindings_sha256": row["source_bindings_sha256"],
        "timeline": timeline,
        "source_bindings": source_bindings,
    }


def _validate_generic_timeline_operation(
    connection: sqlite3.Connection,
    *,
    operation: Mapping[str, Any],
    project_id: str,
    command_type: str,
    expected_head: Mapping[str, Any],
    expected_blueprint: Mapping[str, Any],
    expected_coverage: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    result_revision = operation.get("result_revision")
    result_evidence = _timeline_revision_evidence(
        connection,
        project_id=project_id,
        revision=result_revision,
    )
    result_head = result_evidence["head"]
    parent_revision = int(result_revision) - 1
    parent_evidence = _timeline_revision_evidence(
        connection,
        project_id=project_id,
        revision=parent_revision,
    )
    parent_head = parent_evidence["head"]
    if not (
        expected_head == parent_head
        and result.get("result_head") == result_head
        and operation.get("project_id") == project_id
        and operation.get("sequence") == result_revision
        and operation.get("parent_operation_id") == parent_head["operation_id"]
        and result_evidence["parent_revision"] == parent_revision
        and result_evidence["parent_revision_sha256"]
        == parent_head["revision_sha256"]
        and expected_blueprint == parent_head["blueprint_binding"]
        and expected_coverage == parent_head["coverage_binding"]
        and result_head["blueprint_binding"] == expected_blueprint
        and result_head["coverage_binding"] == expected_coverage
        and result_head["timeline_id"] == parent_head["timeline_id"]
        and operation.get("result_revision_sha256")
        == result_head["revision_sha256"]
        and operation.get("result_timeline_content_sha256")
        == result_head["timeline_content_sha256"]
    ):
        raise _corrupt()
    if command_type == "timeline.apply_edit":
        if set(result) != {"kind", "result_head", "edit"} or result.get(
            "kind"
        ) != "revision_created":
            raise _corrupt()
        return
    if command_type == "timeline.apply_structural_edit":
        if not (
            set(result) == {"kind", "result_head", "structural_edit"}
            and result.get("kind") == "revision_created"
            and _valid_structural_edit(result.get("structural_edit"))
        ):
            raise _corrupt()
        return
    if command_type != "timeline.restore_revision":
        raise _corrupt()
    restore_from = result.get("restore_from")
    if not isinstance(restore_from, Mapping) or not _valid_revision(
        restore_from.get("revision")
    ):
        raise _corrupt()
    restore_evidence = _timeline_revision_evidence(
        connection,
        project_id=project_id,
        revision=restore_from["revision"],
    )
    restore_head = restore_evidence["head"]
    expected_restored_timeline = deepcopy(restore_evidence["timeline"])
    expected_restored_timeline["revision"] = result_revision
    expected_restored_timeline["parent"] = {
        "revision": parent_revision,
        "content_sha256": parent_head["timeline_content_sha256"],
    }
    if not (
        set(result) == {"kind", "result_head", "restore_from"}
        and result.get("kind") == "revision_restored"
        and restore_from == restore_head
        and int(restore_head["revision"]) < parent_revision
        and restore_head["timeline_id"] == parent_head["timeline_id"]
        and restore_head["blueprint_binding"] == expected_blueprint
        and restore_head["coverage_binding"] == expected_coverage
        and result_evidence["source_bindings_sha256"]
        == restore_evidence["source_bindings_sha256"]
        and result_evidence["source_bindings"]
        == restore_evidence["source_bindings"]
        and result_evidence["timeline"] == expected_restored_timeline
    ):
        raise _corrupt()


def _validate_generic_timeline_receipt(
    connection: sqlite3.Connection,
    *,
    receipt_id: object,
    capability: Mapping[str, Any],
    project_id: str,
    database_uuid: str,
) -> dict[str, Any]:
    if not _valid_identifier(receipt_id):
        raise _corrupt()
    try:
        receipt_rows = connection.execute(
            "SELECT * FROM agent_project_command_receipts WHERE id=? LIMIT 2",
            (receipt_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(receipt_rows) != 1:
        raise _corrupt()
    receipt = dict(receipt_rows[0])
    try:
        operation_rows = connection.execute(
            "SELECT * FROM canonical_timeline_operations WHERE project_id=? AND id=? LIMIT 2",
            (project_id, receipt.get("operation_id")),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(operation_rows) != 1:
        raise _corrupt()
    operation = dict(operation_rows[0])
    request = _strict_json(receipt.get("request_json"), dict)
    actor = _strict_json(receipt.get("actor_json"), dict)
    origin = _strict_json(receipt.get("origin_json"), dict)
    response = _strict_receipt_json(receipt.get("response_json"))
    expected_head = _strict_json(operation.get("expected_timeline_head_json"), dict)
    result = _strict_json(operation.get("result_json"), dict)
    expected_blueprint = {
        "revision": operation.get("expected_blueprint_revision"),
        "content_sha256": operation.get("expected_blueprint_content_sha256"),
        "semantic_sha256": operation.get("expected_blueprint_semantic_sha256"),
        "operation_id": operation.get("expected_blueprint_operation_id"),
    }
    expected_coverage = {
        "revision": operation.get("expected_coverage_revision"),
        "content_sha256": operation.get("expected_coverage_content_sha256"),
        "evidence_manifest_sha256": operation.get(
            "expected_coverage_evidence_manifest_sha256"
        ),
        "operation_id": operation.get("expected_coverage_operation_id"),
    }
    command_type = operation.get("command_type")
    request_field: str
    request_object: str
    if command_type == "timeline.apply_edit":
        request_field = "edit"
        request_object = "memolens.timeline_apply_edit_command"
    elif command_type == "timeline.apply_structural_edit":
        request_field = "structural_edit"
        request_object = "memolens.timeline_apply_structural_edit_command"
    elif command_type == "timeline.restore_revision":
        request_field = "restore_from"
        request_object = "memolens.timeline_restore_revision_command"
    else:
        raise _corrupt()
    expected_request = {
        "object": request_object,
        "schema_version": "1",
        "project_id": project_id,
        "expected_database_uuid": database_uuid,
        "expected_blueprint": expected_blueprint,
        "expected_coverage": expected_coverage,
        "expected_timeline_head": expected_head,
        request_field: result.get(request_field),
    }
    expected_actor = {
        "kind": "paired_agent",
        "capability_id": capability["id"],
        "paired_subject_id": capability["paired_subject_id"],
        "claimed_client_label": capability["claimed_client_label"],
        "capability_secret_possession_verified": True,
        "vendor_identity_verified": False,
        "user_authority_verified": False,
    }
    expected_origin = {
        "principal": "paired_agent",
        "surface": "agent_plugin_editor",
    }
    expected_response = {
        "object": "canonical_timeline.command_result",
        "schema_version": "1",
        "project_id": project_id,
        "command_type": command_type,
        "operation_id": operation.get("id"),
        "result": result,
    }
    _validate_generic_timeline_operation(
        connection,
        operation=operation,
        project_id=project_id,
        command_type=command_type,
        expected_head=expected_head,
        expected_blueprint=expected_blueprint,
        expected_coverage=expected_coverage,
        result=result,
    )
    material = {
        "id": receipt.get("id"),
        "database_uuid": receipt.get("database_uuid"),
        "authenticated_principal": receipt.get("authenticated_principal"),
        "capability_id": receipt.get("capability_id"),
        "paired_subject_id": receipt.get("paired_subject_id"),
        "claimed_client_label": receipt.get("claimed_client_label"),
        "project_id": receipt.get("project_id"),
        "command_type": receipt.get("command_type"),
        "command_version": receipt.get("command_version"),
        "idempotency_key": receipt.get("idempotency_key"),
        "request": request,
        "request_sha256": receipt.get("request_sha256"),
        "actor": actor,
        "origin": origin,
        "operation_id": receipt.get("operation_id"),
        "response_status": receipt.get("response_status"),
        "response": response,
        "response_sha256": receipt.get("response_sha256"),
        "resource_type": receipt.get("resource_type"),
        "resource_id": receipt.get("resource_id"),
        "created_at": receipt.get("created_at"),
    }
    if not (
        receipt.get("receipt_schema_version") == "2"
        and receipt.get("request_capture") == "exact_canonical_json"
        and receipt.get("database_uuid") == database_uuid
        and receipt.get("authenticated_principal") == "paired_agent"
        and receipt.get("capability_id") == capability["id"]
        and receipt.get("paired_subject_id") == capability["paired_subject_id"]
        and receipt.get("claimed_client_label")
        == capability["claimed_client_label"]
        and receipt.get("project_id") == project_id
        and receipt.get("command_type") == command_type
        and command_type in _TIMELINE_COMMAND_TYPES
        and receipt.get("command_version") == "1"
        and operation.get("command_version") == "1"
        and _valid_timeline_idempotency_key(receipt.get("idempotency_key"))
        and request == expected_request
        and actor == expected_actor
        and origin == expected_origin
        and hashlib.sha256(receipt["request_json"].encode()).hexdigest()
        == receipt.get("request_sha256")
        and receipt.get("request_sha256") == operation.get("request_sha256")
        and receipt.get("operation_id") == operation.get("id")
        and receipt.get("response_status") == 201
        and response == expected_response
        and hashlib.sha256(receipt["response_json"].encode()).hexdigest()
        == receipt.get("response_sha256")
        and _valid_digest(receipt.get("receipt_sha256"))
        and _canonical_sha256(material) == receipt["receipt_sha256"]
        and receipt.get("resource_type") == "canonical_timeline"
        and receipt.get("resource_id")
        == f"{project_id}:{operation.get('result_revision')}"
        and command_type in _strict_json(capability.get("actions_json"), list)
    ):
        raise _corrupt()
    _timestamp(receipt.get("created_at"))
    return receipt


def _validate_capability_events(
    connection: sqlite3.Connection,
    events: list[Mapping[str, Any]],
    capability: Mapping[str, Any],
    actions: list[str],
    issued_at: datetime,
    agent_receipts_by_id: Mapping[str, Mapping[str, Any]],
    operations: Mapping[str, Mapping[str, Any]],
    *,
    project_id: str,
    database_uuid: str,
) -> tuple[set[str], set[str]]:
    if not events or len(events) > _MAX_EVENTS_PER_CAPABILITY:
        raise _corrupt()
    prior: Mapping[str, Any] | None = None
    revoked = False
    used_receipts: set[str] = set()
    used_generic_receipts: set[str] = set()
    for sequence, event in enumerate(events, start=1):
        timestamp = _timestamp(event.get("created_at"))
        event_type = event.get("event_type")
        if not (
            _valid_identifier(event.get("id"))
            and event.get("capability_id") == capability["id"]
            and event.get("project_id") == project_id
            and event.get("sequence") == sequence
            and event.get("event_schema_version") in {"1", "2"}
            and event.get("parent_event_id") == (prior["id"] if prior is not None else None)
            and event.get("parent_event_sha256") == (prior["event_sha256"] if prior is not None else None)
            and _valid_digest(event.get("event_sha256"))
            and _canonical_sha256(_event_material(event)) == event["event_sha256"]
            and not revoked
        ):
            raise _corrupt()
        if sequence == 1:
            if not (
                event_type == "issued"
                and event.get("event_schema_version") == "1"
                and event.get("agent_project_command_receipt_id") is None
                and timestamp == issued_at
                and all(
                    event.get(name) is None
                    for name in (
                        "action",
                        "operation_id",
                        "agent_command_receipt_id",
                        "reason_code",
                        "presentation_sha256",
                        "native_gesture_nonce",
                    )
                )
            ):
                raise _corrupt()
        elif event_type == "used":
            operation_id = event.get("operation_id")
            if not (
                _valid_identifier(operation_id)
                and event.get("action") in actions
                and all(
                    event.get(name) is None
                    for name in (
                        "reason_code",
                        "presentation_sha256",
                        "native_gesture_nonce",
                    )
                )
            ):
                raise _corrupt()
            if event.get("event_schema_version") == "1":
                receipt_id = event.get("agent_command_receipt_id")
                receipt = agent_receipts_by_id.get(receipt_id)
                operation = operations.get(operation_id)
                if not (
                    event.get("agent_project_command_receipt_id") is None
                    and _valid_identifier(receipt_id)
                    and receipt is not None
                    and operation is not None
                    and operation["principal"] == "paired_agent"
                    and event.get("action") == operation["command_type"]
                    and receipt.get("operation_id") == operation_id
                    and receipt.get("capability_id") == capability["id"]
                    and receipt.get("created_at") == event.get("created_at")
                    and receipt_id not in used_receipts
                ):
                    raise _corrupt()
                used_receipts.add(receipt_id)
            else:
                generic_receipt_id = event.get(
                    "agent_project_command_receipt_id"
                )
                if not (
                    event.get("agent_command_receipt_id") is None
                    and event.get("action") in _TIMELINE_COMMAND_TYPES
                    and _valid_identifier(generic_receipt_id)
                    and generic_receipt_id not in used_generic_receipts
                ):
                    raise _corrupt()
                generic_receipt = _validate_generic_timeline_receipt(
                    connection,
                    receipt_id=generic_receipt_id,
                    capability=capability,
                    project_id=project_id,
                    database_uuid=database_uuid,
                )
                if (
                    generic_receipt.get("operation_id") != operation_id
                    or generic_receipt.get("command_type")
                    != event.get("action")
                    or generic_receipt.get("created_at")
                    != event.get("created_at")
                ):
                    raise _corrupt()
                used_generic_receipts.add(generic_receipt_id)
        elif event_type == "revoked":
            expected_revoke_presentation = {
                "object": "memolens.agent_capability_revoke.presentation",
                "schema_version": "1",
                "database_uuid": capability["database_uuid"],
                "runtime_authority_epoch": capability["runtime_authority_epoch"],
                "capability": _public_capability_before_revoke(
                    capability,
                    actions,
                    operations_used=(
                        len(used_receipts) + len(used_generic_receipts)
                    ),
                ),
            }
            if not (
                event.get("action") is None
                and event.get("event_schema_version") == "1"
                and event.get("operation_id") is None
                and event.get("agent_command_receipt_id") is None
                and event.get("agent_project_command_receipt_id") is None
                and _valid_identifier(event.get("reason_code"))
                and _valid_digest(event.get("presentation_sha256"))
                and _canonical_sha256(expected_revoke_presentation) == event["presentation_sha256"]
                and _valid_identifier(event.get("native_gesture_nonce"))
            ):
                raise _corrupt()
            revoked = True
        else:
            raise _corrupt()
        prior = event
    if (
        len(used_receipts) + len(used_generic_receipts)
        > capability["max_operations"]
    ):
        raise _corrupt()
    return used_receipts, used_generic_receipts


def _agent_state_counts_and_preflight(
    connection: sqlite3.Connection,
    project_id: str,
) -> tuple[int, int, int]:
    try:
        capability_count = connection.execute(
            "SELECT COUNT(*) FROM agent_project_capabilities WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        pairing_count = connection.execute(
            """SELECT COUNT(*) FROM agent_pairing_confirmation_receipts
                WHERE project_id=?""",
            (project_id,),
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM agent_project_capability_events WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if not (
        type(capability_count) is int
        and type(pairing_count) is int
        and type(event_count) is int
        and 0 <= capability_count <= _MAX_CAPABILITIES
        and pairing_count == capability_count
        and capability_count <= event_count <= capability_count * _MAX_EVENTS_PER_CAPABILITY
    ):
        raise _corrupt()
    _preflight_json_budget(
        connection,
        table="agent_project_capabilities",
        project_id=project_id,
        columns=("actions_json",),
        expected_count=capability_count,
    )
    _preflight_json_budget(
        connection,
        table="agent_pairing_confirmation_receipts",
        project_id=project_id,
        columns=("presentation_json",),
        expected_count=pairing_count,
    )
    return capability_count, pairing_count, event_count


def _pairing_observed_revision(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    revision: object,
) -> dict[str, Any] | None:
    if not _valid_revision(revision):
        return None
    try:
        rows = connection.execute(
            """SELECT project_id,revision,content_sha256,semantic_sha256,operation_id
                 FROM creative_blueprint_revisions
                WHERE project_id=? AND revision=? LIMIT 2""",
            (project_id, revision),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(rows) != 1:
        return None
    return {
        "project_id": rows[0]["project_id"],
        "revision": rows[0]["revision"],
        "content_sha256": rows[0]["content_sha256"],
        "semantic_sha256": rows[0]["semantic_sha256"],
        "operation_id": rows[0]["operation_id"],
    }


def _validate_one_capability(
    connection: sqlite3.Connection,
    capability: dict[str, Any],
    *,
    project_id: str,
    database_uuid: str,
) -> tuple[int, int]:
    actions, issued_at, expires_at = _validate_capability(
        capability,
        database_uuid=database_uuid,
        project_id=project_id,
    )
    try:
        pairing_rows = connection.execute(
            """SELECT * FROM agent_pairing_confirmation_receipts
                WHERE id=? LIMIT 2""",
            (capability["pairing_receipt_id"],),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(pairing_rows) != 1:
        raise _corrupt()
    pairing = dict(pairing_rows[0])
    observed = _pairing_observed_revision(
        connection,
        project_id=project_id,
        revision=pairing.get("observed_revision"),
    )
    _validate_pairing_receipt(
        connection,
        pairing,
        capability,
        actions,
        issued_at,
        expires_at,
        observed,
        database_uuid=database_uuid,
        project_id=project_id,
    )

    try:
        receipt_count = connection.execute(
            """SELECT COUNT(*) FROM agent_project_command_receipts
                WHERE project_id=? AND capability_id=?
                  AND receipt_schema_version='1'
                  AND request_capture='digest_only_legacy'
                  AND resource_type='creative_blueprint'""",
            (project_id, capability["id"]),
        ).fetchone()[0]
        generic_receipt_count = connection.execute(
            """SELECT COUNT(*) FROM agent_project_command_receipts
                WHERE project_id=? AND capability_id=?
                  AND receipt_schema_version='2'
                  AND request_capture='exact_canonical_json'
                  AND resource_type='canonical_timeline'""",
            (project_id, capability["id"]),
        ).fetchone()[0]
        if (
            type(receipt_count) is not int
            or type(generic_receipt_count) is not int
            or not 0 <= receipt_count <= capability["max_operations"]
            or not 0 <= generic_receipt_count <= capability["max_operations"]
            or receipt_count + generic_receipt_count
            > capability["max_operations"]
        ):
            raise _corrupt()
        _preflight_json_budget(
            connection,
            table="agent_project_command_receipts",
            project_id=project_id,
            columns=("response_json",),
            expected_count=receipt_count,
            max_bytes=_RECEIPT_JSON_LIMITS.max_bytes,
            additional_predicate=(
                " AND capability_id=?"
                " AND receipt_schema_version='1'"
                " AND request_capture='digest_only_legacy'"
                " AND resource_type='creative_blueprint'"
            ),
            additional_parameters=(capability["id"],),
        )
        _preflight_json_budget(
            connection,
            table="agent_project_command_receipts",
            project_id=project_id,
            columns=(
                "request_json",
                "actor_json",
                "origin_json",
                "response_json",
            ),
            expected_count=generic_receipt_count,
            max_bytes=_RECEIPT_JSON_LIMITS.max_bytes,
            additional_predicate=(
                " AND capability_id=?"
                " AND receipt_schema_version='2'"
                " AND request_capture='exact_canonical_json'"
                " AND resource_type='canonical_timeline'"
            ),
            additional_parameters=(capability["id"],),
        )
        receipt_cursor = connection.execute(
            f"""SELECT {_relation_select("a", "a_", _AGENT_RECEIPT_READ_COLUMNS)},
                       {_operation_select("o", "o_")}
                  FROM agent_project_command_receipts a
                  JOIN creative_blueprint_operations o
                    ON o.project_id=a.project_id AND o.id=a.operation_id
                 WHERE a.project_id=? AND a.capability_id=?
                   AND a.receipt_schema_version='1'
                   AND a.request_capture='digest_only_legacy'
                   AND a.resource_type='creative_blueprint'
                 ORDER BY a.id""",
            (project_id, capability["id"]),
        )
        receipts_by_id: dict[str, dict[str, Any]] = {}
        operations_by_id: dict[str, dict[str, Any]] = {}
        for row in receipt_cursor:
            receipt = _relation_from_row(
                row,
                "a_",
                _AGENT_RECEIPT_READ_COLUMNS,
            )
            operation = _operation_from_row(
                _prefixed_row(row, "o_"),
                project_id=project_id,
            )
            receipt_id = receipt.get("id")
            operation_id = operation["id"]
            if (
                not _valid_identifier(receipt_id)
                or receipt_id in receipts_by_id
                or operation_id in operations_by_id
                or operation["principal"] != "paired_agent"
            ):
                raise _corrupt()
            _validate_agent_receipt(
                receipt,
                operation,
                capability,
                actions,
                database_uuid=database_uuid,
            )
            receipts_by_id[receipt_id] = receipt
            operations_by_id[operation_id] = operation
        event_count = connection.execute(
            """SELECT COUNT(*) FROM agent_project_capability_events
                WHERE project_id=? AND capability_id=?""",
            (project_id, capability["id"]),
        ).fetchone()[0]
        if type(event_count) is not int or not 1 <= event_count <= _MAX_EVENTS_PER_CAPABILITY:
            raise _corrupt()
        events = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM agent_project_capability_events
                    WHERE project_id=? AND capability_id=? ORDER BY sequence""",
                (project_id, capability["id"]),
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if len(receipts_by_id) != receipt_count or len(events) != event_count:
        raise _corrupt()
    used_receipts, used_generic_receipts = _validate_capability_events(
        connection,
        events,
        capability,
        actions,
        issued_at,
        receipts_by_id,
        operations_by_id,
        project_id=project_id,
        database_uuid=database_uuid,
    )
    if (
        used_receipts != set(receipts_by_id)
        or len(used_generic_receipts) != generic_receipt_count
    ):
        raise _corrupt()
    return receipt_count, event_count


def _validate_agent_state_stream(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    database_uuid: str,
    expected_agent_receipts: int,
) -> int:
    capability_count, pairing_count, event_count = _agent_state_counts_and_preflight(
        connection,
        project_id,
    )
    del pairing_count
    observed_capabilities = 0
    observed_receipts = 0
    observed_events = 0
    try:
        cursor = connection.execute(
            "SELECT * FROM agent_project_capabilities WHERE project_id=? ORDER BY id",
            (project_id,),
        )
        for row in cursor:
            observed_capabilities += 1
            receipt_total, event_total = _validate_one_capability(
                connection,
                dict(row),
                project_id=project_id,
                database_uuid=database_uuid,
            )
            observed_receipts += receipt_total
            observed_events += event_total
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if (
        observed_capabilities != capability_count
        or observed_receipts != expected_agent_receipts
        or observed_events != event_count
    ):
        raise _corrupt()
    return capability_count


def validate_agent_receipt_ledger(
    connection: sqlite3.Connection,
    *,
    project_id: str,
) -> dict[str, int | bool]:
    """Validate desktop and Agent receipts for every Blueprint operation.

    The result is deliberately aggregate-only.  No capability secret, pairing
    presentation, command response, receipt identifier, or event body is
    returned to the caller.
    """

    if not _valid_identifier(project_id):
        raise _corrupt()
    database_uuid, _schema_version = _database_identity(connection)
    agent_available = agent_receipt_schema_available(connection)
    try:
        census_projects = validate_v13_agent_project_receipt_census(
            connection
        )
    except MemoLensError as exc:
        raise _corrupt() from exc
    desktop_count, agent_count = _receipt_counts_and_preflight(
        connection,
        project_id,
        agent_available=agent_available,
    )
    operation_count, observed_desktop_count, observed_agent_count = _validate_operation_receipt_stream(
        connection,
        project_id=project_id,
        database_uuid=database_uuid,
        expected_desktop_receipts=desktop_count,
        expected_agent_receipts=agent_count,
    )
    capability_count = _validate_agent_state_stream(
        connection,
        project_id,
        database_uuid=database_uuid,
        expected_agent_receipts=agent_count,
    )
    # A caller reads one project, but the V13 versioned union is a database-wide
    # authority lane. Deep-validate every project named by its receipt/event
    # census so an injected row outside the requested project cannot hide.
    for census_project_id in census_projects:
        if census_project_id == project_id:
            continue
        census_desktop_receipts, census_agent_receipts = (
            _receipt_counts_and_preflight(
                connection,
                census_project_id,
                agent_available=agent_available,
            )
        )
        _validate_operation_receipt_stream(
            connection,
            project_id=census_project_id,
            database_uuid=database_uuid,
            expected_desktop_receipts=census_desktop_receipts,
            expected_agent_receipts=census_agent_receipts,
        )
        _validate_agent_state_stream(
            connection,
            census_project_id,
            database_uuid=database_uuid,
            expected_agent_receipts=census_agent_receipts,
        )
    return {
        "validated": True,
        "operation_count": operation_count,
        "desktop_operation_count": observed_desktop_count,
        "paired_agent_operation_count": observed_agent_count,
        "paired_capability_count": capability_count,
    }


__all__ = [
    "AGENT_RECEIPT_RELATIONS",
    "DESKTOP_RECEIPT_COLUMNS",
    "DESKTOP_RECEIPT_INTEGRITY_COLUMNS",
    "agent_receipt_schema_available",
    "validate_agent_receipt_ledger",
]
