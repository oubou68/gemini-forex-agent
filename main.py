"""
AURA QUANT AI - Root Application Entrypoint
Used by Google Cloud Run, Cloud Functions, and ASGI servers.
"""
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from web.app import app

# Expose app for ASGI servers and Google Cloud Run
__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn
    from config.settings import settings
    port = int(os.environ.get("PORT", settings.SERVER_PORT))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
