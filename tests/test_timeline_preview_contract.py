from __future__ import annotations

import copy
import unittest

from core.timeline_lowering_contract import canonical_timeline_content_sha256
from core.timeline_preview_contract import (
    TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
    TimelinePreviewContractError,
    derive_timeline_preview_scope,
    require_timeline_preview_lease_request,
    require_timeline_preview_range,
)
from tests.test_timeline_lowering_contract import compile_result


class TimelinePreviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        compiled, _blueprint, _coverage = compile_result()
        self.timeline = compiled["timeline"]
        self.sources = compiled["source_bindings"]
        self.head = {
            "revision": self.timeline["revision"],
            "content_sha256": canonical_timeline_content_sha256(self.timeline),
        }
        self.request = {
            "object": "memolens.timeline_preview_lease_request",
            "schema_version": "1",
            "project_id": self.timeline["project_id"],
            "observed_head": self.head,
            "request_nonce": "n" * 32,
        }

    def test_lease_request_is_closed_and_caller_cannot_select_media(self) -> None:
        self.assertEqual(require_timeline_preview_lease_request(self.request), self.request)
        for forbidden, value in (
            ("clip_id", "clip-forged"),
            ("asset_id", "asset-forged"),
            ("asset_source_id", "src_forged"),
            ("path", "/private/source.mp4"),
            ("backend_url", "http://127.0.0.1:9999"),
        ):
            with self.subTest(forbidden=forbidden):
                forged = dict(self.request)
                forged[forbidden] = value
                with self.assertRaises(TimelinePreviewContractError) as raised:
                    require_timeline_preview_lease_request(forged)
                self.assertEqual(raised.exception.code, "agent_preview_scope_denied")

    def test_scope_is_derived_from_exact_timeline_and_contains_video_only(self) -> None:
        scope = derive_timeline_preview_scope(
            timeline=self.timeline,
            source_bindings=self.sources,
            observed_head=self.head,
        )
        video_clip = self.timeline["tracks"][0]["clips"][1]
        video_source = self.sources[1]
        self.assertEqual(scope["project_id"], self.timeline["project_id"])
        self.assertEqual(scope["observed_head"], self.head)
        self.assertEqual(len(scope["clips"]), 1)
        self.assertEqual(scope["clips"][0]["clip_id"], video_clip["clip_id"])
        self.assertEqual(
            scope["clips"][0]["asset_source_id"],
            video_source["asset_source_id"],
        )
        serialized = repr(scope)
        for forbidden in ("/Users/", "/private/", "http://", "proof_secret", "lease_secret"):
            self.assertNotIn(forbidden, serialized)

    def test_stale_head_and_tampered_source_fail_closed(self) -> None:
        stale = dict(self.head)
        stale["content_sha256"] = "0" * 64
        with self.assertRaises(TimelinePreviewContractError) as raised:
            derive_timeline_preview_scope(
                timeline=self.timeline,
                source_bindings=self.sources,
                observed_head=stale,
            )
        self.assertEqual(raised.exception.code, "agent_preview_head_changed")

        forged_sources = copy.deepcopy(self.sources)
        forged_sources[1]["asset_source_id"] = self.sources[0]["asset_source_id"]
        with self.assertRaises(TimelinePreviewContractError) as raised:
            derive_timeline_preview_scope(
                timeline=self.timeline,
                source_bindings=forged_sources,
                observed_head=self.head,
            )
        self.assertEqual(raised.exception.code, "agent_preview_source_changed")

    def test_range_contract_is_single_and_bounded(self) -> None:
        exact = require_timeline_preview_range("bytes=10-19", source_size=100)
        self.assertEqual((exact.start, exact.end, exact.length, exact.status_code), (10, 19, 10, 206))
        suffix = require_timeline_preview_range("bytes=-8", source_size=100)
        self.assertEqual((suffix.start, suffix.end, suffix.length, suffix.status_code), (92, 99, 8, 206))
        whole = require_timeline_preview_range(None, source_size=100)
        self.assertEqual((whole.start, whole.end, whole.length, whole.status_code), (0, 99, 100, 200))
        invalid = (
            "bytes=0-1,4-5",
            "bytes=",
            "bytes=-0",
            "items=0-4",
            f"bytes={TIMELINE_PREVIEW_MAX_RESPONSE_BYTES + 10}-"
            f"{TIMELINE_PREVIEW_MAX_RESPONSE_BYTES + 11}",
            f"bytes=0-{TIMELINE_PREVIEW_MAX_RESPONSE_BYTES}",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(TimelinePreviewContractError) as raised:
                    require_timeline_preview_range(
                        value,
                        source_size=TIMELINE_PREVIEW_MAX_RESPONSE_BYTES + 10,
                    )
                self.assertEqual(raised.exception.code, "agent_preview_range_invalid")
        with self.assertRaises(TimelinePreviewContractError) as raised:
            require_timeline_preview_range(
                None,
                source_size=TIMELINE_PREVIEW_MAX_RESPONSE_BYTES + 1,
            )
        self.assertEqual(raised.exception.code, "agent_preview_range_invalid")

    def test_open_ended_ranges_are_bounded_windows_for_large_video(self) -> None:
        source_size = (TIMELINE_PREVIEW_MAX_RESPONSE_BYTES * 2) + 257
        first = require_timeline_preview_range("bytes=0-", source_size=source_size)
        self.assertEqual(
            (first.start, first.end, first.length, first.status_code),
            (0, TIMELINE_PREVIEW_MAX_RESPONSE_BYTES - 1, TIMELINE_PREVIEW_MAX_RESPONSE_BYTES, 206),
        )
        second = require_timeline_preview_range(
            f"bytes={TIMELINE_PREVIEW_MAX_RESPONSE_BYTES}-",
            source_size=source_size,
        )
        self.assertEqual(
            (second.start, second.end, second.length, second.status_code),
            (
                TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
                (TIMELINE_PREVIEW_MAX_RESPONSE_BYTES * 2) - 1,
                TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
                206,
            ),
        )

    def test_range_numeric_input_is_bounded_before_integer_conversion(self) -> None:
        invalid = (
            "bytes=" + ("9" * 21) + "-",
            "bytes=0-" + ("9" * 21),
            "bytes=-" + ("9" * 5000),
        )
        for value in invalid:
            with self.subTest(length=len(value)):
                with self.assertRaises(TimelinePreviewContractError) as raised:
                    require_timeline_preview_range(value, source_size=1024)
                self.assertEqual(raised.exception.code, "agent_preview_range_invalid")


if __name__ == "__main__":
    unittest.main()
