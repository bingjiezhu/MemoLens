"""V17 full-read parity evidence and activation gate for image projection."""

from __future__ import annotations

import hashlib


V17_SCHEMA_STATEMENTS = (
    """CREATE TABLE image_projection_read_manifests (
        generation_id TEXT PRIMARY KEY,
        previous_generation_id TEXT CHECK(
          previous_generation_id IS NULL OR previous_generation_id!=generation_id),
        database_uuid TEXT NOT NULL,
        canonical_high_water_position INTEGER NOT NULL CHECK(
          canonical_high_water_position>=0),
        compiler_id TEXT NOT NULL CHECK(length(compiler_id) BETWEEN 1 AND 200),
        projector_version TEXT NOT NULL CHECK(
          length(projector_version) BETWEEN 1 AND 200),
        eligible_count INTEGER NOT NULL CHECK(eligible_count>=0),
        projected_count INTEGER NOT NULL CHECK(projected_count>=0),
        alias_count INTEGER NOT NULL CHECK(alias_count>=0),
        missing_count INTEGER NOT NULL CHECK(missing_count>=0),
        unexpected_count INTEGER NOT NULL CHECK(unexpected_count>=0),
        mismatched_count INTEGER NOT NULL CHECK(mismatched_count>=0),
        blocked_count INTEGER NOT NULL CHECK(blocked_count>=0),
        alias_set_sha256 TEXT NOT NULL CHECK(
          length(alias_set_sha256)=64 AND
          alias_set_sha256 NOT GLOB '*[^0-9a-f]*'),
        row_set_sha256 TEXT NOT NULL CHECK(
          length(row_set_sha256)=64 AND
          row_set_sha256 NOT GLOB '*[^0-9a-f]*'),
        manifest_json TEXT NOT NULL CHECK(
          length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 67108864),
        manifest_sha256 TEXT NOT NULL CHECK(
          length(manifest_sha256)=64 AND
          manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        FOREIGN KEY(generation_id)
          REFERENCES image_projection_generations(id) ON DELETE RESTRICT,
        FOREIGN KEY(previous_generation_id)
          REFERENCES image_projection_generations(id) ON DELETE RESTRICT,
        FOREIGN KEY(database_uuid)
          REFERENCES database_meta(database_uuid) ON DELETE RESTRICT)""",
    """CREATE INDEX idx_image_projection_read_manifest_digest
       ON image_projection_read_manifests(manifest_sha256)""",
    """CREATE TRIGGER trg_image_projection_read_manifests_validate_insert
       BEFORE INSERT ON image_projection_read_manifests WHEN
         NOT EXISTS(
           SELECT 1 FROM image_projection_generations generation
           WHERE generation.id=NEW.generation_id
             AND generation.database_uuid=NEW.database_uuid
             AND generation.canonical_high_water_position=
                 NEW.canonical_high_water_position
             AND generation.compiler_id=NEW.compiler_id
             AND generation.projector_version=NEW.projector_version
             AND generation.status='complete')
         OR memolens_validate_image_contract(NEW.manifest_json,'manifest')!=1
         OR json_valid(NEW.manifest_json)!=1
         OR json_extract(NEW.manifest_json,'$.object')!=
            'memolens.image_projection_manifest'
         OR json_extract(NEW.manifest_json,'$.schema_version')!='1'
         OR json_extract(NEW.manifest_json,'$.projection_contract')!=
            'legacy-image-index-shadow/v1'
         OR json_extract(NEW.manifest_json,'$.projector_version')!=
            NEW.projector_version
         OR json_extract(NEW.manifest_json,'$.compiler_version')!=NEW.compiler_id
         OR json_extract(NEW.manifest_json,'$.database_uuid')!=NEW.database_uuid
         OR json_extract(NEW.manifest_json,'$.canonical_high_water_mark')!=
            NEW.canonical_high_water_position
         OR json_extract(NEW.manifest_json,'$.counts.eligible')!=NEW.eligible_count
         OR json_extract(NEW.manifest_json,'$.counts.projected')!=NEW.projected_count
         OR json_extract(NEW.manifest_json,'$.counts.aliases')!=NEW.alias_count
         OR json_extract(NEW.manifest_json,'$.counts.missing')!=NEW.missing_count
         OR json_extract(NEW.manifest_json,'$.counts.unexpected')!=
            NEW.unexpected_count
         OR json_extract(NEW.manifest_json,'$.counts.mismatched')!=
            NEW.mismatched_count
         OR json_extract(NEW.manifest_json,'$.counts.blocked')!=NEW.blocked_count
         OR json_extract(NEW.manifest_json,'$.alias_set_sha256')!=
            NEW.alias_set_sha256
         OR json_extract(NEW.manifest_json,'$.row_set_sha256')!=NEW.row_set_sha256
         OR json_extract(NEW.manifest_json,'$.manifest_sha256')!=
            NEW.manifest_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.manifest_json,'manifest_sha256')!=NEW.manifest_sha256
         OR NEW.eligible_count!=(
           SELECT COUNT(*) FROM image_projection_rows row
            WHERE row.generation_id=NEW.generation_id)
         OR NEW.alias_count!=(
           SELECT COUNT(*) FROM image_projection_aliases alias
            WHERE alias.generation_id=NEW.generation_id)
         OR (
           EXISTS(
             SELECT 1 FROM image_projection_generations generation
              WHERE generation.id=NEW.generation_id
                AND generation.is_active=1)
           AND NEW.previous_generation_id IS NOT NULL)
         OR (
           EXISTS(
             SELECT 1 FROM image_projection_generations generation
              WHERE generation.id=NEW.generation_id
                AND generation.is_active=0)
           AND (
             ((SELECT COUNT(*) FROM image_projection_generations active
                WHERE active.database_uuid=NEW.database_uuid
                  AND active.projection_contract='legacy-image-index-shadow/v1'
                  AND active.is_active=1)=0
              AND NEW.previous_generation_id IS NOT NULL)
             OR
             ((SELECT COUNT(*) FROM image_projection_generations active
                WHERE active.database_uuid=NEW.database_uuid
                  AND active.projection_contract='legacy-image-index-shadow/v1'
                  AND active.is_active=1)=1
              AND NOT EXISTS(
                SELECT 1 FROM image_projection_generations active
                 WHERE active.id=NEW.previous_generation_id
                   AND active.database_uuid=NEW.database_uuid
                   AND active.projection_contract=
                       'legacy-image-index-shadow/v1'
                   AND active.status='complete'
                   AND active.is_active=1))))
       BEGIN SELECT RAISE(ABORT,
         'image projection read manifest binding mismatch'); END""",
    """CREATE TRIGGER trg_image_projection_read_manifests_no_update
       BEFORE UPDATE ON image_projection_read_manifests
       BEGIN SELECT RAISE(ABORT,
         'image_projection_read_manifests are immutable'); END""",
    """CREATE TRIGGER trg_image_projection_read_manifests_no_delete
       BEFORE DELETE ON image_projection_read_manifests
       BEGIN SELECT RAISE(ABORT,
         'image_projection_read_manifests are immutable'); END""",
    "DROP TRIGGER trg_image_generations_activate_clean_only",
    """CREATE TRIGGER trg_image_generations_activate_clean_only
       BEFORE UPDATE ON image_projection_generations
       WHEN OLD.is_active=0 AND NEW.is_active=1 AND NOT EXISTS(
         SELECT 1
           FROM image_projection_manifests canonical_manifest
           JOIN image_projection_read_manifests read_manifest
             ON read_manifest.generation_id=canonical_manifest.generation_id
          WHERE canonical_manifest.generation_id=OLD.id
            AND canonical_manifest.eligible_count=
                canonical_manifest.projected_count
            AND canonical_manifest.missing_count=0
            AND canonical_manifest.unexpected_count=0
            AND canonical_manifest.mismatched_count=0
            AND canonical_manifest.blocked_count=0
            AND read_manifest.eligible_count=read_manifest.projected_count
            AND read_manifest.missing_count=0
            AND read_manifest.unexpected_count=0
            AND read_manifest.mismatched_count=0
            AND read_manifest.blocked_count=0
            AND read_manifest.manifest_json=canonical_manifest.manifest_json
            AND read_manifest.manifest_sha256=
                canonical_manifest.manifest_sha256)
       BEGIN SELECT RAISE(ABORT,
         'active image generation requires full clean read manifest'); END""",
)

V17_CHECKSUM = hashlib.sha256(
    "\n".join(
        " ".join(statement.split()) for statement in V17_SCHEMA_STATEMENTS
    ).encode()
).hexdigest()

V17_SCHEMA_OBJECTS = (
    ("table", "image_projection_read_manifests"),
    ("index", "idx_image_projection_read_manifest_digest"),
    ("trigger", "trg_image_projection_read_manifests_validate_insert"),
    ("trigger", "trg_image_projection_read_manifests_no_update"),
    ("trigger", "trg_image_projection_read_manifests_no_delete"),
    ("trigger", "trg_image_generations_activate_clean_only"),
)


__all__ = (
    "V17_CHECKSUM",
    "V17_SCHEMA_OBJECTS",
    "V17_SCHEMA_STATEMENTS",
)
