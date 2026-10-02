"""Additive V14 schema for canonical image publication and projection.

The V14 tables are sidecars around the long-lived V2 media lifecycle tables.
They deliberately do not rebuild ``analysis_runs``, ``asset_analysis_heads``
or ``media_jobs``; video behavior and history therefore stay byte-for-byte
unchanged while image publication gains exact immutable bindings.
"""

from __future__ import annotations

import hashlib


V14_SCHEMA_STATEMENTS = (
    """CREATE TABLE image_analysis_job_bindings (
        job_id TEXT PRIMARY KEY,
        database_uuid TEXT NOT NULL,
        database_device INTEGER NOT NULL CHECK(database_device>=0),
        database_inode INTEGER NOT NULL CHECK(database_inode>0),
        asset_id TEXT NOT NULL,
        analysis_run_id TEXT NOT NULL,
        intended_revision INTEGER NOT NULL CHECK(intended_revision BETWEEN 1 AND 1800000),
        library_root_id TEXT NOT NULL,
        root_permission_fingerprint TEXT NOT NULL CHECK(
          length(root_permission_fingerprint)=64 AND
          root_permission_fingerprint NOT GLOB '*[^0-9a-f]*'),
        source_id TEXT NOT NULL,
        relative_path TEXT NOT NULL CHECK(
          length(relative_path) BETWEEN 1 AND 4096 AND
          substr(relative_path,1,1)!='/' AND
          instr(relative_path,char(0))=0),
        source_device INTEGER NOT NULL CHECK(source_device>=0),
        source_inode INTEGER NOT NULL CHECK(source_inode>0),
        observed_size INTEGER NOT NULL CHECK(observed_size BETWEEN 1 AND 8589934592),
        observed_mtime_ns INTEGER NOT NULL CHECK(observed_mtime_ns>=0),
        observed_ctime_ns INTEGER NOT NULL CHECK(observed_ctime_ns>=0),
        file_identity_sha256 TEXT NOT NULL CHECK(
          length(file_identity_sha256)=64 AND
          file_identity_sha256 NOT GLOB '*[^0-9a-f]*'),
        input_asset_sha256 TEXT NOT NULL CHECK(
          length(input_asset_sha256)=64 AND
          input_asset_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_binding_sha256 TEXT NOT NULL CHECK(
          length(source_binding_sha256)=64 AND
          source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
        analysis_profile_id TEXT NOT NULL CHECK(length(analysis_profile_id) BETWEEN 1 AND 200),
        analysis_profile_version TEXT NOT NULL CHECK(length(analysis_profile_version) BETWEEN 1 AND 200),
        analysis_profile_json TEXT NOT NULL CHECK(length(analysis_profile_json) BETWEEN 2 AND 262144),
        analysis_profile_sha256 TEXT NOT NULL CHECK(
          length(analysis_profile_sha256)=64 AND
          analysis_profile_sha256 NOT GLOB '*[^0-9a-f]*'),
        expected_head_state TEXT NOT NULL CHECK(expected_head_state IN ('missing','present')),
        expected_head_run_id TEXT,
        expected_head_revision INTEGER,
        expected_head_content_sha256 TEXT,
        enqueue_scope TEXT NOT NULL CHECK(length(enqueue_scope) BETWEEN 1 AND 256),
        idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
        request_json TEXT NOT NULL CHECK(length(request_json) BETWEEN 2 AND 1048576),
        request_sha256 TEXT NOT NULL CHECK(
          length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        CHECK((expected_head_state='missing' AND expected_head_run_id IS NULL
               AND expected_head_revision IS NULL
               AND expected_head_content_sha256 IS NULL) OR
              (expected_head_state='present' AND expected_head_run_id IS NOT NULL
               AND expected_head_revision BETWEEN 1 AND 1800000
               AND length(expected_head_content_sha256)=64
               AND expected_head_content_sha256 NOT GLOB '*[^0-9a-f]*')),
        UNIQUE(database_uuid,enqueue_scope,idempotency_key),
        UNIQUE(job_id,asset_id,analysis_run_id),
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(job_id) REFERENCES media_jobs(id) ON DELETE RESTRICT
          DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(asset_id,analysis_run_id)
          REFERENCES analysis_runs(asset_id,id) ON DELETE RESTRICT,
        FOREIGN KEY(library_root_id) REFERENCES library_roots(id) ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES asset_sources(id) ON DELETE RESTRICT)""",
    """CREATE TABLE image_analysis_attempt_authorities (
        job_id TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 100),
        runtime_generation TEXT NOT NULL CHECK(
          length(runtime_generation)=83 AND
          runtime_generation GLOB 'runtime_generation_[0-9a-f]*' AND
          substr(runtime_generation,20) NOT GLOB '*[^0-9a-f]*'),
        database_uuid TEXT NOT NULL,
        database_device INTEGER NOT NULL CHECK(database_device>=0),
        database_inode INTEGER NOT NULL CHECK(database_inode>0),
        request_sha256 TEXT NOT NULL CHECK(
          length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
        reason TEXT NOT NULL CHECK(
          reason IN ('initial','explicit_resume','process_recovery','runtime_handoff')),
        predecessor_attempt INTEGER,
        predecessor_status TEXT CHECK(
          predecessor_status IS NULL OR predecessor_status IN ('interrupted','failed')),
        predecessor_stage TEXT,
        predecessor_checkpoint_sha256 TEXT CHECK(
          predecessor_checkpoint_sha256 IS NULL OR
          (length(predecessor_checkpoint_sha256)=64 AND
           predecessor_checkpoint_sha256 NOT GLOB '*[^0-9a-f]*')),
        predecessor_authority_sha256 TEXT CHECK(
          predecessor_authority_sha256 IS NULL OR
          (length(predecessor_authority_sha256)=64 AND
           predecessor_authority_sha256 NOT GLOB '*[^0-9a-f]*')),
        start_checkpoint_sha256 TEXT NOT NULL CHECK(
          length(start_checkpoint_sha256)=64 AND
          start_checkpoint_sha256 NOT GLOB '*[^0-9a-f]*'),
        authority_json TEXT NOT NULL CHECK(length(authority_json) BETWEEN 2 AND 65536),
        authority_sha256 TEXT NOT NULL CHECK(
          length(authority_sha256)=64 AND authority_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,attempt),
        UNIQUE(job_id,attempt,authority_sha256),
        CHECK((attempt=1 AND reason='initial' AND predecessor_attempt IS NULL
               AND predecessor_status IS NULL AND predecessor_stage IS NULL
               AND predecessor_checkpoint_sha256 IS NULL
               AND predecessor_authority_sha256 IS NULL) OR
              (attempt>1 AND reason!='initial' AND predecessor_attempt=attempt-1
               AND predecessor_status IS NOT NULL AND predecessor_stage IS NOT NULL
               AND predecessor_checkpoint_sha256 IS NOT NULL
               AND predecessor_authority_sha256 IS NOT NULL)),
        FOREIGN KEY(job_id) REFERENCES image_analysis_job_bindings(job_id) ON DELETE RESTRICT,
        FOREIGN KEY(job_id,predecessor_attempt,predecessor_authority_sha256)
          REFERENCES image_analysis_attempt_authorities(job_id,attempt,authority_sha256)
          ON DELETE RESTRICT)""",
    """CREATE TABLE image_analysis_attempt_states (
        job_id TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 100),
        authority_sha256 TEXT NOT NULL CHECK(
          length(authority_sha256)=64 AND authority_sha256 NOT GLOB '*[^0-9a-f]*'),
        state TEXT NOT NULL CHECK(
          state IN ('queued','claimed','interrupted','finished','cancelled')),
        heartbeat_sequence INTEGER NOT NULL DEFAULT 0 CHECK(heartbeat_sequence>=0),
        claimed_at TEXT,
        heartbeat_at TEXT,
        finished_at TEXT,
        PRIMARY KEY(job_id,attempt),
        FOREIGN KEY(job_id,attempt,authority_sha256)
          REFERENCES image_analysis_attempt_authorities(job_id,attempt,authority_sha256)
          ON DELETE RESTRICT)""",
    """CREATE TABLE image_analysis_results (
        asset_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1800000),
        analysis_run_id TEXT NOT NULL,
        job_id TEXT NOT NULL UNIQUE,
        schema_version TEXT NOT NULL CHECK(schema_version='1'),
        input_asset_sha256 TEXT NOT NULL CHECK(
          length(input_asset_sha256)=64 AND input_asset_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_id TEXT NOT NULL,
        source_binding_sha256 TEXT NOT NULL CHECK(
          length(source_binding_sha256)=64 AND
          source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
        analysis_profile_id TEXT NOT NULL CHECK(length(analysis_profile_id) BETWEEN 1 AND 200),
        analysis_profile_version TEXT NOT NULL CHECK(length(analysis_profile_version) BETWEEN 1 AND 200),
        analysis_profile_sha256 TEXT NOT NULL CHECK(
          length(analysis_profile_sha256)=64 AND
          analysis_profile_sha256 NOT GLOB '*[^0-9a-f]*'),
        parent_analysis_run_id TEXT,
        parent_revision INTEGER,
        parent_content_sha256 TEXT,
        result_json TEXT NOT NULL CHECK(length(result_json) BETWEEN 2 AND 67108864),
        content_sha256 TEXT NOT NULL CHECK(
          length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        published_at TEXT NOT NULL,
        PRIMARY KEY(asset_id,revision),
        UNIQUE(asset_id,analysis_run_id),
        UNIQUE(asset_id,analysis_run_id,revision,content_sha256),
        CHECK((parent_analysis_run_id IS NULL AND parent_revision IS NULL
               AND parent_content_sha256 IS NULL) OR
              (parent_analysis_run_id IS NOT NULL
               AND parent_revision BETWEEN 1 AND 1800000
               AND length(parent_content_sha256)=64
               AND parent_content_sha256 NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,asset_id,analysis_run_id)
          REFERENCES image_analysis_job_bindings(job_id,asset_id,analysis_run_id)
          ON DELETE RESTRICT,
        FOREIGN KEY(asset_id,analysis_run_id)
          REFERENCES analysis_runs(asset_id,id) ON DELETE RESTRICT,
        FOREIGN KEY(asset_id,parent_analysis_run_id,parent_revision,parent_content_sha256)
          REFERENCES image_analysis_results(
            asset_id,analysis_run_id,revision,content_sha256) ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES asset_sources(id) ON DELETE RESTRICT)""",
    """CREATE TABLE image_analysis_artifacts (
        asset_id TEXT NOT NULL,
        analysis_run_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('metadata','geocode','vision','embedding','quality')),
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
        media_type TEXT NOT NULL CHECK(length(media_type) BETWEEN 1 AND 200),
        signal TEXT CHECK(signal IS NULL OR signal IN ('visual','text_derived')),
        model_id TEXT CHECK(model_id IS NULL OR length(model_id) BETWEEN 1 AND 200),
        dimensions INTEGER CHECK(dimensions IS NULL OR dimensions BETWEEN 1 AND 65536),
        artifact_sha256 TEXT NOT NULL CHECK(
          length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
        size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 67108864),
        artifact_blob BLOB NOT NULL CHECK(length(artifact_blob)=size_bytes),
        published_at TEXT NOT NULL,
        PRIMARY KEY(asset_id,analysis_run_id,stage,name),
        CHECK((substr(name,1,7)='vector:' AND stage='embedding'
               AND signal IS NOT NULL AND model_id IS NOT NULL
               AND dimensions IS NOT NULL) OR
              (substr(name,1,7)!='vector:' AND signal IS NULL
               AND model_id IS NULL AND dimensions IS NULL)),
        FOREIGN KEY(asset_id,analysis_run_id)
          REFERENCES image_analysis_results(asset_id,analysis_run_id)
          ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE image_analysis_heads (
        asset_id TEXT PRIMARY KEY,
        analysis_run_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1800000),
        content_sha256 TEXT NOT NULL CHECK(
          length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        updated_at TEXT NOT NULL,
        FOREIGN KEY(asset_id,analysis_run_id,revision,content_sha256)
          REFERENCES image_analysis_results(
            asset_id,analysis_run_id,revision,content_sha256) ON DELETE RESTRICT)""",
    """CREATE TABLE image_projection_changes (
        position INTEGER PRIMARY KEY CHECK(position>=1),
        change_id TEXT NOT NULL UNIQUE CHECK(length(change_id) BETWEEN 1 AND 200),
        database_uuid TEXT NOT NULL,
        projection_contract TEXT NOT NULL CHECK(
          projection_contract='legacy-image-index-shadow/v1'),
        asset_id TEXT NOT NULL,
        analysis_run_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1800000),
        content_sha256 TEXT NOT NULL CHECK(
          length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_id TEXT NOT NULL,
        source_binding_sha256 TEXT CHECK(
          source_binding_sha256 IS NULL OR
          (length(source_binding_sha256)=64 AND
           source_binding_sha256 NOT GLOB '*[^0-9a-f]*')),
        operation TEXT NOT NULL CHECK(operation IN ('upsert','remove')),
        change_json TEXT NOT NULL CHECK(length(change_json) BETWEEN 2 AND 65536),
        change_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(change_sha256)=64 AND change_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        CHECK((operation='upsert' AND source_binding_sha256 IS NOT NULL) OR
              (operation='remove' AND source_binding_sha256 IS NULL)),
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(asset_id,analysis_run_id,revision,content_sha256)
          REFERENCES image_analysis_results(
            asset_id,analysis_run_id,revision,content_sha256) ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES asset_sources(id) ON DELETE RESTRICT)""",
    """CREATE TABLE image_publish_receipts (
        job_id TEXT PRIMARY KEY,
        database_uuid TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        analysis_run_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1800000),
        content_sha256 TEXT NOT NULL CHECK(
          length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        change_position INTEGER NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL CHECK(
          length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
        publish_receipt_json TEXT NOT NULL CHECK(
          length(publish_receipt_json) BETWEEN 2 AND 65536),
        publish_receipt_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(publish_receipt_sha256)=64 AND
          publish_receipt_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(job_id,asset_id,analysis_run_id)
          REFERENCES image_analysis_job_bindings(job_id,asset_id,analysis_run_id)
          ON DELETE RESTRICT,
        FOREIGN KEY(asset_id,analysis_run_id,revision,content_sha256)
          REFERENCES image_analysis_results(
            asset_id,analysis_run_id,revision,content_sha256) ON DELETE RESTRICT,
        FOREIGN KEY(change_position)
          REFERENCES image_projection_changes(position) ON DELETE RESTRICT)""",
    """CREATE TABLE legacy_image_aliases (
        database_uuid TEXT NOT NULL,
        legacy_id TEXT NOT NULL CHECK(length(legacy_id) BETWEEN 1 AND 256),
        canonical_asset_id TEXT NOT NULL,
        library_root_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        alias_scope TEXT NOT NULL CHECK(alias_scope='legacy-image-index/v1'),
        alias_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(alias_sha256)=64 AND alias_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        PRIMARY KEY(database_uuid,legacy_id),
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(canonical_asset_id) REFERENCES assets(id) ON DELETE RESTRICT,
        FOREIGN KEY(library_root_id) REFERENCES library_roots(id) ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES asset_sources(id) ON DELETE RESTRICT)""",
    """CREATE TABLE image_projection_generations (
        id TEXT PRIMARY KEY,
        database_uuid TEXT NOT NULL,
        projection_contract TEXT NOT NULL CHECK(
          projection_contract='legacy-image-index-shadow/v1'),
        canonical_high_water_position INTEGER NOT NULL CHECK(
          canonical_high_water_position>=0),
        compiler_id TEXT NOT NULL CHECK(length(compiler_id) BETWEEN 1 AND 200),
        projector_version TEXT NOT NULL CHECK(length(projector_version) BETWEEN 1 AND 200),
        status TEXT NOT NULL CHECK(status IN ('building','complete','failed')),
        is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
        created_at TEXT NOT NULL,
        completed_at TEXT,
        CHECK(is_active=0 OR status='complete'),
        CHECK((status='building' AND completed_at IS NULL) OR
              (status IN ('complete','failed') AND completed_at IS NOT NULL)),
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT)""",
    """CREATE TABLE image_projection_aliases (
        generation_id TEXT NOT NULL,
        legacy_id TEXT NOT NULL CHECK(length(legacy_id) BETWEEN 1 AND 256),
        canonical_asset_id TEXT NOT NULL,
        library_root_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        alias_scope TEXT NOT NULL CHECK(alias_scope='legacy-image-index/v1'),
        alias_sha256 TEXT NOT NULL CHECK(
          length(alias_sha256)=64 AND alias_sha256 NOT GLOB '*[^0-9a-f]*'),
        snapshotted_at TEXT NOT NULL,
        PRIMARY KEY(generation_id,legacy_id),
        UNIQUE(generation_id,alias_sha256),
        FOREIGN KEY(generation_id)
          REFERENCES image_projection_generations(id) ON DELETE CASCADE,
        FOREIGN KEY(canonical_asset_id) REFERENCES assets(id) ON DELETE RESTRICT,
        FOREIGN KEY(library_root_id) REFERENCES library_roots(id) ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES asset_sources(id) ON DELETE RESTRICT)""",
    """CREATE TABLE image_projection_rows (
        generation_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        analysis_run_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1800000),
        content_sha256 TEXT NOT NULL CHECK(
          length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_id TEXT NOT NULL,
        source_binding_sha256 TEXT NOT NULL CHECK(
          length(source_binding_sha256)=64 AND
          source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
        row_json TEXT NOT NULL CHECK(length(row_json) BETWEEN 2 AND 67108864),
        row_sha256 TEXT NOT NULL CHECK(
          length(row_sha256)=64 AND row_sha256 NOT GLOB '*[^0-9a-f]*'),
        alias_set_sha256 TEXT NOT NULL CHECK(
          length(alias_set_sha256)=64 AND alias_set_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        PRIMARY KEY(generation_id,asset_id),
        FOREIGN KEY(generation_id)
          REFERENCES image_projection_generations(id) ON DELETE CASCADE,
        FOREIGN KEY(asset_id,analysis_run_id,revision,content_sha256)
          REFERENCES image_analysis_results(
            asset_id,analysis_run_id,revision,content_sha256) ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES asset_sources(id) ON DELETE RESTRICT)""",
    """CREATE TABLE image_projection_receipts (
        change_position INTEGER PRIMARY KEY,
        generation_id TEXT,
        projection_contract TEXT NOT NULL CHECK(
          projection_contract='legacy-image-index-shadow/v1'),
        projector_version TEXT NOT NULL CHECK(length(projector_version) BETWEEN 1 AND 200),
        database_uuid TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        analysis_run_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1800000),
        content_sha256 TEXT NOT NULL CHECK(
          length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_binding_sha256 TEXT CHECK(
          source_binding_sha256 IS NULL OR
          (length(source_binding_sha256)=64 AND
           source_binding_sha256 NOT GLOB '*[^0-9a-f]*')),
        projected_row_sha256 TEXT CHECK(
          projected_row_sha256 IS NULL OR
          (length(projected_row_sha256)=64 AND
           projected_row_sha256 NOT GLOB '*[^0-9a-f]*')),
        alias_set_sha256 TEXT NOT NULL CHECK(
          length(alias_set_sha256)=64 AND alias_set_sha256 NOT GLOB '*[^0-9a-f]*'),
        outcome TEXT NOT NULL CHECK(
          outcome IN ('applied','no_change','superseded','removed','blocked')),
        reason_code TEXT CHECK(reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 120),
        receipt_json TEXT NOT NULL CHECK(length(receipt_json) BETWEEN 2 AND 65536),
        receipt_sha256 TEXT NOT NULL UNIQUE CHECK(
          length(receipt_sha256)=64 AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        CHECK((outcome IN ('applied','no_change') AND projected_row_sha256 IS NOT NULL
               AND source_binding_sha256 IS NOT NULL AND reason_code IS NULL) OR
              (outcome='superseded' AND projected_row_sha256 IS NOT NULL
               AND source_binding_sha256 IS NOT NULL AND reason_code IS NOT NULL) OR
              (outcome IN ('removed','blocked') AND projected_row_sha256 IS NULL
               AND reason_code IS NOT NULL)),
        FOREIGN KEY(change_position)
          REFERENCES image_projection_changes(position) ON DELETE RESTRICT,
        FOREIGN KEY(generation_id)
          REFERENCES image_projection_generations(id) ON DELETE RESTRICT,
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(asset_id,analysis_run_id,revision,content_sha256)
          REFERENCES image_analysis_results(
            asset_id,analysis_run_id,revision,content_sha256) ON DELETE RESTRICT)""",
    """CREATE TABLE image_projection_manifests (
        generation_id TEXT PRIMARY KEY,
        database_uuid TEXT NOT NULL,
        canonical_high_water_position INTEGER NOT NULL CHECK(
          canonical_high_water_position>=0),
        compiler_id TEXT NOT NULL CHECK(length(compiler_id) BETWEEN 1 AND 200),
        projector_version TEXT NOT NULL CHECK(length(projector_version) BETWEEN 1 AND 200),
        eligible_count INTEGER NOT NULL CHECK(eligible_count>=0),
        projected_count INTEGER NOT NULL CHECK(projected_count>=0),
        alias_count INTEGER NOT NULL CHECK(alias_count>=0),
        missing_count INTEGER NOT NULL CHECK(missing_count>=0),
        unexpected_count INTEGER NOT NULL CHECK(unexpected_count>=0),
        mismatched_count INTEGER NOT NULL CHECK(mismatched_count>=0),
        blocked_count INTEGER NOT NULL CHECK(blocked_count>=0),
        alias_set_sha256 TEXT NOT NULL CHECK(
          length(alias_set_sha256)=64 AND alias_set_sha256 NOT GLOB '*[^0-9a-f]*'),
        row_set_sha256 TEXT NOT NULL CHECK(
          length(row_set_sha256)=64 AND row_set_sha256 NOT GLOB '*[^0-9a-f]*'),
        manifest_json TEXT NOT NULL CHECK(length(manifest_json) BETWEEN 2 AND 67108864),
        manifest_sha256 TEXT NOT NULL CHECK(
          length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        CHECK(projected_count<=eligible_count),
        FOREIGN KEY(generation_id)
          REFERENCES image_projection_generations(id) ON DELETE RESTRICT,
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT)""",
    "CREATE INDEX idx_image_analysis_jobs_asset_created ON image_analysis_job_bindings(asset_id,created_at)",
    "CREATE INDEX idx_image_attempt_authorities_generation ON image_analysis_attempt_authorities(runtime_generation,job_id,attempt)",
    "CREATE INDEX idx_image_analysis_results_run ON image_analysis_results(analysis_run_id)",
    "CREATE INDEX idx_image_artifacts_digest ON image_analysis_artifacts(artifact_sha256)",
    "CREATE INDEX idx_image_projection_changes_asset_position ON image_projection_changes(asset_id,position DESC)",
    "CREATE UNIQUE INDEX idx_image_projection_one_active ON image_projection_generations(database_uuid) WHERE is_active=1",
    "CREATE INDEX idx_image_projection_rows_binding ON image_projection_rows(asset_id,revision)",
    "CREATE INDEX idx_image_projection_manifest_digest ON image_projection_manifests(manifest_sha256)",
    """CREATE TRIGGER trg_image_job_bindings_no_update
       BEFORE UPDATE ON image_analysis_job_bindings
       BEGIN SELECT RAISE(ABORT,'image_analysis_job_bindings are immutable'); END""",
    """CREATE TRIGGER trg_image_job_bindings_no_delete
       BEFORE DELETE ON image_analysis_job_bindings
       BEGIN SELECT RAISE(ABORT,'image_analysis_job_bindings are immutable'); END""",
    """CREATE TRIGGER trg_image_attempt_authorities_no_update
       BEFORE UPDATE ON image_analysis_attempt_authorities
       BEGIN SELECT RAISE(ABORT,'image analysis attempt authorities are immutable'); END""",
    """CREATE TRIGGER trg_image_attempt_authorities_validate_insert
       BEFORE INSERT ON image_analysis_attempt_authorities WHEN
         memolens_validate_image_attempt_authority(NEW.authority_json)!=1
         OR json_extract(NEW.authority_json,'$.job_id')!=NEW.job_id
         OR json_extract(NEW.authority_json,'$.attempt')!=NEW.attempt
         OR json_extract(NEW.authority_json,'$.runtime_generation')!=NEW.runtime_generation
         OR json_extract(NEW.authority_json,'$.database_binding.database_uuid')!=NEW.database_uuid
         OR json_extract(NEW.authority_json,'$.database_binding.database_device')!=NEW.database_device
         OR json_extract(NEW.authority_json,'$.database_binding.database_inode')!=NEW.database_inode
         OR json_extract(NEW.authority_json,'$.request_sha256')!=NEW.request_sha256
         OR json_extract(NEW.authority_json,'$.reason')!=NEW.reason
         OR json_extract(NEW.authority_json,'$.predecessor.attempt') IS NOT NEW.predecessor_attempt
         OR json_extract(NEW.authority_json,'$.predecessor.status') IS NOT NEW.predecessor_status
         OR json_extract(NEW.authority_json,'$.predecessor.stage') IS NOT NEW.predecessor_stage
         OR json_extract(NEW.authority_json,'$.predecessor.checkpoint_sha256')
              IS NOT NEW.predecessor_checkpoint_sha256
         OR json_extract(NEW.authority_json,'$.predecessor.authority_sha256')
              IS NOT NEW.predecessor_authority_sha256
         OR json_extract(NEW.authority_json,'$.start_checkpoint_sha256')!=NEW.start_checkpoint_sha256
         OR json_extract(NEW.authority_json,'$.authority_sha256')!=NEW.authority_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.authority_json,'authority_sha256')!=NEW.authority_sha256
         OR NOT EXISTS(
           SELECT 1 FROM image_analysis_job_bindings binding
           WHERE binding.job_id=NEW.job_id
             AND binding.database_uuid=NEW.database_uuid
             AND binding.database_device=NEW.database_device
             AND binding.database_inode=NEW.database_inode
             AND binding.request_sha256=NEW.request_sha256)
       BEGIN SELECT RAISE(ABORT,'image analysis attempt authority binding mismatch'); END""",
    """CREATE TRIGGER trg_image_attempt_authorities_no_delete
       BEFORE DELETE ON image_analysis_attempt_authorities
       BEGIN SELECT RAISE(ABORT,'image analysis attempt authorities are immutable'); END""",
    """CREATE TRIGGER trg_image_attempt_states_identity_immutable
       BEFORE UPDATE ON image_analysis_attempt_states WHEN
         NEW.job_id!=OLD.job_id OR NEW.attempt!=OLD.attempt
         OR NEW.authority_sha256!=OLD.authority_sha256
       BEGIN SELECT RAISE(ABORT,'image analysis attempt state identity is immutable'); END""",
    """CREATE TRIGGER trg_image_attempt_states_transition
       BEFORE UPDATE ON image_analysis_attempt_states WHEN
         NEW.heartbeat_sequence<OLD.heartbeat_sequence
         OR NEW.heartbeat_sequence>OLD.heartbeat_sequence+1
         OR (OLD.state='queued' AND NEW.state NOT IN (
               'queued','claimed','interrupted','finished','cancelled'))
         OR (OLD.state='claimed' AND NEW.state NOT IN (
               'claimed','interrupted','finished','cancelled'))
         OR (OLD.state='interrupted'
             AND NEW.state NOT IN ('interrupted','cancelled'))
         OR (OLD.state IN ('finished','cancelled') AND NEW.state!=OLD.state)
       BEGIN SELECT RAISE(ABORT,'image analysis attempt state transition is invalid'); END""",
    """CREATE TRIGGER trg_image_attempt_states_no_delete
       BEFORE DELETE ON image_analysis_attempt_states
       BEGIN SELECT RAISE(ABORT,'image analysis attempt states cannot be deleted'); END""",
    """CREATE TRIGGER trg_image_results_validate_insert
       BEFORE INSERT ON image_analysis_results
       WHEN NOT EXISTS (
         SELECT 1 FROM image_analysis_job_bindings binding
         JOIN media_jobs job ON job.id=binding.job_id
         JOIN analysis_runs run ON run.id=binding.analysis_run_id
         JOIN assets asset ON asset.id=binding.asset_id
         JOIN asset_sources source ON source.id=binding.source_id
         WHERE binding.job_id=NEW.job_id
           AND binding.asset_id=NEW.asset_id
           AND binding.analysis_run_id=NEW.analysis_run_id
           AND binding.intended_revision=NEW.revision
           AND binding.input_asset_sha256=NEW.input_asset_sha256
           AND binding.source_id=NEW.source_id
           AND binding.source_binding_sha256=NEW.source_binding_sha256
           AND binding.analysis_profile_id=NEW.analysis_profile_id
           AND binding.analysis_profile_version=NEW.analysis_profile_version
           AND binding.analysis_profile_sha256=NEW.analysis_profile_sha256
           AND job.kind='image_analysis' AND job.database_uuid=binding.database_uuid
           AND job.asset_id=binding.asset_id AND job.analysis_run_id=binding.analysis_run_id
           AND run.asset_id=binding.asset_id AND run.input_asset_sha256=binding.input_asset_sha256
           AND run.analysis_profile_id=binding.analysis_profile_id
           AND run.status='succeeded' AND asset.kind='image'
           AND asset.sha256=binding.input_asset_sha256
           AND source.asset_id=binding.asset_id)
       BEGIN SELECT RAISE(ABORT,'image_analysis_results binding mismatch'); END""",
    """CREATE TRIGGER trg_image_results_parent_validate_insert
       BEFORE INSERT ON image_analysis_results
       WHEN NOT EXISTS (
         SELECT 1 FROM image_analysis_job_bindings binding
         WHERE binding.job_id=NEW.job_id AND
           ((binding.expected_head_state='missing'
             AND NEW.parent_analysis_run_id IS NULL AND NEW.parent_revision IS NULL
             AND NEW.parent_content_sha256 IS NULL) OR
            (binding.expected_head_state='present'
             AND NEW.parent_analysis_run_id=binding.expected_head_run_id
             AND NEW.parent_revision=binding.expected_head_revision
             AND NEW.parent_content_sha256=binding.expected_head_content_sha256)))
       BEGIN SELECT RAISE(ABORT,'image_analysis_results parent mismatch'); END""",
    """CREATE TRIGGER trg_image_results_document_validate_insert
       BEFORE INSERT ON image_analysis_results WHEN
         memolens_validate_image_contract(NEW.result_json,'result')!=1
         OR json_valid(NEW.result_json)!=1
         OR json_extract(NEW.result_json,'$.object')!='memolens.image_analysis_result'
         OR json_extract(NEW.result_json,'$.schema_version')!='1'
         OR json_extract(NEW.result_json,'$.asset_id')!=NEW.asset_id
         OR json_extract(NEW.result_json,'$.analysis_run_id')!=NEW.analysis_run_id
         OR json_extract(NEW.result_json,'$.revision')!=NEW.revision
         OR json_extract(NEW.result_json,'$.content_sha256')!=NEW.content_sha256
         OR json_extract(NEW.result_json,'$.asset_sha256')!=NEW.input_asset_sha256
         OR json_extract(NEW.result_json,'$.source_binding.source_id')!=NEW.source_id
         OR json_extract(NEW.result_json,'$.analysis_profile.id')!=NEW.analysis_profile_id
         OR json_extract(NEW.result_json,'$.analysis_profile.version')!=NEW.analysis_profile_version
         OR json_extract(NEW.result_json,'$.analysis_profile.content_sha256')!=NEW.analysis_profile_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.result_json,'content_sha256')!=NEW.content_sha256
       BEGIN SELECT RAISE(ABORT,'image_analysis_results document mismatch'); END""",
    """CREATE TRIGGER trg_image_results_no_update
       BEFORE UPDATE ON image_analysis_results
       BEGIN SELECT RAISE(ABORT,'image_analysis_results are immutable'); END""",
    """CREATE TRIGGER trg_image_results_no_delete
       BEFORE DELETE ON image_analysis_results
       BEGIN SELECT RAISE(ABORT,'image_analysis_results are immutable'); END""",
    """CREATE TRIGGER trg_image_artifacts_no_insert_after_publish
       BEFORE INSERT ON image_analysis_artifacts WHEN EXISTS(
         SELECT 1 FROM image_analysis_results result
         JOIN image_publish_receipts receipt ON receipt.job_id=result.job_id
         WHERE result.asset_id=NEW.asset_id
           AND result.analysis_run_id=NEW.analysis_run_id)
       BEGIN SELECT RAISE(ABORT,'published image artifact set is closed'); END""",
    """CREATE TRIGGER trg_image_artifacts_digest_validate_insert
       BEFORE INSERT ON image_analysis_artifacts
       WHEN memolens_blob_sha256(NEW.artifact_blob)!=NEW.artifact_sha256
       BEGIN SELECT RAISE(ABORT,'image artifact digest mismatch'); END""",
    """CREATE TRIGGER trg_image_artifacts_no_update
       BEFORE UPDATE ON image_analysis_artifacts
       BEGIN SELECT RAISE(ABORT,'image_analysis_artifacts are immutable'); END""",
    """CREATE TRIGGER trg_image_artifacts_no_delete
       BEFORE DELETE ON image_analysis_artifacts
       BEGIN SELECT RAISE(ABORT,'image_analysis_artifacts are immutable'); END""",
    """CREATE TRIGGER trg_image_heads_initial_revision
       BEFORE INSERT ON image_analysis_heads WHEN NEW.revision!=1
       BEGIN SELECT RAISE(ABORT,'image_analysis_heads must start at revision one'); END""",
    """CREATE TRIGGER trg_image_heads_forward_only
       BEFORE UPDATE ON image_analysis_heads
       WHEN NEW.asset_id!=OLD.asset_id OR NEW.revision!=OLD.revision+1 OR NOT EXISTS (
         SELECT 1 FROM image_analysis_results result
         WHERE result.asset_id=OLD.asset_id
           AND result.analysis_run_id=NEW.analysis_run_id
           AND result.revision=NEW.revision
           AND result.content_sha256=NEW.content_sha256
           AND result.parent_analysis_run_id=OLD.analysis_run_id
           AND result.parent_revision=OLD.revision
           AND result.parent_content_sha256=OLD.content_sha256)
       BEGIN SELECT RAISE(ABORT,'image_analysis_heads invalid successor'); END""",
    """CREATE TRIGGER trg_image_heads_no_delete
       BEFORE DELETE ON image_analysis_heads
       BEGIN SELECT RAISE(ABORT,'image_analysis_heads cannot be deleted'); END""",
    """CREATE TRIGGER trg_image_heads_mirror_insert
       AFTER INSERT ON image_analysis_heads
       BEGIN
         INSERT INTO asset_analysis_heads(asset_id,analysis_run_id,updated_at)
         VALUES(NEW.asset_id,NEW.analysis_run_id,NEW.updated_at)
         ON CONFLICT(asset_id) DO UPDATE SET
           analysis_run_id=excluded.analysis_run_id,updated_at=excluded.updated_at;
       END""",
    """CREATE TRIGGER trg_image_heads_mirror_update
       AFTER UPDATE ON image_analysis_heads
       BEGIN
         UPDATE asset_analysis_heads SET analysis_run_id=NEW.analysis_run_id,
           updated_at=NEW.updated_at WHERE asset_id=NEW.asset_id;
       END""",
    """CREATE TRIGGER trg_asset_heads_image_exact_insert
       BEFORE INSERT ON asset_analysis_heads
       WHEN EXISTS(SELECT 1 FROM assets WHERE id=NEW.asset_id AND kind='image')
        AND NOT EXISTS(SELECT 1 FROM image_analysis_heads
                       WHERE asset_id=NEW.asset_id
                         AND analysis_run_id=NEW.analysis_run_id)
       BEGIN SELECT RAISE(ABORT,'image asset_analysis_heads must mirror exact image head'); END""",
    """CREATE TRIGGER trg_asset_heads_image_exact_update
       BEFORE UPDATE ON asset_analysis_heads
       WHEN EXISTS(SELECT 1 FROM assets WHERE id=NEW.asset_id AND kind='image')
        AND NOT EXISTS(SELECT 1 FROM image_analysis_heads
                       WHERE asset_id=NEW.asset_id
                         AND analysis_run_id=NEW.analysis_run_id)
       BEGIN SELECT RAISE(ABORT,'image asset_analysis_heads must mirror exact image head'); END""",
    """CREATE TRIGGER trg_asset_heads_image_no_delete
       BEFORE DELETE ON asset_analysis_heads
       WHEN EXISTS(SELECT 1 FROM assets WHERE id=OLD.asset_id AND kind='image')
       BEGIN SELECT RAISE(ABORT,'image asset_analysis_heads cannot be deleted'); END""",
    """CREATE TRIGGER trg_image_runs_identity_immutable
       BEFORE UPDATE ON analysis_runs
       WHEN EXISTS(SELECT 1 FROM image_analysis_job_bindings b
                   WHERE b.analysis_run_id=OLD.id)
        AND (NEW.id!=OLD.id OR NEW.asset_id!=OLD.asset_id
             OR NEW.revision!=OLD.revision OR NEW.run_kind!=OLD.run_kind
             OR NEW.parent_run_id IS NOT OLD.parent_run_id
             OR NEW.analysis_profile_id!=OLD.analysis_profile_id
             OR NEW.analysis_profile_json!=OLD.analysis_profile_json
             OR NEW.input_asset_sha256!=OLD.input_asset_sha256
             OR EXISTS(SELECT 1 FROM image_analysis_results r
                       WHERE r.analysis_run_id=OLD.id))
       BEGIN SELECT RAISE(ABORT,'published image analysis run is immutable'); END""",
    """CREATE TRIGGER trg_image_runs_bound_no_delete
       BEFORE DELETE ON analysis_runs
       WHEN EXISTS(SELECT 1 FROM image_analysis_job_bindings b
                   WHERE b.analysis_run_id=OLD.id)
       BEGIN SELECT RAISE(ABORT,'bound image analysis run cannot be deleted'); END""",
    """CREATE TRIGGER trg_image_jobs_binding_insert
       BEFORE INSERT ON media_jobs WHEN NEW.kind='image_analysis'
        AND NOT EXISTS(
          SELECT 1 FROM image_analysis_job_bindings b
          JOIN image_analysis_attempt_authorities authority
            ON authority.job_id=b.job_id AND authority.attempt=NEW.attempt
          JOIN image_analysis_attempt_states state
            ON state.job_id=authority.job_id AND state.attempt=authority.attempt
           AND state.authority_sha256=authority.authority_sha256
          WHERE b.job_id=NEW.id AND b.database_uuid=NEW.database_uuid
            AND b.asset_id=NEW.asset_id AND b.analysis_run_id=NEW.analysis_run_id
            AND state.state='queued')
       BEGIN SELECT RAISE(ABORT,'image_analysis job requires exact binding'); END""",
    """CREATE TRIGGER trg_image_jobs_identity_immutable
       BEFORE UPDATE ON media_jobs WHEN OLD.kind='image_analysis'
        AND (NEW.id!=OLD.id OR NEW.database_uuid!=OLD.database_uuid
             OR NEW.kind!=OLD.kind OR NEW.asset_id IS NOT OLD.asset_id
             OR NEW.analysis_run_id IS NOT OLD.analysis_run_id
             OR NEW.attempt<OLD.attempt OR NEW.attempt>OLD.attempt+1
             OR NEW.created_at!=OLD.created_at
             OR (NEW.attempt!=OLD.attempt AND NOT EXISTS(
               SELECT 1 FROM image_analysis_attempt_authorities authority
               JOIN image_analysis_attempt_states state
                 ON state.job_id=authority.job_id AND state.attempt=authority.attempt
                AND state.authority_sha256=authority.authority_sha256
               WHERE authority.job_id=OLD.id AND authority.attempt=NEW.attempt
                 AND state.state='queued')))
       BEGIN SELECT RAISE(ABORT,'image_analysis job identity is immutable'); END""",
    """CREATE TRIGGER trg_image_jobs_closed_stage
       BEFORE UPDATE ON media_jobs WHEN OLD.kind='image_analysis'
        AND NEW.stage NOT IN ('queued','source_admission','metadata','geocode','vision',
          'embedding','quality','publish','shadow_projection','completed')
       BEGIN SELECT RAISE(ABORT,'image_analysis job stage is unsupported'); END""",
    """CREATE TRIGGER trg_image_jobs_terminal_immutable
       BEFORE UPDATE ON media_jobs WHEN OLD.kind='image_analysis'
        AND (OLD.status IN ('succeeded','cancelled')
             OR (OLD.status='failed' AND NOT (
               NEW.status='queued' AND NEW.stage='queued'
               AND NEW.attempt=OLD.attempt+1)))
       BEGIN SELECT RAISE(ABORT,'terminal image_analysis job is immutable'); END""",
    """CREATE TRIGGER trg_image_jobs_success_requires_receipts
       BEFORE UPDATE ON media_jobs WHEN OLD.kind='image_analysis'
        AND NEW.status='succeeded' AND
          (NEW.stage!='completed' OR json_valid(NEW.checkpoint_json)!=1
           OR json_extract(NEW.checkpoint_json,'$.object')!='memolens.image_analysis_checkpoint'
           OR json_extract(NEW.checkpoint_json,'$.worker_kind')!='image_analysis'
           OR json_extract(NEW.checkpoint_json,'$.job_id')!=NEW.id
           OR json_extract(NEW.checkpoint_json,'$.attempt')!=NEW.attempt
           OR json_extract(NEW.checkpoint_json,'$.current_stage')!='completed'
           OR NOT EXISTS(
             SELECT 1 FROM image_publish_receipts p
             JOIN image_projection_receipts r
               ON r.change_position=p.change_position
             JOIN image_projection_generations generation
               ON generation.id=r.generation_id
             JOIN image_projection_manifests manifest
               ON manifest.generation_id=generation.id
             JOIN image_projection_rows projected
               ON projected.generation_id=generation.id
              AND projected.asset_id=p.asset_id
             JOIN image_analysis_heads head ON head.asset_id=p.asset_id
             WHERE p.job_id=NEW.id
               AND generation.status='complete' AND generation.is_active=1
               AND generation.canonical_high_water_position>=p.change_position
               AND manifest.eligible_count=manifest.projected_count
               AND manifest.missing_count=0 AND manifest.unexpected_count=0
               AND manifest.mismatched_count=0 AND manifest.blocked_count=0
               AND projected.row_sha256=r.projected_row_sha256
               AND projected.alias_set_sha256=r.alias_set_sha256
               AND ((r.outcome IN ('applied','no_change')
                     AND r.reason_code IS NULL
                     AND projected.analysis_run_id=p.analysis_run_id
                     AND projected.revision=p.revision
                     AND projected.content_sha256=p.content_sha256
                     AND head.analysis_run_id=p.analysis_run_id
                     AND head.revision=p.revision
                     AND head.content_sha256=p.content_sha256)
                    OR (r.outcome='superseded'
                        AND r.reason_code='newer_head_published'
                        AND projected.revision>p.revision
                        AND head.analysis_run_id=projected.analysis_run_id
                        AND head.revision=projected.revision
                        AND head.content_sha256=projected.content_sha256))
               AND json_extract(NEW.checkpoint_json,
                     '$.publish_binding.analysis_run_id')=p.analysis_run_id
               AND json_extract(NEW.checkpoint_json,
                     '$.publish_binding.revision')=p.revision
               AND json_extract(NEW.checkpoint_json,
                     '$.publish_binding.content_sha256')=p.content_sha256
               AND json_extract(NEW.checkpoint_json,
                     '$.publish_binding.change_position')=p.change_position
               AND json_extract(NEW.checkpoint_json,
                     '$.publish_binding.publish_receipt_sha256')=p.publish_receipt_sha256
               AND json_extract(NEW.checkpoint_json,
                     '$.publish_binding.projection_receipt_sha256')=r.receipt_sha256))
       BEGIN SELECT RAISE(ABORT,'image_analysis success requires publication and projection'); END""",
    """CREATE TRIGGER trg_image_changes_no_update
       BEFORE UPDATE ON image_projection_changes
       BEGIN SELECT RAISE(ABORT,'image_projection_changes are immutable'); END""",
    """CREATE TRIGGER trg_image_changes_document_validate_insert
       BEFORE INSERT ON image_projection_changes WHEN
         memolens_validate_image_contract(NEW.change_json,'change')!=1
         OR json_valid(NEW.change_json)!=1
         OR json_extract(NEW.change_json,'$.object')!='memolens.image_projection_change'
         OR json_extract(NEW.change_json,'$.schema_version')!='1'
         OR json_extract(NEW.change_json,'$.database_uuid')!=NEW.database_uuid
         OR json_extract(NEW.change_json,'$.change_position')!=NEW.position
         OR json_extract(NEW.change_json,'$.asset_id')!=NEW.asset_id
         OR json_extract(NEW.change_json,'$.analysis_binding.analysis_run_id')!=NEW.analysis_run_id
         OR json_extract(NEW.change_json,'$.analysis_binding.revision')!=NEW.revision
         OR json_extract(NEW.change_json,'$.analysis_binding.content_sha256')!=NEW.content_sha256
         OR json_extract(NEW.change_json,'$.source_binding_sha256') IS NOT NEW.source_binding_sha256
         OR json_extract(NEW.change_json,'$.operation')!=NEW.operation
         OR json_extract(NEW.change_json,'$.change_sha256')!=NEW.change_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.change_json,'change_sha256')!=NEW.change_sha256
       BEGIN SELECT RAISE(ABORT,'image projection change document mismatch'); END""",
    """CREATE TRIGGER trg_image_changes_no_delete
       BEFORE DELETE ON image_projection_changes
       BEGIN SELECT RAISE(ABORT,'image_projection_changes are immutable'); END""",
    """CREATE TRIGGER trg_image_publish_receipts_no_update
       BEFORE UPDATE ON image_publish_receipts
       BEGIN SELECT RAISE(ABORT,'image_publish_receipts are immutable'); END""",
    """CREATE TRIGGER trg_image_publish_receipts_validate_insert
       BEFORE INSERT ON image_publish_receipts WHEN
         memolens_validate_image_contract(
              NEW.publish_receipt_json,'publish_receipt')!=1
         OR json_valid(NEW.publish_receipt_json)!=1
         OR json_extract(NEW.publish_receipt_json,'$.object')!='memolens.image_publish_receipt'
         OR json_extract(NEW.publish_receipt_json,'$.schema_version')!='1'
         OR json_extract(NEW.publish_receipt_json,'$.job_id')!=NEW.job_id
         OR json_extract(NEW.publish_receipt_json,'$.database_uuid')!=NEW.database_uuid
         OR json_extract(NEW.publish_receipt_json,'$.request_sha256')!=NEW.request_sha256
         OR json_extract(NEW.publish_receipt_json,'$.result_head.analysis_run_id')!=NEW.analysis_run_id
         OR json_extract(NEW.publish_receipt_json,'$.result_head.revision')!=NEW.revision
         OR json_extract(NEW.publish_receipt_json,'$.result_head.content_sha256')!=NEW.content_sha256
         OR json_extract(NEW.publish_receipt_json,'$.change_position')!=NEW.change_position
         OR json_extract(NEW.publish_receipt_json,'$.publish_receipt_sha256')!=NEW.publish_receipt_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.publish_receipt_json,'publish_receipt_sha256')!=NEW.publish_receipt_sha256
         OR NOT EXISTS(
           SELECT 1 FROM image_analysis_job_bindings binding
           JOIN image_analysis_results result ON result.job_id=binding.job_id
           JOIN image_projection_changes change ON change.position=NEW.change_position
           WHERE binding.job_id=NEW.job_id
             AND binding.database_uuid=NEW.database_uuid
             AND binding.request_sha256=NEW.request_sha256
             AND result.asset_id=NEW.asset_id
             AND result.analysis_run_id=NEW.analysis_run_id
             AND result.revision=NEW.revision
             AND result.content_sha256=NEW.content_sha256
             AND change.database_uuid=NEW.database_uuid
             AND change.asset_id=NEW.asset_id
             AND change.analysis_run_id=NEW.analysis_run_id
             AND change.revision=NEW.revision
             AND change.content_sha256=NEW.content_sha256)
       BEGIN SELECT RAISE(ABORT,'image publish receipt binding mismatch'); END""",
    """CREATE TRIGGER trg_image_publish_receipts_no_delete
       BEFORE DELETE ON image_publish_receipts
       BEGIN SELECT RAISE(ABORT,'image_publish_receipts are immutable'); END""",
    """CREATE TRIGGER trg_legacy_image_aliases_no_update
       BEFORE UPDATE ON legacy_image_aliases
       BEGIN SELECT RAISE(ABORT,'legacy_image_aliases cannot be rebound'); END""",
    """CREATE TRIGGER trg_legacy_image_aliases_no_delete
       BEFORE DELETE ON legacy_image_aliases
       BEGIN SELECT RAISE(ABORT,'legacy_image_aliases are immutable'); END""",
    """CREATE TRIGGER trg_image_generations_identity_immutable
       BEFORE UPDATE ON image_projection_generations
       WHEN NEW.id!=OLD.id OR NEW.database_uuid!=OLD.database_uuid
         OR NEW.projection_contract!=OLD.projection_contract
         OR NEW.canonical_high_water_position!=OLD.canonical_high_water_position
         OR NEW.compiler_id!=OLD.compiler_id
         OR NEW.projector_version!=OLD.projector_version
         OR NEW.created_at!=OLD.created_at
       BEGIN SELECT RAISE(ABORT,'image projection generation identity is immutable'); END""",
    """CREATE TRIGGER trg_image_generations_state_transition
       BEFORE UPDATE ON image_projection_generations
       WHEN (OLD.status='building' AND NEW.status NOT IN ('building','complete','failed'))
         OR (OLD.status='complete' AND NEW.status!='complete')
         OR (OLD.status='failed' AND NEW.status!='failed')
         OR (NEW.is_active=1 AND NEW.status!='complete')
       BEGIN SELECT RAISE(ABORT,'image projection generation transition is invalid'); END""",
    """CREATE TRIGGER trg_image_generations_complete_requires_manifest
       BEFORE UPDATE ON image_projection_generations
       WHEN OLD.status='building' AND NEW.status='complete' AND NOT EXISTS(
         SELECT 1 FROM image_projection_manifests manifest
         WHERE manifest.generation_id=OLD.id
           AND manifest.database_uuid=OLD.database_uuid
           AND manifest.canonical_high_water_position=OLD.canonical_high_water_position
           AND manifest.compiler_id=OLD.compiler_id
           AND manifest.projector_version=OLD.projector_version)
       BEGIN SELECT RAISE(ABORT,'complete image generation requires manifest'); END""",
    """CREATE TRIGGER trg_image_generations_activate_clean_only
       BEFORE UPDATE ON image_projection_generations
       WHEN OLD.is_active=0 AND NEW.is_active=1 AND NOT EXISTS(
         SELECT 1 FROM image_projection_manifests manifest
         WHERE manifest.generation_id=OLD.id
           AND manifest.eligible_count=manifest.projected_count
           AND manifest.missing_count=0
           AND manifest.unexpected_count=0 AND manifest.mismatched_count=0
           AND manifest.blocked_count=0)
       BEGIN SELECT RAISE(ABORT,'active image generation requires clean manifest'); END""",
    """CREATE TRIGGER trg_image_generations_no_delete
       BEFORE DELETE ON image_projection_generations
       BEGIN SELECT RAISE(ABORT,'image_projection_generations cannot be deleted'); END""",
    """CREATE TRIGGER trg_image_projection_aliases_building_insert
       BEFORE INSERT ON image_projection_aliases WHEN NOT EXISTS(
         SELECT 1 FROM image_projection_generations generation
         WHERE generation.id=NEW.generation_id AND generation.status='building'
           AND generation.is_active=0)
         OR EXISTS(SELECT 1 FROM image_projection_manifests manifest
                   WHERE manifest.generation_id=NEW.generation_id)
       BEGIN SELECT RAISE(ABORT,'image projection aliases require building generation'); END""",
    """CREATE TRIGGER trg_image_projection_aliases_binding_insert
       BEFORE INSERT ON image_projection_aliases WHEN
         memolens_canonical_json_sha256(json_object(
           'database_uuid',(SELECT generation.database_uuid
                              FROM image_projection_generations generation
                             WHERE generation.id=NEW.generation_id),
           'legacy_id',NEW.legacy_id,
           'canonical_asset_id',NEW.canonical_asset_id,
           'library_root_id',NEW.library_root_id,
           'source_id',NEW.source_id,
           'alias_scope',NEW.alias_scope))!=NEW.alias_sha256
         OR NOT EXISTS(
           SELECT 1 FROM image_projection_generations generation
           JOIN legacy_image_aliases alias
             ON alias.database_uuid=generation.database_uuid
            AND alias.legacy_id=NEW.legacy_id
           WHERE generation.id=NEW.generation_id
             AND generation.created_at=NEW.snapshotted_at
             AND alias.canonical_asset_id=NEW.canonical_asset_id
             AND alias.library_root_id=NEW.library_root_id
             AND alias.source_id=NEW.source_id
             AND alias.alias_scope=NEW.alias_scope
             AND alias.alias_sha256=NEW.alias_sha256)
       BEGIN SELECT RAISE(ABORT,'image projection alias binding mismatch'); END""",
    """CREATE TRIGGER trg_image_projection_aliases_no_update
       BEFORE UPDATE ON image_projection_aliases
       BEGIN SELECT RAISE(ABORT,'image_projection_aliases are immutable'); END""",
    """CREATE TRIGGER trg_image_projection_aliases_no_delete
       BEFORE DELETE ON image_projection_aliases
       BEGIN SELECT RAISE(ABORT,'image_projection_aliases are immutable'); END""",
    """CREATE TRIGGER trg_image_projection_aliases_snapshot_generation
       AFTER INSERT ON image_projection_generations
       BEGIN
         INSERT INTO image_projection_aliases(
           generation_id,legacy_id,canonical_asset_id,library_root_id,source_id,
           alias_scope,alias_sha256,snapshotted_at)
         SELECT NEW.id,alias.legacy_id,alias.canonical_asset_id,
                alias.library_root_id,alias.source_id,alias.alias_scope,
                alias.alias_sha256,NEW.created_at
           FROM legacy_image_aliases alias
          WHERE alias.database_uuid=NEW.database_uuid
          ORDER BY alias.legacy_id;
       END""",
    """CREATE TRIGGER trg_image_projection_rows_building_insert
       BEFORE INSERT ON image_projection_rows WHEN NOT EXISTS(
         SELECT 1 FROM image_projection_generations generation
         WHERE generation.id=NEW.generation_id AND generation.status='building'
           AND generation.is_active=0)
         OR EXISTS(SELECT 1 FROM image_projection_manifests manifest
                   WHERE manifest.generation_id=NEW.generation_id)
       BEGIN SELECT RAISE(ABORT,'image projection rows require building generation'); END""",
    """CREATE TRIGGER trg_image_projection_rows_building_update
       BEFORE UPDATE ON image_projection_rows WHEN NEW.generation_id!=OLD.generation_id
         OR NOT EXISTS(
         SELECT 1 FROM image_projection_generations generation
         WHERE generation.id=OLD.generation_id AND generation.status='building'
           AND generation.is_active=0)
         OR EXISTS(SELECT 1 FROM image_projection_manifests manifest
                   WHERE manifest.generation_id=OLD.generation_id)
       BEGIN SELECT RAISE(ABORT,'complete image projection rows are immutable'); END""",
    """CREATE TRIGGER trg_image_projection_rows_building_delete
       BEFORE DELETE ON image_projection_rows WHEN NOT EXISTS(
         SELECT 1 FROM image_projection_generations generation
         WHERE generation.id=OLD.generation_id AND generation.status='building'
           AND generation.is_active=0)
         OR EXISTS(SELECT 1 FROM image_projection_manifests manifest
                   WHERE manifest.generation_id=OLD.generation_id)
       BEGIN SELECT RAISE(ABORT,'complete image projection rows are immutable'); END""",
    """CREATE TRIGGER trg_image_projection_rows_document_validate_insert
       BEFORE INSERT ON image_projection_rows WHEN
         memolens_validate_projected_image_row(NEW.row_json)!=1
         OR json_valid(NEW.row_json)!=1
         OR json_extract(NEW.row_json,'$.object')!='memolens.projected_image_index_row'
         OR json_extract(NEW.row_json,'$.schema_version')!='1'
         OR json_extract(NEW.row_json,'$.projection_contract')!='legacy-image-index-shadow/v1'
         OR json_extract(NEW.row_json,'$.asset_id')!=NEW.asset_id
         OR json_extract(NEW.row_json,'$.analysis_binding.analysis_run_id')!=NEW.analysis_run_id
         OR json_extract(NEW.row_json,'$.analysis_binding.revision')!=NEW.revision
         OR json_extract(NEW.row_json,'$.analysis_binding.content_sha256')!=NEW.content_sha256
         OR json_extract(NEW.row_json,'$.source_binding_sha256')!=NEW.source_binding_sha256
         OR json_extract(NEW.row_json,'$.row_sha256')!=NEW.row_sha256
       BEGIN SELECT RAISE(ABORT,'image projection row document mismatch'); END""",
    """CREATE TRIGGER trg_image_projection_rows_document_validate_update
       BEFORE UPDATE ON image_projection_rows WHEN
         memolens_validate_projected_image_row(NEW.row_json)!=1
         OR json_extract(NEW.row_json,'$.asset_id')!=NEW.asset_id
         OR json_extract(NEW.row_json,'$.analysis_binding.analysis_run_id')!=NEW.analysis_run_id
         OR json_extract(NEW.row_json,'$.analysis_binding.revision')!=NEW.revision
         OR json_extract(NEW.row_json,'$.analysis_binding.content_sha256')!=NEW.content_sha256
         OR json_extract(NEW.row_json,'$.source_binding_sha256')!=NEW.source_binding_sha256
         OR json_extract(NEW.row_json,'$.row_sha256')!=NEW.row_sha256
       BEGIN SELECT RAISE(ABORT,'image projection row document mismatch'); END""",
    """CREATE TRIGGER trg_image_projection_receipts_validate_insert
       BEFORE INSERT ON image_projection_receipts WHEN
         memolens_validate_image_contract(
              NEW.receipt_json,'projection_receipt')!=1
         OR json_valid(NEW.receipt_json)!=1
         OR json_extract(NEW.receipt_json,'$.object')!='memolens.image_projection_receipt'
         OR json_extract(NEW.receipt_json,'$.schema_version')!='1'
         OR json_extract(NEW.receipt_json,'$.projection_contract')!=NEW.projection_contract
         OR json_extract(NEW.receipt_json,'$.projector_version')!=NEW.projector_version
         OR json_extract(NEW.receipt_json,'$.database_uuid')!=NEW.database_uuid
         OR json_extract(NEW.receipt_json,'$.change_position')!=NEW.change_position
         OR json_extract(NEW.receipt_json,'$.asset_id')!=NEW.asset_id
         OR json_extract(NEW.receipt_json,'$.analysis_binding.analysis_run_id')!=NEW.analysis_run_id
         OR json_extract(NEW.receipt_json,'$.analysis_binding.revision')!=NEW.revision
         OR json_extract(NEW.receipt_json,'$.analysis_binding.content_sha256')!=NEW.content_sha256
         OR json_extract(NEW.receipt_json,'$.source_binding_sha256') IS NOT NEW.source_binding_sha256
         OR json_extract(NEW.receipt_json,'$.projected_row_sha256') IS NOT NEW.projected_row_sha256
         OR json_extract(NEW.receipt_json,'$.alias_set_sha256')!=NEW.alias_set_sha256
         OR json_extract(NEW.receipt_json,'$.outcome')!=NEW.outcome
         OR json_extract(NEW.receipt_json,'$.reason_code') IS NOT NEW.reason_code
         OR json_extract(NEW.receipt_json,'$.receipt_sha256')!=NEW.receipt_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.receipt_json,'receipt_sha256')!=NEW.receipt_sha256
         OR NOT EXISTS(
           SELECT 1 FROM image_projection_changes change
           WHERE change.position=NEW.change_position
             AND change.database_uuid=NEW.database_uuid
             AND change.projection_contract=NEW.projection_contract
             AND change.asset_id=NEW.asset_id
             AND change.analysis_run_id=NEW.analysis_run_id
             AND change.revision=NEW.revision
             AND change.content_sha256=NEW.content_sha256
             AND change.source_binding_sha256 IS NEW.source_binding_sha256)
         OR (NEW.outcome IN ('applied','no_change') AND (
           NEW.generation_id IS NULL OR NOT EXISTS(
             SELECT 1 FROM image_projection_generations generation
             JOIN image_projection_rows row
               ON row.generation_id=generation.id AND row.asset_id=NEW.asset_id
             JOIN image_analysis_heads head ON head.asset_id=NEW.asset_id
             WHERE generation.id=NEW.generation_id
               AND generation.database_uuid=NEW.database_uuid
               AND generation.projection_contract=NEW.projection_contract
               AND generation.projector_version=NEW.projector_version
               AND row.analysis_run_id=NEW.analysis_run_id
               AND row.revision=NEW.revision
               AND row.content_sha256=NEW.content_sha256
               AND row.source_binding_sha256=NEW.source_binding_sha256
               AND row.row_sha256=NEW.projected_row_sha256
               AND row.alias_set_sha256=NEW.alias_set_sha256
               AND head.analysis_run_id=NEW.analysis_run_id
               AND head.revision=NEW.revision
               AND head.content_sha256=NEW.content_sha256)))
       BEGIN SELECT RAISE(ABORT,'image projection receipt binding mismatch'); END""",
    """CREATE TRIGGER trg_image_projection_receipts_no_update
       BEFORE UPDATE ON image_projection_receipts
       BEGIN SELECT RAISE(ABORT,'image_projection_receipts are immutable'); END""",
    """CREATE TRIGGER trg_image_projection_receipts_no_delete
       BEFORE DELETE ON image_projection_receipts
       BEGIN SELECT RAISE(ABORT,'image_projection_receipts are immutable'); END""",
    """CREATE TRIGGER trg_image_manifests_validate_insert
       BEFORE INSERT ON image_projection_manifests WHEN
         NOT EXISTS(
           SELECT 1 FROM image_projection_generations generation
           WHERE generation.id=NEW.generation_id
             AND generation.database_uuid=NEW.database_uuid
             AND generation.canonical_high_water_position=NEW.canonical_high_water_position
             AND generation.compiler_id=NEW.compiler_id
             AND generation.projector_version=NEW.projector_version
             AND generation.status='building' AND generation.is_active=0)
         OR memolens_validate_image_contract(NEW.manifest_json,'manifest')!=1
         OR json_valid(NEW.manifest_json)!=1
         OR json_extract(NEW.manifest_json,'$.object')!='memolens.image_projection_manifest'
         OR json_extract(NEW.manifest_json,'$.schema_version')!='1'
         OR json_extract(NEW.manifest_json,'$.projection_contract')!='legacy-image-index-shadow/v1'
         OR json_extract(NEW.manifest_json,'$.projector_version')!=NEW.projector_version
         OR json_extract(NEW.manifest_json,'$.compiler_version')!=NEW.compiler_id
         OR json_extract(NEW.manifest_json,'$.database_uuid')!=NEW.database_uuid
         OR json_extract(NEW.manifest_json,'$.canonical_high_water_mark')!=NEW.canonical_high_water_position
         OR json_extract(NEW.manifest_json,'$.counts.eligible')!=NEW.eligible_count
         OR json_extract(NEW.manifest_json,'$.counts.projected')!=NEW.projected_count
         OR json_extract(NEW.manifest_json,'$.counts.aliases')!=NEW.alias_count
         OR json_extract(NEW.manifest_json,'$.counts.missing')!=NEW.missing_count
         OR json_extract(NEW.manifest_json,'$.counts.unexpected')!=NEW.unexpected_count
         OR json_extract(NEW.manifest_json,'$.counts.mismatched')!=NEW.mismatched_count
         OR json_extract(NEW.manifest_json,'$.counts.blocked')!=NEW.blocked_count
         OR json_extract(NEW.manifest_json,'$.alias_set_sha256')!=NEW.alias_set_sha256
         OR json_extract(NEW.manifest_json,'$.row_set_sha256')!=NEW.row_set_sha256
         OR json_extract(NEW.manifest_json,'$.manifest_sha256')!=NEW.manifest_sha256
         OR memolens_canonical_json_sha256_without(
              NEW.manifest_json,'manifest_sha256')!=NEW.manifest_sha256
         OR NEW.projected_count!=(
           SELECT COUNT(*) FROM image_projection_rows row
           WHERE row.generation_id=NEW.generation_id)
         OR NEW.alias_count!=(
           SELECT COUNT(*) FROM image_projection_aliases alias
           WHERE alias.generation_id=NEW.generation_id)
         OR memolens_canonical_json_sha256((
           SELECT json_group_array(json_object(
                    'legacy_id',snapshot.legacy_id,
                    'alias_sha256',snapshot.alias_sha256))
             FROM (
               SELECT alias.legacy_id,alias.alias_sha256
                 FROM image_projection_aliases alias
                WHERE alias.generation_id=NEW.generation_id
                ORDER BY alias.legacy_id
             ) snapshot))!=NEW.alias_set_sha256
         OR EXISTS(
           SELECT 1 FROM image_projection_rows row
           WHERE row.generation_id=NEW.generation_id
             AND (memolens_validate_projected_image_row(row.row_json)!=1
                  OR row.alias_set_sha256!=NEW.alias_set_sha256))
         OR EXISTS(
           SELECT 1 FROM image_projection_receipts receipt
           WHERE receipt.generation_id=NEW.generation_id
             AND receipt.alias_set_sha256!=NEW.alias_set_sha256)
       BEGIN SELECT RAISE(ABORT,'image projection manifest binding mismatch'); END""",
    """CREATE TRIGGER trg_image_manifests_no_update
       BEFORE UPDATE ON image_projection_manifests
       BEGIN SELECT RAISE(ABORT,'image_projection_manifests are immutable'); END""",
    """CREATE TRIGGER trg_image_manifests_no_delete
       BEFORE DELETE ON image_projection_manifests
       BEGIN SELECT RAISE(ABORT,'image_projection_manifests are immutable'); END""",
)


