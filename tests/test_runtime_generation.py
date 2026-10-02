from __future__ import annotations

import re
import unittest
from typing import cast

from flask import Flask

from backend.src import swap_runtime
from backend.src.runtime import RuntimeBundle, RuntimeManager
from core.config import Settings


class _RejectingActivationRunner:
    def reconcile_interrupted_storage(self) -> None:
        raise RuntimeError("candidate activation rejected")


def _settings() -> Settings:
    # RuntimeBundle only owns the settings object; these unit tests do not
    # exercise Settings parsing or filesystem setup.
    return cast(Settings, object())


class RuntimeGenerationTests(unittest.TestCase):
    def test_freeze_mints_a_closed_uninjectable_generation_per_bundle(self) -> None:
        settings = _settings()
        extension = object()

        first = RuntimeBundle.freeze(settings, {"extension": extension})
        second = RuntimeBundle.freeze(settings, {"extension": extension})

        self.assertRegex(
            first.generation_id,
            re.compile(r"\Aruntime_generation_[0-9a-f]{64}\Z"),
        )
        self.assertNotEqual(first.generation_id, second.generation_id)
        with self.assertRaisesRegex(TypeError, "generation_id"):
            RuntimeBundle(  # type: ignore[call-arg]
                settings=settings,
                extensions={},
                generation_id="forged-generation",
            )

    def test_leases_pin_one_generation_across_swap_and_retirement(self) -> None:
        retired: list[RuntimeBundle] = []
        manager = RuntimeManager(retired.append)
        first = RuntimeBundle.freeze(_settings(), {"name": "first"})
        second = RuntimeBundle.freeze(_settings(), {"name": "second"})
        first_generation = first.generation_id
        second_generation = second.generation_id

        self.assertIsNone(manager.swap(first))
        lease_a = manager.acquire()
        lease_b = manager.acquire()
        self.assertEqual(lease_a.generation_id, first_generation)
        self.assertEqual(lease_b.generation_id, first_generation)
        self.assertEqual(manager.current_generation_id, first_generation)

        self.assertIs(manager.swap(second), first)
        self.assertEqual(manager.current_generation_id, second_generation)
        self.assertEqual(lease_a.generation_id, first_generation)
        self.assertEqual(lease_b.generation_id, first_generation)
        self.assertEqual(retired, [])
        self.assertEqual(first.generation_id, first_generation)
        self.assertEqual(second.generation_id, second_generation)

        lease_a.release()
        self.assertEqual(retired, [])
        lease_b.release()
        self.assertEqual(retired, [first])
        self.assertEqual(retired[0].generation_id, first_generation)

    def test_repeated_swap_of_same_bundle_does_not_retire_active_extensions(self) -> None:
        retired: list[RuntimeBundle] = []
        manager = RuntimeManager(retired.append)
        bundle = RuntimeBundle.freeze(_settings(), {"name": "only"})
        generation_id = bundle.generation_id

        self.assertIsNone(manager.swap(bundle))
        self.assertIs(manager.swap(bundle), bundle)

        self.assertEqual(manager.current_generation_id, generation_id)
        self.assertEqual(retired, [])
        with manager.acquire() as leased:
            self.assertIs(leased, bundle)
            self.assertEqual(leased.generation_id, generation_id)

    def test_failed_candidate_activation_never_exposes_its_generation(self) -> None:
        app = Flask(__name__)
        retired: list[RuntimeBundle] = []
        manager = RuntimeManager(retired.append)
        active = RuntimeBundle.freeze(_settings(), {"name": "active"})
        manager.swap(active)
        app.extensions["runtime_manager"] = manager
        app.config["SETTINGS"] = active.settings
        app.extensions.update(active.extensions)

        with self.assertRaisesRegex(RuntimeError, "candidate activation rejected"):
            swap_runtime(
                app,
                _settings(),
                {"canonical_export_job_runner": _RejectingActivationRunner()},
            )

        self.assertIs(manager.current_bundle, active)
        self.assertEqual(manager.current_generation_id, active.generation_id)
        self.assertIs(app.config["SETTINGS"], active.settings)
        self.assertEqual(app.extensions["name"], "active")
        self.assertEqual(retired, [])


if __name__ == "__main__":
    unittest.main()
