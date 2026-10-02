from __future__ import annotations

import json
import unittest

from flask import Flask, request

from backend.src.api.strict_json import (
    MAX_RAW_BYTES,
    StrictJsonRequestError,
    strict_json_object,
)


class StrictHttpJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)

    def parse(self, payload: bytes, *, content_type: str = "application/json"):
        with self.app.test_request_context(
            "/command",
            method="POST",
            data=payload,
            content_type=content_type,
        ):
            return strict_json_object(request)

    def assert_code(self, payload: bytes, code: str, *, content_type: str = "application/json") -> None:
        with self.assertRaises(StrictJsonRequestError) as captured:
            self.parse(payload, content_type=content_type)
        self.assertEqual(captured.exception.code, code)

    def test_accepts_one_finite_utf8_object(self) -> None:
        self.assertEqual(
            self.parse(' { "semantic": {"title": "雪"}, "revision": 1 } '.encode()),
            {"semantic": {"title": "雪"}, "revision": 1},
        )
        self.assertEqual(
            self.parse(json.dumps({"text": "[{" * 100 + "}]" * 100}).encode()),
            {"text": "[{" * 100 + "}]" * 100},
        )

    def test_transport_envelope_accepts_core_valid_blueprint_budget(self) -> None:
        value = {
            "semantic": {
                "script": ["x" * 10_000 for _ in range(55)],
            },
            "presentation_sha256": "a" * 64,
        }
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertGreater(len(payload), 524_288)
        self.assertLess(len(payload), MAX_RAW_BYTES)
        self.assertEqual(self.parse(payload), value)

    def test_rejects_duplicate_keys_before_domain_dispatch(self) -> None:
        self.assert_code(b'{"revision":1,"revision":2}', "duplicate_json_key")

    def test_rejects_non_finite_numbers_and_overflow(self) -> None:
        self.assert_code(b'{"value":NaN}', "non_finite_json_number")
        self.assert_code(b'{"value":Infinity}', "non_finite_json_number")
        self.assert_code(b'{"value":1e99999}', "non_finite_json_number")
        self.assert_code(
            b'{"value":' + (b"1" * 4_097) + b"}",
            "json_number_too_large",
        )

    def test_rejects_invalid_utf8_surrogates_and_non_object_root(self) -> None:
        self.assert_code(b'{"value":"\xff"}', "invalid_json_utf8")
        self.assert_code(b'{"value":"\\ud800"}', "invalid_json_unicode")
        self.assert_code(b'{"\\ud800":null}', "invalid_json_unicode")
        self.assert_code(b"[]", "json_object_required")

    def test_rejects_transport_depth_and_content_type_boundaries(self) -> None:
        self.assert_code(b"{}", "json_content_type_required", content_type="text/plain")
        self.assert_code(b" " * (MAX_RAW_BYTES + 1), "json_transport_too_large")
        nested = ("[" * 33 + "0" + "]" * 33).encode()
        self.assert_code(b'{"value":' + nested + b"}", "json_too_deep")
        parser_exhaustion = ("[" * 2_000 + "0" + "]" * 2_000).encode()
        self.assert_code(b'{"value":' + parser_exhaustion + b"}", "json_too_deep")


if __name__ == "__main__":
    unittest.main()
