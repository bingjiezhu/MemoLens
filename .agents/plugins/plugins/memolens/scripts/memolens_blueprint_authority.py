"""Read-only validation and projection of the B1 decision-authority ledger.

The persisted Blueprint document always keeps its creation authority marked as
an Agent proposal.  User authority is a separate append-only ledger and is
projected as-of one exact Blueprint revision by this module.
"""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import re
import sqlite3
from typing import Any, Iterator, Mapping

from memolens_contracts import MemoLensError
from memolens_creative_blueprint import canonical_json, canonical_sha256
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json


DECISION_UNITS = (
    "intent_goal",
    "intent_stance",
    "script",
    "creative_direction",
    "output",
    "material_constraints",
    "references",
    "techniques",
)

AUTHORITY_EVENT_COLUMNS = {
    "id",
    "database_uuid",
    "project_id",
    "sequence",
    "parent_event_id",
    "parent_event_sha256",
    "authority_operation",
    "observed_revision",
    "observed_content_sha256",
    "observed_semantic_sha256",
    "observed_operation_id",
    "decision_units_json",
    "unit_digests_json",
    "active_confirmation_ids_json",
    "presentation_json",
    "presentation_sha256",
    "runtime_authority_epoch",
    "native_gesture_nonce",
    "request_sha256",
    "event_sha256",
    "created_at",
}
AUTHORITY_HEAD_COLUMNS = {
    "project_id",
    "sequence",
    "event_id",
    "event_sha256",
    "updated_at",
}
AUTHORITY_RECEIPT_COLUMNS = {
    "database_uuid",
    "authenticated_principal",
    "project_id",
    "command_type",
    "command_version",
    "idempotency_key",
    "request_sha256",
    "event_id",
    "response_status",
    "response_json",
    "response_sha256",
    "receipt_sha256",
    "resource_type",
    "resource_id",
    "created_at",
}
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
    *AGENT_COMMAND_RECEIPT_COLUMNS,
    "authenticated_principal",
    "claimed_client_label",
    "request_json",
    "actor_json",
    "origin_json",
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
DESKTOP_RECEIPT_INTEGRITY_COLUMNS = {
    "project_id",
    "operation_id",
    "receipt_sha256",
}
AUTHORITY_RELATIONS = {
    "agent_project_capabilities": AGENT_CAPABILITY_COLUMNS,
    "agent_pairing_confirmation_receipts": AGENT_PAIRING_RECEIPT_COLUMNS,
    "agent_project_command_receipts": AGENT_PROJECT_COMMAND_RECEIPT_COLUMNS,
    "agent_project_capability_events": AGENT_CAPABILITY_EVENT_COLUMNS,
    "blueprint_decision_authority_events": AUTHORITY_EVENT_COLUMNS,
    "blueprint_decision_authority_heads": AUTHORITY_HEAD_COLUMNS,
    "blueprint_decision_authority_receipts": AUTHORITY_RECEIPT_COLUMNS,
    "blueprint_desktop_receipt_integrity": DESKTOP_RECEIPT_INTEGRITY_COLUMNS,
}

DESKTOP_COMMAND_RECEIPT_COLUMNS = {
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

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$"
)
_AUTHORITY_JSON_MAX_BYTES = 1_048_576
# Core permits a persisted semantic value to consume 20,000 nodes at depth 16.
# A decision-authority presentation embeds that value below its envelope, so a
# standalone Agent reader must budget the small, deterministic wrapper too.
_JSON_LIMITS = StrictJsonLimits(
    max_bytes=_AUTHORITY_JSON_MAX_BYTES,
    max_depth=18,
    max_nodes=20_128,
    max_object_items=64,
    max_array_items=512,
    max_string_chars=12_000,
)
_MAX_AUTHORITY_EVENTS = 4_096
_MAX_AUTHORITY_PROJECTION_CACHE = 64
# These are Core-produced canonical JSON columns.  The three compact columns
# have exact shape-derived ceilings; only the presentation and response use
# Core's frozen B1 1 MiB envelope.  Per-project aggregate ceilings remain a
# separate Agent recovery resource boundary and are checked before SELECT *.
_MAX_DECISION_UNITS_BYTES = len(canonical_json(list(DECISION_UNITS)).encode("utf-8"))
_MAX_UNIT_DIGESTS_BYTES = len(
    canonical_json({unit: "0" * 64 for unit in DECISION_UNITS}).encode("utf-8")
)
_MAX_ACTIVE_CONFIRMATION_IDS_BYTES = len(
    canonical_json({unit: "x" * 200 for unit in DECISION_UNITS}).encode("utf-8")
)
_AUTHORITY_EVENT_JSON_COLUMN_LIMITS = {
    "decision_units_json": _MAX_DECISION_UNITS_BYTES,
    "unit_digests_json": _MAX_UNIT_DIGESTS_BYTES,
    "active_confirmation_ids_json": _MAX_ACTIVE_CONFIRMATION_IDS_BYTES,
    "presentation_json": _AUTHORITY_JSON_MAX_BYTES,
}
_MAX_RECEIPT_RESPONSE_BYTES = _AUTHORITY_JSON_MAX_BYTES
_MAX_AUTHORITY_EVENT_JSON_BYTES = 64 * 1_048_576
_MAX_AUTHORITY_RECEIPT_JSON_BYTES = 64 * 1_048_576
_AGENT_ACTIONS = (
    "blueprint.commit_proposal",
    "blueprint.restore_revision",
    "timeline.apply_edit",
    "timeline.apply_structural_edit",
    "timeline.restore_revision",
    "timeline.preview_media",
)

# Frozen from Core's managed schema releases.  The read-only Agent surface
# cannot import the application package when installed as a standalone plugin,
# so it carries the exact migration and normalized sqlite_schema digests it
# trusts.  This is deliberately stricter than a column census: a same-name
# view, a weakened CHECK/FK, or a replaced immutability trigger must fail
# closed.  Each release extends the prior manifest; it never replaces those
# proofs.
CURRENT_MANAGED_SCHEMA_VERSION = 20
_V5_MIGRATIONS = (
    (1, "image_index_baseline", "5ffaaf8a9a06e4f8fe2e091f92a726f1ac4b747f153c0c5750f5c8750e40f50a"),
    (2, "video_creative_workbench", "8563c16d6ccb95c36e13abf4143a888b109e8a6606da9744c64307755f052654"),
    (3, "creator_memory_media_inbox", "4ba03fd7b65b59683d721cd5340805d893dda59d72a087bf824af117f265fc90"),
    (4, "canonical_blueprint_proposal_ledger", "dfc3227eff26b8c8b2fde38269a9549fe890c13a364a517f740959a95b528d39"),
    (5, "paired_agent_decision_authority", "5889c2f79a36aeed8937e4d8f31994767cd56ae193d8bf27dc636906abc72526"),
)

_V6_MIGRATIONS = _V5_MIGRATIONS + (
    (6, "canonical_coverage_plan_baseline", "cddb91190311db0f5593db6af5788d54f51c2d82385cad4ea6067da51461716e"),
)

_V7_MIGRATIONS = _V6_MIGRATIONS + (
    (7, "canonical_timeline_first_cut", "687918fbb4692d872b025467321d7c2509c64836b8bad7717eef46960783edf2"),
)

_V8_MIGRATIONS = _V7_MIGRATIONS + (
    (8, "canonical_export_usage_ledger", "6993c2ab8b74b59b39e275462ea7377f5fb35fa1a646b01c0c6ced3c5c89e33d"),
)

_V9_MIGRATIONS = _V8_MIGRATIONS + (
    (9, "canonical_timeline_reconciliation", "75f10472504c0abce2f6aa4ced7a59745a31f45edb8c209dc5af3f5efc29c2a0"),
)

_V10_MIGRATIONS = _V9_MIGRATIONS + (
    (10, "canonical_timeline_persistent_edit", "30431939248aefb84a4f62b96c4f6fcf144672679b2bb8c9ba41540cb353a7af"),
)

_V11_MIGRATIONS = _V10_MIGRATIONS + (
    (11, "paired_agent_canonical_timeline_edit", "11c5a402bbbfa70ced8477bc835fb85c3c223bec8eb200cc66e3eecfbd13a31e"),
)

_V12_MIGRATIONS = _V11_MIGRATIONS + (
    (12, "paired_agent_project_command_receipt_convergence", "3cb7ae9007333c0cc65bf2f420cb31c50b2c9e2aefc4c857481805ff0722da66"),
)

_V13_MIGRATIONS = _V12_MIGRATIONS + (
    (13, "paired_agent_canonical_timeline_restore", "c28e18ceebf0c0fe5ca116917068b857db800aacff8ee05513dfeaa6c3dc6f27"),
)

_V14_MIGRATIONS = _V13_MIGRATIONS + (
    (14, "canonical_image_publication_bridge", "3f6988dc802bdcb82db20bf040537b56b5424787f2f56eef73708decbe63293b"),
)

_V15_MIGRATIONS = _V14_MIGRATIONS + (
    (15, "paired_agent_timeline_preview_media", "d143a791612e9fb072560b2ddbfa72e2ab4b4222bfd416bb393cbd67a879e5c1"),
)

_V16_MIGRATIONS = _V15_MIGRATIONS + (
    (16, "provider_egress_one_shot_authority", "0b090372f28818744ddfba595588d752009aeef9abae193990c66d945646a946"),
)

_V17_MIGRATIONS = _V16_MIGRATIONS + (
    (17, "canonical_image_read_cutover", "8981e17aa8f74fbb639692ff409414c700ef573fd2bb86a6144391403595060c"),
)

_V18_MIGRATIONS = _V17_MIGRATIONS + (
    (18, "canonical_timeline_structural_edit", "986674ef1e9fc2a62289bdd1d4afd4661d37ee42315f3ad4631d13cd0f3e0f93"),
)

_V19_MIGRATIONS = _V18_MIGRATIONS + (
    (19, "canonical_export_output_root_anchor", "31c9436102ff0017e332ebf794d86a80fcfd436cf8648d98ca35897dcc083d96"),
)

_V20_MIGRATIONS = _V19_MIGRATIONS + (
    (20, "plugin_first_library_bootstrap", "cd41495b297c0abc97ca3b1aec0371b2531bf30c12719a80e067a9ae5f0119fe"),
)

_V5_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "schema_migrations"): "963890796f79ffe44ba53712bebc8a36520d4dab4bea0e9c9a59844be2f5d110",
    ("table", "database_meta"): "d2a119cc22d22132ed91e2b5ed628fba2c1e2468aeb45f900c0764e0970450fe",
    ("table", "creative_blueprint_operations"): "faa3a702627c938d11abd99cfd52f7002d263e6064af6f122c7feafb781589dd",
    ("table", "creative_blueprint_revisions"): "4b9461c0ff50e784d2c2e7a325eb1624a062411fb7568fa9c7acb5de1e63ff7f",
    ("table", "creative_blueprint_heads"): "cd2950b3731895c45097f3c24effd05618bc083d923f87aa739e06076ceb9751",
    ("table", "blueprint_command_receipts"): "899cab90613ef4abe69c9355f3f034e7a85e54dbf942cef981ce2a9fdf2383fa",
    ("index", "idx_blueprint_revisions_operation"): "c5ec2e325f9dad29612714d10694ec06456e9d268ea094ddc7d271822caba32e",
    ("index", "idx_creative_blueprint_operations_project_sequence"): "803ba1bceb4cf69c8dc1290baed7a928ef2caebaba68e574ce096bc4a30c01cf",
    ("index", "idx_blueprint_receipts_operation"): "62d31a9ff7f518b0abb579f68d6ecbd4631a701bda5e7b8b93079b57c9234f81",
    ("trigger", "trg_blueprint_heads_initial_revision"): "de06177759d67d11bed09eb1a6d3183b5d5e2a0fa1d9df9efb62458990cc0ec2",
    ("trigger", "trg_blueprint_heads_no_delete"): "aba494962499986074c883be3fe049b7ae44462982e8c1a5e8cbc40d54b89d44",
    ("trigger", "trg_blueprint_heads_forward_only"): "0641b7e7631c0a8c1e9d9769af0c9a90bbcd774a3f0c5e7e48ef4ae193f09870",
    ("trigger", "trg_blueprint_revisions_no_update"): "56aa567362b06842588dca2502550a93fe572ec08715ef1102623f352b7e7431",
    ("trigger", "trg_blueprint_revisions_no_delete"): "76bffb91ffc2cfd5f8dbe8317a07f2c4c9207fe8bf09e252588461f9799f5902",
    ("trigger", "trg_creative_blueprint_operations_no_update"): "f668c227e989867ed11af179735f51932ac3ddd21dabf7679c5128bb3513f78e",
    ("trigger", "trg_creative_blueprint_operations_no_delete"): "e5acc32bce33cf04c40e322c44ba07b58649ab64d003c7c4bb8b5545b6ec92d0",
    ("trigger", "trg_blueprint_receipts_no_update"): "7ca5e6f9364ace5af1328ea1f4b0aac7017da2fa2a855853a32e82a13b1aa39e",
    ("trigger", "trg_blueprint_receipts_no_delete"): "85fa36964e517b326553d6d7f4ba58dedf1049f2a5fbfe79c8b28d2930335a06",
    ("trigger", "trg_database_meta_no_schema_downgrade"): "05673aeabdc72c89de7ed6d29636e348ddee87d09a19bdbe5ef7883966915ca0",
    ("table", "agent_project_capabilities"): "2135d702a97844495258dafcce59c538823d64f086f5f43d92b94e4f998b898b",
    ("table", "agent_pairing_confirmation_receipts"): "0c939499eb0e52f06189292d49f577ff2d78c94f61b41b3812e9bb92e72f2e44",
    ("table", "agent_blueprint_command_receipts"): "294bdb0e7586acc4bd9f24e4df6e8f80d1b66a41eacad771239dd89ad7cb71fd",
    ("table", "agent_project_capability_events"): "9141e88ccddb7d65563504f0b606574991a5cebe0ef47ae696bb17bdc15da238",
    ("table", "blueprint_decision_authority_events"): "37b7a0248ad7b02d87da5f76b15606bc4682de0d4c2e85c6ef83e431b7e4ebff",
    ("table", "blueprint_decision_authority_heads"): "13b1e86a8e2b364b3d245453c8400e3de6f0ee2a008f900fcf87358d0d746640",
    ("table", "blueprint_decision_authority_receipts"): "f4d944f2b2518de99bce0366b98ae91976920e7f4a147c8278ecfee436b006ad",
    ("table", "blueprint_desktop_receipt_integrity"): "9558ba544dd7f2ae3190e6848afd73f109f1a3a655743f8145db16067aa2e686",
    ("index", "idx_agent_capabilities_project_expiry"): "b54e5ffddcf05f46ae626cd7fadea4fc1bbb605f2a76a422ff18f0578067c312",
    ("index", "idx_agent_capability_events_sequence"): "eb44cd77d21cf63d3fec9d10a1734033908b1b46d46cbce0396a4d9ab3b59681",
    ("index", "idx_agent_capability_events_native_gesture"): "37131ad4de9f1629a993299f1c48aaf4389c9ceae38dffdeb8abdcc9bd90b72e",
    ("index", "idx_agent_blueprint_receipts_operation"): "375a9b6ca21c3e9a69f2150e863d00911d1ce247433b1b804683f80729a1b9cc",
    ("index", "idx_blueprint_authority_events_revision"): "2e2a3fa772f1ffbf8a13fe7c3860abddb171dd47d292f5d87912eb9a1cb131cd",
    ("index", "idx_blueprint_authority_receipts_event"): "b8cc4d3aa7e50c3a6fac382cf28610465e4ec551e41bcea0ebc337750af5918e",
    ("trigger", "trg_agent_capabilities_no_update"): "7105a898dbffe91178d6cada719fc2df06d470d37c3c8cedb7f3b4af1ba2deff",
    ("trigger", "trg_agent_capabilities_no_delete"): "b36b66d3761d167226d0501bf3a33fe21c6d136f821cdefeb5301672ef408321",
    ("trigger", "trg_agent_pairing_receipts_no_update"): "72e6298996e5743f40def9017b7810bdfa89eab34333b53c4d09c4cc4e85e325",
    ("trigger", "trg_agent_pairing_receipts_no_delete"): "ab7cb179d0897f9aba2e885092288f983a737584a11faf1828777346afd2927c",
    ("trigger", "trg_agent_blueprint_receipts_no_update"): "298bf2ff2eae06f9efc40b8b829e308b353f27e2e277d996a5c08556a9c5249d",
    ("trigger", "trg_agent_blueprint_receipts_no_delete"): "13839a9c287bbe729c6337332ff53848cdfc0b8457640ac99d1ca21b1a3c2d3f",
    ("trigger", "trg_agent_capability_events_no_update"): "a38e581faed1b0d1ff730e24974f6d18daa067d633c2c01c8d19a631c62d0dfb",
    ("trigger", "trg_agent_capability_events_no_delete"): "847f8c93591dd03667345d9cc3e3dd07ea285b9389aa9ab9abe8731a25c184b7",
    ("trigger", "trg_blueprint_authority_events_no_update"): "2a3e652db1270d11f47377886674a4a33e4d3140bcf1ffe3a492983c0fb222da",
    ("trigger", "trg_blueprint_authority_events_no_delete"): "5d59ca098da3117d4d3718f506178bee162f035fa7e84843d8e3fdd5fae638df",
    ("trigger", "trg_blueprint_authority_receipts_no_update"): "444d130c787c5fc1b8f44856b84ded1b5e4596d335229ecf8da3c7a270a13613",
    ("trigger", "trg_blueprint_authority_receipts_no_delete"): "c4d5f9c33c02bee45c382a8dfa334767c491b7c8c48028cb2f1f2b77ea9a4de9",
    ("trigger", "trg_blueprint_authority_heads_initial_sequence"): "eabbf0f64c60884a53d6ccab624874712c6bf6baf46dd2776d70058d15f841a5",
    ("trigger", "trg_blueprint_authority_heads_no_delete"): "5759aca5cd6b8409f23ee5f97519ea32ac088be7eea02c5330418c5c4104c3c6",
    ("trigger", "trg_blueprint_authority_heads_forward_only"): "ce3174025738cca3f2d52007b6757c1b4e6ad4f1d6fd696dc13b369b8e9a5c70",
    ("trigger", "trg_blueprint_desktop_receipt_integrity_no_update"): "93885b31aaff37d4af64388457d83882efa9f0562f3586504bbf78039d2eace0",
    ("trigger", "trg_blueprint_desktop_receipt_integrity_no_delete"): "f09cbac731580acfcbbf2d8a27f9217d0376be47826fce44a929fcad68dbcc91",
}

