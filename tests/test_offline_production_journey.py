from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.src import DESKTOP_TOKEN_HEADER, create_app, shutdown_runtime_extensions
from core.config import Settings
from core.network_policy import (
    network_policy_observation,
    reset_network_policy_observations,
)


class OfflineProductionJourneyTests(unittest.TestCase):
    def test_offline_index_search_and_agent_read_journey_has_zero_non_loopback_attempts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-offline-journey-") as raw_root:
            root = Path(raw_root)
            library = root / "library"
            library.mkdir()
            image_path = library / "quiet-lake.jpg"
            Image.new("RGB", (48, 27), "navy").save(image_path)
            desktop_token = "offline-journey-desktop-token"
            environment = {
                "APP_CONFIG_PATH": str(
                    Path(__file__).resolve().parents[1] / "config.yaml"
                ),
                "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                "IMAGE_LIBRARY_DIR": str(library),
                "SQLITE_DB_PATH": str(root / "state" / "photo-index.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": desktop_token,
                "MEMOLENS_NETWORK_PROFILE": "offline",
                "EMBEDDING_BACKEND": "semantic_hash",
                "GEOCODE_ENABLED": "false",
                # A configured credential must not be the reason the journey
                # remains offline; the network profile must win first.
                "MINIMAX_KEY": "configured-offline-provider-key",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            }
            with patch.dict(os.environ, environment, clear=False):
                reset_network_policy_observations()
                app = create_app(Settings.from_env())
                try:
                    client = app.test_client()
                    with patch(
                        "indexing.vision.OpenAICompatibleVisionClient._describe_image_with_minimax",
                        side_effect=AssertionError(
                            "offline indexing reached the configured provider adapter"
                        ),
                    ) as provider_send:
                        indexed = client.post(
                            "/v1/indexing/jobs",
                            json={
                                "image_dir": str(library),
                                "library_root_identity": {
                                    "device": str(library.stat().st_dev),
                                    "inode": str(library.stat().st_ino),
                                },
                                "persist_to_server": True,
                            },
                            headers={DESKTOP_TOKEN_HEADER: desktop_token},
                        )
                    self.assertEqual(
                        indexed.status_code,
                        202,
                        indexed.get_data(as_text=True),
                    )
                    self.assertEqual(indexed.json["status"], "queued")
                    self.assertEqual(indexed.json["data"], [])
                    self.assertEqual(len(indexed.json["queued"]), 1)
                    provider_send.assert_not_called()

                    job_id = str(indexed.json["job_id"])
                    deadline = time.monotonic() + 10
                    job = indexed.json["job"]
                    while (
                        job["status"] in {"queued", "running", "cancelling"}
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        observed = client.get(f"/v1/index/jobs/{job_id}")
                        self.assertEqual(observed.status_code, 200)
                        job = observed.json["job"]
                    self.assertEqual(job["status"], "succeeded", job)

                    # Codex and DeepSeek adapters possess no desktop mutation
                    # token.  Replaying their shared read contract must remain
                    # deterministic and must never select a provider path.
                    query = {
                        "text": "quiet lake",
                        "top_k": 1,
                        "include_copy": True,
                    }
                    codex_read = client.post("/v1/retrieval/query", json=query)
                    deepseek_read = client.post("/v1/retrieval/query", json=query)
                    self.assertEqual(codex_read.status_code, 200)
                    self.assertEqual(deepseek_read.status_code, 200)
                    self.assertEqual(codex_read.json["status"], "completed")
                    self.assertEqual(codex_read.json["data"], deepseek_read.json["data"])
                    image = codex_read.json["data"][0]
                    self.assertEqual(image["asset_id"], image["id"])
                    self.assertEqual(image["analysis_status"], "current")
                    self.assertEqual(
                        image["analysis_binding"],
                        image["canonical_image_observation"]["analysis_binding"],
                    )
                    self.assertEqual(
                        image["projection"],
                        image["canonical_image_observation"]["projection"],
                    )
                    self.assertEqual(
                        image["canonical_image_observation"]["asset_id"],
                        image["id"],
                    )
                    self.assertEqual(
                        codex_read.json["generated_copy"],
                        deepseek_read.json["generated_copy"],
                    )

                    observation = network_policy_observation()
                    counters = observation["counters"]
                    self.assertEqual(counters["blocked_dns"], 0)
                    self.assertEqual(counters["blocked_connect"], 0)
                    self.assertEqual(counters["blocked_datagram"], 0)
                    self.assertEqual(counters["blocked_provider_request"], 0)
                    self.assertFalse(observation["scope"]["os_packet_capture"])
                    self.assertFalse(
                        observation["scope"]["child_process_network_observed"]
                    )
                    encoded = json.dumps(observation, sort_keys=True)
                    self.assertNotIn(raw_root, encoded)
                    self.assertNotIn("quiet lake", encoded)
                    self.assertNotIn("quiet-lake.jpg", encoded)
                    print(
                        json.dumps(
                            {
                                "gate": "ML-004-A1/ML-007-A1",
                                "journey": "offline-index-search-two-agent-reads",
                                "observation": observation,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                finally:
                    shutdown_runtime_extensions(app.extensions)


if __name__ == "__main__":
    unittest.main()
