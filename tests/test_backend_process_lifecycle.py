from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from backend.src.process_lifecycle import run_managed_backend


class _Recorder:
    def __init__(self, events: list[str], label: str) -> None:
        self.events = events
        self.label = label

    def shutdown(self) -> None:
        self.events.append(f"shutdown:{self.label}")

    def close(self) -> None:
        self.events.append(f"close:{self.label}")


class _FakeApp:
    def __init__(self, run) -> None:
        self._run = run
        self.extensions: dict[str, object] = {}
        self.run_kwargs: dict[str, object] | None = None

    def run(self, **kwargs: object) -> object:
        self.run_kwargs = kwargs
        return self._run()


class BackendProcessLifecycleTests(unittest.TestCase):
    def test_sigterm_requests_runner_shutdown_before_repository_close(self) -> None:
        events: list[str] = []
        installed: dict[signal.Signals, object] = {}
        previous = {
            signal.SIGTERM: object(),
            signal.SIGINT: object(),
        }

        def install(signum, handler):
            installed[signum] = handler
            return previous[signum]

        app = _FakeApp(
            lambda: installed[signal.SIGTERM](int(signal.SIGTERM), None)
        )
        app.extensions = {
            "canonical_export_job_runner": _Recorder(events, "canonical"),
            "media_job_runner": _Recorder(events, "media"),
            "render_job_runner": _Recorder(events, "render"),
            "media_repository": _Recorder(events, "repository"),
        }
        with (
            patch("backend.src.process_lifecycle.signal.getsignal", side_effect=previous.get),
            patch("backend.src.process_lifecycle.signal.signal", side_effect=install),
        ):
            run_managed_backend(app, host="127.0.0.1", port=5519, debug=False)

        self.assertEqual(
            events,
            [
                "shutdown:canonical",
                "shutdown:media",
                "shutdown:render",
                "close:repository",
            ],
        )
        self.assertEqual(
            app.run_kwargs,
            {
                "host": "127.0.0.1",
                "port": 5519,
                "debug": False,
                "use_reloader": False,
            },
        )

    def test_unexpected_server_failure_still_quiesces_runtime_then_propagates(self) -> None:
        events: list[str] = []

        def fail() -> None:
            raise RuntimeError("server failed")

        app = _FakeApp(fail)
        app.extensions = {
            "canonical_export_job_runner": _Recorder(events, "canonical"),
            "media_repository": _Recorder(events, "repository"),
        }
        with self.assertRaisesRegex(RuntimeError, "server failed"):
            run_managed_backend(app, host="127.0.0.1", port=5519, debug=False)
        self.assertEqual(
            events,
            ["shutdown:canonical", "close:repository"],
        )


if __name__ == "__main__":
    unittest.main()
