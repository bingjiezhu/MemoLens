from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from backend.src.media.library_scan import LibraryScanRunner, discover_library_scan_batch
from backend.src.media.video import MediaJobRunner
from core.db import ImageIndexRepository
from core.media_db import MediaRepository


GENERATION = f"runtime_generation_{'d' * 64}"


class MediaJobSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-job-scheduling-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "media.db"
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(None)
        descriptor = os.open(self.library, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        identity = os.fstat(descriptor)
        now_ms = int(time.time() * 1000)
        try:
            self.repository.commit_library_bootstrap(
                request_id=f"lb_{'a' * 64}",
                intent_created_at_ms=now_ms - 1_000,
                intent_expires_at_ms=now_ms + 299_000,
                native_confirmed_at_ms=now_ms,
                candidate_binding_sha256=self.repository.library_bootstrap_candidate_binding_sha256(
                    canonical_root=self.library,
                    expected_library_root_device=int(identity.st_dev),
                    expected_library_root_inode=int(identity.st_ino),
                ),
                library_root_path=self.library,
                library_root_fd=descriptor,
                expected_library_root_device=int(identity.st_dev),
                expected_library_root_inode=int(identity.st_ino),
            )
        finally:
            os.close(descriptor)
        scan = self.repository.list_recoverable_library_scans()[0]
        self.scan_id = str(scan["job_id"])
        self.root_id = str(scan["library_root_id"])
        self.runner = MediaJobRunner(
            self.repository,
            self.root / "cache",
            image_embedding_service=SimpleNamespace(
                backend="semantic_hash", semantic_vector_dimensions=8,
            ),
        )
        self.runner.activate_runtime_generation(GENERATION, active_library_root_id=self.root_id)

    def tearDown(self) -> None:
        self.runner.shutdown()
        self.repository.close()
        self.temporary.cleanup()

    def test_first_child_finishes_while_scan_is_blocked_before_second_batch(self) -> None:
        for index in range(17):
            Image.new("RGB", (24, 12), (index, 0, 100)).save(self.library / f"still-{index:02d}.png")
        scan_blocked = threading.Event()
        release_scan = threading.Event()
        child_finished = threading.Event()
        child_ids: list[str] = []
        workers: set[threading.Thread] = set()
        process_image = self.runner._image_processor.run

        def gated_discovery(root, *, last_relative_path, **kwargs):
            workers.add(threading.current_thread())
            if last_relative_path is not None:
                scan_blocked.set()
                if not release_scan.wait(15):
                    raise RuntimeError("test scan barrier timed out")
            return discover_library_scan_batch(root, last_relative_path=last_relative_path, **kwargs)

        def observe_image(job_id, **kwargs):
            workers.add(threading.current_thread())
            process_image(job_id, **kwargs)
            child_ids.append(job_id)
            child_finished.set()

        with (
            patch("backend.src.media.library_scan.discover_library_scan_batch", side_effect=gated_discovery),
            patch.object(self.runner._image_processor, "run", side_effect=observe_image),
        ):
            try:
                self.runner.submit(self.scan_id)
                self.assertTrue(scan_blocked.wait(10), "scan never committed its first batch")
                self.assertTrue(child_finished.wait(3), "child is starved behind the unfinished scan")
                scan = self.repository.get_media_job(self.scan_id)
                self.assertEqual(scan["status"], "running")
                self.assertEqual(scan["checkpoint"]["processed_count"], 16)
                child = self.repository.get_media_job(child_ids[0])
                self.assertEqual((child["status"], child["stage"]), ("succeeded", "completed"))
                current = self.repository.get_current_image_analysis(str(child["asset_id"]))
                self.assertEqual(current["projection"]["status"], "current")
            finally:
                release_scan.set()
                self.runner.shutdown()
        self.assertEqual(len(workers), 2)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(self.runner._submitted, set())

    def test_duplicate_scan_submission_keeps_one_worker_for_the_same_id(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def block_scan(job_id, **_kwargs):
            calls.append(job_id)
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test dispatch barrier timed out")

        with patch.object(self.runner._library_scan_processor, "run", side_effect=block_scan):
            try:
                self.runner.submit(self.scan_id)
                self.assertTrue(entered.wait(3))
                submitters = [threading.Thread(target=self.runner.submit, args=(self.scan_id,)) for _ in range(8)]
                for thread in submitters:
                    thread.start()
                for thread in submitters:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
            finally:
                release.set()
                self.runner.shutdown()
        self.assertEqual(calls, [self.scan_id])
        self.assertEqual(self.runner._submitted, set())

    def test_shutdown_rejects_submission_still_building_its_admission_identity(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        failures: list[Exception] = []
        build_identity = self.runner._library_scan_admission_identity

        def block_identity(*args, **kwargs):
            identity = build_identity(*args, **kwargs)
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test admission barrier timed out")
            return identity

        def submit():
            try:
                self.runner.submit(self.scan_id)
            except Exception as exc:
                failures.append(exc)

        with patch.object(self.runner, "_library_scan_admission_identity", side_effect=block_identity):
            thread = threading.Thread(target=submit)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.runner.shutdown()
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.runner._submitted, set())
        self.assertEqual(self.repository.get_media_job(self.scan_id)["status"], "queued")

    def test_child_slot_stays_serial_and_duplicate_child_is_not_dispatched_twice(self) -> None:
        for index in range(2):
            Image.new("RGB", (24, 12), (index, 0, 100)).save(self.library / f"still-{index}.png")
        children: list[str] = []
        LibraryScanRunner(self.repository, submit_child=children.append).run(
            self.scan_id,
            runtime_generation_id=GENERATION,
            active_library_root_id=self.root_id,
            should_stop=lambda: False,
        )
        self.assertEqual(len(children), 2)
        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[str] = []
        workers: set[threading.Thread] = set()
        process_image = self.runner._image_processor.run

        def gated_image(job_id, **kwargs):
            calls.append(job_id)
            workers.add(threading.current_thread())
            if job_id == children[0]:
                first_started.set()
                if not release.wait(10):
                    raise RuntimeError("test child barrier timed out")
            else:
                second_started.set()
            process_image(job_id, **kwargs)
            if job_id == children[1]:
                finished.set()

        with patch.object(self.runner._image_processor, "run", side_effect=gated_image):
            try:
                self.runner.submit(children[0])
                self.assertTrue(first_started.wait(3))
                self.runner.submit(children[1])
                submitters = [threading.Thread(target=self.runner.submit, args=(children[0],)) for _ in range(8)]
                for thread in submitters:
                    thread.start()
                for thread in submitters:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                self.assertFalse(second_started.wait(0.1), "second child bypassed the occupied analysis slot")
                release.set()
                self.assertTrue(finished.wait(5))
            finally:
                release.set()
                self.runner.shutdown()
        self.assertEqual(calls, children)
        self.assertEqual(len(workers), 1)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        for child_id in children:
            self.assertEqual(self.repository.get_media_job(child_id)["status"], "succeeded")

    def test_shutdown_waits_for_scan_worker_to_exit_and_refuses_new_work(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        shutdown_started = threading.Event()
        shutdown_finished = threading.Event()
        workers: list[threading.Thread] = []
        process_scan = self.runner._library_scan_processor.run

        def gated_scan(job_id, **kwargs):
            workers.append(threading.current_thread())
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test shutdown barrier timed out")
            process_scan(job_id, **kwargs)

        def shutdown():
            shutdown_started.set()
            self.runner.shutdown()
            shutdown_finished.set()

        with patch.object(self.runner._library_scan_processor, "run", side_effect=gated_scan):
            self.runner.submit(self.scan_id)
            shutdown_thread = threading.Thread(target=shutdown)
            try:
                self.assertTrue(entered.wait(3))
                shutdown_thread.start()
                self.assertTrue(shutdown_started.wait(3))
                self.assertFalse(shutdown_finished.wait(0.1))
                self.runner.submit(self.scan_id)
            finally:
                release.set()
                if shutdown_thread.ident is not None:
                    shutdown_thread.join(5)
                self.runner.shutdown()
        self.assertTrue(shutdown_finished.is_set())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(len(workers), 1)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(self.runner._submitted, set())
        self.assertEqual(self.repository.get_media_job(self.scan_id)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
