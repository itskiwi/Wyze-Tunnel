import os
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
TMP = Path(tempfile.mkdtemp(prefix="wyze-tunnel-test-"))

# config.py reads these at import time, so set them before any app module loads.
os.environ.setdefault("TOKEN_PATH", str(TMP / "tokens"))
os.environ.setdefault("IMG_DIR", str(TMP / "img"))
os.environ.setdefault("MEDIAMTX_CONFIG", str(TMP / "mediamtx.yml"))
(TMP / "mediamtx.yml").write_text("paths: {}\n")
sys.path.insert(0, str(APP))
os.chdir(APP)
