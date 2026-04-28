"""Verify the UI build artifact is present and well-formed."""
from pathlib import Path

UI_DIST = Path(__file__).parent.parent / "ui" / "dist"


def test_ui_dist_exists():
    assert UI_DIST.exists(), "ui/dist/ not found — run 'make build' first"


def test_ui_index_html():
    index = UI_DIST / "index.html"
    assert index.exists(), "ui/dist/index.html missing"
    assert "<script" in index.read_text(encoding="utf-8"), "index.html has no script tags"


def test_ui_has_js_assets():
    assets = list((UI_DIST / "assets").glob("*.js"))
    assert len(assets) > 0, "No JS bundles found in ui/dist/assets/"