_V6_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "coverage_plan_operations"): "a2f329eb166de3a13ab7082a6827c91cd7980709e0b3bf2d98d04d46baacd6de",
    ("table", "coverage_plan_revisions"): "c4bf4f3db7a89f06394a9e32459c57641204550f13cb1f4bfcb89fb4da7b886a",
    ("table", "coverage_plan_heads"): "466bc32b2886e5a18d60a85f01fe36558087cecf7efe47cb2d876455a5ba560c",
    ("table", "coverage_plan_receipts"): "9d340765839e13cda74df0caa5c57368401587fd4b20e085a8ddf15741942767",
    ("index", "idx_coverage_operations_project_sequence"): "44c91383f547dd1491db30d19bb3e006b5b7d9d7da65905b86ea1f7a918827e5",
    ("index", "idx_coverage_revisions_blueprint"): "9f2e58044c76f2e49f89202c8d6f1c32a33e208fb69dd22bbb2e5941bd60446f",
    ("index", "idx_coverage_receipts_operation"): "db4eea9ea6abdc1db8b336eeb5eb0b58b89464112bbde523468b058d8c5cbf81",
    ("trigger", "trg_coverage_operations_no_update"): "3cdb1eb4fafd4ecaec203279895bbb4abecafe0db06904b520b42ab3be40c3ea",
    ("trigger", "trg_coverage_operations_no_delete"): "a9d41192266f063ce147c5a620d33a210b9c4645500c81752d8cfec0626cd12f",
    ("trigger", "trg_coverage_revisions_no_update"): "856796385c1c8665459b911328c40b4ff591b20733a2195c74947db9e952cc14",
    ("trigger", "trg_coverage_revisions_no_delete"): "871e0721f389c7d7bc29e3d88638e6abb1747c457d5bd6a4b2239bb4ab9ca95b",
    ("trigger", "trg_coverage_heads_initial_revision"): "6445097e635c53f82c26d82ca8aaf2ddbea9abd7dc4cde58215206bfb1481a45",
    ("trigger", "trg_coverage_heads_no_delete"): "c10dbb6fe94098495762e78225c0ed1d900c65c62412ba51c05e3d9cf852d05b",
    ("trigger", "trg_coverage_heads_forward_only"): "957cde32983ec5eef2b0173442d3be8ca3147516e863494fd878983b1233b33b",
    ("trigger", "trg_coverage_receipts_no_update"): "3da12090ad0b2a413a0bde6926aafacdc6b1bdd4cc44726425985404d6792b42",
    ("trigger", "trg_coverage_receipts_no_delete"): "1deb5da944ab3f287b6e52d39717528e778f977bb3683b84e7a5aaa1253b707c",
}

_V6_PHYSICAL_SCHEMA_SHA256 = {
    **_V5_PHYSICAL_SCHEMA_SHA256,
    **_V6_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V7_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "canonical_timeline_operations"): "8639673ef024e7f6ccbf105ddff6bfcafcc6b976d3913f1e2609d583583a0d35",
    ("table", "canonical_timeline_revisions"): "32dcd4e5677d534e1a4f38f62370232ebd4108d8d56eadea5750a73a89cc910d",
    ("table", "canonical_timeline_heads"): "a996015b4ab6b413cef98b82d803f588d3b5b3be464d108a4e57c50fd6b183f7",
    ("table", "canonical_timeline_receipts"): "5a075065990f10c1f3ea2c3424e665bd372a5ab752fc25189236b89587e470ea",
    ("index", "idx_canonical_timeline_operations_project_sequence"): "176c2317862c5b65ef91d878d6887a3b48bd28c0401eabebb619677c30d4c810",
    ("index", "idx_canonical_timeline_revisions_coverage"): "d46368c0dce694ff30366bd5a850e01647a777646257a30409a2e8f7614fadd4",
    ("index", "idx_canonical_timeline_receipts_operation"): "8c8cfe3af213042f6ec8e694af339130725f9d04d56a2ce4e251f52dcc351b16",
    ("trigger", "trg_canonical_timeline_operations_no_update"): "9641b81a645f704517e4149400be75b5845df86fa27d2a91e7a584a2398520d4",
    ("trigger", "trg_canonical_timeline_operations_no_delete"): "62f52e83b698be983d0cacf7d926d3eb217ea720c4de7b9fc87c1c11c8097567",
    ("trigger", "trg_canonical_timeline_revisions_no_update"): "3f3559cc2940b0b3f1c07b8b6817defeaa04e4dcdeed7cf0e2804b6a86a5e7fa",
    ("trigger", "trg_canonical_timeline_revisions_no_delete"): "6cdf327159ed19f4d3f6b29d3a3500b7f431d100d54f264288f040f80e83c284",
    ("trigger", "trg_canonical_timeline_heads_initial_revision"): "0e475b6a871bdb8bbaa6a28071c30b0b835855f50a8f73e126fdbce0c27ab318",
    ("trigger", "trg_canonical_timeline_heads_no_delete"): "749065d215e7cbf57e75a5dbbbdaecf04f6fdf6075e091b9847867329bce7d1a",
    ("trigger", "trg_canonical_timeline_heads_forward_only"): "7bc61857fc91b6f07565049a154f56a61f439bb950ec54fa4c65b55fa5fdb44c",
    ("trigger", "trg_canonical_timeline_receipts_no_update"): "846740a5757a40a23d78a95fc08146d5a49d0e9b74c60078ae26c2a43508c2fa",
    ("trigger", "trg_canonical_timeline_receipts_no_delete"): "55da7c44f66aae1016b5969743ef55031f68e77d56b4551968b31345688a221f",
}

_V7_PHYSICAL_SCHEMA_SHA256 = {
    **_V6_PHYSICAL_SCHEMA_SHA256,
    **_V7_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V8_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "canonical_export_operations"): "1db0569d2787ffd0e9379f42df05f75b76ba2881616308ec546d11aa7a715d80",
    ("table", "canonical_export_jobs"): "ec3c8a2a4c0c59e8c60713d5a4b68aa462034fa292316f552aa42d43ab4b17f6",
    ("table", "canonical_export_revisions"): "2019eeff09383cdacf088890dcc8cbb3724c81849d71f8a22ec42a0496dfe6f6",
    ("table", "canonical_usage_occurrences"): "5da41299688b1bfddd1a6cfe5e3333c2d034ee8d56c55b35ea4f7414e9c28b8b",
    ("table", "canonical_export_receipts"): "e53df3d3c1bf93f17e2f7f3c244dd1e8893e59f922670a96f5b3a12d12da65ad",
    ("index", "idx_canonical_export_operations_project_sequence"): "d90cb317fa9fe4b9b8c04baf17edc8c00389a30691850f3be208d52db2b97f8a",
    ("index", "idx_canonical_export_jobs_project_status_created"): "456060f3639d86c4aa1df9e9b3a42fd418daa11d1a33dc980dbbf00c57f8f6b3",
    ("index", "idx_canonical_export_revisions_timeline"): "a6d8fed90430397f42a4149c5885bf8c5a596cc6c32cde5a48f1b3c7afca5b27",
    ("index", "idx_canonical_usage_occurrences_asset"): "4c1dfbb63d6892c2f5c332810d5d1b099e498b79b00cc5204a5b5aa0ddc0d0d4",
    ("index", "idx_canonical_export_receipts_operation"): "7760f2f23e1822aee90cc54a2836c5945cb5244312d1c706fee325c7d0776a2e",
    ("trigger", "trg_canonical_export_operations_no_update"): "17bcc5c0b50010677e9d0786457d766bc98a2c910aa45c9ab7d20e871293eea8",
    ("trigger", "trg_canonical_export_operations_no_delete"): "484cd3e41fbf30ef81649bb1eaa79647088650a028fbd3062840428c7574f0ed",
    ("trigger", "trg_canonical_export_jobs_identity_immutable"): "d762b0dfae911c51e25ac300283cd9b5306023e820284b0aefba3a9dddefb172",
    ("trigger", "trg_canonical_export_jobs_no_delete"): "10842e9ae46b26d4693971dfd7715b3122a086282c4f76bef3c7f7a69823b4b3",
    ("trigger", "trg_canonical_export_jobs_terminal_immutable"): "bb99c58367672ea13bca9d6ffb798de5e0edce0dcc2e678ad27f94584fefc356",
    ("trigger", "trg_canonical_export_jobs_attestation_once"): "852b1d552468e6d13c8654eb4da4a91b50334a93f9f1a1573e913e22257a908a",
    ("trigger", "trg_canonical_export_jobs_state_transition"): "dcbff0a6f78a5f4d2f89c1264d290640089c74cd6dd3aad2cea63173dc16be69",
    ("trigger", "trg_canonical_export_jobs_commit_requires_attestation"): "34f62b4c6bfbdf35e5346977a325848e73239b9b35c1147cc24cfbf6835b4dee",
    ("trigger", "trg_canonical_export_jobs_success_requires_revision"): "20ecdd02eced95f55119ac8b61148403c8acb28a151283182706c96709b9a4ec",
    ("trigger", "trg_canonical_export_revisions_no_update"): "0ed66a08ad22051f1758ac68bcfb78a32d73ceeca09ac9c7f4645747ac9eff5b",
    ("trigger", "trg_canonical_export_revisions_no_delete"): "a5c37d06b9f0e01107f038d0f65ea422a765ee1eb1d3544ebf4472fb03fa32d1",
    ("trigger", "trg_canonical_usage_occurrences_no_update"): "39302a05ec9069c2852f16ca98c18a75600de90a973f54dd92c371177d47250c",
    ("trigger", "trg_canonical_usage_occurrences_no_delete"): "f15363ce1f23d3555901283226c7a5e2f6366548b9809c7af1d173dd50018bb9",
    ("trigger", "trg_canonical_export_receipts_no_update"): "e44b38f24ec6688b7fdaf58bbdad3c337922116431b067a9e8dd6f8c07aacb74",
    ("trigger", "trg_canonical_export_receipts_no_delete"): "e71a9bc0aad050688573b1e3cf30ab4aaf2816b67b3cf8105bb93d48835c919c",
}

