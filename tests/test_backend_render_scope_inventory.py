from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from flask import Flask

from backend.src import DESKTOP_TOKEN_HEADER
from backend.src.api import api_blueprint


DESKTOP_TOKEN = "render-scope-inventory-token"


def _timeline() -> dict[str, object]:
    return {
        "format": {
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "duration_ms": 1000,
            "background_color": "#000000",
        },
        "tracks": [
            {
                "id": "track-1",
                "kind": "video",
                "role": "primary",
                "clips": [
                    {
                        "id": "clip-1",
                        "kind": "image",
                        "asset_source_id": "source-1",
                        "timeline_start_ms": 0,
                        "timeline_duration_ms": 1000,
                    }
                ],
            }
        ],
        "transitions": [],
    }


class _RenderAdmissionRepository:
    database_uuid = "db-render-scope"

    def __init__(self, source: dict[str, object], db_path: Path) -> None:
        self.source = source
        self.db_path = db_path.resolve()
        self.effects: list[tuple[str, object]] = []
        self.timeline = _timeline()
        self.timeline_sha256 = "a" * 64

    @staticmethod
    def replay_idempotent_write(**_values: object) -> None:
        return None

    def get_timeline(self, timeline_id: str, revision: int) -> dict[str, object] | None:
        if timeline_id != "timeline-1" or revision != 3:
            return None
        return {
            "id": timeline_id,
            "revision": revision,
            "project_id": "project-1",
            "content_sha256": self.timeline_sha256,
            "timeline": self.timeline,
        }

    def get_asset_source(self, source_id: str) -> dict[str, object] | None:
        return dict(self.source) if source_id == self.source["id"] else None

    def mark_source_availability(self, source_id: str, availability: str) -> None:
        self.effects.append(("mark_source_availability", (source_id, availability)))

    def execute_idempotent_write(self, **values: object) -> None:
        self.effects.append(("execute_idempotent_write", values))


class BackendRenderScopeInventoryTests(unittest.TestCase):
    def test_render_start_rejects_revision_profile_root_and_source_scope_before_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-scope-") as directory:
            root = Path(directory)
            source_path = root / "still.jpg"
            source_path.write_bytes(b"render-scope-source")
            observed = source_path.stat()
            repository = _RenderAdmissionRepository(
                {
                    "id": "source-1",
                    "asset_id": "asset-1",
                    "availability": "available",
                    "root_path": str(root),
                    "relative_path": source_path.name,
                    "source_file_id": str(observed.st_ino + 1),
                    "observed_size": observed.st_size,
                    "observed_mtime_ns": observed.st_mtime_ns,
                    "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    "kind": "image",
                },
                root / "state.db",
            )
            app = Flask(__name__)
            app.config["DESKTOP_SESSION_TOKEN"] = DESKTOP_TOKEN
            app.extensions["media_repository"] = repository
            app.extensions["app_preview_root_id"] = "preview-root"
            app.register_blueprint(api_blueprint)
            client = app.test_client()
            headers = {
                DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN,
                "Idempotency-Key": "render-scope-oracle",
            }

            base = {
                "db_path": str(repository.db_path),
                "timeline_id": "timeline-1",
                "timeline_revision": 3,
                "expected_timeline_sha256": repository.timeline_sha256,
                "profile": "preview-low",
                "output": {"root_id": "preview-root"},
            }
            cases = (
                ({**base, "timeline_revision": 4}, 404, "timeline_not_found"),
                ({**base, "expected_timeline_sha256": "b" * 64}, 409, "timeline_hash_mismatch"),
                (
                    {**base, "output": {"root_id": "outside-root"}},
                    403,
                    "export_grant_required",
                ),
                ({**base, "profile": "unknown-profile"}, 400, "invalid_render_request"),
                ({**base, "profile": "export-1080p"}, 403, "export_grant_required"),
                (base, 409, "source_changed"),
            )

            for index, (payload, status, code) in enumerate(cases):
                with self.subTest(index=index, code=code):
                    response = client.post(
                        "/v1/renders",
                        json=payload,
                        headers={
                            **headers,
                            "Idempotency-Key": f"render-scope-oracle-{index}",
                        },
                    )
                    self.assertEqual(response.status_code, status, response.get_json())
                    self.assertEqual(response.get_json()["code"], code)
                    self.assertEqual(repository.effects, [])


if __name__ == "__main__":
    unittest.main()
