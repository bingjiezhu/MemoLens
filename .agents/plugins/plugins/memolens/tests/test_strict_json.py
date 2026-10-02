from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_strict_json import (  # noqa: E402
    StrictJsonError,
    StrictJsonLimits,
    decode_strict_json,
    validate_json_value,
)


class StrictJsonDecodeTests(unittest.TestCase):
    def test_decodes_utf8_object_without_mutation(self) -> None:
        self.assertEqual(
            decode_strict_json(b'{"title":"\xe8\xa7\x86\xe9\xa2\x91","items":[1,true,null]}'),
            {"title": "\u89c6\u9891", "items": [1, True, None]},
        )

    def test_object_root_is_required_by_default_and_can_be_disabled(self) -> None:
        with self.assertRaises(StrictJsonError) as captured:
            decode_strict_json("[1,2]")
        self.assertEqual(captured.exception.code, "root_not_object")
        self.assertEqual(
            decode_strict_json("[1,2]", require_object=False),
            [1, 2],
        )

    def test_duplicate_keys_are_rejected_at_any_depth(self) -> None:
        for payload in ('{"same":1,"same":2}', '{"nested":{"same":1,"same":2}}'):
            with self.subTest(payload=payload), self.assertRaises(
                StrictJsonError
            ) as captured:
                decode_strict_json(payload)
            self.assertEqual(captured.exception.code, "duplicate_key")
            self.assertEqual(captured.exception.path, "$")

    def test_nonstandard_and_overflowed_numbers_are_rejected(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity", "1e9999", "-1e9999"):
            with self.subTest(token=token), self.assertRaises(
                StrictJsonError
            ) as captured:
                decode_strict_json(f'{{"number":{token}}}')
            self.assertEqual(captured.exception.code, "non_finite_number")

    def test_invalid_utf8_and_unpaired_surrogates_are_rejected(self) -> None:
        for payload in (
            b'{"x":"\xff"}',
            '{"x":"\ud800"}',
            '{"x":"\\ud800"}',
            '{"\\ud800":1}',
        ):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(
                StrictJsonError
            ) as captured:
                decode_strict_json(payload)
            self.assertEqual(captured.exception.code, "invalid_utf8")

    def test_malformed_json_has_a_fixed_non_reflective_error(self) -> None:
        secret = "private-prompt-do-not-reflect"
        with self.assertRaises(StrictJsonError) as captured:
            decode_strict_json(f'{{"{secret}":]')
        error = captured.exception
        self.assertEqual(
            error.as_dict(),
            {"code": "invalid_json", "path": "$", "message": "Input must be valid JSON."},
        )
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error.as_dict()))

    def test_rejects_wrong_input_type(self) -> None:
        with self.assertRaises(StrictJsonError) as captured:
            decode_strict_json(memoryview(b"{}"))  # type: ignore[arg-type]
        self.assertEqual(captured.exception.code, "invalid_input_type")


class StrictJsonResourceLimitTests(unittest.TestCase):
    def test_byte_limit_counts_encoded_utf8_bytes(self) -> None:
        limits = StrictJsonLimits(max_bytes=6)
        self.assertEqual(decode_strict_json("{}", limits=limits), {})
        for payload in ('{"x":"\u89c6"}', b'{"x":1}'):
            with self.subTest(payload=type(payload).__name__), self.assertRaises(
                StrictJsonError
            ) as captured:
                decode_strict_json(payload, limits=limits)
            self.assertEqual(captured.exception.code, "input_too_large")

    def test_depth_limit_is_applied_before_recursive_decode(self) -> None:
        limits = StrictJsonLimits(max_depth=2)
        self.assertEqual(decode_strict_json('{"ok":{}}', limits=limits), {"ok": {}})
        with self.assertRaises(StrictJsonError) as captured:
            decode_strict_json('{"deep":{"deeper":[]}}', limits=limits)
        self.assertEqual(captured.exception.code, "max_depth_exceeded")

    def test_depth_precheck_ignores_brackets_inside_strings(self) -> None:
        limits = StrictJsonLimits(max_depth=1)
        self.assertEqual(
            decode_strict_json('{"text":"[[[{{{\\\""}', limits=limits),
            {"text": '[[[{{{"'},
        )

    def test_node_object_array_and_string_limits(self) -> None:
        cases = (
            (
                '{"a":1,"b":2}',
                StrictJsonLimits(max_nodes=2),
                "max_nodes_exceeded",
            ),
            (
                '{"a":1,"b":2}',
                StrictJsonLimits(max_object_items=1),
                "max_object_items_exceeded",
            ),
            (
                '{"a":[1,2]}',
                StrictJsonLimits(max_array_items=1),
                "max_array_items_exceeded",
            ),
            (
                '{"a":"long"}',
                StrictJsonLimits(max_string_chars=3),
                "max_string_length_exceeded",
            ),
            (
                '{"long":1}',
                StrictJsonLimits(max_string_chars=3),
                "max_string_length_exceeded",
            ),
        )
        for payload, limits, code in cases:
            with self.subTest(code=code), self.assertRaises(
                StrictJsonError
            ) as captured:
                decode_strict_json(payload, limits=limits)
            self.assertEqual(captured.exception.code, code)

    def test_limits_must_be_positive_plain_integers(self) -> None:
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StrictJsonLimits(max_depth=value)  # type: ignore[arg-type]


