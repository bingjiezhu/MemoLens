from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.src import DESKTOP_TOKEN_HEADER, create_app, shutdown_runtime_extensions
from core.config import Settings


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_TOKEN = "read-scope-inventory-desktop-token"


@dataclass(frozen=True)
class _LocalRuntimeFixture:
    def to_dict(self) -> dict[str, object]:
        return {
            "available": False,
            "provider": "ollama",
            "status": "not_running",
        }


class BackendReadScopeInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-read-scope-inventory-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.root / "state" / "inventory.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": DESKTOP_TOKEN,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": "read-scope-inventory-main-token",
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary.cleanup()

    def test_media_capabilities_reject_unmanaged_toolchain_before_codec_process(self) -> None:
        def unsupported_binary(name: str) -> dict[str, object]:
            self.assertIn(name, {"ffmpeg", "ffprobe"})
            return {"available": False, "supported": False, "version": "5.1"}

        with (
            patch(
                "backend.src.api.routes.binary_capability",
                side_effect=unsupported_binary,
            ),
            patch(
                "backend.src.api.routes.ffmpeg_encode_capability",
                side_effect=AssertionError("unsupported toolchain reached codec probe"),
            ) as codec_probe,
        ):
            response = self.client.get(
                "/v1/media/capabilities",
                query_string={"ffmpeg_path": str(self.root / "attacker-ffmpeg")},
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "degraded")
        self.assertEqual(response.json["supported"]["render_profiles"], [])
        self.assertFalse(response.json["supported"]["verified_preview_save_as"])
        self.assertEqual(response.json["encoder_probe"]["code"], "ffmpeg_unsupported")
        codec_probe.assert_not_called()

    def test_settings_read_ignores_candidate_database_root_and_store_scope(self) -> None:
        forged_root = self.root / "forged-library"
        forged_state = self.root / "forged-state"
        forged_root.mkdir()
        forged_state.mkdir()
        forged_db = forged_state / "forged.db"
        forged_settings = forged_state / "desktop-settings.json"
        forged_db.write_bytes(b"not the active database")
        forged_settings.write_text('{"autoStartBackend": false}', encoding="utf-8")

        with patch(
            "backend.src.api.routes.detect_local_model_runtime",
            return_value=_LocalRuntimeFixture(),
        ):
            response = self.client.get(
                "/v1/settings",
                query_string={
                    "app_state_dir": str(forged_state),
                    "db_path": str(forged_db),
                    "image_library_dir": str(forged_root),
                    "settings_path": str(forged_settings),
                },
            )

        self.assertEqual(response.status_code, 200)
        effective = response.json["effective"]
        settings = self.app.config["SETTINGS"]
        self.assertEqual(effective["db_path"], str(settings.db_path))
        self.assertEqual(effective["image_library_dir"], str(settings.image_library_dir))
        self.assertEqual(effective["app_state_dir"], str(settings.app_state_dir))
        self.assertEqual(effective["settings_path"], str(settings.persisted_settings_path))
        serialized = response.get_data(as_text=True)
        self.assertNotIn(str(forged_root), serialized)
        self.assertNotIn(str(forged_db), serialized)
        self.assertNotIn(str(forged_settings), serialized)


if __name__ == "__main__":
    unittest.main()
