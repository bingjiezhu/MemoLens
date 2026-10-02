from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from backend.src import create_app, shutdown_runtime_extensions
from backend.src.api.routes import (
    LEGACY_DESKTOP_AUTHORITY_ENDPOINTS,
    MEDIA_PRIVILEGED_ENDPOINTS,
)
from core.config import Settings
from core.production_surface_inventory import load_production_surface_inventory


ROOT = Path(__file__).resolve().parents[1]
HIGH_IMPACT_EFFECTS = frozenset(
    {"egress", "filesystem-read", "filesystem-write", "mutation", "process-start"}
)
AUDITED_ROUTE_LOCAL_DESKTOP_ACTIONS = frozenset(
    {
        "flask_http.GET:/v1/library/files/<path:relative_path>",
        "flask_http.GET:/v1/media/capabilities",
    }
)


class BusinessDispatchBeforeDesktopDenial(AssertionError):
    """Raised when a missing-token probe crosses into route business logic."""


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


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    """Freeze file contents and directory/link structure without following links."""

    rows: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            rows.append((relative, "symlink", os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            rows.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
        elif stat.S_ISDIR(metadata.st_mode):
            rows.append((relative, "directory", ""))
        else:
            rows.append((relative, "other", str(stat.S_IFMT(metadata.st_mode))))
    return tuple(rows)


class BackendDesktopAuthorityInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-desktop-inventory-"
        )
        cls.root = Path(cls.temporary.name).resolve()
        library = cls.root / "library"
        library.mkdir()
        cls.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(cls.root / "state"),
                "IMAGE_LIBRARY_DIR": str(library),
                "SQLITE_DB_PATH": str(cls.root / "state" / "inventory.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": "desktop-inventory-token",
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": "desktop-inventory-main-token",
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

    def test_every_high_impact_flask_desktop_action_denies_before_business_dispatch(
        self,
    ) -> None:
        inventory = load_production_surface_inventory()
        target_action_ids = frozenset(
            str(action["id"])
            for action in inventory["actions"]
            if action["surface"] == "flask_http"
            and "desktop-session" in action["authority"]
            and HIGH_IMPACT_EFFECTS.intersection(action["effects"])
        )
        self.assertTrue(target_action_ids, "Frozen inventory selected no target actions.")

        cases: dict[str, tuple[object, str, str, str]] = {}
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
                endpoint = rule.endpoint.rsplit(".", 1)[-1]
                cases[action_id] = (rule, method, path, endpoint)

        self.assertEqual(
            frozenset(cases),
            target_action_ids,
            "Every and only frozen high-impact desktop action must be requested.",
        )
        self.assertEqual(len(cases), len(target_action_ids))

        pre_dispatch_endpoints = (
            LEGACY_DESKTOP_AUTHORITY_ENDPOINTS | MEDIA_PRIVILEGED_ENDPOINTS
        )
        pre_dispatch_action_ids = frozenset(
            action_id
            for action_id, (_rule, _method, _path, endpoint) in cases.items()
            if endpoint in pre_dispatch_endpoints
        )
        route_local_action_ids = target_action_ids - pre_dispatch_action_ids
        self.assertFalse(
            route_local_action_ids - AUDITED_ROUTE_LOCAL_DESKTOP_ACTIONS,
            "A new route-local desktop-authority action lacks a business sentinel.",
        )

        original_views: dict[str, object] = {}

        def dispatch_sentinel(**_values: object) -> None:
            raise BusinessDispatchBeforeDesktopDenial("Flask endpoint dispatch")

        for action_id in pre_dispatch_action_ids:
            rule, _method, _path, _endpoint = cases[action_id]
            if rule.endpoint not in original_views:
                original_views[rule.endpoint] = self.app.view_functions[rule.endpoint]
                self.app.view_functions[rule.endpoint] = dispatch_sentinel

        requested_action_ids: set[str] = set()
        results: dict[str, tuple[int | str, str]] = {}
        try:
            with (
                patch(
                    "flask.wrappers.Request.get_json",
                    side_effect=BusinessDispatchBeforeDesktopDenial(
                        "request JSON parsing"
                    ),
                ),
                patch(
                    "backend.src.api.routes._open_library_file",
                    side_effect=BusinessDispatchBeforeDesktopDenial(
                        "library file opening"
                    ),
                ),
                patch(
                    "backend.src.api.routes.binary_capability",
                    side_effect=BusinessDispatchBeforeDesktopDenial(
                        "binary capability process probe"
                    ),
                ),
                patch(
                    "backend.src.api.routes.ffmpeg_encode_capability",
                    side_effect=BusinessDispatchBeforeDesktopDenial(
                        "FFmpeg encoder process probe"
                    ),
                ),
            ):
                for action_id in sorted(target_action_ids):
                    _rule, method, path, _endpoint = cases[action_id]
                    with self.subTest(action_id=action_id, path=path):
                        requested_action_ids.add(action_id)
                        before = _tree_snapshot(self.root)
                        try:
                            response = self.client.open(
                                path,
                                method=method,
                                data=b'{"' if method != "GET" else None,
                                content_type=(
                                    "application/json" if method != "GET" else None
                                ),
                            )
                        except BusinessDispatchBeforeDesktopDenial as exc:
                            results[action_id] = ("business-dispatch", str(exc))
                        else:
                            payload = response.get_json(silent=True)
                            code = (
                                str(payload.get("code") or "")
                                if isinstance(payload, dict)
                                else ""
                            )
                            results[action_id] = (response.status_code, code)
                        self.assertEqual(
                            _tree_snapshot(self.root),
                            before,
                            f"Missing-token probe changed filesystem state: {action_id}",
                        )
        finally:
            for endpoint, original_view in original_views.items():
                self.app.view_functions[endpoint] = original_view

        self.assertEqual(frozenset(requested_action_ids), target_action_ids)
        self.assertEqual(len(requested_action_ids), len(target_action_ids))
        failures = {
            action_id: result
            for action_id, result in results.items()
            if result != (401, "desktop_auth_required")
        }
        self.assertEqual(
            failures,
            {},
            "Every frozen desktop-session claim must deny before business dispatch.",
        )


if __name__ == "__main__":
    unittest.main()
