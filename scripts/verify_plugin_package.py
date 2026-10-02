#!/usr/bin/env python3
"""Verify the actual npm archive outside the checkout and private app state."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / ".agents" / "plugins" / "plugins" / "memolens"
SCHEMAS = (
    "creative-blueprint-candidate-v1.schema.json",
    "creative-blueprint-v1.schema.json",
    "creative-blueprint-residual-v1.schema.json",
)


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(
            f"Package check failed ({Path(command[0]).name}): {result.stderr.strip()}"
        )
    return result.stdout


def main() -> int:
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise RuntimeError("npm and Node.js are required for package verification")
    with tempfile.TemporaryDirectory(prefix="memolens-plugin-package-") as temporary:
        root = Path(temporary).resolve()
        env = {
            key: value for key, value in os.environ.items()
            if key in {"PATH", "SYSTEMROOT", "SystemRoot", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
        }
        env.update({
            "HOME": str(root / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MEMOLENS_APP_STATE_DIR": str(root / "state"),
            "MEMOLENS_DB_PATH": str(root / "missing-index.db"),
            "MEMOLENS_LIBRARY_DIR": str(root / "empty-library"),
            "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0",
        })
        packed = json.loads(run(
            [npm, "pack", "--ignore-scripts", "--offline", "--json", "--pack-destination", str(root)],
            cwd=PLUGIN_ROOT, env=env,
        ))
        filename = packed[0]["filename"]
        if Path(filename).name != filename:
            raise RuntimeError("npm returned a nonlocal archive name")
        with tarfile.open(root / filename, "r:gz") as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "package":
                    raise RuntimeError("Package archive contains an unsafe path")
                target = root.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError("Package archive member cannot be read")
                    with source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                else:
                    raise RuntimeError("Package archive contains a link or special file")
        package = root / "package"
        for name in SCHEMAS:
            if (package / "schemas" / name).read_bytes() != (PLUGIN_ROOT / "schemas" / name).read_bytes():
                raise RuntimeError(f"Packaged schema differs from source: {name}")
        license_text = (package / "LICENSE").read_text(encoding="utf-8")
        if not license_text.startswith((REPO_ROOT / "LICENSE").read_text(encoding="utf-8").rstrip()):
            raise RuntimeError("Standalone plugin does not contain the complete repository license")
        if "https://github.com/bingjiezhu/MemoLens/blob/main/COMMERCIAL-LICENSE.md" not in license_text:
            raise RuntimeError("Standalone plugin is missing the commercial-license notice link")
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if manifest["license"] != "SEE LICENSE IN LICENSE":
            raise RuntimeError("Package license metadata disagrees with the shipped license")
        schema_program = (
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "import memolens_creative_blueprint as candidate; "
            "import memolens_persisted_blueprint as persisted; "
            "assert candidate.BLUEPRINT_SCHEMA_AVAILABLE; "
            "assert persisted.PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE; "
            "assert persisted.PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA is not None; "
            "print(json.dumps({'schema_count':3}))"
        )
        run([sys.executable, "-I", "-c", schema_program, str(package / "scripts")], cwd=root, env=env)
        cli = package / "scripts" / "memolens_cli.py"
        run([sys.executable, "-E", "-s", str(cli), "--help"], cwd=root, env=env)
        status = json.loads(run([sys.executable, "-E", "-s", str(cli), "status"], cwd=root, env=env))
        if not isinstance(status, dict):
            raise RuntimeError("Packaged CLI did not return a JSON status object")
        module_program = (
            "const {apply}=await import(process.argv[1]); let bundle; "
            "apply({provide(name,value){if(name==='memolensBundle')bundle=value;}}); "
            "if(bundle?.root!==process.argv[2])throw new Error('wrong installed root');"
        )
        run([node, "--input-type=module", "-e", module_program, (package / "index.js").as_uri(), str(package)], cwd=root, env=env)
        print(json.dumps({
            "gate": "plugin-package", "status": "passed", "schema_count": len(SCHEMAS),
            "package_files": packed[0]["entryCount"], "cli": "help-and-status", "license": "complete",
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