_V8_PHYSICAL_SCHEMA_SHA256 = {
    **_V7_PHYSICAL_SCHEMA_SHA256,
    **_V8_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V9_PHYSICAL_SCHEMA_SHA256 = {
    **_V8_PHYSICAL_SCHEMA_SHA256,
    ("table", "canonical_timeline_operations"): "5be8ff09026a77e3129f3a8c3ffa8a6a7b3c95121509cf0264223b36f32acaf7",
    ("table", "canonical_timeline_receipts"): "dad27cdccc18afee93fe87acada3756b7e3fa6327990bcac205330bdac8ecf66",
}

_V11_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "agent_project_capabilities"): "a2ff0df16993c33644f9440ebb3739bbbbb757304f1334157ecc97b9530c7b19",
    ("table", "agent_project_command_receipts"): "9f1cceef1d28dd9f500df0be914dcf1e908b197b8f86e74ed99fcfb3e1d1a6ef",
    ("table", "agent_project_capability_events"): "83ee6244fc7f7169d1716da8731f629b287e805e40fe61cefaeb8e6c6da0462f",
    ("index", "idx_agent_capabilities_project_expiry"): "b54e5ffddcf05f46ae626cd7fadea4fc1bbb605f2a76a422ff18f0578067c312",
    ("index", "idx_agent_project_receipts_operation"): "4271fa3bd412022f3aace230cf86829119277482e72c297829bf9d93ae4dd84c",
    ("index", "idx_agent_capability_events_sequence"): "eb44cd77d21cf63d3fec9d10a1734033908b1b46d46cbce0396a4d9ab3b59681",
    ("index", "idx_agent_capability_events_native_gesture"): "37131ad4de9f1629a993299f1c48aaf4389c9ceae38dffdeb8abdcc9bd90b72e",
    ("trigger", "trg_agent_capabilities_no_update"): "7105a898dbffe91178d6cada719fc2df06d470d37c3c8cedb7f3b4af1ba2deff",
    ("trigger", "trg_agent_capabilities_no_delete"): "b36b66d3761d167226d0501bf3a33fe21c6d136f821cdefeb5301672ef408321",
    ("trigger", "trg_agent_project_receipts_no_update"): "3fca1f13d511c87433f38e4a7bd42cc26dfd3fe9d4efdc300f01a7026e2e6a4c",
    ("trigger", "trg_agent_project_receipts_no_delete"): "af2f0ac43dbe9001f7b6206d8eeea4fc122897c273ed58c36dfd0b9a0feb542a",
    ("trigger", "trg_agent_capability_events_no_update"): "a38e581faed1b0d1ff730e24974f6d18daa067d633c2c01c8d19a631c62d0dfb",
    ("trigger", "trg_agent_capability_events_no_delete"): "847f8c93591dd03667345d9cc3e3dd07ea285b9389aa9ab9abe8731a25c184b7",
}

_V11_PHYSICAL_SCHEMA_SHA256 = {
    **_V9_PHYSICAL_SCHEMA_SHA256,
    **_V11_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V12_RETIRED_SCHEMA_OBJECTS = (
    "agent_blueprint_command_receipts",
    "idx_agent_blueprint_receipts_operation",
    "trg_agent_blueprint_receipts_no_update",
    "trg_agent_blueprint_receipts_no_delete",
)

_V12_RETIRED_SCHEMA_IDENTITIES = {
    ("table", "agent_blueprint_command_receipts"),
    ("index", "idx_agent_blueprint_receipts_operation"),
    ("trigger", "trg_agent_blueprint_receipts_no_update"),
    ("trigger", "trg_agent_blueprint_receipts_no_delete"),
}

_V12_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "agent_project_command_receipts"): "c385db0f0d85f751e89f8e8d67c8de509df65f4cef934ac94c7ae59aabab7a78",
    ("table", "agent_project_capability_events"): "836eb9a6ec718a9c2bda16b36b600aaa1806bcfdc74d680738399d38d5c979d4",
    ("index", "idx_agent_project_receipts_operation"): "ff793108ff007029685f438bddeed0c6b6f6df9ae658029a89fc81c8c834fb8b",
    ("index", "idx_agent_capability_events_sequence"): "eb44cd77d21cf63d3fec9d10a1734033908b1b46d46cbce0396a4d9ab3b59681",
    ("index", "idx_agent_capability_events_native_gesture"): "37131ad4de9f1629a993299f1c48aaf4389c9ceae38dffdeb8abdcc9bd90b72e",
    ("trigger", "trg_agent_project_receipts_no_update"): "3fca1f13d511c87433f38e4a7bd42cc26dfd3fe9d4efdc300f01a7026e2e6a4c",
    ("trigger", "trg_agent_project_receipts_no_delete"): "af2f0ac43dbe9001f7b6206d8eeea4fc122897c273ed58c36dfd0b9a0feb542a",
    ("trigger", "trg_agent_capability_events_no_update"): "a38e581faed1b0d1ff730e24974f6d18daa067d633c2c01c8d19a631c62d0dfb",
    ("trigger", "trg_agent_capability_events_no_delete"): "847f8c93591dd03667345d9cc3e3dd07ea285b9389aa9ab9abe8731a25c184b7",
}

_V12_PHYSICAL_SCHEMA_SHA256 = {
    identity: digest
    for identity, digest in _V11_PHYSICAL_SCHEMA_SHA256.items()
    if identity not in _V12_RETIRED_SCHEMA_IDENTITIES
}
_V12_PHYSICAL_SCHEMA_SHA256.update(_V12_ONLY_PHYSICAL_SCHEMA_SHA256)

_V13_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "agent_project_capabilities"): "b195b774352bf4b6e955eb199da779ec807c95e279346af8d768edfd10d8f8a0",
    ("table", "canonical_timeline_operations"): "8b7139ea0809e228cf97aa17eae9a6757a881f4daa9313e651d915ddbd20915a",
    ("table", "agent_project_command_receipts"): "21d60065db41ec96627f60e8a459be9886fc93b64e512e43ca351111fe3fc524",
    ("table", "agent_project_capability_events"): "64fac55ceab44fce741bdc937ca61b2b830db62a7486ea8ed5cbb9c5be9d3136",
    ("index", "idx_agent_capabilities_project_expiry"): "b54e5ffddcf05f46ae626cd7fadea4fc1bbb605f2a76a422ff18f0578067c312",
    ("index", "idx_canonical_timeline_operations_project_sequence"): "176c2317862c5b65ef91d878d6887a3b48bd28c0401eabebb619677c30d4c810",
    ("index", "idx_agent_project_receipts_operation"): "ff793108ff007029685f438bddeed0c6b6f6df9ae658029a89fc81c8c834fb8b",
    ("index", "idx_agent_capability_events_sequence"): "eb44cd77d21cf63d3fec9d10a1734033908b1b46d46cbce0396a4d9ab3b59681",
    ("index", "idx_agent_capability_events_native_gesture"): "37131ad4de9f1629a993299f1c48aaf4389c9ceae38dffdeb8abdcc9bd90b72e",
    ("trigger", "trg_agent_capabilities_no_update"): "7105a898dbffe91178d6cada719fc2df06d470d37c3c8cedb7f3b4af1ba2deff",
    ("trigger", "trg_agent_capabilities_no_delete"): "b36b66d3761d167226d0501bf3a33fe21c6d136f821cdefeb5301672ef408321",
    ("trigger", "trg_canonical_timeline_operations_no_update"): "9641b81a645f704517e4149400be75b5845df86fa27d2a91e7a584a2398520d4",
    ("trigger", "trg_canonical_timeline_operations_no_delete"): "62f52e83b698be983d0cacf7d926d3eb217ea720c4de7b9fc87c1c11c8097567",
    ("trigger", "trg_agent_project_receipts_no_update"): "3fca1f13d511c87433f38e4a7bd42cc26dfd3fe9d4efdc300f01a7026e2e6a4c",
    ("trigger", "trg_agent_project_receipts_no_delete"): "af2f0ac43dbe9001f7b6206d8eeea4fc122897c273ed58c36dfd0b9a0feb542a",
    ("trigger", "trg_agent_capability_events_no_update"): "a38e581faed1b0d1ff730e24974f6d18daa067d633c2c01c8d19a631c62d0dfb",
    ("trigger", "trg_agent_capability_events_no_delete"): "847f8c93591dd03667345d9cc3e3dd07ea285b9389aa9ab9abe8731a25c184b7",
}

_V13_PHYSICAL_SCHEMA_SHA256 = {
    **_V12_PHYSICAL_SCHEMA_SHA256,
    **_V13_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V14_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "image_analysis_job_bindings"): "d788254a4b60ae75259f048028bf3fadfe29526344337dbed860e5085cdb8033",
    ("table", "image_analysis_attempt_authorities"): "5d394784c6a545910f9195bbf2c1bfaf41dacef2c44de6b1417d96d8f745cdc9",
    ("table", "image_analysis_attempt_states"): "f2bd5bb4e9930be0114ddc6c989e35868f6572db7c13656215174f2d48fa103c",
    ("table", "image_analysis_results"): "f5c966300a85d66492c357a01728b318dad74568beaa898ed34a3b20827f5758",
    ("table", "image_analysis_artifacts"): "52fadbf0c4f642706ce345625da4bc22a68cfdd103d368af647677759213560b",
    ("table", "image_analysis_heads"): "6c2de800f2ca90ef2487953fb0c55421cc2b46697ba6f9d0a08e40faa9185a26",
    ("table", "image_projection_changes"): "031322be4ff3900a4a255d9922ad9845a80e191d60e6c50790fa632e52a230df",
    ("table", "image_publish_receipts"): "446cd2bd62974cb86b7eeac2a886553660cfc71914be7c3a963cd228eaf6c4cc",
    ("table", "legacy_image_aliases"): "2a9fb93c95699183fd0485097ac8e4744a16b3962dc0dbd546f780d7e7547958",
    ("table", "image_projection_generations"): "4e7ae0a109d521b2626f84c09b239ec86c137ebfa7c72da4a04c5aa439a5f240",
    ("table", "image_projection_aliases"): "f7933b32d7a2d7c5e48b391ccac778ca8b16ae29a7d1a5cccf460aaa8990115c",
    ("table", "image_projection_rows"): "cf8997d5bfd3bc214808d18e3570808ee4f61a549925787e4a5666018925e57d",
    ("table", "image_projection_receipts"): "e8cb3f4832a550aafa1a8614c72a09fa0c45bb7a48e560cee9d187e6878327e1",
    ("table", "image_projection_manifests"): "59f254eb54d0da8a0edb50868164404954b75aa6dc3298a98fb9df9e72ad3033",
    ("index", "idx_image_analysis_jobs_asset_created"): "b2949a77021c2bcb26c68c9eebaaf3db8c5a0ae7019ecf5acd5433d684de3514",
    ("index", "idx_image_attempt_authorities_generation"): "28dcb407d1f913d6cb2d7b6e5a3557609e12e18da801dde90c83250cd8e34a2d",
    ("index", "idx_image_analysis_results_run"): "0458d1d3387e55f07e3cde7fd3f7cc170cca225f3bb6a741dad64d6879c8e03b",
    ("index", "idx_image_artifacts_digest"): "0472e8af82604b166161025d9dcd02ad469ea249f328e556bb8ec2c316be7447",
    ("index", "idx_image_projection_changes_asset_position"): "adbc19de143d7d146823880f2524d25111507f8f240f8346ff96b3540837afd5",
    ("index", "idx_image_projection_one_active"): "59515657d3c2de53bc518244dc82835da15d2e620d38b2c15047e1bc33c839a5",
    ("index", "idx_image_projection_rows_binding"): "2e65303aa69e4ae62bc3a26606d1b43872f200b6a36f7a77009a70760fc1de60",
    ("index", "idx_image_projection_manifest_digest"): "371460df90b7c93264d956967ed0ffd436ed98776e0dc0f8642d108af40a08a6",
    ("trigger", "trg_image_job_bindings_no_update"): "85ce129425ca1dbb448949dab5fa4d8ae55e070c3416fec1d1fafbc686ff925f",
    ("trigger", "trg_image_job_bindings_no_delete"): "3c1d8728f4ece82a6cf35bae67946b2327180553efe66406ba3d56ab59208233",
    ("trigger", "trg_image_attempt_authorities_no_update"): "a8d661c1e5c961297be0ba27823fcb41569f0b13dd2e922a883d5fb9197a77bc",
    ("trigger", "trg_image_attempt_authorities_validate_insert"): "8b4f2b8df101024b4c4e5e6c9c91dfc8d8c0b0815b5013e6b46d0afde2143925",
    ("trigger", "trg_image_attempt_authorities_no_delete"): "326efc87d09cb605ccba79ac080df488c60f64565ed83ad40b81945d4146eb8d",
    ("trigger", "trg_image_attempt_states_identity_immutable"): "447cc255065a154a20d41eb69835ae45352cb3fba697e6c2e7e029236fbd2419",
    ("trigger", "trg_image_attempt_states_transition"): "43b15d6be7b98ae60912a7cf0a854c1cb711e310ac8ad04b8a00d24601096d0c",
    ("trigger", "trg_image_attempt_states_no_delete"): "691d7f5c505e681cb564413d120de4e56ad47ef8739dc07a420956f8a04838c1",
    ("trigger", "trg_image_results_validate_insert"): "3a518cedd97e096cb82040cf62fce67d61dbb06a739f9e1dd46a61a60c6775d3",
    ("trigger", "trg_image_results_parent_validate_insert"): "be307f0f7daa3a688cabf34b7e59596cd7fcfec8e777fcac629ce960e6a33026",
    ("trigger", "trg_image_results_document_validate_insert"): "db46899d32fd6ebcac77e48e6a12660e90171dbc026b988e085768ce2e2ea5d8",
    ("trigger", "trg_image_results_no_update"): "fef6acbfe3adc8f95656aff0db8bf104eaadb7dbb52806459d80f6b404247225",
    ("trigger", "trg_image_results_no_delete"): "daad7294ebe9dde5521ce651cde68b0e5f6b4e2045b1cca698cf618266861f6b",
    ("trigger", "trg_image_artifacts_no_insert_after_publish"): "b2c8312bd1babbd5cda441f285f2e3adf0a19b5bcf889e1e54c47c74be1af7b2",
    ("trigger", "trg_image_artifacts_digest_validate_insert"): "aadafa666a8b090307617d98ceaee8dcbd4b866b844a62c40130059a871f2fd9",
    ("trigger", "trg_image_artifacts_no_update"): "012be9d2560a77482c52beee8688366d343d0999299bb1d44d23edcc3601c3a8",
    ("trigger", "trg_image_artifacts_no_delete"): "706de8eb652bba7b8769223ef9cb9c6ae31d6a178903d003343a17e5f766da57",
    ("trigger", "trg_image_heads_initial_revision"): "2781f81ac8f5d93c2847c92501908fd3a4774557ce7546ce7e579b55b79eddf0",
    ("trigger", "trg_image_heads_forward_only"): "f95f2cbb1096b78e0f540e1c9a2aca97fa37a7380332e565112301551548b602",
    ("trigger", "trg_image_heads_no_delete"): "04518b8a91d9f17c8d7eb56572a1647d52cae82ec9f6fdc23efebf3af23fece3",
    ("trigger", "trg_image_heads_mirror_insert"): "5701456aa05d3c69c05df79974ce965cd934e071660d75c860891987956d6fb3",
    ("trigger", "trg_image_heads_mirror_update"): "607c2052d09320c42877f171beef1af1cf472a627081c0e0001d1f677d3816b2",
    ("trigger", "trg_asset_heads_image_exact_insert"): "5190cc2f75565c0f258641a4eb6d579878b2b23c5b35aab5c9097f73c6ffe502",
    ("trigger", "trg_asset_heads_image_exact_update"): "f9906652e65bcabb109aa362ab71831755a7f3a68ee132af37a685ed393848e7",
    ("trigger", "trg_asset_heads_image_no_delete"): "da4b00f0d87d9e17f70c3d22d263b37e4a33e29ed20dbb3bef810a641e616f9e",
    ("trigger", "trg_image_runs_identity_immutable"): "a65bf8e79648f8919e0dce972a07d35b38435b2bd80527bb5aff230a3efe6e07",
    ("trigger", "trg_image_runs_bound_no_delete"): "1a2d6cae424bf14e05eb69e0beefc2bc578aae978d928ef6d141d47af779ca17",
    ("trigger", "trg_image_jobs_binding_insert"): "b527b63655b6e4bb040ecf33a47def81db98f271e17ac1d31fe7f961d7724d77",
    ("trigger", "trg_image_jobs_identity_immutable"): "587bc92e3046d503bb4bc9f8cc0e74f2648fde92b896d342a6ca111b12f1a1ea",
    ("trigger", "trg_image_jobs_closed_stage"): "516d523a4dabee0726c6be815af560d87527615e0870e30a7fdc02701379996b",
    ("trigger", "trg_image_jobs_terminal_immutable"): "2496c1fe970beb873ed07b5fed3134485770a69cfa7982547c5c246c251894dc",
    ("trigger", "trg_image_jobs_success_requires_receipts"): "55f2d4da01ccf2ff5099fc98b519b2d17b07f6aad75a67cc07ba6016d5c2dc9e",
    ("trigger", "trg_image_changes_no_update"): "1ef0d819014b92dd3dffbc388bed3f8c1997b09880bb3a4e3511ce4ea4d79685",
    ("trigger", "trg_image_changes_document_validate_insert"): "21bbc30a8d24bba200a991f79563512a504c8ff211cfee97b2de4bd9a727f9c0",
    ("trigger", "trg_image_changes_no_delete"): "79c2d3a10c47cbd6b6f6729056b6ec21bc7d32b07e35f19e38559612fd1441d4",
    ("trigger", "trg_image_publish_receipts_no_update"): "82cdaaedc275f75eaedf91ca1420b6651fe07645d0f8e19c3243b4de0984d619",
    ("trigger", "trg_image_publish_receipts_validate_insert"): "4d6e5f3c7f8e2a4182f7391857e2225142765010c8453e7e8c3931643cf17410",
    ("trigger", "trg_image_publish_receipts_no_delete"): "f7ccee09058e2c8115d71630f0d7f3fe270122cbfec6197c7b947eb204c86757",
    ("trigger", "trg_legacy_image_aliases_no_update"): "b5ed25aae3dde7de73aaf95b2cc3fc41d16d7282d1751095fcac81f77ad41bdf",
    ("trigger", "trg_legacy_image_aliases_no_delete"): "54c7c0740dfd8f18fb756770c278d14e6d9b3c6a3d129596745c85edb0f3a1ee",
    ("trigger", "trg_image_generations_identity_immutable"): "7107fc8b10659a72921521d80941b05de3ba5b09559b3aefdf84e6b5104084ad",
    ("trigger", "trg_image_generations_state_transition"): "82799dd3c0ef63f055794d8af5b6aaf0c995532cf16350eea25ca67f7f784ccc",
    ("trigger", "trg_image_generations_complete_requires_manifest"): "0ba2a1bb9f199d36183636b3582f5cc71dfec89797c3825f20dfd067601e5124",
    ("trigger", "trg_image_generations_activate_clean_only"): "37099fe16a981a305ee303a000985fdd7af5f53aba86a6d4da5d9d0b7759fc16",
    ("trigger", "trg_image_generations_no_delete"): "0887601597e99930b42cbe3162fdaae276c0602223b4e39fe0103f7892481b64",
    ("trigger", "trg_image_projection_aliases_building_insert"): "db2f8bcccc1054bb9e48093d9a96bc9f752fe04960fe59cef262da841322f9d9",
    ("trigger", "trg_image_projection_aliases_binding_insert"): "bae49b4db2e9d9d761239a24c556be97c8a4af941761c3921b8e53a08e17a081",
    ("trigger", "trg_image_projection_aliases_no_update"): "486c76e6a4a6cb11295ed92944d5ad8b44ddfe660646b694fe734af36645e2e6",
    ("trigger", "trg_image_projection_aliases_no_delete"): "abd7f6e7e74f3acaabf286f69ed825e38c5e2d93761d1c80f2afd5f116d0dc62",
    ("trigger", "trg_image_projection_aliases_snapshot_generation"): "769285ab80e7f07614fe4efcbbd09c8a765fa55a3ff272749675ccd8ea8d39d6",
    ("trigger", "trg_image_projection_rows_building_insert"): "4fcffce57b67e9f73e67101792715dc5e002e4bd39826d74585b736a37941c85",
    ("trigger", "trg_image_projection_rows_building_update"): "1a6e897e94110d6a0378c3395b5373c03bfd1cd78eaabe50486f4f7c8e0769a6",
    ("trigger", "trg_image_projection_rows_building_delete"): "af6cf30cfaca2dbcfd18958b407a098eb17fe0b026a02291f1f57bc7f45896e9",
    ("trigger", "trg_image_projection_rows_document_validate_insert"): "51028fb66d46ea4607d74fca1d6879b0a63a8e1f4e382345b11b4da96b30b6d0",
    ("trigger", "trg_image_projection_rows_document_validate_update"): "e8c550ec9ce0b451a93b332c9adbb8204be2ae402c5518ea7854bc6ec80060d6",
    ("trigger", "trg_image_projection_receipts_validate_insert"): "2f88632fb6a4f8374d3a30c1a1b57b9f2367cceeda6d88d40795b1288ff1e4e9",
    ("trigger", "trg_image_projection_receipts_no_update"): "a4d9220e9f86d75018e67339c38941a66be9673f6c4d20d397537e7174053985",
    ("trigger", "trg_image_projection_receipts_no_delete"): "3c91c338f44f0cff940b4eefa1e2c11fea82666fd078160741aad55902ea88cc",
    ("trigger", "trg_image_manifests_validate_insert"): "61643d1cd868b60b74a780e82d2d2ac2e1621eb69bb7e40057ee7c3be8145ffe",
    ("trigger", "trg_image_manifests_no_update"): "676b6f84b440286d61262d1d383268c533529f29de74745c884f20d5ce1a0756",
    ("trigger", "trg_image_manifests_no_delete"): "ce325e41f9174f22a838ce1b0c26e67e5ef19a0753e3d7013dd47f99356776c7",
}

