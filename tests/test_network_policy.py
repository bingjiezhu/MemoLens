from __future__ import annotations

import json
import os
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.src.retrieval.copywriter import RetrievalCopywriter
from backend.src.retrieval.planner import OpenAICompatibleQueryPlanner
from core.config import Settings
from core import llm_utils
from core.network_policy import (
    configure_offline_process_network_environment,
    local_model_load_kwargs,
    NetworkPolicyError,
    install_python_network_audit_hook,
    network_policy_observation,
    network_profile,
    require_offline_safe_url,
    reset_network_policy_observations,
)
from indexing.vision import OpenAICompatibleVisionClient
from indexing.geocoder import ReverseGeocoder


class OfflineNetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_python_network_audit_hook()

    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"MEMOLENS_NETWORK_PROFILE": "offline"},
            clear=False,
        )
        self.environment.start()
        reset_network_policy_observations()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_only_literal_loopback_urls_are_admitted_offline(self) -> None:
        require_offline_safe_url("http://127.0.0.1:5519")
        require_offline_safe_url("https://[::1]:8443")

        for target in (
            "https://example.invalid/v1",
            "http://localhost:5519",
            "https://user:secret@127.0.0.1:8443",
        ):
            with self.subTest(target=target), self.assertRaises(NetworkPolicyError):
                require_offline_safe_url(target)

    def test_python_audit_hook_blocks_dns_and_connect_before_system_call(self) -> None:
        with self.assertRaises(NetworkPolicyError):
            socket.getaddrinfo("example.invalid", 443)

        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(NetworkPolicyError):
                candidate.connect(("203.0.113.10", 443))
        finally:
            candidate.close()

        loopback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(ConnectionRefusedError):
                loopback.connect(("127.0.0.1", 1))
        finally:
            loopback.close()

    def test_provider_preflight_runs_before_client_or_http_request(self) -> None:
        with patch.object(
            llm_utils,
            "OpenAI",
            side_effect=AssertionError("OpenAI client construction was reached"),
        ) as constructor:
            with self.assertRaises(NetworkPolicyError):
                llm_utils.create_openai_client(
                    api_key="not-a-real-key",
                    base_url="https://api.example.invalid/v1",
                )
        constructor.assert_not_called()

        with patch.object(
            llm_utils.requests,
            "post",
            side_effect=AssertionError("HTTP send was reached"),
        ) as post:
            with self.assertRaises(NetworkPolicyError):
                llm_utils.request_minimax_chat_completion(
                    api_key="not-a-real-key",
                    base_url="https://api.example.invalid/v1",
                    model="fixture",
                    messages=[],
                    temperature=None,
                    max_tokens=None,
                )
        post.assert_not_called()

    def test_offline_service_selection_uses_local_fallback_without_denial_attempt(self) -> None:
        settings = Settings.from_env()
        planner = OpenAICompatibleQueryPlanner(settings)
        plan = planner.plan(
            "surprise me with a cinematic story from the whole library",
            "2026-08-24T00:00:00Z",
        )
        self.assertTrue(plan.can_fulfill)

        vision = OpenAICompatibleVisionClient(settings)
        metadata = vision.describe_image(
            SimpleNamespace(source_name="fixture.jpg"),
            settings.vision_model,
        )
        self.assertTrue(metadata.description)

        copywriter = RetrievalCopywriter(settings)
        generated = copywriter.generate(
            query_text="fixture",
            retrieved_images=[],
            image_library_dir=settings.image_library_dir,
        )
        self.assertTrue(generated.body)

        counters = network_policy_observation()["counters"]
        self.assertEqual(counters["blocked_provider_request"], 0)
        self.assertEqual(counters["blocked_dns"], 0)
        self.assertEqual(counters["blocked_connect"], 0)

    def test_transformers_are_cache_only_and_gcloud_is_not_spawned_offline(self) -> None:
        self.assertEqual(local_model_load_kwargs(), {"local_files_only": True})
        with patch.object(
            llm_utils.subprocess,
            "run",
            side_effect=AssertionError("gcloud process was reached"),
        ) as run:
            with self.assertRaises(NetworkPolicyError):
                llm_utils._run_gcloud_text(["gcloud", "auth", "print-access-token"])
        run.assert_not_called()

    def test_offline_environment_removes_inherited_proxy_routes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://203.0.113.10:8080",
                "HTTPS_PROXY": "http://203.0.113.10:8080",
                "ALL_PROXY": "socks5://203.0.113.10:1080",
            },
            clear=False,
        ):
            configure_offline_process_network_environment()
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertNotIn("ALL_PROXY", os.environ)
            self.assertEqual(os.environ["NO_PROXY"], "127.0.0.0/8,::1")
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")

    def test_geocoder_degrades_locally_without_calling_requests(self) -> None:
        geocoder = ReverseGeocoder(
            SimpleNamespace(geocode_enabled=True, geocode_user_agent="fixture")
        )
        with patch(
            "indexing.geocoder.requests.get",
            side_effect=AssertionError("geocoder send was reached"),
        ) as request:
            result = geocoder.reverse(37.0, -122.0)
        request.assert_not_called()
        self.assertIsNone(result.place_name)
        self.assertIsNone(result.country)

    def test_observation_is_scoped_and_contains_no_targets_or_payloads(self) -> None:
        for target in ("https://one.example.invalid", "https://two.example.invalid"):
            with self.assertRaises(NetworkPolicyError):
                require_offline_safe_url(target)

        observation = network_policy_observation()
        encoded = json.dumps(observation, sort_keys=True)
        self.assertEqual(observation["profile"], "offline")
        self.assertFalse(observation["scope"]["os_packet_capture"])
        self.assertFalse(observation["scope"]["child_process_network_observed"])
        self.assertEqual(observation["counters"]["blocked_provider_request"], 2)
        self.assertNotIn("example.invalid", encoded)
        self.assertNotIn("/Users/", encoded)

    def test_unknown_profile_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"MEMOLENS_NETWORK_PROFILE": "future-auto"},
            clear=False,
        ):
            with self.assertRaises(NetworkPolicyError):
                network_profile()


if __name__ == "__main__":
    unittest.main()
