"""Fresh-process adapter used only by the B2B4 automated journey harness.

This is not a Codex or DeepSeek model/UI emulator.  Each invocation is one
separately launched Python process that exercises the production pairing
client and paired canonical-editor backend under an explicit adapter label.
No OS isolation claim is made.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SCRIPTS = (
    REPOSITORY_ROOT / ".agents/plugins/plugins/memolens/scripts"
).resolve()
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from memolens_agent import AgentPairingWorkflow  # noqa: E402
from memolens_canonical_editor import (  # noqa: E402
    closed_canonical_edit,
    paired_canonical_editor_backend,
    validate_saved_result,
)


ADAPTERS = {
    "codex": {
        "adapter": "codex_plugin_process_adapter",
        "claimed_client_label": "Codex automated process adapter",
    },
    "deepseek": {
        "adapter": "deepseek_harness_process_adapter",
        "claimed_client_label": "DeepSeek Harness automated process adapter",
    },
}
EVIDENCE_SCOPE = "automated_python_process_harness"
REPORTED_AUTHORITY_ENV_VARS = (
    "MEMOLENS_MAIN_AUTHORITY_TOKEN",
    "MEMOLENS_DESKTOP_SESSION_TOKEN",
    "SQLITE_DB_PATH",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=tuple(ADAPTERS), required=True)
    parser.add_argument("--base-url", required=True)
    return parser


def _project_id(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("project_id is required")
    return value


def _workflow(base_url: str) -> AgentPairingWorkflow:
    return AgentPairingWorkflow(base_url=base_url, timeout=10.0)


def _pair(
    *,
    base_url: str,
    project_id: str,
    claimed_client_label: str,
) -> dict[str, object]:
    workflow = _workflow(base_url)
    workspace = workflow.client.project_workspace(project_id)
    project = workspace.get("project")
    if not isinstance(project, dict):
        raise ValueError("project workspace is invalid")
    head = project.get("current_blueprint")
    if not isinstance(head, dict):
        raise ValueError("current Blueprint head is unavailable")
    return workflow.create(
        project_id=project_id,
        current_head=head,
        claimed_client_label=claimed_client_label,
        ttl_seconds=900,
        max_operations=1,
        actions=["timeline.apply_edit"],
    )


def _status(*, base_url: str, project_id: str) -> dict[str, object]:
    return _workflow(base_url).status(project_id)


def _save(
    *,
    project_id: str,
    duration_ms: object,
) -> dict[str, object]:
    if type(duration_ms) is not int or not 1 <= duration_ms <= 1_800_000:
        raise ValueError("duration_ms is invalid")
    backend = paired_canonical_editor_backend(project_id, timeout=10.0)
    prior = backend.read()
    timeline = prior.public.get("timeline")
    if not isinstance(timeline, dict):
        raise ValueError("canonical Timeline is unavailable")
    tracks = timeline.get("tracks")
    if not isinstance(tracks, list) or len(tracks) != 1:
        raise ValueError("canonical Timeline track is invalid")
    track = tracks[0]
    clips = track.get("clips") if isinstance(track, dict) else None
    if not isinstance(clips, list):
        raise ValueError("canonical Timeline clips are invalid")
    clip = next(
        (
            candidate
            for candidate in clips
            if isinstance(candidate, dict) and candidate.get("media_kind") == "image"
        ),
        None,
    )
    if not isinstance(clip, dict):
        raise ValueError("an editable image clip is unavailable")
    edit, preview = closed_canonical_edit(
        {
            "op": "set_clip_duration",
            "clip_id": clip.get("clip_id"),
            "duration_ms": duration_ms,
        },
        snapshot=prior,
    )
    if preview is None:
        raise ValueError("the edit did not produce a pending preview")
    result = backend.save(snapshot=prior, edit=edit)
    reread = backend.read()
    verified = validate_saved_result(
        project_id=project_id,
        prior=prior,
        edit=edit,
        result=result,
        reread=reread,
    )
    return {
        "database_uuid": prior.public["database_uuid"],
        "project_id": project_id,
        "before_head": prior.expected_timeline_head,
        "edit": edit,
        "command_result": result,
        "verified_save": verified,
        "reread_head": reread.expected_timeline_head,
    }


def _handle(
    request: object,
    *,
    base_url: str,
    claimed_client_label: str,
) -> dict[str, object]:
    if type(request) is not dict:
        raise ValueError("request must be one object")
    command = request.get("command")
    if command == "pair" and set(request) == {"request_id", "command", "project_id"}:
        return _pair(
            base_url=base_url,
            project_id=_project_id(request.get("project_id")),
            claimed_client_label=claimed_client_label,
        )
    if command == "status" and set(request) == {
        "request_id",
        "command",
        "project_id",
    }:
        return _status(
            base_url=base_url,
            project_id=_project_id(request.get("project_id")),
        )
    if command == "save" and set(request) == {
        "request_id",
        "command",
        "project_id",
        "duration_ms",
    }:
        return _save(
            project_id=_project_id(request.get("project_id")),
            duration_ms=request.get("duration_ms"),
        )
    if command == "shutdown" and set(request) == {"request_id", "command"}:
        return {"shutdown": True}
    raise ValueError("unsupported or non-closed harness command")


def main() -> int:
    arguments = _parser().parse_args()
    adapter = ADAPTERS[arguments.host]
    process_nonce = uuid.uuid4().hex
    reported_absent_authority_env_vars = [
        name for name in REPORTED_AUTHORITY_ENV_VARS if name not in os.environ
    ]
    for line in sys.stdin:
        request_id: object = None
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                request_id = request.get("request_id")
            result = _handle(
                request,
                base_url=arguments.base_url,
                claimed_client_label=str(adapter["claimed_client_label"]),
            )
            response: dict[str, object] = {
                "ok": True,
                "request_id": request_id,
                "adapter": adapter["adapter"],
                "evidence_scope": EVIDENCE_SCOPE,
                "pid": os.getpid(),
                "process_nonce": process_nonce,
                "reported_absent_authority_env_vars": (
                    reported_absent_authority_env_vars
                ),
                "result": result,
            }
        except Exception as exc:  # The parent needs one bounded protocol error.
            response = {
                "ok": False,
                "request_id": request_id,
                "adapter": adapter["adapter"],
                "evidence_scope": EVIDENCE_SCOPE,
                "pid": os.getpid(),
                "process_nonce": process_nonce,
                "reported_absent_authority_env_vars": (
                    reported_absent_authority_env_vars
                ),
                "error": {
                    "type": type(exc).__name__,
                    "code": getattr(exc, "code", "harness_worker_error"),
                },
            }
        sys.stdout.write(
            json.dumps(
                response,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        )
        sys.stdout.flush()
        if response.get("ok") is True and isinstance(response.get("result"), dict):
            if response["result"].get("shutdown") is True:
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
