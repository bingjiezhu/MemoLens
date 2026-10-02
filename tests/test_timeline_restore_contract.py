from __future__ import annotations

import copy
import unittest

from core.timeline_edit_contract import apply_timeline_edit
from core.timeline_lowering_contract import (
    canonical_timeline_clip_id,
    canonical_timeline_content_sha256,
    inspect_canonical_timeline_for_read_only,
    validate_canonical_timeline,
    validate_timeline_source_bindings,
)
from core.timeline_restore_contract import (
    TimelineRestoreContractError,
    require_exact_timeline_restore_replay,
    restore_canonical_timeline,
)
from tests.test_timeline_edit_contract import _clip, _fixture


class TimelineRestoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target, _blueprint, self.plan = _fixture()
        image = _clip(self.target, "image")
        self.current = apply_timeline_edit(
            parent_timeline=self.target["timeline"],
            parent_source_bindings=self.target["source_bindings"],
            coverage_plan=self.plan,
            edit={
                "op": "set_clip_duration",
                "clip_id": image["clip_id"],
                "duration_ms": 1_250,
            },
        )

    def restore(self) -> dict[str, object]:
        return restore_canonical_timeline(
            current_timeline=self.current["timeline"],
            current_source_bindings=self.current["source_bindings"],
            restore_timeline=self.target["timeline"],
            restore_source_bindings=self.target["source_bindings"],
        )

    def assert_error(self, code: str, callback) -> None:
        with self.assertRaises(TimelineRestoreContractError) as raised:
            callback()
        self.assertEqual(raised.exception.errors[0]["code"], code)

    def test_restore_reenvelopes_only_revision_and_parent(self) -> None:
        frozen_target = copy.deepcopy(self.target)
        frozen_current = copy.deepcopy(self.current)
        restored = self.restore()
        expected = copy.deepcopy(self.target["timeline"])
        expected["revision"] = 3
        expected["parent"] = {
            "revision": 2,
            "content_sha256": canonical_timeline_content_sha256(
                self.current["timeline"]
            ),
        }

        self.assertEqual(restored["timeline"], expected)
        self.assertEqual(restored["source_bindings"], self.target["source_bindings"])
        self.assertNotEqual(
            canonical_timeline_content_sha256(restored["timeline"]),
            canonical_timeline_content_sha256(self.target["timeline"]),
        )
        self.assertEqual(validate_canonical_timeline(restored["timeline"]), [])
        self.assertEqual(
            validate_timeline_source_bindings(
                restored["source_bindings"],
                timeline=restored["timeline"],
            ),
            [],
        )
        self.assertEqual(self.target, frozen_target)
        self.assertEqual(self.current, frozen_current)

    def test_restore_rejects_current_or_cross_upstream_target(self) -> None:
        self.assert_error(
            "timeline_restore_target_invalid",
            lambda: restore_canonical_timeline(
                current_timeline=self.current["timeline"],
                current_source_bindings=self.current["source_bindings"],
                restore_timeline=self.current["timeline"],
                restore_source_bindings=self.current["source_bindings"],
            ),
        )
        mismatched = copy.deepcopy(self.target)
        mismatched["timeline"]["coverage_binding"]["content_sha256"] = "f" * 64
        self.assert_error(
            "timeline_restore_binding_mismatch",
            lambda: restore_canonical_timeline(
                current_timeline=self.current["timeline"],
                current_source_bindings=self.current["source_bindings"],
                restore_timeline=mismatched["timeline"],
                restore_source_bindings=mismatched["source_bindings"],
            ),
        )

    def test_invalid_source_manifest_and_replay_drift_fail_closed(self) -> None:
        self.assert_error(
            "timeline_restore_target_invalid",
            lambda: restore_canonical_timeline(
                current_timeline=self.current["timeline"],
                current_source_bindings=self.current["source_bindings"],
                restore_timeline=self.target["timeline"],
                restore_source_bindings=self.target["source_bindings"][:-1],
            ),
        )
        restored = self.restore()
        require_exact_timeline_restore_replay(
            restored["timeline"],
            source_bindings=restored["source_bindings"],
            current_timeline=self.current["timeline"],
            current_source_bindings=self.current["source_bindings"],
            restore_timeline=self.target["timeline"],
            restore_source_bindings=self.target["source_bindings"],
        )
        drifted_sources = copy.deepcopy(restored["source_bindings"])
        drifted_sources[0]["asset_source_id"] = "src_" + "9" * 24
        self.assert_error(
            "timeline_restore_replay_mismatch",
            lambda: require_exact_timeline_restore_replay(
                restored["timeline"],
                source_bindings=drifted_sources,
                current_timeline=self.current["timeline"],
                current_source_bindings=self.current["source_bindings"],
                restore_timeline=self.target["timeline"],
                restore_source_bindings=self.target["source_bindings"],
            ),
        )

    def test_legacy_identity_only_target_is_readable_but_cannot_be_restored(self) -> None:
        legacy = copy.deepcopy(self.target)
        image = _clip(legacy, "image")
        source = next(
            row for row in legacy["source_bindings"] if row["media_kind"] == "image"
        )
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            image.pop(field)
            source.pop(field)
        image["clip_id"] = canonical_timeline_clip_id(
            timeline_id=legacy["timeline"]["timeline_id"],
            beat_id=image["beat_id"],
            assignment_id=image["assignment_id"],
            evidence_ref=image["evidence_ref"],
            asset_id=image["asset_id"],
            asset_sha256=image["asset_sha256"],
            media_kind="image",
        )
        source["clip_id"] = image["clip_id"]

        self.assertEqual(validate_canonical_timeline(legacy["timeline"]), [])
        self.assertEqual(
            inspect_canonical_timeline_for_read_only(legacy["timeline"])[
                "evidence_currency"
            ],
            "legacy_non_current",
        )
        self.assert_error(
            "timeline_restore_target_invalid",
            lambda: restore_canonical_timeline(
                current_timeline=self.current["timeline"],
                current_source_bindings=self.current["source_bindings"],
                restore_timeline=legacy["timeline"],
                restore_source_bindings=legacy["source_bindings"],
            ),
        )


if __name__ == "__main__":
    unittest.main()
