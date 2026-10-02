from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from core.residual_identity_contract import (
    ResidualIdentityContractError,
    build_residual_binding,
    canonical_usage_projection_sha256,
    require_unique_residual_bindings,
    require_valid_residual_binding,
    residual_binding_sha256,
    source_binding_sha256,
)


ASSET_SHA = "a" * 64
ASSET_ID = f"asset_{ASSET_SHA[:24]}"
SOURCE_ID = f"src_{'b' * 24}"
RUN_ID = f"arun_{'c' * 32}"
PARENT_ID = f"seg_{ASSET_SHA[:24]}_1_0"
USAGE_REVISION = "d" * 64


def usage_projection() -> dict[str, object]:
    return {
        "asset_id": ASSET_ID,
        "media_kind": "video",
        "occurrence_count": 1,
        "used": True,
        "used_in": [{"project_id": "project_alpha", "export_revision": 1}],
        "source_domain": [0, 60_000],
        "candidate_domain": [0, 60_000],
        "used_intervals": [[10_000, 14_000]],
        "residual_intervals": [[0, 10_000], [14_000, 60_000]],
        "fully_used": False,
        "has_residual": True,
    }


def source_facts() -> dict[str, object]:
    return {
        "asset_source_id": SOURCE_ID,
        "asset_id": ASSET_ID,
        "asset_sha256": ASSET_SHA,
        "library_root_id": "root_alpha",
        "observed_size": 123_456,
        "observed_mtime_ns": 987_654_321,
        "source_file_id": "file-alpha",
    }


def binding_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "parent_segment_id": PARENT_ID,
        "asset_id": ASSET_ID,
        "asset_sha256": ASSET_SHA,
        "asset_source_id": SOURCE_ID,
        "source_binding_sha256": source_binding_sha256(source_facts()),
        "analysis_run_id": RUN_ID,
        "analysis_revision": 1,
        "input_asset_sha256": ASSET_SHA,
        "parent_start_ms": 0,
        "parent_end_ms": 60_000,
        "source_in_ms": 0,
        "source_out_ms": 10_000,
        "usage_revision": USAGE_REVISION,
        "parent_usage_projection_sha256": canonical_usage_projection_sha256(
            usage_projection()
        ),
    }
    values.update(overrides)
    return values


