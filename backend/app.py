import os
from pathlib import Path
import sys


# This is a writer-process entrypoint, not an import-time library contract.
# Every supported launcher must start it with ``python -I`` so PYTHONPATH,
# PYTHONHOME, user-site hooks, and related startup injection cannot self-attest
# a forged SQLite capability before Core admission runs.
if not sys.flags.isolated:
    print(
        "MemoLens backend must start with isolated Python (-I); use "
        "scripts/run_python.sh backend/app.py.",
        file=sys.stderr,
    )
    raise SystemExit(78)


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

# Import the backend through its canonical ``backend.src`` package name.  Using
# the shorter ``src`` alias here while routes import ``backend.src`` loads the
# same files twice under different module identities; runtime objects then fail
# exact type checks (notably the Agent pairing broker).
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.src import create_app
from backend.src.media.library_bootstrap import load_bootstrap_candidate_from_environment
from backend.src.process_lifecycle import run_managed_backend


bootstrap_candidate = load_bootstrap_candidate_from_environment()
app = (
    create_app(bootstrap_candidate=bootstrap_candidate)
    if bootstrap_candidate is not None
    else create_app()
)


if __name__ == "__main__":
    host = os.environ.get("MEMOLENS_BACKEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("MEMOLENS_BACKEND_PORT", "5519"))
    debug = os.environ.get("MEMOLENS_BACKEND_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # A reloader would create a second writer process outside Electron's
    # lifecycle ownership. Debug diagnostics may be enabled, but process
    # supervision always remains single-owner.
    run_managed_backend(app, host=host, port=port, debug=debug)