V14_CHECKSUM = hashlib.sha256(
    "\n".join(" ".join(statement.split()) for statement in V14_SCHEMA_STATEMENTS).encode()
).hexdigest()


_V14_TABLES = (
    "image_analysis_job_bindings",
    "image_analysis_attempt_authorities",
    "image_analysis_attempt_states",
    "image_analysis_results",
    "image_analysis_artifacts",
    "image_analysis_heads",
    "image_projection_changes",
    "image_publish_receipts",
    "legacy_image_aliases",
    "image_projection_generations",
    "image_projection_aliases",
    "image_projection_rows",
    "image_projection_receipts",
    "image_projection_manifests",
)

_V14_INDEXES = (
    "idx_image_analysis_jobs_asset_created",
    "idx_image_attempt_authorities_generation",
    "idx_image_analysis_results_run",
    "idx_image_artifacts_digest",
    "idx_image_projection_changes_asset_position",
    "idx_image_projection_one_active",
    "idx_image_projection_rows_binding",
    "idx_image_projection_manifest_digest",
)

_V14_TRIGGERS = tuple(
    statement.split()[2] for statement in V14_SCHEMA_STATEMENTS if statement.lstrip().startswith("CREATE TRIGGER ")
)

V14_SCHEMA_OBJECTS = (
    tuple(("table", name) for name in _V14_TABLES)
    + tuple(("index", name) for name in _V14_INDEXES)
    + tuple(("trigger", name) for name in _V14_TRIGGERS)
)


__all__ = [
    "V14_CHECKSUM",
    "V14_SCHEMA_OBJECTS",
    "V14_SCHEMA_STATEMENTS",
]
