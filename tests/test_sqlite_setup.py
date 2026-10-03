from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = PROJECT_ROOT / "scripts" / "bootstrap_mac.sh"
PREPARE_DESKTOP = PROJECT_ROOT / "scripts" / "prepare_desktop_runtime.sh"
PREPARE_ELECTRON = PROJECT_ROOT / "scripts" / "prepare_macos_electron_runtime.sh"
RUN_PYTHON = PROJECT_ROOT / "scripts" / "run_python.sh"
BACKEND_ENTRY = PROJECT_ROOT / "backend" / "app.py"


class SQLiteSetupBoundaryTests(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_bootstrap_rejects_symlink_venv_before_any_external_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-bootstrap-symlink-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(BOOTSTRAP, scripts / "bootstrap_mac.sh")
            external = root / "external-environment"
            external.mkdir()
            sentinel = external / "must-survive.txt"
            sentinel.write_text("keep", encoding="utf-8")
            (project / ".venv").symlink_to(external, target_is_directory=True)
            fake_bin = root / "fake-bin"
            invocation_log = root / "external-invocations.log"
            fake_command = textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                printf '%s\\n' "$(basename "$0") $*" >> {shlex.quote(str(invocation_log))}
                if [ "$(basename "$0")" = "ffmpeg" ]; then
                  echo 'ffmpeg version 6.0'
                elif [ "$(basename "$0")" = "node" ]; then
                  echo yes
                fi
                exit 0
                """
            )
            for command in ("npm", "ffmpeg", "ffprobe", "brew", "node", "python3.14"):
                self._write_executable(fake_bin / command, fake_command)

            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
            process = subprocess.run(
                ["bash", str(scripts / "bootstrap_mac.sh")],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("symbolic link", process.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse(invocation_log.exists())

    def test_run_python_ignores_pythonpath_sitecustomize_for_checker_and_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-pythonpath-hook-") as temporary:
            root = Path(temporary)
            hook_dir = root / "hook"
            hook_dir.mkdir()
            marker = root / "sitecustomize-ran.txt"
            (hook_dir / "sitecustomize.py").write_text(
                textwrap.dedent(
                    """\
                    import os
                    from pathlib import Path
                    import sqlite3

                    Path(os.environ["MEMOLENS_HOOK_MARKER"]).write_text("ran", encoding="utf-8")
                    sqlite3.sqlite_version = "3.53.0"
                    sqlite3.sqlite_version_info = (3, 53, 0)
                    """
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "MEMOLENS_PYTHON": sys.executable,
                    "MEMOLENS_HOOK_MARKER": str(marker),
                    "PYTHONPATH": str(hook_dir),
                    "PYTHONHOME": "",
                    "PYTHONINSPECT": "1",
                    "PYTHONSTARTUP": str(hook_dir / "sitecustomize.py"),
                    "PYTHONUSERBASE": str(root / "user-base"),
                }
            )
            process = subprocess.run(
                [
                    "bash",
                    str(RUN_PYTHON),
                    "-c",
                    "import sqlite3; print('TARGET_OK', sqlite3.sqlite_version)",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("TARGET_OK", process.stdout)
            self.assertFalse(marker.exists())

    def test_explicit_unsafe_python_never_executes_target_or_falls_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-explicit-unsafe-") as temporary:
            root = Path(temporary)
            fake_python = root / "unsafe-python"
            target_marker = root / "target-ran.txt"
            self._write_executable(
                fake_python,
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    for argument in "$@"; do
                      if [ "$argument" = "--quiet" ]; then
                        exit 78
                      fi
                      if [ "$argument" = "--json" ]; then
                        echo '{{"wal_reset_safe":false}}'
                        exit 78
                      fi
                    done
                    printf ran > {shlex.quote(str(target_marker))}
                    exit 0
                    """
                ),
            )
            environment = os.environ.copy()
            environment["MEMOLENS_PYTHON"] = str(fake_python)
            process = subprocess.run(
                ["bash", str(RUN_PYTHON), "-c", "print('must not run')"],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 78)
            self.assertFalse(target_marker.exists())
            self.assertNotIn("must not run", process.stdout)

    def test_bootstrap_does_not_replace_explicit_unsafe_python(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-explicit-bootstrap-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(BOOTSTRAP, scripts / "bootstrap_mac.sh")

            unsafe_python = root / "unsafe-python"
            self._write_executable(
                unsafe_python,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    for argument in "$@"; do
                      if [ "$argument" = "--quiet" ]; then
                        exit 78
                      fi
                      if [ "$argument" = "--json" ]; then
                        echo '{"wal_reset_safe":false}'
                        exit 78
                      fi
                    done
                    exit 0
                    """
                ),
            )

            fake_bin = root / "fake-bin"
            fallback_marker = root / "fallback-used.txt"
            common_command = textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                command_name="$(basename "$0")"
                if [ "$command_name" = "ffmpeg" ]; then
                  echo 'ffmpeg version 6.0'
                elif [ "$command_name" = "node" ]; then
                  echo yes
                elif [ "$command_name" = "python3.14" ]; then
                  printf used > {shlex.quote(str(fallback_marker))}
                fi
                exit 0
                """
            )
            for command in ("npm", "ffmpeg", "ffprobe", "brew", "node", "python3.14"):
                self._write_executable(fake_bin / command, common_command)

            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
            environment["MEMOLENS_PYTHON"] = str(unsafe_python)
            process = subprocess.run(
                ["bash", str(scripts / "bootstrap_mac.sh")],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("MEMOLENS_PYTHON uses an SQLite runtime", process.stderr)
            self.assertFalse(fallback_marker.exists())

    def _assert_online_macos_setup_uses_locked_dependencies(
        self, entrypoint: Path, architecture: str
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-macos-locked-setup-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            for source in (BOOTSTRAP, PREPARE_DESKTOP, PREPARE_ELECTRON):
                shutil.copy2(source, scripts / source.name)
            shutil.copy2(PROJECT_ROOT / "scripts/check_sqlite_runtime.py", scripts)
            (project / "core").mkdir()
            for filename in ("sqlite_runtime.py", "sqlite_runtime_policy.json"):
                shutil.copy2(PROJECT_ROOT / "core" / filename, project / "core" / filename)
            manifests = {}
            for filename in ("package.json", "package-lock.json"):
                manifests[filename] = (PROJECT_ROOT / filename).read_bytes()
                (project / filename).write_bytes(manifests[filename])
            lock = json.loads(manifests["package-lock.json"])
            vite_package = json.dumps(
                {"name": "vite", "version": lock["packages"]["node_modules/vite"]["version"]}
            )
            fixture_vite = root / "vite-package.json"
            fixture_vite.write_text(vite_package, encoding="utf-8")
            fixture_runtime = root / "Electron.app"
            source_bin = fixture_runtime / "Contents/MacOS/Electron"
            self._write_executable(source_bin, "#!/bin/sh\nexit 0\n")
            log = root / "external-invocations.log"
            fake_bin = root / "fake-bin"
            self._write_executable(
                project / ".venv/bin/python",
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    if [ "$1" = "-I" ] && [ "${{2-}}" = "-m" ] && [ "${{3-}}" = "pip" ]; then
                      printf 'pip %s\\n' "$*" >> "$MEMOLENS_SETUP_LOG"
                      exit 0
                    fi
                    exec {shlex.quote(sys.executable)} "$@"
                    """
                ),
            )
            (project / ".venv/bin/activate").write_text("", encoding="utf-8")
            (project / "requirements.txt").write_text("", encoding="utf-8")
            self._write_executable(
                fake_bin / "uname",
                '#!/bin/sh\nif [ "$1" = "-m" ]; then echo "$MEMOLENS_TEST_ARCH"; else echo Darwin; fi\n',
            )
            self._write_executable(fake_bin / "ffmpeg", "#!/bin/sh\necho 'ffmpeg version 6.0'\n")
            self._write_executable(fake_bin / "ffprobe", "#!/bin/sh\nexit 0\n")
            self._write_executable(
                fake_bin / "node",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [ "$1" = "-p" ]; then echo yes; exit 0; fi
                    printf 'node %s\\n' "$*" >> "$MEMOLENS_SETUP_LOG"
                    exit 1
                    """
                ),
            )
            self._write_executable(
                fake_bin / "npm",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    printf 'npm %s\\n' "$*" >> "$MEMOLENS_SETUP_LOG"
                    case "$*" in
                      ci)
                        mkdir -p node_modules/vite node_modules/electron/dist
                        cp "$MEMOLENS_FIXTURE_VITE" node_modules/vite/package.json
                        cp -R "$MEMOLENS_FIXTURE_RUNTIME" node_modules/electron/dist/Electron.app
                        ;;
                      'run build')
                        cmp node_modules/vite/package.json "$MEMOLENS_FIXTURE_VITE" || exit 98
                        mkdir -p dist electron-dist/electron
                        printf '<meta http-equiv="Content-Security-Policy">' > dist/index.html
                        printf renderer > electron-dist/electron/main.js
                        printf preload > electron-dist/electron/preload.cjs
                        ;;
                      *)
                        mkdir -p node_modules/vite
                        printf drifted > node_modules/vite/package.json
                        exit 97
                        ;;
                    esac
                    """
                ),
            )
            self._write_executable(
                fake_bin / "ditto",
                '#!/bin/sh\nprintf "ditto\\n" >> "$MEMOLENS_SETUP_LOG"\ncp -R "$1" "$2"\n',
            )
            for command in ("xattr", "codesign"):
                self._write_executable(
                    fake_bin / command,
                    f'#!/bin/sh\nprintf "{command} %s\\n" "$*" >> "$MEMOLENS_SETUP_LOG"\n',
                )
            if entrypoint == PREPARE_DESKTOP:
                installed_vite = project / "node_modules/vite/package.json"
                installed_vite.parent.mkdir(parents=True)
                shutil.copy2(fixture_vite, installed_vite)
                shutil.copytree(fixture_runtime, project / "node_modules/electron/dist/Electron.app")
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "MEMOLENS_NETWORK_PROFILE": "online",
                    "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                    "MEMOLENS_PYTHON": sys.executable,
                    "MEMOLENS_SETUP_LOG": str(log),
                    "MEMOLENS_TEST_ARCH": architecture,
                    "MEMOLENS_FIXTURE_VITE": str(fixture_vite),
                    "MEMOLENS_FIXTURE_RUNTIME": str(fixture_runtime),
                }
            )
            process = subprocess.run(
                ["bash", str(scripts / entrypoint.name)],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            expected_npm = ["npm ci", "npm run build"] if entrypoint == BOOTSTRAP else ["npm run build"]
            self.assertEqual([call for call in calls if call.startswith("npm ")], expected_npm)
            self.assertFalse(any(call.startswith("node ") for call in calls))
            self.assertEqual(calls[-4], "ditto")
            self.assertTrue(calls[-3].startswith("xattr -cr "))
            self.assertTrue(calls[-2].startswith("codesign --force --deep --sign - "))
            self.assertTrue(calls[-1].startswith("codesign --verify --deep --verbose=2 "))
            for filename, content in manifests.items():
                self.assertEqual((project / filename).read_bytes(), content)
            self.assertEqual((project / "node_modules/vite/package.json").read_text(encoding="utf-8"), vite_package)
            for filename in ("dist/index.html", "electron-dist/electron/main.js", "electron-dist/electron/preload.cjs"):
                self.assertTrue((project / filename).is_file())
            target_bin = root / "state/runtime/Electron.app/Contents/MacOS/Electron"
            self.assertEqual(target_bin.read_bytes(), source_bin.read_bytes())
            self.assertTrue(os.access(target_bin, os.X_OK))

    def test_desktop_prepare_online_macos_does_not_install_dependencies(self) -> None:
        for architecture in ("arm64", "x86_64"):
            with self.subTest(architecture=architecture):
                self._assert_online_macos_setup_uses_locked_dependencies(PREPARE_DESKTOP, architecture)

    def test_bootstrap_online_macos_installs_locked_dependencies_before_build(self) -> None:
        for architecture in ("arm64", "x86_64"):
            with self.subTest(architecture=architecture):
                self._assert_online_macos_setup_uses_locked_dependencies(BOOTSTRAP, architecture)

    def test_shell_and_cli_do_not_copy_sqlite_version_thresholds(self) -> None:
        forbidden = ("3.44.6", "3.50.7", "3.51.3", "3.53")
        for relative_path in (
            "scripts/check_sqlite_runtime.py",
            "scripts/bootstrap_mac.sh",
            "scripts/prepare_desktop_runtime.sh",
            "scripts/run_python.sh",
        ):
            content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertTrue(all(version not in content for version in forbidden))

    def test_ci_checker_calls_use_isolated_startup(self) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        calls = re.findall(
            r'(?:python|\.venv/bin/python|"\$\{python_bin\}")(?:\s+-I)?\s+scripts/check_sqlite_runtime\.py',
            workflow,
        )

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(" -I " in call for call in calls))

    def test_ubuntu_writer_ci_provisions_verified_upstream_sqlite_before_admission(
        self,
    ) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        app_job, separator, remaining_jobs = workflow.partition("  plugin-python310:")
        self.assertTrue(separator)
        provision = app_job.index("Provision admitted SQLite writer library")
        admission = app_job.index("Admit SQLite WAL runtime")
        self.assertLess(provision, admission)
        self.assertIn(
            "https://www.sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF}.tar.gz",
            app_job,
        )
        self.assertIn(
            "454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338",
            app_job,
        )
        self.assertIn("hashlib.sha3_256", app_job)
        self.assertIn('echo "LD_LIBRARY_PATH=${install_dir}/lib"', app_job)
        self.assertNotIn("Provision admitted SQLite writer library", remaining_jobs)

    def test_backend_entry_refuses_nonisolated_python_before_app_import(self) -> None:
        process = subprocess.run(
            [sys.executable, "-S", str(BACKEND_ENTRY)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(process.returncode, 78)
        self.assertIn("isolated Python (-I)", process.stderr)
        self.assertNotIn("ModuleNotFoundError", process.stderr)

    def test_supported_backend_launchers_route_through_isolated_runner(self) -> None:
        for relative_path in (
            "scripts/dev_local.sh",
            "scripts/run_backend_for_photon_bot.sh",
        ):
            content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn("run_python.sh", content)
                self.assertNotRegex(content, r'(?m)^\s*(?:python3|"\$\{PYTHON_BIN\}") backend/app\.py')

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("bash scripts/run_python.sh backend/app.py", readme)
        self.assertNotIn("python3 backend/app.py", readme)

    def test_backend_writer_never_enables_flask_reloader(self) -> None:
        entry = BACKEND_ENTRY.read_text(encoding="utf-8")
        lifecycle = (
            PROJECT_ROOT / "backend" / "src" / "process_lifecycle.py"
        ).read_text(encoding="utf-8")

        self.assertIn("run_managed_backend(app, host=host, port=port, debug=debug)", entry)
        self.assertIn("debug=debug", lifecycle)
        self.assertIn("use_reloader=False", lifecycle)
        self.assertNotIn("use_reloader=debug", entry + lifecycle)

    def test_backend_entry_uses_one_canonical_package_identity(self) -> None:
        entry = BACKEND_ENTRY.read_text(encoding="utf-8")

        self.assertIn("from backend.src import create_app", entry)
        self.assertIn(
            "from backend.src.process_lifecycle import run_managed_backend",
            entry,
        )
        self.assertNotRegex(entry, r"(?m)^from src(?:\.| import)")

    def test_legacy_backfills_are_not_advertised_as_managed_product_workflows(self) -> None:
        package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertNotIn("quality:backfill", package["scripts"])
        self.assertFalse(
            any(
                "backfill_image_quality.py" in command
                or "backfill_text_embeddings.py" in command
                for command in package["scripts"].values()
            )
        )

    def test_macos_ci_attests_homebrew_sqlite_before_creating_venv(self) -> None:
        workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        macos = workflow.split("  macos-desktop-runtime:", 1)[1]
        self.assertIn("brew install python@3.14 sqlite", macos)
        self.assertIn('python_bin="$(brew --prefix python@3.14)/bin/python3.14"', macos)
        admission = macos.index('"${python_bin}" -I scripts/check_sqlite_runtime.py --json')
        environment = macos.index('"${python_bin}" -m venv .venv')
        self.assertLess(admission, environment)
        self.assertIn(".venv/bin/python -I scripts/check_sqlite_runtime.py --json", macos)

    def test_production_oracle_entrypoint_builds_electron_before_loading_node_tests(self) -> None:
        package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            package["scripts"]["test:production-oracles"],
            "npm run build:electron && bash ./scripts/run_python.sh scripts/run_production_oracles.py",
        )

    def test_quality_backfill_admission_failure_has_zero_database_side_effects(self) -> None:
        from scripts import backfill_image_quality

        with (
            mock.patch.object(backfill_image_quality, "parse_args", return_value=object()),
            mock.patch.object(
                backfill_image_quality,
                "require_safe_sqlite_runtime",
                side_effect=RuntimeError("unsafe runtime"),
            ),
            mock.patch.object(
                backfill_image_quality.Settings,
                "from_env",
                side_effect=AssertionError("settings reached before admission"),
            ) as settings,
            mock.patch.object(
                backfill_image_quality,
                "ImageIndexRepository",
                side_effect=AssertionError("repository reached before admission"),
            ) as repository,
            self.assertRaisesRegex(RuntimeError, "unsafe runtime"),
        ):
            backfill_image_quality.main()

        settings.assert_not_called()
        repository.assert_not_called()

    def test_repository_writer_scripts_self_admit_before_database_side_effects(self) -> None:
        writer_markers = {
            "scripts/backfill_image_quality.py": "repository = ImageIndexRepository",
            "scripts/backfill_text_embeddings.py": "repository = ImageIndexRepository",
            "scripts/test_indexing.py": "app = create_app()",
            "scripts/test_query.py": "settings.ensure_directories()",
        }
        for relative_path, first_side_effect in writer_markers.items():
            content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                isolation = content.index("if not sys.flags.isolated:")
                admission = content.index("require_safe_sqlite_runtime()")
                side_effect = content.index(first_side_effect)
                self.assertLess(isolation, admission)
                self.assertLess(admission, side_effect)

    def test_repository_writer_scripts_reject_direct_nonisolated_launch(self) -> None:
        for relative_path in (
            "scripts/backfill_image_quality.py",
            "scripts/backfill_text_embeddings.py",
            "scripts/test_indexing.py",
            "scripts/test_query.py",
        ):
            process = subprocess.run(
                [sys.executable, "-S", str(PROJECT_ROOT / relative_path), "--help"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            with self.subTest(path=relative_path):
                self.assertEqual(process.returncode, 78)
                self.assertIn("isolated Python (-I)", process.stderr)
                self.assertNotIn("ModuleNotFoundError", process.stderr)

if __name__ == "__main__":
    unittest.main()
