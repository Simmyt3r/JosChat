"""Serves the real app on :5055 with dummy credentials, for the browser tests."""
import os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.update(
    SUPABASE_URL="https://example.supabase.co", SUPABASE_ANON_KEY="sb_publishable_dummy",
    POSTGRES_URL="postgres://u:p@localhost:5432/postgres",
    CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="k", CLOUDINARY_API_SECRET="s",
)
from app import app
app.run(port=5055, use_reloader=False)
