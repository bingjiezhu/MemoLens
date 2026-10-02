"""Closed production-action inventory and runtime discovery helpers.

The inventory is a release input, not descriptive documentation.  Its digest
therefore covers every field except ``inventory_sha256`` itself, and the
helpers below compare runtime registries in both directions so a newly added
action cannot inherit an unrelated capability classification by default.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


INVENTORY_SCHEMA_VERSION = "production-surface-inventory.v1"
INVENTORY_CONTRACT_VERSION = "3"
INVENTORY_PATH = Path(__file__).with_name("production_surface_inventory.v1.json")

SURFACES = frozenset(
    {
        "electron_ipc",
        "flask_http",
        "mcp_tool",
        "photon_bot",
        "plugin_editor_action",
        "plugin_editor_http",
        "python_worker",
        "render_profile",
    }
)
OWNERS = frozenset(
    {"agent_plugin", "backend", "electron_main", "photon_bot"}
)
EFFECTS = frozenset(
    {
        "agent-intent-spool-write",
        "egress",
        "filesystem-read",
        "filesystem-write",
        "mutation",
        "process-start",
        "read",
    }
)
AUTHORITIES = frozenset(
    {
        "agent-pairing",
        "bot-allowlist",
        "bootstrap-receipt",
        "desktop-session",
        "editor-boot-token",
        "editor-session",
        "electron-main",
        "export-grant",
        "idempotency-key",
        "loopback-client",
        "native-user-gesture",
        "none",
        "provider-credential",
        "revision-cas",
        "runtime-generation",
        "source-identity",
        "trusted-ipc-sender",
        "worker-internal",
    }
)
NETWORK_INBOUND = frozenset(
    {"internal", "ipc", "loopback", "none", "platform", "stdio"}
)
NETWORK_OUTBOUND = frozenset(
    {"loopback", "none", "os-external", "provider"}
)
OFFLINE_POLICIES = frozenset({"allow", "deny"})
VERIFICATION_MODES = frozenset({"automatic", "static"})
VERIFICATION_STATUSES = frozenset({"enforced", "pending_external_gate"})
ORACLE_RUNNERS = frozenset({"node-test", "photon-test", "python-unittest"})
ORACLE_CLAIMS = frozenset(
    {
        "artifact-sanitization",
        "authority-deny",
        "branch-deny",
        "offline-deny",
        "registry-deny",
        "scope-deny",
        "surface-equality",
    }
)
HIGH_IMPACT_EFFECTS = frozenset(
    {
        "agent-intent-spool-write",
        "egress",
        "filesystem-read",
        "filesystem-write",
        "mutation",
        "process-start",
    }
)

_ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/<>-]{0,399}$")
# A scope names a resource-binding dimension, never the transport action that
# reaches it.  Keeping this grammar separate from action/test identifiers
# prevents route, IPC channel, tool, Bot action, job kind, or render profile
# names from masquerading as resource-scope proof.
_SCOPE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_TEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/<>-]{0,399}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProductionSurfaceInventoryError(ValueError):
    """Raised when the frozen inventory or a runtime action set is invalid."""


class UnknownProductionActionError(ProductionSurfaceInventoryError):
    """Raised when a dispatcher receives an action absent from the manifest."""


@dataclass(frozen=True)
class ActionOracleClaim:
    """One oracle's exact proof boundary for one production action."""

    claims: frozenset[str]
    authority_denials: frozenset[str]
    scope_denials: frozenset[str]


def _fail(message: str) -> None:
    raise ProductionSurfaceInventoryError(message)


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _reject_non_finite(token: str) -> None:
    _fail(f"Non-finite JSON number is forbidden: {token}")


def parse_inventory_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_non_finite,
        )
    except ProductionSurfaceInventoryError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionSurfaceInventoryError("Inventory JSON is invalid.") from exc
    if type(value) is not dict:
        _fail("Inventory root must be an object.")
    _reject_non_finite_tree(value)
    return value


