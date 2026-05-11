import os
from pathlib import Path

APP_MODE = os.getenv("APP_MODE", "production")
DIST_PATH = (
    Path(__file__).parent.parent / "ui_dist"
    if (Path(__file__).parent.parent / "ui_dist").exists()
    else Path(__file__).parent.parent.parent / "ui" / "dist"
)
