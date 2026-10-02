from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = PROJECT_ROOT / "scripts" / "bootstrap_mac.sh"
PREPARE_DESKTOP = PROJECT_ROOT / "scripts" / "prepare_desktop_runtime.sh"
PREPARE_ELECTRON = PROJECT_ROOT / "scripts" / "prepare_macos_electron_runtime.sh"


class SetupOfflinePolicyTests(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def _environment(fake_bin: Path, **overrides: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(overrides)
        environment["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
        return environment

    def _install_network_sentinels(self, fake_bin: Path, marker: Path) -> None:
        body = textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$(basename "$0") $*" >> {marker!s}
            exit 97
            """
        )
        for command in ("brew", "npm", "pip", "pip3", "curl", "wget"):
            self._write_executable(fake_bin / command, body)

    def test_bootstrap_offline_fails_before_dependency_provisioning(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-bootstrap-offline-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(BOOTSTRAP, scripts / BOOTSTRAP.name)
            marker = root / "network-command.log"
            fake_bin = root / "fake-bin"
            self._install_network_sentinels(fake_bin, marker)

            process = subprocess.run(
                ["bash", str(scripts / BOOTSTRAP.name)],
                cwd=project,
                env=self._environment(
                    fake_bin,
                    MEMOLENS_NETWORK_PROFILE=" OFFLINE ",
                ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 78, process.stderr)
            self.assertIn("No brew, pip, npm, or Electron download was attempted", process.stderr)
            self.assertFalse(marker.exists())

    def test_setup_scripts_reject_unknown_and_empty_network_profiles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-setup-invalid-profile-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            for source in (BOOTSTRAP, PREPARE_DESKTOP, PREPARE_ELECTRON):
                shutil.copy2(source, scripts / source.name)
            marker = root / "network-command.log"
            fake_bin = root / "fake-bin"
            self._install_network_sentinels(fake_bin, marker)

            for profile in ("", "future-auto"):
                for script in (BOOTSTRAP, PREPARE_DESKTOP, PREPARE_ELECTRON):
                    with self.subTest(profile=profile, script=script.name):
                        process = subprocess.run(
                            ["bash", str(scripts / script.name)],
                            cwd=project,
                            env=self._environment(
                                fake_bin,
                                MEMOLENS_NETWORK_PROFILE=profile,
                            ),
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(process.returncode, 78, process.stderr)
                        self.assertIn("network profile is invalid", process.stderr)
            self.assertFalse(marker.exists())

    def test_desktop_prepare_offline_does_not_bootstrap_missing_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-desktop-offline-missing-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(PREPARE_DESKTOP, scripts / PREPARE_DESKTOP.name)
            marker = root / "network-command.log"
            fake_bin = root / "fake-bin"
            self._install_network_sentinels(fake_bin, marker)
            self._write_executable(
                scripts / "bootstrap_mac.sh",
                f"#!/usr/bin/env bash\nprintf bootstrap >> {marker!s}\nexit 97\n",
            )

            process = subprocess.run(
                ["bash", str(scripts / PREPARE_DESKTOP.name)],
                cwd=project,
                env=self._environment(
                    fake_bin,
                    MEMOLENS_NETWORK_PROFILE="offline",
                ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 78, process.stderr)
            self.assertIn("stopped before bootstrap, pip, npm, or any download", process.stderr)
            self.assertFalse(marker.exists())

    def test_desktop_prepare_offline_reuses_complete_build_without_npm(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-desktop-offline-ready-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(PREPARE_DESKTOP, scripts / PREPARE_DESKTOP.name)
            (scripts / "check_sqlite_runtime.py").write_text("", encoding="utf-8")
            self._write_executable(project / ".venv" / "bin" / "python", "#!/bin/sh\nexit 0\n")
            (project / "dist").mkdir()
            (project / "dist" / "index.html").write_text(
                '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">',
                encoding="utf-8",
            )
            (project / "electron-dist" / "electron").mkdir(parents=True)
            (project / "electron-dist" / "electron" / "main.js").write_text("", encoding="utf-8")
            (project / "electron-dist" / "electron" / "preload.cjs").write_text("", encoding="utf-8")

            marker = root / "network-command.log"
            fake_bin = root / "fake-bin"
            self._install_network_sentinels(fake_bin, marker)
            self._write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self._write_executable(
                scripts / "bootstrap_mac.sh",
                f"#!/usr/bin/env bash\nprintf bootstrap >> {marker!s}\nexit 97\n",
            )

            process = subprocess.run(
                ["bash", str(scripts / PREPARE_DESKTOP.name)],
                cwd=project,
                env=self._environment(
                    fake_bin,
                    MEMOLENS_NETWORK_PROFILE="offline",
                ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertFalse(marker.exists())

    def test_electron_prepare_offline_stops_before_runtime_downloader(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-electron-offline-missing-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(PREPARE_ELECTRON, scripts / PREPARE_ELECTRON.name)
            marker = root / "network-command.log"
            fake_bin = root / "fake-bin"
            self._write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
            self._write_executable(
                project / "node_modules" / ".bin" / "electron",
                f"#!/usr/bin/env bash\nprintf electron >> {marker!s}\nexit 97\n",
            )

            process = subprocess.run(
                ["bash", str(scripts / PREPARE_ELECTRON.name)],
                cwd=project,
                env=self._environment(
                    fake_bin,
                    MEMOLENS_APP_STATE_DIR=str(root / "state"),
                    MEMOLENS_NETWORK_PROFILE="offline",
                ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 78, process.stderr)
            self.assertIn("stopped before its downloader was invoked", process.stderr)
            self.assertFalse(marker.exists())

    def test_electron_prepare_offline_uses_local_runtime_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-electron-offline-ready-") as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(PREPARE_ELECTRON, scripts / PREPARE_ELECTRON.name)
            source_app = project / "node_modules" / "electron" / "dist" / "Electron.app"
            (source_app / "Contents" / "MacOS").mkdir(parents=True)
            self._write_executable(
                source_app / "Contents" / "MacOS" / "Electron",
                "#!/bin/sh\nexit 0\n",
            )

            marker = root / "network-command.log"
            fake_bin = root / "fake-bin"
            self._write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
            self._write_executable(fake_bin / "xattr", "#!/bin/sh\nexit 0\n")
            self._write_executable(fake_bin / "codesign", "#!/bin/sh\nexit 0\n")
            self._write_executable(
                fake_bin / "ditto",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    /bin/mkdir -p "$2/Contents/MacOS"
                    /bin/cp "$1/Contents/MacOS/Electron" "$2/Contents/MacOS/Electron"
                    """
                ),
            )
            self._write_executable(
                project / "node_modules" / ".bin" / "electron",
                f"#!/usr/bin/env bash\nprintf electron >> {marker!s}\nexit 97\n",
            )
            state = root / "state"

            process = subprocess.run(
                ["bash", str(scripts / PREPARE_ELECTRON.name)],
                cwd=project,
                env=self._environment(
                    fake_bin,
                    MEMOLENS_APP_STATE_DIR=str(state),
                    MEMOLENS_NETWORK_PROFILE="offline",
                ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue((state / "runtime" / "Electron.app").is_dir())
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