_V14_PHYSICAL_SCHEMA_SHA256 = {
    **_V13_PHYSICAL_SCHEMA_SHA256,
    **_V14_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V15_PHYSICAL_SCHEMA_SHA256 = {
    **_V14_PHYSICAL_SCHEMA_SHA256,
    ("table", "agent_project_capabilities"): "10ad6d999b9ad849e21d67bab710fb4cef083a37b2e79b8ff53a530c605f8c61",
}

_V16_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "provider_egress_grants"): "325899cc57b74c52c8ce0bad112116a8b218b5e79c257c3824bcb9c9df322e14",
    ("table", "provider_egress_manifests"): "f84f2a439663d0dcf9296e2cfb6ef3b56e9df9095a5bdc48ce67e8934bcaf25d",
    ("index", "idx_provider_egress_grants_status_expiry"): "9ba1d392023e8c80f2e8051de6d85cca8ec05bf8e14c6d86c256656abebfcd16",
    ("index", "idx_provider_egress_manifests_status_created"): "224417e8a0c992ea42aca1acdf70ff47a8ccf78a7a93b829cc3ec3141d94fafb",
    ("index", "idx_provider_egress_manifests_grant"): "d36418a88946cfa4f91c71e6a97ea6f5e1b6599f8ee5390bedfe4225a41bbbce",
    ("trigger", "trg_provider_egress_grants_identity_immutable"): "26d1660cbd713b80e381f9d0cb15a8db04b548e4aa26d28028508a49b0a5278d",
    ("trigger", "trg_provider_egress_grants_state_transition"): "9d42d14c40207fe955c9134aeac61e3c56d7b6512b6e354ee11a43805a585855",
    ("trigger", "trg_provider_egress_grants_no_delete"): "43a6965b9486cf361844a96821ea3e57b35e7088d312194f940d8c5bc1578ee4",
    ("trigger", "trg_provider_egress_manifests_insert_binding"): "ac8db83238edaa6d83cec84e61590ad8b0082b020028884af19a55f30547e122",
    ("trigger", "trg_provider_egress_manifests_identity_immutable"): "2af9fd1f85633dda936704b707a4113eb615c2b96f380bc558c1fa839cb96e79",
    ("trigger", "trg_provider_egress_manifests_state_transition"): "33e1cbc1569dbece47b361e54ae68406c754bb1c89b2aa80d0d18e478ad5df59",
    ("trigger", "trg_provider_egress_manifests_no_delete"): "f1f27c6fce2b053d78561d372dd00b118a74528487207db502a7585fe144386d",
}

_V16_PHYSICAL_SCHEMA_SHA256 = {
    **_V15_PHYSICAL_SCHEMA_SHA256,
    **_V16_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V17_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "image_projection_read_manifests"): "565b57b2043a2a9537262517656e5b5a286c025a6ec831fec9f61f79425d6267",
    ("index", "idx_image_projection_read_manifest_digest"): "312d7d9140249f43ba218f5a68fed13acb5d23aba8cc9ee1c4ee71b6d07f60d4",
    ("trigger", "trg_image_projection_read_manifests_validate_insert"): "8cb8213a86454e0d941ab9d7744cb521d23842b7c0044bdbc32fb65596a16c7f",
    ("trigger", "trg_image_projection_read_manifests_no_update"): "8fd59286e9df8122ffd32fe0c90fa49c7ac2b0512e7c750cbe03e36addaad5f0",
    ("trigger", "trg_image_projection_read_manifests_no_delete"): "8be1ef17913a46607e6f6f1c266ad5e3609928ccdfcbb2f387b74863444578f0",
    ("trigger", "trg_image_generations_activate_clean_only"): "39a40bb31ebbb9a438f194dd58b43000195a080f04480518292173043fbc00a0",
}

_V17_PHYSICAL_SCHEMA_SHA256 = {
    **_V16_PHYSICAL_SCHEMA_SHA256,
    **_V17_ONLY_PHYSICAL_SCHEMA_SHA256,
}

# V18 rebuilds exactly these five tables.  All indexes, immutability triggers,
# image-read cutover objects, and provider-egress objects retain their frozen
# V17 bytes.  Keep this as a full manifest override instead of accepting any
# table that merely exposes the expected columns.
_V18_REBUILT_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "agent_project_capabilities"): "02f0420e06c492e41175691c26c0e5706ca4814ee0213cf6bace8df1482426c2",
    ("table", "canonical_timeline_operations"): "12f9fe134efd814732ccf2763ccc679ceccaa7c52d4fbc8e069f1db678638c8c",
    ("table", "canonical_timeline_revisions"): "40871f7655ea136e5676562ffba43e2e0345428db9bf8d924cac471c1a97a2ea",
    ("table", "agent_project_command_receipts"): "8fdaaee66329ec97a7b00e944da4ed2929b0032e354e15ad792185c0608f6a34",
    ("table", "agent_project_capability_events"): "d6d70ec186ec73f4a314a5a6a4f41ecbb9d83f98c01c137d447e6c4d989f6613",
}

_V18_PHYSICAL_SCHEMA_SHA256 = {
    **_V17_PHYSICAL_SCHEMA_SHA256,
    **_V18_REBUILT_PHYSICAL_SCHEMA_SHA256,
}

_V19_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("trigger", "trg_output_roots_identity_immutable"): "95deebd8459fb66d7ea8ce38add927dfaf59afa267b00081f43c358cf82684e0",
    ("trigger", "trg_user_export_output_roots_no_delete"): "8a1e04fea61a16755204d1dd5318e4144d74b6dbb8b5a15b50b19bb3cb59cf77",
}

_V19_PHYSICAL_SCHEMA_SHA256 = {
    **_V18_PHYSICAL_SCHEMA_SHA256,
    **_V19_ONLY_PHYSICAL_SCHEMA_SHA256,
}

_V20_ONLY_PHYSICAL_SCHEMA_SHA256 = {
    ("table", "library_bootstrap_receipts"): "722e4722703f00c5d68cae36bc0057f42b3d63b540bc52456a198349a6a5716b",
    ("index", "idx_library_bootstrap_receipts_root"): "fa7054929bdb7303e18623cd6c7d7d40c7e2fc7fd9c75cd9b44cb7f1b3df3525",
    ("index", "idx_library_bootstrap_receipts_project"): "160eb01815e7d54109391059bce2e79a2d646c31d3e305266193277de2ef4561",
    ("trigger", "trg_library_bootstrap_receipts_complete"): "359864b6a1448646cd4d9b9671d8bf61c2ca55a822d92cbed994a09f33d0a6bd",
    ("trigger", "trg_library_bootstrap_receipts_no_update"): "2c4f1cf99505459b6f0f848350bce4eb1c4457e76465180d1e8362cfbbe66877",
    ("trigger", "trg_library_bootstrap_receipts_no_delete"): "e11de053fda958274e238af1adb3976d41063e36c34a68c5c00fb677bb97cce0",
    ("trigger", "trg_library_bootstrap_roots_identity_immutable"): "3acca7600e2234ee2b9ed0c13104d1bfcef1495aefe55a27c6348735f0648e8e",
    ("trigger", "trg_library_bootstrap_jobs_identity_immutable"): "2d0b065c052e78bec43fedea5222db277fa77b6090eb5ac5251d49b38be10722",
    ("trigger", "trg_library_bootstrap_briefs_no_update"): "1e78728ae91332711141721aec366f66de62de5f1c27c7b4c9a0ad7faeda3143",
    ("trigger", "trg_library_bootstrap_briefs_no_delete"): "abd9f3a946431d8c30a9aea28c4599c8ee2cbdfb0e1894057168896aa757851e",
}

_V20_PHYSICAL_SCHEMA_SHA256 = {
    **_V19_PHYSICAL_SCHEMA_SHA256,
    **_V20_ONLY_PHYSICAL_SCHEMA_SHA256,
}


@dataclass
class ValidatedAuthorityLedger:
    """Validated event stream plus indexes for bounded historical projection."""

    events: tuple[dict[str, Any], ...]
    unit_event_revisions: dict[str, tuple[int, ...]]
    unit_event_values: dict[str, tuple[dict[str, Any] | None, ...]]
    projection_cache: dict[int, dict[str, Any]] = field(default_factory=dict)


def _corrupt() -> MemoLensError:
    return MemoLensError(
        "Persisted Creative Blueprint decision authority failed integrity validation.",
        code="blueprint_authority_integrity_error",
    )


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _valid_revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= 1_000_000