class ResidualIdentityContractTests(unittest.TestCase):
    def test_known_vector_round_trip_is_process_and_key_order_stable(self) -> None:
        self.assertEqual(
            source_binding_sha256(source_facts()),
            "f9c22c3de3cc956c06d72b6f632bbc6c369b781534ef592545d6a75b0e2d7dcf",
        )
        self.assertEqual(
            canonical_usage_projection_sha256(usage_projection()),
            "06c88d34519a2aa95a51d9a4239d3a6904a05aa51d5f42b9968efa73ca1f7a24",
        )
        binding = build_residual_binding(**binding_kwargs())
        self.assertEqual(
            binding["residual_id"],
            "rseg_4dc2f2577b0008f345a4c5269fa36c8e663c2d57e1915d286b5e37faf97599e1",
        )
        self.assertEqual(
            require_valid_residual_binding(dict(reversed(list(binding.items())))),
            binding,
        )
        self.assertRegex(residual_binding_sha256(binding), r"^[0-9a-f]{64}$")

    def test_every_identity_dimension_changes_the_residual_id(self) -> None:
        baseline = build_residual_binding(**binding_kwargs())
        alternatives = [
            binding_kwargs(asset_source_id=f"src_{'e' * 24}"),
            binding_kwargs(source_binding_sha256="e" * 64),
            binding_kwargs(analysis_run_id=f"arun_{'e' * 32}"),
            binding_kwargs(
                parent_segment_id=f"seg_{ASSET_SHA[:24]}_2_0",
                analysis_revision=2,
            ),
            binding_kwargs(parent_segment_id=f"seg_{ASSET_SHA[:24]}_1_1"),
            binding_kwargs(parent_end_ms=61_000),
            binding_kwargs(source_out_ms=9_999),
            binding_kwargs(usage_revision="e" * 64),
            binding_kwargs(parent_usage_projection_sha256="e" * 64),
        ]
        ids = {str(baseline["residual_id"])}
        for values in alternatives:
            ids.add(str(build_residual_binding(**values)["residual_id"]))
        self.assertEqual(len(ids), len(alternatives) + 1)

    def test_asset_identity_changes_as_one_closed_content_addressed_fact(self) -> None:
        alternate_sha = "e" * 64
        alternate = build_residual_binding(
            **binding_kwargs(
                asset_id=f"asset_{alternate_sha[:24]}",
                asset_sha256=alternate_sha,
                input_asset_sha256=alternate_sha,
                parent_segment_id=f"seg_{alternate_sha[:24]}_1_0",
            )
        )
        self.assertNotEqual(
            alternate["residual_id"],
            build_residual_binding(**binding_kwargs())["residual_id"],
        )

    def test_closed_binding_rejects_substitution_bounds_and_bool_as_int(self) -> None:
        valid = build_residual_binding(**binding_kwargs())
        invalid_values: list[dict[str, object]] = []
        missing = deepcopy(valid)
        missing.pop("usage_revision")
        invalid_values.append(missing)
        extra = deepcopy(valid)
        extra["relative_path"] = "renamed/final.mp4"
        invalid_values.append(extra)
        for field, replacement in (
            ("analysis_revision", True),
            ("asset_sha256", ASSET_SHA.upper()),
            ("input_asset_sha256", "e" * 64),
            ("parent_segment_id", f"seg_{ASSET_SHA[:24]}_2_0"),
            ("source_in_ms", -1),
            ("source_out_ms", 60_001),
            ("residual_id", f"rseg_{'0' * 64}"),
        ):
            mutated = deepcopy(valid)
            mutated[field] = replacement
            invalid_values.append(mutated)
        whole_parent = deepcopy(valid)
        whole_parent["source_out_ms"] = whole_parent["parent_end_ms"]
        whole_parent["residual_id"] = f"rseg_{'0' * 64}"
        invalid_values.append(whole_parent)
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ResidualIdentityContractError):
                    require_valid_residual_binding(value)

    def test_source_binding_is_path_free_closed_and_content_addressed(self) -> None:
        baseline = source_binding_sha256(source_facts())
        renamed = source_facts()
        renamed["display_filename"] = "renamed-final.mp4"
        with self.assertRaises(ResidualIdentityContractError) as extra:
            source_binding_sha256(renamed)
        self.assertEqual(extra.exception.code, "source_binding_invalid")
        changed_source = source_facts()
        changed_source["asset_source_id"] = f"src_{'e' * 24}"
        self.assertNotEqual(source_binding_sha256(changed_source), baseline)

    def test_usage_projection_is_closed_and_exactly_partitions_parent(self) -> None:
        valid = usage_projection()
        self.assertRegex(canonical_usage_projection_sha256(valid), r"^[0-9a-f]{64}$")
        invalid_values: list[dict[str, object]] = []
        extra = deepcopy(valid)
        extra["score"] = 0.99
        invalid_values.append(extra)
        overlapping = deepcopy(valid)
        overlapping["residual_intervals"] = [[0, 11_000], [14_000, 60_000]]
        invalid_values.append(overlapping)
        gap = deepcopy(valid)
        gap["residual_intervals"] = [[0, 9_000], [14_000, 60_000]]
        invalid_values.append(gap)
        bool_count = deepcopy(valid)
        bool_count["occurrence_count"] = True
        invalid_values.append(bool_count)
        inconsistent = deepcopy(valid)
        inconsistent["has_residual"] = False
        invalid_values.append(inconsistent)
        unsorted = deepcopy(valid)
        unsorted["used_in"] = [
            {"project_id": "project_z", "export_revision": 2},
            {"project_id": "project_a", "export_revision": 1},
        ]
        invalid_values.append(unsorted)
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ResidualIdentityContractError) as error:
                    canonical_usage_projection_sha256(value)
                self.assertEqual(error.exception.code, "usage_projection_invalid")

    def test_distinct_preimages_with_forced_hash_collision_fail_closed(self) -> None:
        with patch(
            "core.residual_identity_contract.canonical_sha256",
            return_value="f" * 64,
        ):
            left = build_residual_binding(**binding_kwargs())
            right = build_residual_binding(
                **binding_kwargs(source_in_ms=14_000, source_out_ms=60_000)
            )
            self.assertEqual(left["residual_id"], right["residual_id"])
            with self.assertRaises(ResidualIdentityContractError) as error:
                require_unique_residual_bindings([left, right])
        self.assertEqual(error.exception.code, "residual_identity_collision")


if __name__ == "__main__":
    unittest.main()
