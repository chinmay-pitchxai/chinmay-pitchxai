"""Entry: ``uvicorn backend.main:app`` from project root."""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Initialize DB at import time so --lifespan off still works
from core.storage import init_db
_data_dir = os.environ.get("VERN_DATA_DIR") or ""
if _data_dir:
    _data_dir = os.path.abspath(_data_dir)
else:
    _data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
init_db(_data_dir)

# Initialize state cache at import time
from core.state import init_state
init_state()

from config import settings
from api.app import app

__all__ = ["app"]


if __name__ == "__main__":
    if sys.platform == "win32":
        import asyncio

        # Avoid intermittent Proactor accept failures (WinError 64) that can
        # leave the process alive while port 9090 stops accepting connections.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import uvicorn

    uvicorn.run(app, host=settings.host, port=int(settings.port), log_level="info")
