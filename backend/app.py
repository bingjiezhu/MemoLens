import os
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

# Prefer backend/ on sys.path so `import src` resolves to backend/src,
# not the React renderer folder at repo-root src/.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from src import create_app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("MEMOLENS_BACKEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("MEMOLENS_BACKEND_PORT", "5519"))
    debug = os.environ.get("MEMOLENS_BACKEND_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.run(host=host, port=port, debug=debug, use_reloader=debug)
