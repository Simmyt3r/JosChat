"""
api/index.py
------------
Vercel's Python runtime looks for a WSGI/ASGI `app` object under the
`api/` directory. This file just re-exports the real Flask app from the
project root so `vercel.json` can route all /api/* traffic here.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

# Vercel's Python builder expects a module-level variable named `app`.
