from __future__ import annotations

import copy
import unittest
from unittest import mock

from core.timeline_edit_contract import apply_timeline_edit
from core.timeline_lowering_contract import (
    canonical_timeline_content_sha256,
    validate_canonical_timeline,
    validate_timeline_source_bindings,
)
from core.timeline_restore_contract import restore_canonical_timeline
from core.timeline_structural_edit_contract import (
    TimelineStructuralEditContractError,
    apply_timeline_structural_edit,
    require_exact_timeline_structural_edit_replay,
)
from tests.test_timeline_edit_contract import _fixture


def _clip(result: dict[str, object], media_kind: str) -> dict[str, object]:
    return next(
        clip
        for clip in result["timeline"]["tracks"][0]["clips"]
        if clip["media_kind"] == media_kind
    )


class TimelineStructuralEditContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result, _blueprint, self.plan = _fixture()
        self.image = _clip(self.result, "image")
        self.video = _clip(self.result, "video")

    def apply(
        self,
        structural_edit: object,
        *,
        result: dict[str, object] | None = None,
    ) -> dict[str, object]:
        current = result or self.result
        return apply_timeline_structural_edit(
            parent_timeline=current["timeline"],
            parent_source_bindings=current["source_bindings"],
            coverage_plan=self.plan,
            edit=structural_edit,
        )

    def assert_error(self, code: str, callback) -> None:
        with self.assertRaises(TimelineStructuralEditContractError) as raised:
            callback()
        self.assertEqual(raised.exception.errors[0]["code"], code)

    def test_split_upgrades_to_v2_with_exact_stable_children_and_no_parent_mutation(self) -> None:
        frozen_parent = copy.deepcopy(self.result)
        edit = {
            "op": "split_clip",
            "clip_id": self.video["clip_id"],
            "source_split_ms": 3_000,
        }
        first = self.apply(edit)
        replayed = self.apply(edit)
        timeline = first["timeline"]
        clips = timeline["tracks"][0]["clips"]
        children = [clip for clip in clips if clip["media_kind"] == "video"]

        self.assertEqual(self.result, frozen_parent)
        self.assertEqual(first, replayed)
        self.assertEqual(timeline["schema_version"], "2")
        self.assertEqual(timeline["revision"], 2)
        self.assertEqual(
            timeline["parent"],
            {
                "revision": 1,
                "content_sha256": canonical_timeline_content_sha256(
                    self.result["timeline"]
                ),
            },
        )
        self.assertEqual(len(clips), 3)
        self.assertEqual(
            [(child["source_in_ms"], child["source_out_ms"]) for child in children],
            [(1_000, 3_000), (3_000, 5_000)],
        )
        self.assertEqual(len({child["clip_id"] for child in children}), 2)
        self.assertEqual(len({child["assignment_id"] for child in children}), 1)
        self.assertEqual(len({child["evidence_ref"] for child in children}), 1)
        self.assertEqual(timeline["output"]["duration_ms"], 8_000)
        self.assertEqual(validate_canonical_timeline(timeline), [])
        self.assertEqual(
            validate_timeline_source_bindings(
                first["source_bindings"],
                timeline=timeline,
            ),
            [],
        )

    def test_delete_removes_only_occurrence_reflows_and_never_deletes_source(self) -> None:
        deleted = self.apply(
            {"op": "delete_clip", "clip_id": self.video["clip_id"]}
        )
        timeline = deleted["timeline"]
        clips = timeline["tracks"][0]["clips"]

        self.assertEqual(timeline["schema_version"], "2")
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["clip_id"], self.image["clip_id"])
        self.assertEqual(timeline["output"]["duration_ms"], 4_000)
        self.assertEqual(len(deleted["source_bindings"]), 1)
        self.assertEqual(
            deleted["source_bindings"][0]["asset_source_id"],
            self.result["source_bindings"][0]["asset_source_id"],
        )
        self.assertEqual(validate_canonical_timeline(timeline), [])

    def test_split_identity_collisions_fail_before_reflow_without_parent_mutation(
        self,
    ) -> None:
        edit = {
            "op": "split_clip",
            "clip_id": self.video["clip_id"],
            "source_split_ms": 3_000,
        }
        frozen_parent = copy.deepcopy(self.result)
        collision_cases = (
            (
                "children-share-one-id",
                ["clip_forced_collision", "clip_forced_collision"],
            ),
            (
                "child-collides-with-untouched-clip",
                [self.image["clip_id"], "clip_forced_unique_child"],
            ),
            (
                "child-reuses-split-parent-id",
                [self.video["clip_id"], "clip_forced_unique_child"],
            ),
        )

        for label, stable_ids in collision_cases:
            with self.subTest(collision=label), mock.patch(
                "core.timeline_structural_edit_contract._stable_clip_id",
                side_effect=stable_ids,
            ) as stable_id, mock.patch(
                "core.timeline_structural_edit_contract._reflow",
                side_effect=AssertionError("collision must fail before reflow"),
            ) as reflow:
                with self.assertRaises(TimelineStructuralEditContractError) as raised:
                    self.apply(edit)
                self.assertEqual(
                    raised.exception.errors[0],
                    {
                        "code": "split_identity_collision",
                        "path": "/edit/source_split_ms",
                        "message": (
                            "Split children must have distinct stable identities "
                            "absent from the parent."
                        ),
                    },
                )
                self.assertEqual(stable_id.call_count, 2)
                reflow.assert_not_called()
                self.assertEqual(self.result, frozen_parent)

    def test_structural_v2_remains_editable_and_v1_v2_history_is_restorable(self) -> None:
        split = self.apply(
            {
                "op": "split_clip",
                "clip_id": self.video["clip_id"],
                "source_split_ms": 3_000,
            }
        )
        moved = apply_timeline_edit(
            parent_timeline=split["timeline"],
            parent_source_bindings=split["source_bindings"],
            coverage_plan=self.plan,
            edit={
                "op": "move_clip",
                "clip_id": split["timeline"]["tracks"][0]["clips"][2]["clip_id"],
                "to_index": 1,
            },
        )
        self.assertEqual(moved["timeline"]["schema_version"], "2")
        restored = restore_canonical_timeline(
            current_timeline=moved["timeline"],
            current_source_bindings=moved["source_bindings"],
            restore_timeline=self.result["timeline"],
            restore_source_bindings=self.result["source_bindings"],
        )
        self.assertEqual(restored["timeline"]["schema_version"], "1")
        self.assertEqual(restored["timeline"]["revision"], 4)
        self.assertEqual(validate_canonical_timeline(restored["timeline"]), [])

    def test_rejects_legacy_dialect_wrong_targets_unsafe_cuts_and_last_delete(self) -> None:
        for invalid in (
            {"op": "remove_clip", "clip_id": self.video["clip_id"]},
            {"op": "delete_clip", "clip_id": self.video["clip_id"], "offset_ms": 1},
            {
                "op": "split_clip",
                "clip_id": self.video["clip_id"],
                "source_split_ms": 3_000,
                "operations": [],
            },
        ):
            self.assert_error(
                "unsupported_structural_edit"
                if invalid["op"] == "remove_clip"
                else "invalid_structural_edit",
                lambda invalid=invalid: self.apply(invalid),
            )
        self.assert_error(
            "invalid_split_target",
            lambda: self.apply(
                {
                    "op": "split_clip",
                    "clip_id": self.image["clip_id"],
                    "source_split_ms": 2_000,
                }
            ),
        )
        for split_ms in (1_000, 1_099, 4_901, 5_000, True, 3_000.0):
            expected = (
                "invalid_source_split"
                if type(split_ms) is not int
                else "split_child_too_short"
            )
            self.assert_error(
                expected,
                lambda split_ms=split_ms: self.apply(
                    {
                        "op": "split_clip",
                        "clip_id": self.video["clip_id"],
                        "source_split_ms": split_ms,
                    }
                ),
            )
        one_clip = self.apply(
            {"op": "delete_clip", "clip_id": self.video["clip_id"]}
        )
        self.assert_error(
            "cannot_delete_last_clip",
            lambda: self.apply(
                {"op": "delete_clip", "clip_id": self.image["clip_id"]},
                result=one_clip,
            ),
        )

    def test_clip_limit_unknown_clip_and_exact_replay_tamper_fail_closed(self) -> None:
        self.assert_error(
            "unknown_clip",
            lambda: self.apply(
                {"op": "delete_clip", "clip_id": "clip_missing"}
            ),
        )
        with mock.patch(
            "core.timeline_structural_edit_contract.TIMELINE_MAX_CLIPS",
            2,
        ):
            self.assert_error(
                "timeline_clip_limit_exceeded",
                lambda: self.apply(
                    {
                        "op": "split_clip",
                        "clip_id": self.video["clip_id"],
                        "source_split_ms": 3_000,
                    }
                ),
            )

        edit = {
            "op": "split_clip",
            "clip_id": self.video["clip_id"],
            "source_split_ms": 3_000,
        }
        split = self.apply(edit)
        self.assertEqual(
            require_exact_timeline_structural_edit_replay(
                split["timeline"],
                source_bindings=split["source_bindings"],
                parent_timeline=self.result["timeline"],
                parent_source_bindings=self.result["source_bindings"],
                coverage_plan=self.plan,
                edit=edit,
            ),
            split["timeline"],
        )
        tampered = copy.deepcopy(split["timeline"])
        tampered["tracks"][0]["clips"][1]["source_out_ms"] = 2_999
        self.assert_error(
            "timeline_structural_edit_replay_mismatch",
            lambda: require_exact_timeline_structural_edit_replay(
                tampered,
                source_bindings=split["source_bindings"],
                parent_timeline=self.result["timeline"],
                parent_source_bindings=self.result["source_bindings"],
                coverage_plan=self.plan,
                edit=edit,
            ),
        )


if __name__ == "__main__":
    unittest.main()