def _valid_timestamp(value: object) -> bool:
    if not (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and _UTC_TIMESTAMP.fullmatch(value) is not None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (OverflowError, ValueError):
        return False
    return parsed.utcoffset() == timedelta(0) and parsed.isoformat() == value


def _strict_json(raw: object, expected_type: type[Any]) -> Any:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _JSON_LIMITS.max_bytes:
        raise _corrupt()
    try:
        value = decode_strict_json(
            raw.encode("utf-8"),
            require_object=expected_type is dict,
            limits=_JSON_LIMITS,
        )
    except (UnicodeError, StrictJsonError) as exc:
        raise _corrupt() from exc
    if not isinstance(value, expected_type) or canonical_json(value) != raw:
        raise _corrupt()
    return value


def _preflight_stored_json_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    where_clause: str,
    parameters: tuple[object, ...],
    column_max_bytes: Mapping[str, int],
    max_rows: int,
    aggregate_max_bytes: int,
) -> int:
    """Bound row count and JSON bytes before materializing attacker-owned text."""

    columns = tuple(column_max_bytes)
    expressions = ",".join(
        expression
        for column in columns
        for expression in (
            f"typeof({column})",
            f"length(CAST({column} AS BLOB))",
        )
    )
    observed = 0
    aggregate = 0
    try:
        rows = connection.execute(
            f"SELECT {expressions} FROM {table} WHERE {where_clause}",
            parameters,
        )
        for raw in rows:
            observed += 1
            if observed > max_rows:
                raise _corrupt()
            for column_index, offset in enumerate(range(0, len(raw), 2)):
                storage_type = raw[offset]
                byte_length = raw[offset + 1]
                column = columns[column_index]
                if not (
                    storage_type == "text"
                    and type(byte_length) is int
                    and 0 <= byte_length <= column_max_bytes[column]
                ):
                    raise _corrupt()
                aggregate += byte_length
                if aggregate > aggregate_max_bytes:
                    raise _corrupt()
    except MemoLensError:
        raise
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    return observed


def _decision_payloads(semantic: Mapping[str, Any]) -> dict[str, Any]:
    intent = semantic["intent"]
    return {
        "intent_goal": {
            "goal": intent["goal"],
            "audience": intent["audience"],
            "platform": intent["platform"],
        },
        "intent_stance": {"stance": intent["stance"]},
        "script": semantic["script"],
        "creative_direction": semantic["direction"],
        "output": semantic["output"],
        "material_constraints": {
            "constraints": semantic["constraints"],
            "material_hints": semantic["material_hints"],
        },
        "references": {
            "reference_refs": semantic["reference_refs"],
            "bindings": semantic["bindings"],
        },
        "techniques": semantic["technique_refs"],
    }


def _revision_material(
    revision_chain: Mapping[int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    revision: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    bundle = revision_chain.get(revision)
    if bundle is None:
        raise _corrupt()
    return _revision_material_from_bundle(bundle, revision=revision)


def _revision_material_from_bundle(
    bundle: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    *,
    revision: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    row, document, _operation = bundle
    semantic = document.get("semantic")
    persisted_units = document.get("authority", {}).get("decision_units")
    if (
        row.get("revision") != revision
        or not isinstance(semantic, dict)
        or not isinstance(persisted_units, dict)
    ):
        raise _corrupt()
    try:
        payloads = _decision_payloads(semantic)
    except (KeyError, TypeError) as exc:
        raise _corrupt() from exc
    digests = {name: canonical_sha256(payloads[name]) for name in DECISION_UNITS}
    if set(persisted_units) != set(DECISION_UNITS) or any(
        not isinstance(persisted_units[name], dict)
        or persisted_units[name].get("semantic_sha256") != digests[name]
        or persisted_units[name].get("claim") != "agent_proposal"
        or persisted_units[name].get("verified") is not False
        for name in DECISION_UNITS
    ):
        raise _corrupt()
    return row, payloads, digests


def _revision_material_stream(
    revision_chain: Mapping[
        int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
    ],
    through_revision: int,
) -> Iterator[tuple[int, tuple[dict[str, Any], dict[str, Any], dict[str, str]]]]:
    """Yield revision material sequentially without requiring per-key SQL reads."""

    stream_factory = getattr(revision_chain, "iter_bundles", None)
    if not callable(stream_factory):
        for revision in range(1, through_revision + 1):
            yield revision, _revision_material(revision_chain, revision)
        return
    observed = 0
    for expected_revision, item in enumerate(stream_factory(), start=1):
        try:
            revision, bundle = item
        except (TypeError, ValueError) as exc:
            raise _corrupt() from exc
        observed += 1
        if revision != expected_revision or revision > through_revision:
            raise _corrupt()
        yield revision, _revision_material_from_bundle(
            bundle,
            revision=revision,
        )
    if observed != through_revision:
        raise _corrupt()


def _database_identity(connection: sqlite3.Connection) -> tuple[str, int]:
    try:
        rows = connection.execute(
            "SELECT database_uuid,schema_version FROM database_meta LIMIT 2"
        ).fetchall()
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


def _normalized_schema_sql(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _validate_absent_schema_objects(
    connection: sqlite3.Connection,
    names: tuple[str, ...],
) -> None:
    placeholders = ",".join("?" for _name in names)
    try:
        rows = connection.execute(
            f"SELECT name FROM main.sqlite_schema WHERE name IN ({placeholders}) LIMIT 1",
            names,
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if rows:
        raise _corrupt()


def _validate_exact_image_schema_namespace(
    connection: sqlite3.Connection,
    *,
    expected: set[tuple[str, str]],
) -> None:
    """Reject missing or unknown objects inside the managed image namespace."""

    prefixes = (
        "image_analysis_",
        "image_projection_",
        "image_publish_",
        "legacy_image_",
        "idx_image_analysis_",
        "idx_image_attempt_",
        "idx_image_artifacts_",
        "idx_image_projection_",
        "trg_image_",
        "trg_asset_heads_image_",
        "trg_legacy_image_",
    )
    try:
        rows = connection.execute(
            "SELECT type,name FROM main.sqlite_schema "
            "WHERE type IN ('table','index','trigger','view')"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    observed = {
        (str(row["type"]), str(row["name"]))
        for row in rows
        if str(row["name"]).startswith(prefixes)
    }
    if observed != expected:
        raise _corrupt()


def _validate_exact_provider_egress_schema_namespace(
    connection: sqlite3.Connection,
    *,
    expected: set[tuple[str, str]],
) -> None:
    """Reject missing or unknown objects inside the V16 provider namespace."""

    prefixes = (
        "provider_egress_",
        "idx_provider_egress_",
        "trg_provider_egress_",
    )
    try:
        rows = connection.execute(
            "SELECT type,name FROM main.sqlite_schema "
            "WHERE type IN ('table','index','trigger','view')"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    observed = {
        (str(row["type"]), str(row["name"]))
        for row in rows
        if str(row["name"]).startswith(prefixes)
    }
    if observed != expected:
        raise _corrupt()


def _validate_exact_library_bootstrap_schema_namespace(
    connection: sqlite3.Connection,
    *,
    expected: set[tuple[str, str]],
) -> None:
    """Reject missing or unknown objects inside the V20 bootstrap namespace."""

    prefixes = (
        "library_bootstrap_",
        "idx_library_bootstrap_",
        "trg_library_bootstrap_",
    )
    try:
        rows = connection.execute(
            "SELECT type,name FROM main.sqlite_schema "
            "WHERE type IN ('table','index','trigger','view')"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    observed = {
        (str(row["type"]), str(row["name"]))
        for row in rows
        if str(row["name"]).startswith(prefixes)
    }
    if observed != expected:
        raise _corrupt()


def _validate_exact_schema_manifest(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
    migrations: tuple[tuple[int, str, str], ...],
    physical_schema_sha256: Mapping[tuple[str, str], str],
) -> None:
    try:
        meta_rows = connection.execute(
            "SELECT singleton,database_uuid,schema_version,created_at,updated_at "
            "FROM database_meta LIMIT 2"
        ).fetchall()
        migration_rows = connection.execute(
            "SELECT version,name,checksum,applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
        schema_rows: dict[str, sqlite3.Row] = {}
        for _object_type, name in physical_schema_sha256:
            rows = connection.execute(
                "SELECT type,name,sql FROM sqlite_schema WHERE name=? LIMIT 2",
                (name,),
            ).fetchall()
            if len(rows) != 1:
                raise _corrupt()
            schema_rows[name] = rows[0]
    except MemoLensError:
        raise
    except sqlite3.Error as exc:
        raise _corrupt() from exc

    if not (
        len(meta_rows) == 1
        and meta_rows[0]["singleton"] == 1
        and _valid_identifier(meta_rows[0]["database_uuid"])
        and meta_rows[0]["schema_version"] == schema_version
        and _valid_timestamp(meta_rows[0]["created_at"])
        and _valid_timestamp(meta_rows[0]["updated_at"])
        and len(migration_rows) == len(migrations)
    ):
        raise _corrupt()
    for row, expected in zip(migration_rows, migrations, strict=True):
        version, name, checksum = expected
        if not (
            type(row["version"]) is int
            and row["version"] == version
            and row["name"] == name
            and row["checksum"] == checksum
            and _valid_timestamp(row["applied_at"])
        ):
            raise _corrupt()
    for (object_type, name), expected_sha256 in physical_schema_sha256.items():
        row = schema_rows[name]
        normalized = _normalized_schema_sql(row["sql"])
        if not (
            row["type"] == object_type
            and row["name"] == name
            and normalized
            and hashlib.sha256(normalized.encode()).hexdigest() == expected_sha256
        ):
            raise _corrupt()


def validate_exact_v5_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the frozen V5 migration history and V4/V5 physical schema."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=5,
        migrations=_V5_MIGRATIONS,
        physical_schema_sha256=_V5_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v6_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the frozen V6 chain plus every V4/V5/V6 protected object."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=6,
        migrations=_V6_MIGRATIONS,
        physical_schema_sha256=_V6_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v7_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the frozen V7 chain plus every V4 through V7 protected object."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=7,
        migrations=_V7_MIGRATIONS,
        physical_schema_sha256=_V7_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v8_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the frozen V8 chain plus every V4 through V8 protected object."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=8,
        migrations=_V8_MIGRATIONS,
        physical_schema_sha256=_V8_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v9_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the frozen V9 chain and exact generalized Timeline envelopes."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=9,
        migrations=_V9_MIGRATIONS,
        physical_schema_sha256=_V9_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v10_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V10 migration chain with unchanged V9 physical schema."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=10,
        migrations=_V10_MIGRATIONS,
        physical_schema_sha256=_V9_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v11_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V11 chain and exact resource-neutral paired authority schema."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=11,
        migrations=_V11_MIGRATIONS,
        physical_schema_sha256=_V11_PHYSICAL_SCHEMA_SHA256,
    )


def validate_exact_v12_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V12 chain, typed union, and absence of retired V1 objects."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=12,
        migrations=_V12_MIGRATIONS,
        physical_schema_sha256=_V12_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)


def validate_exact_v13_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V13 chain and exact closed Timeline restore authority."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=13,
        migrations=_V13_MIGRATIONS,
        physical_schema_sha256=_V13_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)


def validate_exact_v14_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V14 chain and exact canonical image bridge authority."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=14,
        migrations=_V14_MIGRATIONS,
        physical_schema_sha256=_V14_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected=set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
    )


def validate_exact_v15_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V15 chain and exact read-only Timeline preview capability."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=15,
        migrations=_V15_MIGRATIONS,
        physical_schema_sha256=_V15_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected=set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
    )
    _validate_exact_provider_egress_schema_namespace(connection, expected=set())


def validate_exact_v16_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V16 chain and exact one-shot provider-egress authority."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=16,
        migrations=_V16_MIGRATIONS,
        physical_schema_sha256=_V16_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected=set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
    )
    _validate_exact_provider_egress_schema_namespace(
        connection,
        expected=set(_V16_ONLY_PHYSICAL_SCHEMA_SHA256),
    )


def validate_exact_v17_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V17 chain and exact canonical-image read-cutover gate."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=17,
        migrations=_V17_MIGRATIONS,
        physical_schema_sha256=_V17_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected={
            *set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
            *set(_V17_ONLY_PHYSICAL_SCHEMA_SHA256),
        },
    )
    _validate_exact_provider_egress_schema_namespace(
        connection,
        expected=set(_V16_ONLY_PHYSICAL_SCHEMA_SHA256),
    )


def validate_exact_v18_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V18 chain and exact structural-edit authority rebuild."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=18,
        migrations=_V18_MIGRATIONS,
        physical_schema_sha256=_V18_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected={
            *set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
            *set(_V17_ONLY_PHYSICAL_SCHEMA_SHA256),
        },
    )
    _validate_exact_provider_egress_schema_namespace(
        connection,
        expected=set(_V16_ONLY_PHYSICAL_SCHEMA_SHA256),
    )


def validate_exact_v19_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V19 chain and immutable canonical Export root anchors."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=19,
        migrations=_V19_MIGRATIONS,
        physical_schema_sha256=_V19_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected={
            *set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
            *set(_V17_ONLY_PHYSICAL_SCHEMA_SHA256),
        },
    )
    _validate_exact_provider_egress_schema_namespace(
        connection,
        expected=set(_V16_ONLY_PHYSICAL_SCHEMA_SHA256),
    )
    _validate_exact_library_bootstrap_schema_namespace(connection, expected=set())


def validate_exact_v20_schema_manifest(connection: sqlite3.Connection) -> None:
    """Require the V20 chain and exact plugin-first bootstrap authority."""

    _validate_exact_schema_manifest(
        connection,
        schema_version=20,
        migrations=_V20_MIGRATIONS,
        physical_schema_sha256=_V20_PHYSICAL_SCHEMA_SHA256,
    )
    _validate_absent_schema_objects(connection, _V12_RETIRED_SCHEMA_OBJECTS)
    _validate_exact_image_schema_namespace(
        connection,
        expected={
            *set(_V14_ONLY_PHYSICAL_SCHEMA_SHA256),
            *set(_V17_ONLY_PHYSICAL_SCHEMA_SHA256),
        },
    )
    _validate_exact_provider_egress_schema_namespace(
        connection,
        expected=set(_V16_ONLY_PHYSICAL_SCHEMA_SHA256),
    )
    _validate_exact_library_bootstrap_schema_namespace(
        connection,
        expected=set(_V20_ONLY_PHYSICAL_SCHEMA_SHA256),
    )


def validate_exact_managed_schema_manifest(connection: sqlite3.Connection) -> None:
    """Validate the exact schema produced by the current supported Core."""

    validate_exact_v20_schema_manifest(connection)