def _reject_non_finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        _fail("Inventory contains a non-finite number.")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_non_finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            _reject_non_finite_tree(child)


def canonical_inventory_json(inventory: Mapping[str, Any]) -> str:
    payload = dict(inventory)
    payload.pop("inventory_sha256", None)
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProductionSurfaceInventoryError(
            "Inventory cannot be represented as canonical JSON."
        ) from exc


def canonical_inventory_sha256(inventory: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_inventory_json(inventory).encode("utf-8")).hexdigest()


def _closed_object(
    value: Any,
    *,
    field: str,
    required: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{field} must be an object.")
    keys = frozenset(value)
    if keys != required:
        missing = sorted(required - keys)
        unknown = sorted(keys - required)
        _fail(f"{field} is not closed (missing={missing}, unknown={unknown}).")
    return value


def _string(value: Any, *, field: str, allowed: frozenset[str] | None = None) -> str:
    if type(value) is not str or not value:
        _fail(f"{field} must be a non-empty string.")
    if allowed is not None and value not in allowed:
        _fail(f"{field} is unsupported: {value}")
    return value


def _sorted_unique_strings(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if type(value) is not list or not value:
        _fail(f"{field} must be a non-empty array.")
    rows = tuple(
        _string(item, field=f"{field}[{index}]", allowed=allowed)
        for index, item in enumerate(value)
    )
    if list(rows) != sorted(rows) or len(rows) != len(set(rows)):
        _fail(f"{field} must be sorted and unique.")
    if pattern is not None and any(pattern.fullmatch(item) is None for item in rows):
        _fail(f"{field} contains an invalid identifier.")
    return rows


def _sorted_unique_optional_strings(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if type(value) is not list:
        _fail(f"{field} must be an array.")
    rows = tuple(
        _string(item, field=f"{field}[{index}]", allowed=allowed)
        for index, item in enumerate(value)
    )
    if list(rows) != sorted(rows) or len(rows) != len(set(rows)):
        _fail(f"{field} must be sorted and unique.")
    if pattern is not None and any(pattern.fullmatch(item) is None for item in rows):
        _fail(f"{field} contains an invalid identifier.")
    return rows


def _parse_action_claim_rows(
    value: Any, *, field: str
) -> dict[str, ActionOracleClaim]:
    if type(value) is not list or not value:
        _fail(f"{field} must be a non-empty array.")
    action_ids: list[str] = []
    claims_by_action: dict[str, ActionOracleClaim] = {}
    for index, raw in enumerate(value):
        row = _closed_object(
            raw,
            field=f"{field}[{index}]",
            required=frozenset(
                {"action_id", "authority_denials", "claims", "scope_denials"}
            ),
        )
        action_id = _string(row["action_id"], field=f"{field}.action_id")
        if _ACTION_ID.fullmatch(action_id) is None:
            _fail(f"{field}.action_id is invalid: {action_id}")
        claims = _sorted_unique_strings(
            row["claims"],
            field=f"{field}.claims",
            allowed=ORACLE_CLAIMS,
        )
        authority_denials = _sorted_unique_optional_strings(
            row["authority_denials"],
            field=f"{field}.authority_denials",
            allowed=AUTHORITIES,
        )
        if "none" in authority_denials:
            _fail(f"{field}.authority_denials cannot deny the `none` authority.")
        scope_denials = _sorted_unique_optional_strings(
            row["scope_denials"],
            field=f"{field}.scope_denials",
            pattern=_SCOPE,
        )
        if ("authority-deny" in claims) != bool(authority_denials):
            _fail(
                f"{field} must bind authority-deny to one or more exact authorities."
            )
        if ("scope-deny" in claims) != bool(scope_denials):
            _fail(f"{field} must bind scope-deny to one or more exact scopes.")
        action_ids.append(action_id)
        claims_by_action[action_id] = ActionOracleClaim(
            claims=frozenset(claims),
            authority_denials=frozenset(authority_denials),
            scope_denials=frozenset(scope_denials),
        )
    if action_ids != sorted(action_ids) or len(action_ids) != len(set(action_ids)):
        _fail(f"{field} must be sorted by action_id and unique.")
    return claims_by_action


def _validate_oracle_action_backlinks(
    *,
    actions: Sequence[Mapping[str, Any]],
    oracle_ids: Sequence[str],
    oracle_action_claims: Mapping[str, Mapping[str, ActionOracleClaim]],
) -> None:
    actions_by_id = {str(row["id"]): row for row in actions}
    declared_action_ids_set = frozenset(actions_by_id)
    oracle_references: dict[str, set[str]] = {
        oracle_id: set() for oracle_id in oracle_ids
    }
    for row in actions:
        action_id = str(row["id"])
        for oracle_id in row["negative_tests"]:
            oracle_references[str(oracle_id)].add(action_id)
    for oracle_id in oracle_ids:
        claims_by_action = oracle_action_claims[oracle_id]
        unknown_actions = sorted(set(claims_by_action) - declared_action_ids_set)
        if unknown_actions:
            _fail(
                f"{oracle_id} claims undeclared production actions: {unknown_actions}"
            )
        claimed_actions = set(claims_by_action)
        referenced_actions = oracle_references[oracle_id]
        if claimed_actions != referenced_actions:
            missing_claims = sorted(referenced_actions - claimed_actions)
            stale_claims = sorted(claimed_actions - referenced_actions)
            _fail(
                f"{oracle_id} action coverage drifted "
                f"(missing_action_claims={missing_claims}, "
                f"stale_action_claims={stale_claims})."
            )
        for action_id, proof in claims_by_action.items():
            action = actions_by_id[action_id]
            declared_authorities = frozenset(
                str(item) for item in action.get("authority", []) if item != "none"
            )
            undeclared_authorities = sorted(
                proof.authority_denials - declared_authorities
            )
            if undeclared_authorities:
                _fail(
                    f"{oracle_id} denies undeclared authorities for {action_id}: "
                    f"{undeclared_authorities}"
                )
            declared_scopes = frozenset(str(item) for item in action.get("scope", []))
            undeclared_scopes = sorted(proof.scope_denials - declared_scopes)
            if undeclared_scopes:
                _fail(
                    f"{oracle_id} denies undeclared scopes for {action_id}: "
                    f"{undeclared_scopes}"
                )


def validate_inventory(inventory: Mapping[str, Any]) -> dict[str, Any]:
    root = _closed_object(
        inventory,
        field="inventory",
        required=frozenset(
            {
                "actions",
                "contract_version",
                "inventory_sha256",
                "negative_oracles",
                "schema_version",
                "surface_verification",
            }
        ),
    )
    if root["schema_version"] != INVENTORY_SCHEMA_VERSION:
        _fail("Inventory schema_version is unsupported.")
    if root["contract_version"] != INVENTORY_CONTRACT_VERSION:
        _fail("Inventory contract_version is unsupported.")
    digest = _string(root["inventory_sha256"], field="inventory.inventory_sha256")
    if _SHA256.fullmatch(digest) is None:
        _fail("inventory.inventory_sha256 must be a lowercase SHA-256 digest.")

    verification = root["surface_verification"]
    if type(verification) is not list or not verification:
        _fail("inventory.surface_verification must be a non-empty array.")
    verification_surfaces: list[str] = []
    for index, raw in enumerate(verification):
        row = _closed_object(
            raw,
            field=f"inventory.surface_verification[{index}]",
            required=frozenset({"mode", "owner", "status", "surface"}),
        )
        surface = _string(row["surface"], field="verification.surface", allowed=SURFACES)
        mode = _string(row["mode"], field="verification.mode", allowed=VERIFICATION_MODES)
        status = _string(
            row["status"], field="verification.status", allowed=VERIFICATION_STATUSES
        )
        _string(row["owner"], field="verification.owner", allowed=OWNERS)
        if mode == "static" and status != "pending_external_gate":
            _fail("Static surface verification cannot claim enforced status.")
        if mode == "automatic" and status != "enforced":
            _fail("Automatic surface verification must be enforced.")
        verification_surfaces.append(surface)
    if verification_surfaces != sorted(verification_surfaces) or len(
        verification_surfaces
    ) != len(set(verification_surfaces)):
        _fail("surface_verification must be sorted by surface and unique.")

    negative_oracles = root["negative_oracles"]
    if type(negative_oracles) is not list or not negative_oracles:
        _fail("inventory.negative_oracles must be a non-empty array.")
    oracle_ids: list[str] = []
    oracle_action_claims: dict[str, dict[str, ActionOracleClaim]] = {}
    for index, raw in enumerate(negative_oracles):
        row = _closed_object(
            raw,
            field=f"inventory.negative_oracles[{index}]",
            required=frozenset(
                {"action_claims", "id", "path", "runner", "selector"}
            ),
        )
        oracle_id = _string(row["id"], field="negative_oracle.id")
        if _TEST_ID.fullmatch(oracle_id) is None or "pending" in oracle_id.lower():
            _fail(f"Negative oracle id is invalid or unresolved: {oracle_id}")
        relative_path = _string(row["path"], field="negative_oracle.path")
        if len(relative_path) > 500:
            _fail("negative_oracle.path exceeds its size boundary.")
        parsed_path = PurePosixPath(relative_path)
        if (
            parsed_path.is_absolute()
            or not parsed_path.parts
            or any(part in {"", ".", ".."} for part in parsed_path.parts)
            or "\\" in relative_path
        ):
            _fail(f"Negative oracle path must be a canonical repository-relative path: {relative_path}")
        selector = _string(row["selector"], field="negative_oracle.selector")
        if len(selector) > 500:
            _fail("negative_oracle.selector exceeds its size boundary.")
        _string(row["runner"], field="negative_oracle.runner", allowed=ORACLE_RUNNERS)
        oracle_action_claims[oracle_id] = _parse_action_claim_rows(
            row["action_claims"],
            field=f"inventory.negative_oracles[{index}].action_claims",
        )
        oracle_ids.append(oracle_id)
    if oracle_ids != sorted(oracle_ids) or len(oracle_ids) != len(set(oracle_ids)):
        _fail("negative_oracles must be sorted by id and unique.")
    declared_oracle_ids = frozenset(oracle_ids)

    actions = root["actions"]
    if type(actions) is not list or not actions:
        _fail("inventory.actions must be a non-empty array.")
    action_ids: list[str] = []
    action_surfaces: set[str] = set()
    for index, raw in enumerate(actions):
        row = _closed_object(
            raw,
            field=f"inventory.actions[{index}]",
            required=frozenset(
                {
                    "authority",
                    "contract_version",
                    "effects",
                    "id",
                    "negative_tests",
                    "network",
                    "owner",
                    "scope",
                    "surface",
                }
            ),
        )
        action_id = _string(row["id"], field="action.id")
        if _ACTION_ID.fullmatch(action_id) is None:
            _fail(f"Action id is invalid: {action_id}")
        surface = _string(row["surface"], field="action.surface", allowed=SURFACES)
        _string(row["owner"], field="action.owner", allowed=OWNERS)
        _string(row["contract_version"], field="action.contract_version")
        _sorted_unique_strings(row["effects"], field="action.effects", allowed=EFFECTS)
        _sorted_unique_strings(
            row["authority"], field="action.authority", allowed=AUTHORITIES
        )
        _sorted_unique_strings(row["scope"], field="action.scope", pattern=_SCOPE)
        negative_tests = _sorted_unique_strings(
            row["negative_tests"], field="action.negative_tests", pattern=_TEST_ID
        )
        unknown_oracles = sorted(set(negative_tests) - declared_oracle_ids)
        if unknown_oracles:
            _fail(f"{action_id} references unresolved negative oracles: {unknown_oracles}")
        network = _closed_object(
            row["network"],
            field="action.network",
            required=frozenset({"inbound", "offline_policy", "outbound"}),
        )
        inbound = _string(
            network["inbound"], field="network.inbound", allowed=NETWORK_INBOUND
        )
        outbound = _string(
            network["outbound"], field="network.outbound", allowed=NETWORK_OUTBOUND
        )
        offline_policy = _string(
            network["offline_policy"],
            field="network.offline_policy",
            allowed=OFFLINE_POLICIES,
        )
        expected_offline = (
            "deny"
            if inbound == "platform" or outbound in {"os-external", "provider"}
            else "allow"
        )
        if offline_policy != expected_offline:
            _fail(
                f"{action_id} offline_policy must be {expected_offline} for outbound={outbound}."
            )
        action_ids.append(action_id)
        action_surfaces.add(surface)
    if action_ids != sorted(action_ids) or len(action_ids) != len(set(action_ids)):
        _fail("inventory.actions must be sorted by id and unique.")
    if action_surfaces != set(verification_surfaces):
        _fail("surface_verification must cover exactly the surfaces present in actions.")

    _validate_oracle_action_backlinks(
        actions=actions,
        oracle_ids=oracle_ids,
        oracle_action_claims=oracle_action_claims,
    )

    expected_digest = canonical_inventory_sha256(root)
    if digest != expected_digest:
        _fail("Inventory SHA-256 does not match its canonical content.")
    return dict(root)


def negative_oracle_action_claims(
    inventory: Mapping[str, Any],
) -> dict[str, dict[str, ActionOracleClaim]]:
    """Return each oracle's exact hash-bound action-to-claim closure."""

    rows = inventory.get("negative_oracles")
    if type(rows) is not list:
        _fail("Inventory negative_oracles are unavailable.")
    result: dict[str, dict[str, ActionOracleClaim]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("Inventory negative_oracles contain an invalid row.")
        action_claim_rows = row.get("action_claims")
        result[str(row["id"])] = _parse_action_claim_rows(
            action_claim_rows,
            field=f"negative_oracle[{row.get('id')}].action_claims",
        )
    return result


def high_impact_oracle_gaps(inventory: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Report high-impact actions missing authority, scope, or offline denial proof."""

    oracle_action_claims = negative_oracle_action_claims(inventory)
    gaps: dict[str, tuple[str, ...]] = {}
    for row in inventory.get("actions", []):
        if not isinstance(row, Mapping):
            continue
        effects = frozenset(str(item) for item in row.get("effects", []))
        if not effects.intersection(HIGH_IMPACT_EFFECTS):
            continue
        proofs = tuple(
            proof
            for oracle_id in row.get("negative_tests", [])
            if (
                proof := oracle_action_claims.get(str(oracle_id), {}).get(
                    str(row.get("id"))
                )
            )
        )
        claims = frozenset(claim for proof in proofs for claim in proof.claims)
        denied_authorities = frozenset(
            authority for proof in proofs for authority in proof.authority_denials
        )
        denied_scopes = frozenset(
            scope for proof in proofs for scope in proof.scope_denials
        )
        required_authorities = frozenset(
            str(item) for item in row.get("authority", []) if item != "none"
        )
        required_scopes = frozenset(str(item) for item in row.get("scope", []))
        missing: list[str] = [
            *(f"authority-deny:{item}" for item in sorted(required_authorities - denied_authorities)),
            *(f"scope-deny:{item}" for item in sorted(required_scopes - denied_scopes)),
        ]
        network = row.get("network")
        if (
            isinstance(network, Mapping)
            and network.get("offline_policy") == "deny"
            and "offline-deny" not in claims
        ):
            missing.append("offline-deny")
        if missing:
            gaps[str(row.get("id", "<unknown>"))] = tuple(sorted(missing))
    return gaps


def load_production_surface_inventory(path: Path = INVENTORY_PATH) -> dict[str, Any]:
    return validate_inventory(parse_inventory_json(path.read_text(encoding="utf-8")))


def declared_action_ids(
    inventory: Mapping[str, Any], *, surface: str | None = None
) -> frozenset[str]:
    rows = inventory.get("actions")
    if type(rows) is not list:
        _fail("Inventory actions are unavailable.")
    return frozenset(
        str(row["id"])
        for row in rows
        if isinstance(row, Mapping) and (surface is None or row.get("surface") == surface)
    )


def require_declared_action(
    inventory: Mapping[str, Any], action_id: str
) -> Mapping[str, Any]:
    for row in inventory.get("actions", []):
        if isinstance(row, Mapping) and row.get("id") == action_id:
            return row
    raise UnknownProductionActionError(
        f"Production action is not declared and must fail closed: {action_id}"
    )


def assert_surface_set_equality(
    inventory: Mapping[str, Any], *, surface: str, discovered: Iterable[str]
) -> None:
    if surface not in SURFACES:
        _fail(f"Unknown production surface: {surface}")
    actual_rows = tuple(discovered)
    if len(actual_rows) != len(set(actual_rows)):
        _fail(f"Runtime discovery returned duplicate {surface} action ids.")
    actual = frozenset(actual_rows)
    declared = declared_action_ids(inventory, surface=surface)
    if actual != declared:
        missing = sorted(actual - declared)
        stale = sorted(declared - actual)
        _fail(
            f"{surface} action set drifted (undeclared_runtime={missing}, stale_manifest={stale})."
        )


def discover_flask_actions(app: Any) -> frozenset[str]:
    actions: set[str] = set()
    for rule in app.url_map.iter_rules():
        for method in set(rule.methods or ()) - {"HEAD", "OPTIONS"}:
            actions.add(f"flask_http.{method}:{rule.rule}")
    return frozenset(actions)


def discover_mcp_actions(tools: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    names: list[str] = []
    for index, tool in enumerate(tools):
        name = tool.get("name")
        if type(name) is not str or not name:
            _fail(f"MCP tool {index} has no valid name.")
        names.append(name)
    if len(names) != len(set(names)):
        _fail("MCP TOOLS contains duplicate names.")
    return frozenset(f"mcp_tool.{name}" for name in names)


def _source_tree(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ProductionSurfaceInventoryError(
            f"Production source cannot be inspected: {path.name}"
        ) from exc


def _function(tree: ast.Module, class_name: str, function_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == function_name:
                    return member
    _fail(f"Cannot discover {class_name}.{function_name}.")
    raise AssertionError("unreachable")


def _action_comparisons(function: ast.FunctionDef) -> frozenset[str]:
    values: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "action"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and type(node.comparators[0].value) is str
        ):
            values.add(node.comparators[0].value)
    return frozenset(values)


def _literal_assignment(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and (
                isinstance(getattr(node, "target", None), ast.Name)
                and getattr(node, "target").id == name
                or any(
                    isinstance(target, ast.Name) and target.id == name
                    for target in getattr(node, "targets", ())
                )
            )
        ):
            try:
                return ast.literal_eval(node.value)
            except (TypeError, ValueError) as exc:
                raise ProductionSurfaceInventoryError(
                    f"{name} must remain a literal registry for inventory discovery."
                ) from exc
    _fail(f"Cannot discover literal registry {name}.")
    raise AssertionError("unreachable")


def discover_plugin_editor_actions(
    *, editor_server_path: Path, canonical_editor_path: Path
) -> frozenset[str]:
    server_tree = _source_tree(editor_server_path)
    canonical_tree = _source_tree(canonical_editor_path)
    legacy = _action_comparisons(
        _function(server_tree, "EditorServerManager", "apply_action")
    )
    canonical = _action_comparisons(
        _function(server_tree, "EditorServerManager", "_apply_canonical_action")
    )
    edit_fields = _literal_assignment(canonical_tree, "_EDIT_FIELDS")
    if type(edit_fields) is not dict or not all(type(key) is str for key in edit_fields):
        _fail("_EDIT_FIELDS is not a closed editor operation registry.")
    return frozenset(
        [*(f"plugin_editor_action.legacy.{name}" for name in legacy)]
        + [*(f"plugin_editor_action.canonical.{name}" for name in canonical)]
        + [
            *(f"plugin_editor_action.canonical.edit.{name}" for name in edit_fields)
        ]
    )


def discover_plugin_editor_http_actions(editor_server_path: Path) -> frozenset[str]:
    tree = _source_tree(editor_server_path)
    discovered: set[str] = set()
    for method in ("GET", "HEAD", "POST"):
        function = _function(tree, "_EditorRequestHandler", f"do_{method}")
        assigned_path_templates: dict[str, str] = {}
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and (template := _http_path_template(node.value)) is not None
            ):
                assigned_path_templates[node.targets[0].id] = template
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_clip_media_id"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "path"
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == "session_id"
            ):
                discovered.add(
                    f"plugin_editor_http.{method}:"
                    "/api/sessions/<session_id>/clip-media/<clip_id>"
                )
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "path"
                and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
                and len(node.comparators) == 1
            ):
                template = _http_path_template(node.comparators[0])
                if template is not None:
                    discovered.add(f"plugin_editor_http.{method}:{template}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "path"
                and len(node.args) == 1
            ):
                prefix_nodes = (
                    node.args[0].elts
                    if isinstance(node.args[0], ast.Tuple)
                    else [node.args[0]]
                )
                for prefix_node in prefix_nodes:
                    prefix = _http_path_template(prefix_node)
                    if prefix is None and isinstance(prefix_node, ast.Name):
                        prefix = assigned_path_templates.get(prefix_node.id)
                    if prefix in {"/editor/", "/canonical-editor/"}:
                        discovered.add(
                            f"plugin_editor_http.{method}:{prefix}<path:session_route>"
                        )
                    elif prefix is not None and prefix.endswith(
                        "/replacement-previews/"
                    ):
                        discovered.add(
                            f"plugin_editor_http.{method}:"
                            f"{prefix}<clip_id>/<assignment_id>"
                        )
                    elif prefix is not None and prefix.endswith("/previews/"):
                        discovered.add(
                            f"plugin_editor_http.{method}:{prefix}<clip_id>"
                        )
    if not discovered:
        _fail("Plugin editor HTTP route discovery returned no actions.")
    return frozenset(discovered)


def _http_path_template(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value if node.value.startswith("/") else None
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and type(value.value) is str:
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
            parts.append(f"<{value.value.id}>")
        else:
            return None
    template = "".join(parts)
    return template if template.startswith("/") else None


def discover_python_worker_actions() -> dict[str, frozenset[str]]:
    from backend.src.media.canonical_export import CANONICAL_EXPORT_WORKER_ACTIONS
    from backend.src.media.render_plan import RENDER_PROFILE_REGISTRY
    from backend.src.media.video import MEDIA_JOB_KIND_REGISTRY

    return {
        "python_worker": frozenset(
            [*(f"python_worker.media.{kind}" for kind in MEDIA_JOB_KIND_REGISTRY)]
            + [
                *(f"python_worker.canonical_export.{action}" for action in CANONICAL_EXPORT_WORKER_ACTIONS)
            ]
        ),
        "render_profile": frozenset(
            f"render_profile.{profile}" for profile in RENDER_PROFILE_REGISTRY
        ),
    }


__all__ = [
    "INVENTORY_CONTRACT_VERSION",
    "INVENTORY_PATH",
    "INVENTORY_SCHEMA_VERSION",
    "ProductionSurfaceInventoryError",
    "UnknownProductionActionError",
    "assert_surface_set_equality",
    "canonical_inventory_json",
    "canonical_inventory_sha256",
    "declared_action_ids",
    "discover_flask_actions",
    "discover_mcp_actions",
    "discover_plugin_editor_actions",
    "discover_plugin_editor_http_actions",
    "discover_python_worker_actions",
    "high_impact_oracle_gaps",
    "load_production_surface_inventory",
    "negative_oracle_action_claims",
    "parse_inventory_json",
    "require_declared_action",
    "validate_inventory",
]
