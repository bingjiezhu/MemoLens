from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import unittest

from backend.src.media.provider_egress import (
    ProviderEgressServiceError,
    ProviderSendPermitAuthority,
)


class ProviderSendPermitAuthorityTests(unittest.TestCase):
    def test_issue_is_unique_and_digest_binds_the_process_private_permit(self) -> None:
        authority = ProviderSendPermitAuthority()

        first_permit, first_digest = authority.issue("pmanifest_" + "1" * 32)
        second_permit, second_digest = authority.issue("pmanifest_" + "2" * 32)

        self.assertNotEqual(first_permit, second_permit)
        self.assertNotEqual(first_digest, second_digest)
        self.assertEqual(
            first_digest,
            hashlib.sha256(first_permit.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            second_digest,
            hashlib.sha256(second_permit.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(authority.active_count(), 2)

    def test_wrong_manifest_or_permit_does_not_destroy_the_valid_permit(self) -> None:
        authority = ProviderSendPermitAuthority()
        manifest_id = "pmanifest_" + "3" * 32
        permit, digest = authority.issue(manifest_id)

        with self.assertRaisesRegex(
            ProviderEgressServiceError,
            "provider_send_permit_unavailable",
        ):
            authority.consume("pmanifest_" + "4" * 32, permit)
        with self.assertRaisesRegex(
            ProviderEgressServiceError,
            "provider_send_permit_unavailable",
        ):
            authority.consume(manifest_id, "wrong-process-private-permit")

        self.assertEqual(authority.active_count(), 1)
        self.assertEqual(authority.consume(manifest_id, permit), digest)
        self.assertEqual(authority.active_count(), 0)

    def test_replay_is_rejected_after_the_exact_first_consume(self) -> None:
        authority = ProviderSendPermitAuthority()
        manifest_id = "pmanifest_" + "5" * 32
        permit, _ = authority.issue(manifest_id)

        authority.consume(manifest_id, permit)

        with self.assertRaisesRegex(
            ProviderEgressServiceError,
            "provider_send_permit_unavailable",
        ):
            authority.consume(manifest_id, permit)
        self.assertFalse(authority.discard(manifest_id, permit))

    def test_only_one_concurrent_consumer_can_cross_the_send_boundary(self) -> None:
        authority = ProviderSendPermitAuthority()
        manifest_id = "pmanifest_" + "6" * 32
        permit, digest = authority.issue(manifest_id)
        barrier = threading.Barrier(8)

        def consume_once(_: int) -> str:
            barrier.wait()
            try:
                return authority.consume(manifest_id, permit)
            except ProviderEgressServiceError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(consume_once, range(8)))

        self.assertEqual(outcomes.count(digest), 1)
        self.assertEqual(outcomes.count("provider_send_permit_unavailable"), 7)
        self.assertEqual(authority.active_count(), 0)

    def test_invalid_issue_and_discard_are_closed(self) -> None:
        authority = ProviderSendPermitAuthority()
        with self.assertRaisesRegex(
            ProviderEgressServiceError,
            "provider_egress_manifest_invalid",
        ):
            authority.issue("")
        self.assertFalse(authority.discard("pmanifest_" + "7" * 32, object()))  # type: ignore[arg-type]
        self.assertEqual(authority.active_count(), 0)


if __name__ == "__main__":
    unittest.main()
