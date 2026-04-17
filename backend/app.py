import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
