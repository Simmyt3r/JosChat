"""Start the local app shell and execute the calling browser checks."""
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ.setdefault("LEDGER_SIGNING_KEY","browser-test-only-key")
os.environ.update(SUPABASE_URL='https://example.supabase.co',SUPABASE_ANON_KEY='dummy',POSTGRES_URL='postgres://u:p@localhost/postgres',CLOUDINARY_CLOUD_NAME='demo',CLOUDINARY_API_KEY='k',CLOUDINARY_API_SECRET='s')
from app import create_app
from werkzeug.serving import make_server

server=make_server('localhost',5055,create_app(),threaded=True)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    for script in sys.argv[1:] or ['run_calls_ui.py','run_webrtc.py']:
        result=subprocess.run([sys.executable,str(ROOT/'tests/e2e'/script)],cwd=ROOT)
        if result.returncode: sys.exit(result.returncode)
finally: server.shutdown()
