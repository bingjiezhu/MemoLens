"""V16 durable provider-egress consent and send-accounting schema."""

from __future__ import annotations

import hashlib


V16_SCHEMA_STATEMENTS = (
    """CREATE TABLE provider_egress_grants (
        id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 200),
        database_uuid TEXT NOT NULL
          REFERENCES database_meta(database_uuid) ON DELETE RESTRICT,
        grant_intent_json TEXT NOT NULL
          CHECK(length(CAST(grant_intent_json AS BLOB)) BETWEEN 2 AND 65536),
        grant_intent_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(grant_intent_sha256)=64 AND
          grant_intent_sha256 NOT GLOB '*[^0-9a-f]*'),
        token_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(token_sha256)=64 AND token_sha256 NOT GLOB '*[^0-9a-f]*'),
        status TEXT NOT NULL CHECK(status IN
          ('active','reserved','consumed','ambiguous','expired','revoked')),
        reserved_manifest_id TEXT,
        single_use INTEGER NOT NULL CHECK(single_use=1),
        issued_at TEXT NOT NULL CHECK(length(issued_at) BETWEEN 20 AND 64),
        expires_at TEXT NOT NULL CHECK(length(expires_at) BETWEEN 20 AND 64),
        reserved_at TEXT,
        consumed_at TEXT,
        terminal_at TEXT,
        CHECK(
          (status='active' AND reserved_manifest_id IS NULL AND
             reserved_at IS NULL AND consumed_at IS NULL AND terminal_at IS NULL) OR
          (status='reserved' AND reserved_manifest_id IS NOT NULL AND
             reserved_at IS NOT NULL AND consumed_at IS NULL AND terminal_at IS NULL) OR
          (status='consumed' AND reserved_manifest_id IS NOT NULL AND
             reserved_at IS NOT NULL AND consumed_at IS NOT NULL) OR
          (status='ambiguous' AND reserved_manifest_id IS NOT NULL AND
             reserved_at IS NOT NULL AND consumed_at IS NOT NULL AND terminal_at IS NOT NULL) OR
          (status IN ('expired','revoked') AND terminal_at IS NOT NULL)))""",
    """CREATE TABLE provider_egress_manifests (
        id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 200),
        grant_id TEXT NOT NULL
          REFERENCES provider_egress_grants(id) ON DELETE RESTRICT,
        manifest_plan_json TEXT NOT NULL
          CHECK(length(CAST(manifest_plan_json AS BLOB)) BETWEEN 2 AND 262144),
        manifest_plan_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(manifest_plan_sha256)=64 AND
          manifest_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
        status TEXT NOT NULL CHECK(status IN
          ('planned','sending','sent','failed_before_send',
           'failed_after_send','ambiguous','cancelled')),
        permit_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(permit_sha256)=64 AND permit_sha256 NOT GLOB '*[^0-9a-f]*'),
        bytes_sent INTEGER CHECK(bytes_sent IS NULL OR bytes_sent>=0),
        failure_code TEXT CHECK(
          failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 120),
        created_at TEXT NOT NULL CHECK(length(created_at) BETWEEN 20 AND 64),
        reserved_at TEXT NOT NULL CHECK(length(reserved_at) BETWEEN 20 AND 64),
        send_started_at TEXT,
        completed_at TEXT,
        CHECK(
          (status='planned' AND bytes_sent IS NULL AND failure_code IS NULL AND
             send_started_at IS NULL AND completed_at IS NULL) OR
          (status='sending' AND bytes_sent IS NULL AND failure_code IS NULL AND
             send_started_at IS NOT NULL AND completed_at IS NULL) OR
          (status='sent' AND bytes_sent IS NOT NULL AND failure_code IS NULL AND
             send_started_at IS NOT NULL AND completed_at IS NOT NULL) OR
          (status IN ('failed_after_send','ambiguous') AND failure_code IS NOT NULL AND
             send_started_at IS NOT NULL AND completed_at IS NOT NULL) OR
          (status IN ('failed_before_send','cancelled') AND bytes_sent IS NULL AND
             failure_code IS NOT NULL AND send_started_at IS NULL AND completed_at IS NOT NULL)))""",
    """CREATE INDEX idx_provider_egress_grants_status_expiry
       ON provider_egress_grants(status,expires_at,id)""",
    """CREATE INDEX idx_provider_egress_manifests_status_created
       ON provider_egress_manifests(status,created_at,id)""",
    """CREATE INDEX idx_provider_egress_manifests_grant
       ON provider_egress_manifests(grant_id,created_at,id)""",
    """CREATE TRIGGER trg_provider_egress_grants_identity_immutable
       BEFORE UPDATE ON provider_egress_grants
       WHEN NEW.id IS NOT OLD.id OR NEW.database_uuid IS NOT OLD.database_uuid OR
            NEW.grant_intent_json IS NOT OLD.grant_intent_json OR
            NEW.grant_intent_sha256 IS NOT OLD.grant_intent_sha256 OR
            NEW.token_sha256 IS NOT OLD.token_sha256 OR
            NEW.single_use IS NOT OLD.single_use OR
            NEW.issued_at IS NOT OLD.issued_at OR NEW.expires_at IS NOT OLD.expires_at
       BEGIN SELECT RAISE(ABORT,'provider egress grant identity is immutable'); END""",
    """CREATE TRIGGER trg_provider_egress_grants_state_transition
       BEFORE UPDATE ON provider_egress_grants
       WHEN NOT (
         (OLD.status='active' AND NEW.status='reserved' AND
            NEW.reserved_manifest_id IS NOT NULL) OR
         (OLD.status='active' AND NEW.status IN ('expired','revoked')) OR
         (OLD.status='reserved' AND NEW.status='active' AND
            NEW.reserved_manifest_id IS NULL AND EXISTS(
              SELECT 1 FROM provider_egress_manifests manifest
               WHERE manifest.id=OLD.reserved_manifest_id AND
                     manifest.grant_id=OLD.id AND
                     manifest.status IN ('failed_before_send','cancelled'))) OR
         (OLD.status='reserved' AND NEW.status='consumed' AND EXISTS(
              SELECT 1 FROM provider_egress_manifests manifest
               WHERE manifest.id=OLD.reserved_manifest_id AND
                     manifest.grant_id=OLD.id AND manifest.status='planned')) OR
         (OLD.status='reserved' AND NEW.status IN ('expired','revoked') AND EXISTS(
              SELECT 1 FROM provider_egress_manifests manifest
               WHERE manifest.id=OLD.reserved_manifest_id AND
                     manifest.grant_id=OLD.id AND manifest.status='cancelled')) OR
         (OLD.status='consumed' AND NEW.status='consumed' AND
            OLD.terminal_at IS NULL AND NEW.terminal_at IS NOT NULL AND EXISTS(
              SELECT 1 FROM provider_egress_manifests manifest
               WHERE manifest.id=OLD.reserved_manifest_id AND
                     manifest.grant_id=OLD.id AND
                     manifest.status IN ('sent','failed_after_send'))) OR
         (OLD.status='consumed' AND NEW.status='ambiguous' AND EXISTS(
              SELECT 1 FROM provider_egress_manifests manifest
               WHERE manifest.id=OLD.reserved_manifest_id AND
                     manifest.grant_id=OLD.id AND manifest.status='ambiguous')))
       BEGIN SELECT RAISE(ABORT,'provider egress grant state transition invalid'); END""",
    """CREATE TRIGGER trg_provider_egress_grants_no_delete
       BEFORE DELETE ON provider_egress_grants
       BEGIN SELECT RAISE(ABORT,'provider egress grants are durable audit evidence'); END""",
    """CREATE TRIGGER trg_provider_egress_manifests_insert_binding
       BEFORE INSERT ON provider_egress_manifests
       WHEN NOT EXISTS(
         SELECT 1 FROM provider_egress_grants grant_row
          WHERE grant_row.id=NEW.grant_id AND grant_row.status='reserved' AND
                grant_row.reserved_manifest_id=NEW.id)
       BEGIN SELECT RAISE(ABORT,'provider egress manifest reservation invalid'); END""",
    """CREATE TRIGGER trg_provider_egress_manifests_identity_immutable
       BEFORE UPDATE ON provider_egress_manifests
       WHEN NEW.id IS NOT OLD.id OR NEW.grant_id IS NOT OLD.grant_id OR
            NEW.manifest_plan_json IS NOT OLD.manifest_plan_json OR
            NEW.manifest_plan_sha256 IS NOT OLD.manifest_plan_sha256 OR
            NEW.permit_sha256 IS NOT OLD.permit_sha256 OR
            NEW.created_at IS NOT OLD.created_at OR NEW.reserved_at IS NOT OLD.reserved_at
       BEGIN SELECT RAISE(ABORT,'provider egress manifest identity is immutable'); END""",
    """CREATE TRIGGER trg_provider_egress_manifests_state_transition
       BEFORE UPDATE ON provider_egress_manifests
       WHEN NOT (
         (OLD.status='planned' AND NEW.status='sending' AND EXISTS(
              SELECT 1 FROM provider_egress_grants grant_row
               WHERE grant_row.id=OLD.grant_id AND grant_row.status='consumed' AND
                     grant_row.reserved_manifest_id=OLD.id)) OR
         (OLD.status='planned' AND NEW.status IN ('failed_before_send','cancelled') AND EXISTS(
              SELECT 1 FROM provider_egress_grants grant_row
               WHERE grant_row.id=OLD.grant_id AND grant_row.status='reserved' AND
                     grant_row.reserved_manifest_id=OLD.id)) OR
         (OLD.status='sending' AND
            NEW.status IN ('sent','failed_after_send','ambiguous') AND EXISTS(
              SELECT 1 FROM provider_egress_grants grant_row
               WHERE grant_row.id=OLD.grant_id AND grant_row.status='consumed' AND
                     grant_row.reserved_manifest_id=OLD.id)))
       BEGIN SELECT RAISE(ABORT,'provider egress manifest state transition invalid'); END""",
    """CREATE TRIGGER trg_provider_egress_manifests_no_delete
       BEFORE DELETE ON provider_egress_manifests
       BEGIN SELECT RAISE(ABORT,'provider egress manifests are durable audit evidence'); END""",
)

V16_CHECKSUM = hashlib.sha256(
    "\n".join(" ".join(statement.split()) for statement in V16_SCHEMA_STATEMENTS).encode()
).hexdigest()

V16_SCHEMA_OBJECTS = (
    ("table", "provider_egress_grants"),
    ("table", "provider_egress_manifests"),
    ("index", "idx_provider_egress_grants_status_expiry"),
    ("index", "idx_provider_egress_manifests_status_created"),
    ("index", "idx_provider_egress_manifests_grant"),
    ("trigger", "trg_provider_egress_grants_identity_immutable"),
    ("trigger", "trg_provider_egress_grants_state_transition"),
    ("trigger", "trg_provider_egress_grants_no_delete"),
    ("trigger", "trg_provider_egress_manifests_insert_binding"),
    ("trigger", "trg_provider_egress_manifests_identity_immutable"),
    ("trigger", "trg_provider_egress_manifests_state_transition"),
    ("trigger", "trg_provider_egress_manifests_no_delete"),
)


__all__ = ["V16_CHECKSUM", "V16_SCHEMA_OBJECTS", "V16_SCHEMA_STATEMENTS"]
