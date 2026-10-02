from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from backend.src import create_app, shutdown_runtime_extensions
from core.config import Settings
from core.production_surface_inventory import load_production_surface_inventory


ROOT = Path(__file__).resolve().parents[1]
HIGH_IMPACT_EFFECTS = frozenset(
    {"egress", "filesystem-read", "filesystem-write", "mutation", "process-start"}
)
NON_LOOPBACK_REMOTE_ADDR = "198.51.100.7"


def _converter_probe(converter: object) -> object:
    """Return one deterministic value admitted by a Werkzeug URL converter."""

    converter_name = type(converter).__name__
    if converter_name == "IntegerConverter":
        return 1
    if converter_name == "FloatConverter":
        return 1.0
    if converter_name == "UUIDConverter":
        return UUID("00000000-0000-4000-8000-000000000001")
    if converter_name == "AnyConverter":
        items = tuple(getattr(converter, "items", ()))
        if not items:
            raise AssertionError("AnyConverter has no closed probe candidate.")
        return items[0]
    return "inventory-probe"


class BackendLoopbackAuthorityInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-loopback-inventory-"
        )
        root = Path(cls.temporary.name).resolve()
        library = root / "library"
        library.mkdir()
        cls.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                "IMAGE_LIBRARY_DIR": str(library),
                "SQLITE_DB_PATH": str(root / "state" / "inventory.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": "loopback-inventory-desktop-token",
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": "loopback-inventory-main-token",
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        cls.environment.start()
        cls.app = create_app(Settings.from_env())
        cls.app.testing = True
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        shutdown_runtime_extensions(cls.app.extensions)
        cls.environment.stop()
        cls.temporary.cleanup()

    def test_every_high_impact_flask_loopback_action_rejects_non_loopback_before_dispatch(
        self,
    ) -> None:
        inventory = load_production_surface_inventory()
        target_action_ids = frozenset(
            str(action["id"])
            for action in inventory["actions"]
            if action["surface"] == "flask_http"
            and "loopback-client" in action["authority"]
            and HIGH_IMPACT_EFFECTS.intersection(action["effects"])
        )
        self.assertTrue(target_action_ids, "Frozen inventory selected no target actions.")

        cases: dict[str, tuple[object, str, str]] = {}
        for rule in self.app.url_map.iter_rules():
            for method in sorted(set(rule.methods or ()) - {"HEAD", "OPTIONS"}):
                action_id = f"flask_http.{method}:{rule.rule}"
                if action_id not in target_action_ids:
                    continue
                self.assertNotIn(
                    action_id,
                    cases,
                    f"Runtime url_map duplicated target action {action_id}.",
                )
                values = {
                    argument: _converter_probe(rule._converters[argument])
                    for argument in rule.arguments
                }
                built = rule.build(values, append_unknown=False)
                self.assertIsNotNone(
                    built,
                    f"Could not build a concrete URL for {action_id}.",
                )
                assert built is not None
                _subdomain, path = built
                cases[action_id] = (rule, method, path)

        self.assertEqual(
            frozenset(cases),
            target_action_ids,
            "Every and only frozen high-impact loopback action must be requested.",
        )
        self.assertEqual(len(cases), len(target_action_ids))

        original_views: dict[str, object] = {}

        def dispatch_sentinel(**_values: object) -> None:
            raise AssertionError(
                "A non-loopback request reached a Flask endpoint before denial."
            )

        for rule, _method, _path in cases.values():
            if rule.endpoint not in original_views:
                original_views[rule.endpoint] = self.app.view_functions[rule.endpoint]
                self.app.view_functions[rule.endpoint] = dispatch_sentinel

        requested_action_ids: set[str] = set()
        try:
            with patch(
                "flask.wrappers.Request.get_json",
                side_effect=AssertionError(
                    "A non-loopback request parsed JSON before denial."
                ),
            ):
                for action_id in sorted(target_action_ids):
                    _rule, method, path = cases[action_id]
                    with self.subTest(action_id=action_id, path=path):
                        requested_action_ids.add(action_id)
                        response = self.client.open(
                            path,
                            method=method,
                            data=b'{"',
                            content_type="application/json",
                            environ_overrides={
                                "REMOTE_ADDR": NON_LOOPBACK_REMOTE_ADDR
                            },
                        )
                        self.assertEqual(response.status_code, 403)
        finally:
            for endpoint, original_view in original_views.items():
                self.app.view_functions[endpoint] = original_view

        self.assertEqual(frozenset(requested_action_ids), target_action_ids)
        self.assertEqual(len(requested_action_ids), len(target_action_ids))


if __name__ == "__main__":
    unittest.main()