class StrictPythonValueTests(unittest.TestCase):
    def test_returns_the_same_valid_direct_object(self) -> None:
        value = {"b": [1, 2], "a": {"finite": 1.5}}
        self.assertIs(validate_json_value(value), value)

    def test_shared_subtree_is_allowed_but_cycles_are_rejected(self) -> None:
        shared = [1]
        value = {"left": shared, "right": shared}
        self.assertIs(validate_json_value(value), value)

        cyclic: dict[str, object] = {}
        cyclic["cycle"] = cyclic
        with self.assertRaises(StrictJsonError) as captured:
            validate_json_value(cyclic)
        self.assertEqual(captured.exception.code, "cyclic_reference")

    def test_non_string_keys_are_rejected_without_reflection(self) -> None:
        secret_key = ("private", "/Users/example/private.mov")
        with self.assertRaises(StrictJsonError) as captured:
            validate_json_value({secret_key: "secret-value"})  # type: ignore[dict-item]
        error = captured.exception
        self.assertEqual(error.code, "non_string_key")
        self.assertEqual(set(error.as_dict()), {"code", "path", "message"})
        rendered = repr(error.as_dict())
        self.assertNotIn("private.mov", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_direct_nonfinite_values_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(
                StrictJsonError
            ) as captured:
                validate_json_value({"number": value})
            self.assertEqual(captured.exception.code, "non_finite_number")

    def test_direct_unpaired_surrogates_are_rejected(self) -> None:
        for value in ({"text": "\ud800"}, {"\ud800": "text"}):
            with self.subTest(value_kind="key" if "text" not in value else "value"), self.assertRaises(
                StrictJsonError
            ) as captured:
                validate_json_value(value)
            self.assertEqual(captured.exception.code, "invalid_utf8")

    def test_direct_resource_limits_are_enforced(self) -> None:
        cases = (
            ({"a": {"b": {}}}, StrictJsonLimits(max_depth=2), "max_depth_exceeded"),
            ({"a": 1, "b": 2}, StrictJsonLimits(max_nodes=2), "max_nodes_exceeded"),
            ({"a": 1, "b": 2}, StrictJsonLimits(max_object_items=1), "max_object_items_exceeded"),
            ({"a": [1, 2]}, StrictJsonLimits(max_array_items=1), "max_array_items_exceeded"),
            ({"a": "long"}, StrictJsonLimits(max_string_chars=3), "max_string_length_exceeded"),
        )
        for value, limits, code in cases:
            with self.subTest(code=code), self.assertRaises(
                StrictJsonError
            ) as captured:
                validate_json_value(value, limits=limits)
            self.assertEqual(captured.exception.code, code)

    def test_direct_non_json_types_and_non_object_roots_are_rejected(self) -> None:
        with self.assertRaises(StrictJsonError) as captured:
            validate_json_value({"tuple": (1, 2)})
        self.assertEqual(captured.exception.code, "unsupported_type")

        with self.assertRaises(StrictJsonError) as captured:
            validate_json_value([], require_object=True)
        self.assertEqual(captured.exception.code, "root_not_object")


if __name__ == "__main__":
    unittest.main()