def validate_v11_agent_project_receipt_census(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Close the current V11 generic Timeline receipt/event/operation lane.

    The generic table is resource-neutral at the DDL level for forward
    migration, but the current writer admits only
    ``canonical_timeline``/``timeline.apply_edit``. Historic Blueprint
    receipts intentionally remain in their frozen V1 table and are not part of
    this census until the separately specified migration is implemented.
    """

    validate_exact_v11_schema_manifest(connection)
    try:
        broken = connection.execute(
            """SELECT 1 FROM (
                   SELECT receipt.id
                     FROM main.agent_project_command_receipts receipt
                     LEFT JOIN main.agent_project_capabilities capability
                       ON capability.id=receipt.capability_id
                      AND capability.project_id=receipt.project_id
                     LEFT JOIN main.canonical_timeline_operations operation
                       ON operation.project_id=receipt.project_id
                      AND operation.id=receipt.operation_id
                    WHERE receipt.authenticated_principal!='paired_agent'
                       OR receipt.resource_type!='canonical_timeline'
                       OR receipt.command_type!='timeline.apply_edit'
                       OR receipt.command_version!='1'
                       OR capability.id IS NULL
                       OR operation.id IS NULL
                       OR operation.command_type!='timeline.apply_edit'
                       OR operation.command_version!='1'
                       OR operation.request_sha256!=receipt.request_sha256
                       OR EXISTS (
                           SELECT 1 FROM main.canonical_timeline_receipts desktop
                            WHERE desktop.project_id=receipt.project_id
                              AND desktop.operation_id=receipt.operation_id)
                       OR (SELECT COUNT(*)
                             FROM main.agent_project_capability_events event
                            WHERE event.agent_project_command_receipt_id=receipt.id
                              AND event.event_schema_version='2'
                              AND event.event_type='used'
                              AND event.agent_command_receipt_id IS NULL
                              AND event.capability_id=receipt.capability_id
                              AND event.project_id=receipt.project_id
                              AND event.action=receipt.command_type
                              AND event.operation_id=receipt.operation_id
                              AND event.created_at=receipt.created_at) != 1
                   UNION ALL
                   SELECT event.id
                     FROM main.agent_project_capability_events event
                    WHERE (event.event_schema_version='2'
                           OR event.agent_project_command_receipt_id IS NOT NULL)
                      AND (event.event_schema_version!='2'
                           OR event.event_type!='used'
                           OR event.action!='timeline.apply_edit'
                           OR event.agent_command_receipt_id IS NOT NULL
                           OR event.agent_project_command_receipt_id IS NULL
                           OR event.reason_code IS NOT NULL
                           OR event.presentation_sha256 IS NOT NULL
                           OR event.native_gesture_nonce IS NOT NULL
                           OR (SELECT COUNT(*)
                                 FROM main.agent_project_command_receipts receipt
                                WHERE receipt.id=event.agent_project_command_receipt_id
                                  AND receipt.capability_id=event.capability_id
                                  AND receipt.project_id=event.project_id
                                  AND receipt.command_type=event.action
                                  AND receipt.operation_id=event.operation_id
                                  AND receipt.created_at=event.created_at) != 1
                           OR (SELECT COUNT(*)
                                 FROM main.canonical_timeline_operations operation
                                WHERE operation.project_id=event.project_id
                                  AND operation.id=event.operation_id
                                  AND operation.command_type=event.action
                                  AND operation.command_version='1') != 1)
                   UNION ALL
                   SELECT operation.id
                     FROM main.canonical_timeline_operations operation
                    WHERE operation.command_type='timeline.apply_edit'
                      AND NOT EXISTS (
                          SELECT 1 FROM main.canonical_timeline_receipts desktop
                           WHERE desktop.project_id=operation.project_id
                             AND desktop.operation_id=operation.id)
                      AND ((SELECT COUNT(*)
                              FROM main.agent_project_command_receipts receipt
                             WHERE receipt.project_id=operation.project_id
                               AND receipt.operation_id=operation.id
                               AND receipt.resource_type='canonical_timeline'
                               AND receipt.command_type='timeline.apply_edit') != 1
                           OR (SELECT COUNT(*)
                                 FROM main.agent_project_capability_events event
                                WHERE event.project_id=operation.project_id
                                  AND event.operation_id=operation.id
                                  AND event.event_schema_version='2'
                                  AND event.event_type='used'
                                  AND event.action='timeline.apply_edit') != 1)
               ) LIMIT 1"""
        ).fetchone()
        project_rows = connection.execute(
            """SELECT DISTINCT project_id
                 FROM main.agent_project_command_receipts
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_project_capability_events
                WHERE event_schema_version='2'
                   OR agent_project_command_receipt_id IS NOT NULL
                ORDER BY project_id"""
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    projects = tuple(row[0] for row in project_rows)
    if broken is not None or any(not _valid_identifier(project) for project in projects):
        raise _corrupt()
    return projects


def validate_v12_agent_project_receipt_census(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Close every branch of the V12 versioned paired-command receipt union."""

    validate_exact_v12_schema_manifest(connection)
    try:
        broken = connection.execute(
            """SELECT 1 FROM (
                   SELECT receipt.id
                     FROM main.agent_project_command_receipts receipt
                     LEFT JOIN main.agent_project_capabilities capability
                       ON capability.id=receipt.capability_id
                      AND capability.project_id=receipt.project_id
                     LEFT JOIN main.creative_blueprint_operations blueprint_operation
                       ON blueprint_operation.project_id=receipt.project_id
                      AND blueprint_operation.id=receipt.operation_id
                     LEFT JOIN main.canonical_timeline_operations timeline_operation
                       ON timeline_operation.project_id=receipt.project_id
                      AND timeline_operation.id=receipt.operation_id
                    WHERE capability.id IS NULL
                       OR receipt.receipt_schema_version IS NULL
                       OR receipt.receipt_schema_version NOT IN ('1','2')
                       OR (receipt.receipt_schema_version='1'
                           AND (receipt.request_capture IS NOT 'digest_only_legacy'
                                OR receipt.resource_type IS NOT 'creative_blueprint'
                                OR receipt.command_type IS NULL
                                OR receipt.command_type NOT IN (
                                    'blueprint.commit_proposal',
                                    'blueprint.restore_revision')
                                OR receipt.command_version IS NOT '1'
                                OR receipt.authenticated_principal IS NOT NULL
                                OR receipt.claimed_client_label IS NOT NULL
                                OR receipt.request_json IS NOT NULL
                                OR receipt.actor_json IS NOT NULL
                                OR receipt.origin_json IS NOT NULL
                                OR blueprint_operation.id IS NULL
                                OR blueprint_operation.command_type!=receipt.command_type
                                OR blueprint_operation.command_version!='1'
                                OR blueprint_operation.input_sha256!=receipt.request_sha256
                                OR EXISTS (
                                    SELECT 1
                                      FROM main.blueprint_command_receipts desktop
                                     WHERE desktop.project_id=receipt.project_id
                                       AND desktop.operation_id=receipt.operation_id)
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_capability_events event
                                     WHERE event.event_schema_version='1'
                                       AND event.event_type='used'
                                       AND event.agent_command_receipt_id=receipt.id
                                       AND event.agent_project_command_receipt_id IS NULL
                                       AND event.capability_id=receipt.capability_id
                                       AND event.project_id=receipt.project_id
                                       AND event.action=receipt.command_type
                                       AND event.operation_id=receipt.operation_id
                                       AND event.created_at=receipt.created_at) != 1))
                       OR (receipt.receipt_schema_version='2'
                           AND (receipt.request_capture IS NOT 'exact_canonical_json'
                                OR receipt.resource_type IS NOT 'canonical_timeline'
                                OR receipt.command_type IS NOT 'timeline.apply_edit'
                                OR receipt.command_version IS NOT '1'
                                OR receipt.authenticated_principal IS NOT 'paired_agent'
                                OR receipt.claimed_client_label IS NULL
                                OR receipt.request_json IS NULL
                                OR receipt.actor_json IS NULL
                                OR receipt.origin_json IS NULL
                                OR timeline_operation.id IS NULL
                                OR timeline_operation.command_type!='timeline.apply_edit'
                                OR timeline_operation.command_version!='1'
                                OR timeline_operation.request_sha256!=receipt.request_sha256
                                OR EXISTS (
                                    SELECT 1
                                      FROM main.canonical_timeline_receipts desktop
                                     WHERE desktop.project_id=receipt.project_id
                                       AND desktop.operation_id=receipt.operation_id)
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_capability_events event
                                     WHERE event.event_schema_version='2'
                                       AND event.event_type='used'
                                       AND event.agent_command_receipt_id IS NULL
                                       AND event.agent_project_command_receipt_id=receipt.id
                                       AND event.capability_id=receipt.capability_id
                                       AND event.project_id=receipt.project_id
                                       AND event.action=receipt.command_type
                                       AND event.operation_id=receipt.operation_id
                                       AND event.created_at=receipt.created_at) != 1))
                   UNION ALL
                   SELECT event.id
                     FROM main.agent_project_capability_events event
                    WHERE event.event_schema_version IS NULL
                       OR event.event_schema_version NOT IN ('1','2')
                       OR (event.event_type='used'
                           AND event.event_schema_version='1'
                           AND (event.agent_command_receipt_id IS NULL
                                OR event.agent_project_command_receipt_id IS NOT NULL
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_command_receipts receipt
                                     WHERE receipt.id=event.agent_command_receipt_id
                                       AND receipt.receipt_schema_version='1'
                                       AND receipt.request_capture='digest_only_legacy'
                                       AND receipt.capability_id=event.capability_id
                                       AND receipt.project_id=event.project_id
                                       AND receipt.command_type=event.action
                                       AND receipt.operation_id=event.operation_id
                                       AND receipt.created_at=event.created_at) != 1))
                       OR (event.event_type='used'
                           AND event.event_schema_version='2'
                           AND (event.agent_command_receipt_id IS NOT NULL
                                OR event.agent_project_command_receipt_id IS NULL
                                OR event.action IS NOT 'timeline.apply_edit'
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_command_receipts receipt
                                     WHERE receipt.id=event.agent_project_command_receipt_id
                                       AND receipt.receipt_schema_version='2'
                                       AND receipt.request_capture='exact_canonical_json'
                                       AND receipt.capability_id=event.capability_id
                                       AND receipt.project_id=event.project_id
                                       AND receipt.command_type=event.action
                                       AND receipt.operation_id=event.operation_id
                                       AND receipt.created_at=event.created_at) != 1))
                       OR (event.event_type!='used'
                           AND (event.agent_command_receipt_id IS NOT NULL
                                OR event.agent_project_command_receipt_id IS NOT NULL))
                   UNION ALL
                   SELECT operation.id
                     FROM main.creative_blueprint_operations operation
                    WHERE NOT EXISTS (
                              SELECT 1
                                FROM main.blueprint_command_receipts desktop
                               WHERE desktop.project_id=operation.project_id
                                 AND desktop.operation_id=operation.id)
                      AND ((SELECT COUNT(*)
                              FROM main.agent_project_command_receipts receipt
                             WHERE receipt.project_id=operation.project_id
                               AND receipt.operation_id=operation.id
                               AND receipt.receipt_schema_version='1'
                               AND receipt.request_capture='digest_only_legacy'
                               AND receipt.resource_type='creative_blueprint'
                               AND receipt.command_type=operation.command_type) != 1
                           OR (SELECT COUNT(*)
                                 FROM main.agent_project_capability_events event
                                WHERE event.project_id=operation.project_id
                                  AND event.operation_id=operation.id
                                  AND event.event_schema_version='1'
                                  AND event.event_type='used'
                                  AND event.action=operation.command_type) != 1)
                   UNION ALL
                   SELECT operation.id
                     FROM main.canonical_timeline_operations operation
                    WHERE operation.command_type='timeline.apply_edit'
                      AND NOT EXISTS (
                              SELECT 1
                                FROM main.canonical_timeline_receipts desktop
                               WHERE desktop.project_id=operation.project_id
                                 AND desktop.operation_id=operation.id)
                      AND ((SELECT COUNT(*)
                              FROM main.agent_project_command_receipts receipt
                             WHERE receipt.project_id=operation.project_id
                               AND receipt.operation_id=operation.id
                               AND receipt.receipt_schema_version='2'
                               AND receipt.request_capture='exact_canonical_json'
                               AND receipt.resource_type='canonical_timeline'
                               AND receipt.command_type='timeline.apply_edit') != 1
                           OR (SELECT COUNT(*)
                                 FROM main.agent_project_capability_events event
                                WHERE event.project_id=operation.project_id
                                  AND event.operation_id=operation.id
                                  AND event.event_schema_version='2'
                                  AND event.event_type='used'
                                  AND event.action='timeline.apply_edit') != 1)
               ) LIMIT 1"""
        ).fetchone()
        project_rows = connection.execute(
            """SELECT DISTINCT project_id
                 FROM main.agent_project_capabilities
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_pairing_confirmation_receipts
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_project_command_receipts
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_project_capability_events
                UNION
               SELECT DISTINCT project_id
                 FROM main.creative_blueprint_operations
                UNION
               SELECT DISTINCT project_id
                 FROM main.canonical_timeline_operations
                WHERE command_type='timeline.apply_edit'
                ORDER BY project_id"""
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    projects = tuple(row[0] for row in project_rows)
    if broken is not None or any(not _valid_identifier(project) for project in projects):
        raise _corrupt()
    return projects


def validate_v13_agent_project_receipt_census(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Close the frozen V13/V14 or current typed write-receipt union."""

    _database_uuid, schema_version = _database_identity(connection)
    if schema_version == 13:
        validate_exact_v13_schema_manifest(connection)
    elif schema_version == 14:
        validate_exact_v14_schema_manifest(connection)
    elif schema_version == CURRENT_MANAGED_SCHEMA_VERSION:
        validate_exact_managed_schema_manifest(connection)
    else:
        raise _corrupt()
    timeline_commands = (
        "timeline.apply_edit",
        "timeline.restore_revision",
    )
    if schema_version >= 18:
        timeline_commands = (
            "timeline.apply_edit",
            "timeline.apply_structural_edit",
            "timeline.restore_revision",
        )
    timeline_commands_sql = ",".join(f"'{command}'" for command in timeline_commands)
    try:
        broken = connection.execute(
            f"""SELECT 1 FROM (
                   SELECT receipt.id
                     FROM main.agent_project_command_receipts receipt
                     LEFT JOIN main.agent_project_capabilities capability
                       ON capability.id=receipt.capability_id
                      AND capability.project_id=receipt.project_id
                     LEFT JOIN main.creative_blueprint_operations blueprint_operation
                       ON blueprint_operation.project_id=receipt.project_id
                      AND blueprint_operation.id=receipt.operation_id
                     LEFT JOIN main.canonical_timeline_operations timeline_operation
                       ON timeline_operation.project_id=receipt.project_id
                      AND timeline_operation.id=receipt.operation_id
                    WHERE capability.id IS NULL
                       OR receipt.receipt_schema_version IS NULL
                       OR receipt.receipt_schema_version NOT IN ('1','2')
                       OR (receipt.receipt_schema_version='1'
                           AND (receipt.request_capture IS NOT 'digest_only_legacy'
                                OR receipt.resource_type IS NOT 'creative_blueprint'
                                OR receipt.command_type IS NULL
                                OR receipt.command_type NOT IN (
                                    'blueprint.commit_proposal',
                                    'blueprint.restore_revision')
                                OR receipt.command_version IS NOT '1'
                                OR receipt.authenticated_principal IS NOT NULL
                                OR receipt.claimed_client_label IS NOT NULL
                                OR receipt.request_json IS NOT NULL
                                OR receipt.actor_json IS NOT NULL
                                OR receipt.origin_json IS NOT NULL
                                OR blueprint_operation.id IS NULL
                                OR blueprint_operation.command_type!=receipt.command_type
                                OR blueprint_operation.command_version!='1'
                                OR blueprint_operation.input_sha256!=receipt.request_sha256
                                OR EXISTS (
                                    SELECT 1
                                      FROM main.blueprint_command_receipts desktop
                                     WHERE desktop.project_id=receipt.project_id
                                       AND desktop.operation_id=receipt.operation_id)
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_capability_events event
                                     WHERE event.event_schema_version='1'
                                       AND event.event_type='used'
                                       AND event.agent_command_receipt_id=receipt.id
                                       AND event.agent_project_command_receipt_id IS NULL
                                       AND event.capability_id=receipt.capability_id
                                       AND event.project_id=receipt.project_id
                                       AND event.action=receipt.command_type
                                       AND event.operation_id=receipt.operation_id
                                       AND event.created_at=receipt.created_at) != 1))
                       OR (receipt.receipt_schema_version='2'
                           AND (receipt.request_capture IS NOT 'exact_canonical_json'
                                OR receipt.resource_type IS NOT 'canonical_timeline'
                                OR receipt.command_type IS NULL
                                OR receipt.command_type NOT IN (
                                    {timeline_commands_sql})
                                OR receipt.command_version IS NOT '1'
                                OR receipt.authenticated_principal IS NOT 'paired_agent'
                                OR receipt.claimed_client_label IS NULL
                                OR receipt.request_json IS NULL
                                OR receipt.actor_json IS NULL
                                OR receipt.origin_json IS NULL
                                OR timeline_operation.id IS NULL
                                OR timeline_operation.command_type!=receipt.command_type
                                OR timeline_operation.command_version!='1'
                                OR timeline_operation.request_sha256!=receipt.request_sha256
                                OR EXISTS (
                                    SELECT 1
                                      FROM main.canonical_timeline_receipts desktop
                                     WHERE desktop.project_id=receipt.project_id
                                       AND desktop.operation_id=receipt.operation_id)
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_capability_events event
                                     WHERE event.event_schema_version='2'
                                       AND event.event_type='used'
                                       AND event.agent_command_receipt_id IS NULL
                                       AND event.agent_project_command_receipt_id=receipt.id
                                       AND event.capability_id=receipt.capability_id
                                       AND event.project_id=receipt.project_id
                                       AND event.action=receipt.command_type
                                       AND event.operation_id=receipt.operation_id
                                       AND event.created_at=receipt.created_at) != 1))
                   UNION ALL
                   SELECT event.id
                     FROM main.agent_project_capability_events event
                    WHERE event.event_schema_version IS NULL
                       OR event.event_schema_version NOT IN ('1','2')
                       OR (event.event_type='used'
                           AND event.event_schema_version='1'
                           AND (event.agent_command_receipt_id IS NULL
                                OR event.agent_project_command_receipt_id IS NOT NULL
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_command_receipts receipt
                                     WHERE receipt.id=event.agent_command_receipt_id
                                       AND receipt.receipt_schema_version='1'
                                       AND receipt.request_capture='digest_only_legacy'
                                       AND receipt.capability_id=event.capability_id
                                       AND receipt.project_id=event.project_id
                                       AND receipt.command_type=event.action
                                       AND receipt.operation_id=event.operation_id
                                       AND receipt.created_at=event.created_at) != 1))
                       OR (event.event_type='used'
                           AND event.event_schema_version='2'
                           AND (event.agent_command_receipt_id IS NOT NULL
                                OR event.agent_project_command_receipt_id IS NULL
                                OR event.action IS NULL
                                OR event.action NOT IN (
                                    {timeline_commands_sql})
                                OR (SELECT COUNT(*)
                                      FROM main.agent_project_command_receipts receipt
                                     WHERE receipt.id=event.agent_project_command_receipt_id
                                       AND receipt.receipt_schema_version='2'
                                       AND receipt.request_capture='exact_canonical_json'
                                       AND receipt.capability_id=event.capability_id
                                       AND receipt.project_id=event.project_id
                                       AND receipt.command_type=event.action
                                       AND receipt.operation_id=event.operation_id
                                       AND receipt.created_at=event.created_at) != 1))
                       OR (event.event_type!='used'
                           AND (event.agent_command_receipt_id IS NOT NULL
                                OR event.agent_project_command_receipt_id IS NOT NULL))
                   UNION ALL
                   SELECT operation.id
                     FROM main.creative_blueprint_operations operation
                    WHERE NOT EXISTS (
                              SELECT 1
                                FROM main.blueprint_command_receipts desktop
                               WHERE desktop.project_id=operation.project_id
                                 AND desktop.operation_id=operation.id)
                      AND ((SELECT COUNT(*)
                              FROM main.agent_project_command_receipts receipt
                             WHERE receipt.project_id=operation.project_id
                               AND receipt.operation_id=operation.id
                               AND receipt.receipt_schema_version='1'
                               AND receipt.request_capture='digest_only_legacy'
                               AND receipt.resource_type='creative_blueprint'
                               AND receipt.command_type=operation.command_type) != 1
                           OR (SELECT COUNT(*)
                                 FROM main.agent_project_capability_events event
                                WHERE event.project_id=operation.project_id
                                  AND event.operation_id=operation.id
                                  AND event.event_schema_version='1'
                                  AND event.event_type='used'
                                  AND event.action=operation.command_type) != 1)
                   UNION ALL
                   SELECT operation.id
                     FROM main.canonical_timeline_operations operation
                    WHERE operation.command_type IN (
                              {timeline_commands_sql})
                      AND NOT EXISTS (
                              SELECT 1
                                FROM main.canonical_timeline_receipts desktop
                               WHERE desktop.project_id=operation.project_id
                                 AND desktop.operation_id=operation.id)
                      AND ((SELECT COUNT(*)
                              FROM main.agent_project_command_receipts receipt
                             WHERE receipt.project_id=operation.project_id
                               AND receipt.operation_id=operation.id
                               AND receipt.receipt_schema_version='2'
                               AND receipt.request_capture='exact_canonical_json'
                               AND receipt.resource_type='canonical_timeline'
                               AND receipt.command_type=operation.command_type) != 1
                           OR (SELECT COUNT(*)
                                 FROM main.agent_project_capability_events event
                                WHERE event.project_id=operation.project_id
                                  AND event.operation_id=operation.id
                                  AND event.event_schema_version='2'
                                  AND event.event_type='used'
                                  AND event.action=operation.command_type) != 1)
               ) LIMIT 1"""
        ).fetchone()
        project_rows = connection.execute(
            f"""SELECT DISTINCT project_id
                 FROM main.agent_project_capabilities
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_pairing_confirmation_receipts
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_project_command_receipts
                UNION
               SELECT DISTINCT project_id
                 FROM main.agent_project_capability_events
                UNION
               SELECT DISTINCT project_id
                 FROM main.creative_blueprint_operations
                UNION
               SELECT DISTINCT project_id
                 FROM main.canonical_timeline_operations
                WHERE command_type IN (
                    {timeline_commands_sql})
                ORDER BY project_id"""
        ).fetchall()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    projects = tuple(row[0] for row in project_rows)
    if broken is not None or any(not _valid_identifier(project) for project in projects):
        raise _corrupt()
    return projects


def authority_schema_available(
    connection: sqlite3.Connection,
    observed_columns: Mapping[str, set[str]],
) -> bool:
    """Validate the all-or-none authority set against the exact managed schema."""

    present = {name for name, columns in observed_columns.items() if columns}
    if not present:
        try:
            meta_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(database_meta)"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            raise _corrupt() from exc
        if not {"database_uuid", "schema_version"} <= meta_columns:
            return False
        _database_uuid, schema_version = _database_identity(connection)
        if schema_version >= 5:
            raise _corrupt()
        return False
    if present != set(AUTHORITY_RELATIONS):
        raise _corrupt()
    for name, required in AUTHORITY_RELATIONS.items():
        if not required <= observed_columns[name]:
            raise _corrupt()
    _database_uuid, schema_version = _database_identity(connection)
    if schema_version != CURRENT_MANAGED_SCHEMA_VERSION:
        raise _corrupt()
    validate_exact_managed_schema_manifest(connection)
    try:
        receipt_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(blueprint_command_receipts)"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if not DESKTOP_COMMAND_RECEIPT_COLUMNS <= receipt_columns:
        raise _corrupt()
    return True


def _normalize_units(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(type(item) is not str for item in value):
        raise _corrupt()
    normalized = tuple(value)
    expected = tuple(name for name in DECISION_UNITS if name in normalized)
    if normalized != expected or len(normalized) != len(set(normalized)):
        raise _corrupt()
    return normalized


def _event_material(
    *,
    event_id: str,
    database_uuid: str,
    project_id: str,
    sequence: int,
    parent_event_id: str | None,
    parent_event_sha256: str | None,
    authority_operation: str,
    observed_head: Mapping[str, object],
    decision_units: tuple[str, ...],
    unit_digests: Mapping[str, str],
    active_confirmation_ids: Mapping[str, str],
    presentation_sha256: str,
    runtime_authority_epoch: str,
    native_gesture_nonce: str,
    request_sha256: str,
    created_at: str,
) -> dict[str, object]:
    return {
        "id": event_id,
        "database_uuid": database_uuid,
        "project_id": project_id,
        "sequence": sequence,
        "parent_event_id": parent_event_id,
        "parent_event_sha256": parent_event_sha256,
        "authority_operation": authority_operation,
        "observed_head": dict(observed_head),
        "decision_units": list(decision_units),
        "unit_digests": dict(unit_digests),
        "active_confirmation_ids": dict(active_confirmation_ids),
        "presentation_sha256": presentation_sha256,
        "runtime_authority_epoch": runtime_authority_epoch,
        "native_gesture_nonce": native_gesture_nonce,
        "request_sha256": request_sha256,
        "created_at": created_at,
    }


def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
    units = _normalize_units(_strict_json(row["decision_units_json"], list))
    unit_digests = _strict_json(row["unit_digests_json"], dict)
    active_ids = _strict_json(row["active_confirmation_ids_json"], dict)
    presentation = _strict_json(row["presentation_json"], dict)
    if (
        set(unit_digests) != set(units)
        or any(not _valid_digest(value) for value in unit_digests.values())
        or any(key not in units or not _valid_identifier(value) for key, value in active_ids.items())
    ):
        raise _corrupt()
    observed = {
        "project_id": row["project_id"],
        "revision": row["observed_revision"],
        "content_sha256": row["observed_content_sha256"],
        "semantic_sha256": row["observed_semantic_sha256"],
        "operation_id": row["observed_operation_id"],
    }
    parent_id = row["parent_event_id"]
    parent_digest = row["parent_event_sha256"]
    material = _event_material(
        event_id=row["id"],
        database_uuid=row["database_uuid"],
        project_id=row["project_id"],
        sequence=row["sequence"],
        parent_event_id=parent_id,
        parent_event_sha256=parent_digest,
        authority_operation=row["authority_operation"],
        observed_head=observed,
        decision_units=units,
        unit_digests=unit_digests,
        active_confirmation_ids=active_ids,
        presentation_sha256=row["presentation_sha256"],
        runtime_authority_epoch=row["runtime_authority_epoch"],
        native_gesture_nonce=row["native_gesture_nonce"],
        request_sha256=row["request_sha256"],
        created_at=row["created_at"],
    )
    if not (
        _valid_identifier(row["id"])
        and _valid_identifier(row["database_uuid"])
        and _valid_identifier(row["project_id"])
        and _valid_revision(row["sequence"])
        and row["authority_operation"] in {"confirm", "revoke"}
        and _valid_revision(row["observed_revision"])
        and _valid_digest(row["observed_content_sha256"])
        and _valid_digest(row["observed_semantic_sha256"])
        and _valid_identifier(row["observed_operation_id"])
        and _valid_identifier(row["runtime_authority_epoch"])
        and _valid_identifier(row["native_gesture_nonce"])
        and _valid_digest(row["presentation_sha256"])
        and _valid_digest(row["request_sha256"])
        and _valid_digest(row["event_sha256"])
        and _valid_timestamp(row["created_at"])
        and (
            (row["sequence"] == 1 and parent_id is None and parent_digest is None)
            or (
                row["sequence"] > 1
                and _valid_identifier(parent_id)
                and _valid_digest(parent_digest)
            )
        )
        and canonical_sha256(presentation) == row["presentation_sha256"]
        and canonical_sha256(material) == row["event_sha256"]
    ):
        raise _corrupt()
    return {**material, "event_sha256": row["event_sha256"], "presentation": presentation}


def _empty_active() -> dict[str, dict[str, Any] | None]:
    return {name: None for name in DECISION_UNITS}


def _projection_from_active(
    *,
    revision_chain: Mapping[int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    selected_revision: int,
    active: Mapping[str, dict[str, Any] | None],
    selected_material: tuple[dict[str, Any], dict[str, str]] | None = None,
) -> dict[str, Any]:
    if selected_material is None:
        selected, _payloads, selected_digests = _revision_material(
            revision_chain, selected_revision
        )
    else:
        selected, selected_digests = selected_material
    confirmed = sum(value is not None for value in active.values())
    state = (
        "confirmed"
        if confirmed == len(DECISION_UNITS)
        else "partially_confirmed"
        if confirmed
        else "unverified"
    )
    return {
        "state": state,
        "confirmed_decision_unit_count": confirmed,
        "decision_unit_count": len(DECISION_UNITS),
        "as_of_revision": selected_revision,
        "as_of_content_sha256": selected["content_sha256"],
        "decision_units": {
            unit: {
                "semantic_sha256": selected_digests[unit],
                "state": "confirmed" if active[unit] is not None else "unverified",
                "confirmed": active[unit] is not None,
                "active_confirmation": active[unit],
            }
            for unit in DECISION_UNITS
        },
    }


def _build_validated_ledger(
    *,
    events: list[dict[str, Any]],
    unit_event_revisions: Mapping[str, list[int]] | None = None,
    unit_event_values: Mapping[str, list[dict[str, Any] | None]] | None = None,
) -> ValidatedAuthorityLedger:
    unit_revisions = unit_event_revisions or {
        unit: [] for unit in DECISION_UNITS
    }
    unit_values = unit_event_values or {unit: [] for unit in DECISION_UNITS}
    if set(unit_revisions) != set(DECISION_UNITS) or set(unit_values) != set(
        DECISION_UNITS
    ):
        raise _corrupt()
    return ValidatedAuthorityLedger(
        events=tuple(events),
        unit_event_revisions={
            unit: tuple(unit_revisions[unit]) for unit in DECISION_UNITS
        },
        unit_event_values={
            unit: tuple(unit_values[unit]) for unit in DECISION_UNITS
        },
    )


def _project_validated_ledger(
    *,
    revision_chain: Mapping[
        int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
    ],
    ledger: ValidatedAuthorityLedger,
    selected_revision: int,
) -> dict[str, Any]:
    if selected_revision not in revision_chain:
        raise _corrupt()
    cached = ledger.projection_cache.get(selected_revision)
    if cached is not None:
        return deepcopy(cached)
    active = _empty_active()
    for unit in DECISION_UNITS:
        event_revisions = ledger.unit_event_revisions[unit]
        index = bisect_right(event_revisions, selected_revision) - 1
        window = ledger.unit_event_values[unit][index] if index >= 0 else None
        if window is None:
            continue
        invalidated_at = window["invalidated_at_revision"]
        if invalidated_at is None or invalidated_at > selected_revision:
            active[unit] = window["confirmation"]
    projection = _projection_from_active(
        revision_chain=revision_chain,
        selected_revision=selected_revision,
        active=active,
    )
    if len(ledger.projection_cache) >= _MAX_AUTHORITY_PROJECTION_CACHE:
        del ledger.projection_cache[next(iter(ledger.projection_cache))]
    ledger.projection_cache[selected_revision] = projection
    return deepcopy(projection)


def _command_response(event: Mapping[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "creative_blueprint.authority_command_result",
        "schema_version": "1",
        "project_id": event["project_id"],
        "command_type": f"blueprint.authority.{event['authority_operation']}",
        "event_id": event["id"],
        "creation_authority": {"state": "unverified", "verified": False},
        "authority_projection": projection,
    }


def _authority_receipt_material(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "database_uuid": receipt["database_uuid"],
        "authenticated_principal": receipt["authenticated_principal"],
        "project_id": receipt["project_id"],
        "command_type": receipt["command_type"],
        "command_version": receipt["command_version"],
        "idempotency_key": receipt["idempotency_key"],
        "request_sha256": receipt["request_sha256"],
        "event_id": receipt["event_id"],
        "response_status": receipt["response_status"],
        "response_sha256": receipt["response_sha256"],
        "resource_type": receipt["resource_type"],
        "resource_id": receipt["resource_id"],
        "created_at": receipt["created_at"],
    }


def _validate_authority_receipt(
    receipt: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    projection: Mapping[str, Any],
    database_uuid: str,
    project_id: str,
) -> None:
    raw_response = receipt["response_json"]
    if (
        not isinstance(raw_response, str)
        or len(raw_response.encode("utf-8")) > _MAX_RECEIPT_RESPONSE_BYTES
    ):
        raise _corrupt()
    response = _strict_json(raw_response, dict)
    if not (
        receipt["database_uuid"] == database_uuid == event["database_uuid"]
        and receipt["authenticated_principal"] == "main_native_user_gesture"
        and receipt["project_id"] == project_id
        and receipt["command_type"]
        == f"blueprint.authority.{event['authority_operation']}"
        and receipt["command_version"] == "1"
        and isinstance(receipt["idempotency_key"], str)
        and 1 <= len(receipt["idempotency_key"]) <= 256
        and receipt["request_sha256"] == event["request_sha256"]
        and receipt["response_status"] == 201
        and receipt["resource_type"] == "blueprint_decision_authority"
        and receipt["resource_id"] == f"{project_id}:{event['id']}"
        and receipt["created_at"] == event["created_at"]
        and response == _command_response(event, projection)
        and _valid_digest(receipt["response_sha256"])
        and _valid_digest(receipt["receipt_sha256"])
        and hashlib.sha256(raw_response.encode()).hexdigest()
        == receipt["response_sha256"]
        and canonical_sha256(_authority_receipt_material(receipt))
        == receipt["receipt_sha256"]
    ):
        raise _corrupt()


def validate_authority_ledger(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    current_head: Mapping[str, Any],
    revision_chain: Mapping[int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    authority_available: bool,
) -> ValidatedAuthorityLedger:
    """Validate the entire authority ledger once for one snapshot/project."""

    if not authority_available:
        return _build_validated_ledger(events=[])
    database_uuid, schema_version = _database_identity(connection)
    if schema_version != CURRENT_MANAGED_SCHEMA_VERSION:
        raise _corrupt()
    event_count = _preflight_stored_json_rows(
        connection,
        table="blueprint_decision_authority_events",
        where_clause="project_id=?",
        parameters=(project_id,),
        column_max_bytes=_AUTHORITY_EVENT_JSON_COLUMN_LIMITS,
        max_rows=_MAX_AUTHORITY_EVENTS,
        aggregate_max_bytes=_MAX_AUTHORITY_EVENT_JSON_BYTES,
    )
    receipt_count = _preflight_stored_json_rows(
        connection,
        table="blueprint_decision_authority_receipts",
        where_clause="project_id=?",
        parameters=(project_id,),
        column_max_bytes={"response_json": _MAX_RECEIPT_RESPONSE_BYTES},
        max_rows=_MAX_AUTHORITY_EVENTS,
        aggregate_max_bytes=_MAX_AUTHORITY_RECEIPT_JSON_BYTES,
    )
    if receipt_count != event_count:
        raise _corrupt()
    receipt_columns = tuple(sorted(AUTHORITY_RECEIPT_COLUMNS))
    receipt_projection = ",".join(
        f"receipt.{column} AS authority_receipt_{column}"
        for column in receipt_columns
    )
    try:
        rows = connection.execute(
            f"""SELECT event.*,{receipt_projection}
                  FROM blueprint_decision_authority_events event
                  JOIN blueprint_decision_authority_receipts receipt
                    ON receipt.project_id=event.project_id
                   AND receipt.event_id=event.id
                 WHERE event.project_id=? ORDER BY event.sequence""",
            (project_id,),
        )
        head = connection.execute(
            "SELECT * FROM blueprint_decision_authority_heads WHERE project_id=?",
            (project_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise _corrupt() from exc
    if event_count == 0:
        if head is not None:
            raise _corrupt()
        return _build_validated_ledger(events=[])

    current_revision = current_head.get("revision")
    if not (
        _valid_revision(current_revision)
        and revision_chain
        and len(revision_chain) == current_revision
        and current_revision in revision_chain
    ):
        raise _corrupt()
    current_revision = int(current_revision)
    events: list[dict[str, Any]] = []
    active = _empty_active()
    active_windows: dict[str, dict[str, Any] | None] = {
        unit: None for unit in DECISION_UNITS
    }
    unit_event_revisions: dict[str, list[int]] = {
        unit: [] for unit in DECISION_UNITS
    }
    unit_event_values: dict[str, list[dict[str, Any] | None]] = {
        unit: [] for unit in DECISION_UNITS
    }
    revision_materials = iter(
        _revision_material_stream(revision_chain, current_revision)
    )
    try:
        stream_revision, initial_material = next(revision_materials)
    except StopIteration as exc:
        raise _corrupt() from exc
    if stream_revision != 1:
        raise _corrupt()
    stream_row, stream_payloads, stream_digests = initial_material

    def advance_revision_stream(target_revision: int) -> None:
        nonlocal stream_revision, stream_row, stream_payloads, stream_digests
        while stream_revision < target_revision:
            expected_revision = stream_revision + 1
            try:
                next_revision, next_material = next(revision_materials)
            except StopIteration as exc:
                raise _corrupt() from exc
            if next_revision != expected_revision:
                raise _corrupt()
            next_row, next_payloads, next_digests = next_material
            for unit in DECISION_UNITS:
                if stream_digests[unit] == next_digests[unit]:
                    continue
                window = active_windows[unit]
                if window is not None:
                    window["invalidated_at_revision"] = next_revision
                active_windows[unit] = None
                active[unit] = None
            stream_revision = next_revision
            stream_row = next_row
            stream_payloads = next_payloads
            stream_digests = next_digests

    prior_revision = 0
    prior_event: dict[str, Any] | None = None
    observed_events = 0
    for expected_sequence, raw in enumerate(rows, start=1):
        observed_events += 1
        if observed_events > event_count:
            raise _corrupt()
        event = _decode_event(raw)
        observed = event["observed_head"]
        observed_revision = observed["revision"]
        if not (
            event["database_uuid"] == database_uuid
            and event["project_id"] == project_id
            and event["sequence"] == expected_sequence
            and event["parent_event_id"]
            == (prior_event["id"] if prior_event is not None else None)
            and event["parent_event_sha256"]
            == (prior_event["event_sha256"] if prior_event is not None else None)
            and prior_revision <= observed_revision <= current_revision
        ):
            raise _corrupt()
        advance_revision_stream(observed_revision)
        if observed != {
            "project_id": project_id,
            "revision": observed_revision,
            "content_sha256": stream_row["content_sha256"],
            "semantic_sha256": stream_row["semantic_sha256"],
            "operation_id": stream_row["operation_id"],
        }:
            raise _corrupt()
        units = event["decision_units"]
        if any(
            event["unit_digests"].get(unit) != stream_digests[unit]
            for unit in units
        ):
            raise _corrupt()
        expected_presentation = {
            "object": "memolens.blueprint_decision_authority.presentation",
            "schema_version": "1",
            "database_uuid": database_uuid,
            "runtime_authority_epoch": event["runtime_authority_epoch"],
            "project_id": project_id,
            "authority_operation": event["authority_operation"],
            "observed_head": observed,
            "decision_units": [
                {
                    "name": unit,
                    "semantic_sha256": stream_digests[unit],
                    "content": stream_payloads[unit],
                }
                for unit in units
            ],
            "active_confirmation_ids": {
                unit: active[unit]["event_id"] if active[unit] is not None else None
                for unit in units
            },
        }
        if event["presentation"] != expected_presentation:
            raise _corrupt()
        if event["authority_operation"] == "confirm":
            if event["active_confirmation_ids"]:
                raise _corrupt()
            for unit in units:
                confirmation = {
                    "event_id": event["id"],
                    "confirmed_at": event["created_at"],
                    "observed_revision": observed_revision,
                    "presentation_sha256": event["presentation_sha256"],
                }
                window = {
                    "confirmation": confirmation,
                    "invalidated_at_revision": None,
                }
                active[unit] = confirmation
                active_windows[unit] = window
                unit_event_revisions[unit].append(observed_revision)
                unit_event_values[unit].append(window)
        else:
            expected_refs = {
                unit: active[unit]["event_id"] if active[unit] is not None else None
                for unit in units
            }
            if any(value is None for value in expected_refs.values()) or event["active_confirmation_ids"] != expected_refs:
                raise _corrupt()
            for unit in units:
                active[unit] = None
                active_windows[unit] = None
                unit_event_revisions[unit].append(observed_revision)
                unit_event_values[unit].append(None)
        projection_after = _projection_from_active(
            revision_chain=revision_chain,
            selected_revision=observed_revision,
            active=active,
            selected_material=(stream_row, stream_digests),
        )
        receipt = {
            column: raw[f"authority_receipt_{column}"]
            for column in receipt_columns
        }
        _validate_authority_receipt(
            receipt,
            event=event,
            projection=projection_after,
            database_uuid=database_uuid,
            project_id=project_id,
        )
        compact_event = {
            "id": event["id"],
            "sequence": event["sequence"],
            "event_sha256": event["event_sha256"],
            "authority_operation": event["authority_operation"],
            "observed_revision": observed_revision,
        }
        events.append(compact_event)
        prior_event = compact_event
        prior_revision = observed_revision

    # Finish the one-pass merge through the current Blueprint head so a
    # confirmation's first invalidating semantic change is frozen without
    # retaining a revision-by-unit digest graph.
    advance_revision_stream(current_revision)
    if next(revision_materials, None) is not None:
        raise _corrupt()

    if not (
        observed_events == event_count
        and head is not None
        and head["project_id"] == project_id
        and head["sequence"] == len(events)
        and head["event_id"] == events[-1]["id"]
        and head["event_sha256"] == events[-1]["event_sha256"]
        and _valid_timestamp(head["updated_at"])
    ):
        raise _corrupt()

    return _build_validated_ledger(
        events=events,
        unit_event_revisions=unit_event_revisions,
        unit_event_values=unit_event_values,
    )


def project_validated_authority(
    *,
    revision_chain: Mapping[
        int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
    ],
    validated_events: ValidatedAuthorityLedger,
    selected_revision: int,
) -> dict[str, Any]:
    """Project one exact revision from an already validated snapshot ledger."""

    return _project_validated_ledger(
        revision_chain=revision_chain,
        ledger=validated_events,
        selected_revision=selected_revision,
    )


def validate_and_project_authority(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    current_head: Mapping[str, Any],
    revision_chain: Mapping[
        int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
    ],
    selected_revision: int,
    authority_available: bool,
) -> dict[str, Any]:
    """Validate the entire authority ledger and project one exact revision."""

    events = validate_authority_ledger(
        connection,
        project_id=project_id,
        current_head=current_head,
        revision_chain=revision_chain,
        authority_available=authority_available,
    )
    return project_validated_authority(
        revision_chain=revision_chain,
        validated_events=events,
        selected_revision=selected_revision,
    )


__all__ = [
    "AUTHORITY_RELATIONS",
    "CURRENT_MANAGED_SCHEMA_VERSION",
    "DECISION_UNITS",
    "ValidatedAuthorityLedger",
    "authority_schema_available",
    "project_validated_authority",
    "validate_authority_ledger",
    "validate_and_project_authority",
    "validate_exact_managed_schema_manifest",
    "validate_exact_v5_schema_manifest",
    "validate_exact_v6_schema_manifest",
    "validate_exact_v7_schema_manifest",
    "validate_exact_v8_schema_manifest",
    "validate_exact_v9_schema_manifest",
    "validate_exact_v10_schema_manifest",
    "validate_exact_v11_schema_manifest",
    "validate_exact_v12_schema_manifest",
    "validate_exact_v13_schema_manifest",
    "validate_exact_v14_schema_manifest",
    "validate_exact_v15_schema_manifest",
    "validate_exact_v16_schema_manifest",
    "validate_exact_v17_schema_manifest",
    "validate_exact_v18_schema_manifest",
    "validate_exact_v19_schema_manifest",
    "validate_exact_v20_schema_manifest",
    "validate_v11_agent_project_receipt_census",
    "validate_v12_agent_project_receipt_census",
    "validate_v13_agent_project_receipt_census",
]
