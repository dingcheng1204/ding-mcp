# Vercel entrypoint: Vercel's Python runtime serves the ASGI `app` found in this file.
# The real code lives in server.py at the project root, so add the root to the import path.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app  # noqa: E402,F401
