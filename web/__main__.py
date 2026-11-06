"""Run the site locally.

  python -m web --dev     local development: reads .env.dev (WEB_DEV=1, the demo login), http://127.0.0.1:8000
  python -m web           production-like: WEB_SECRET, a mail key and the rest must be set (see .env.example)

The demo login (WEB_DEMO_EMAIL / WEB_DEMO_CODE) exists only with --dev and only from the local, untracked .env.dev.
"""

import os
import sys

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    if "--dev" in sys.argv:
        load_dotenv(".env.dev", override=True)
        os.environ["WEB_DEV"] = "1"
    from .main import create_app

    uvicorn.run(create_app(), host=os.environ.get("WEB_HOST", "127.0.0.1"), port=8000)  # WEB_HOST=0.0.0.0 in Docker
