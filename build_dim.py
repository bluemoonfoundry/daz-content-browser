#!/usr/bin/env python
"""Build a DAZ Install Manager (DIM) package for BMF Content Browser.

The package bundles:
  - The plugin DLL (from the bmf-daz-content-browser-plugin build)
  - The PyInstaller server bundle (dist/vab/)
  - The pre-exported ONNX model (models/bge-large-en-v1.5/)

Install layout inside the DIM zip (zip root = DAZ Studio application directory):
  plugins/BmfContentBrowser/BmfContentBrowser.dll
  resources/BlueMoonFoundry/ContentBrowser/server/vab/   (PyInstaller bundle)
  resources/BlueMoonFoundry/ContentBrowser/models/bge-large-en-v1.5/

DIM installs everything relative to the application directory (InstallTypes=Application).
resources/ is a sibling of plugins/ and is never scanned for plugin DLLs.
The plugin locates the server via QCoreApplication::applicationDirPath() +
"resources/BlueMoonFoundry/ContentBrowser/server/vab/vab.exe".

Usage:
    python build_dim.py [options]

Options:
    --plugin-dll PATH   Path to BmfContentBrowser.dll (Windows) or .dylib (macOS)
    --server-dir PATH   Path to PyInstaller output directory (default: dist/vab)
    --model-dir PATH    Path to exported ONNX model directory (default: models/bge-large-en-v1.5)
    --version VERSION   Package version string (default: 1.0.0)
    --out-dir PATH      Output directory (default: dist)
    --platform PLATFORM windows or macos (default: auto-detect)

Makefile: make release-dim
Prereqs:
    make release-exe        (builds dist/vab/)
    python export_model.py  (exports ONNX model to models/bge-large-en-v1.5/)
    cmake --build ...       (builds BmfContentBrowser.dll from plugin repo)
"""
import argparse
import platform
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).parent
DIST = ROOT / "dist"

VENDOR = "BlueMoonFoundry"
PRODUCT = "ContentBrowser"
PLUGIN_NAME = "BmfContentBrowser"
MODEL_NAME = "bge-large-en-v1.5"

# DIM zip root maps directly to the DAZ Studio application directory.
# plugins/ and resources/ are siblings in that directory; DAZ only scans
# plugins/ for plugin DLLs — resources/ is never plugin-scanned.
PLUGIN_DEST = f"plugins/{PLUGIN_NAME}"
SERVER_DEST = f"resources/{VENDOR}/{PRODUCT}/server"
MODEL_DEST = f"resources/{VENDOR}/{PRODUCT}/models/{MODEL_NAME}"


def detect_platform() -> str:
    s = platform.system()
    if s == "Windows":
        return "windows"
    if s == "Darwin":
        return "macos"
    sys.exit(f"ERROR: unsupported platform '{s}'. Pass --platform windows or macos.")


def find_plugin_dll(plat: str, override: str | None) -> Path:
    if override:
        p = Path(override)
        if not p.exists():
            sys.exit(f"ERROR: plugin DLL not found at {p}")
        return p

    # Convention: plugin repo lives alongside this repo
    plugin_repo = ROOT.parent.parent / "bmf-daz-content-browser-plugin"
    if plat == "windows":
        candidates = [
            plugin_repo / "build" / "plugin" / "Release" / f"{PLUGIN_NAME}.dll",
            plugin_repo / "build" / "Release" / f"{PLUGIN_NAME}.dll",
        ]
        ext = ".dll"
    else:
        candidates = [
            plugin_repo / "build" / "plugin" / f"{PLUGIN_NAME}.dylib",
            plugin_repo / "build" / f"{PLUGIN_NAME}.dylib",
        ]
        ext = ".dylib"

    for c in candidates:
        if c.exists():
            return c

    sys.exit(
        f"ERROR: {PLUGIN_NAME}{ext} not found. Build the plugin first, or pass --plugin-dll."
    )


def stage_files(staging: Path, plugin_dll: Path, server_dir: Path, model_dir: Path) -> list[str]:
    """Copy all files into staging/ and return a list of relative paths (for the manifest)."""
    staged_paths: list[str] = []

    def copy_file(src: Path, rel_dest: str):
        dst = staging / rel_dest
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        staged_paths.append(rel_dest)

    def copy_tree(src: Path, rel_dest_prefix: str):
        for item in src.rglob("*"):
            if item.is_file():
                rel = item.relative_to(src)
                copy_file(item, f"{rel_dest_prefix}/{rel}")

    # Plugin DLL
    dll_dest = f"{PLUGIN_DEST}/{plugin_dll.name}"
    copy_file(plugin_dll, dll_dest)
    print(f"  [dll]    {plugin_dll.name} -> {PLUGIN_DEST}/")

    # PyInstaller server bundle (entire directory)
    server_bundle_name = server_dir.name  # typically "vab"
    server_dest_prefix = f"{SERVER_DEST}/{server_bundle_name}"
    n_before = len(staged_paths)
    copy_tree(server_dir, server_dest_prefix)
    print(f"  [server] {server_dir.name}/ ({len(staged_paths) - n_before} files) -> {SERVER_DEST}/")

    # ONNX model directory
    model_dest_prefix = MODEL_DEST
    n_before = len(staged_paths)
    copy_tree(model_dir, model_dest_prefix)
    print(f"  [model]  {model_dir.name}/ ({len(staged_paths) - n_before} files) -> {MODEL_DEST}/")

    return staged_paths


def write_manifest(staging: Path, staged_paths: list[str], plat: str):
    # PLATFORM values per DIM spec: "PC" (Windows) or "Mac" (macOS)
    plat_tag = "PC" if plat == "windows" else "Mac"
    # Use a stable UUID derived from product name so re-builds produce the same GlobalID
    global_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bluemoonfoundry.{PRODUCT}"))

    file_entries = "\n".join(
        f'  <File TARGET="{p}" ACTION="Install" PLATFORM="{plat_tag}" BITARCH="64"/>'
        for p in sorted(staged_paths)
    )

    manifest = dedent(f"""\
        <DAZInstallManifest VERSION="0.1">
          <GlobalID VALUE="{global_id}"/>
        {file_entries}
        </DAZInstallManifest>
    """)

    manifest_path = staging / "Manifest.dsx"
    manifest_path.write_text(manifest, encoding="utf-8")
    print(f"  [manifest] Manifest.dsx ({len(staged_paths)} entries)")


def write_supplement(staging: Path):
    # ProductStoreIDX format: {SKU}-{PackageID}
    supplement = dedent(f"""\
        <ProductSupplement VERSION="0.1">
          <ProductName VALUE="BMF Content Browser"/>
          <ProductStoreIDX VALUE="999101-1"/>
          <InstallTypes VALUE="Application"/>
          <ProductTags VALUE="DAZStudio4_5"/>
        </ProductSupplement>
    """)
    (staging / "Supplement.dsx").write_text(supplement, encoding="utf-8")


def zip_staging(staging: Path, out_path: Path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in sorted(staging.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(staging))
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"\nCreated {out_path} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Build BMF Content Browser DIM package")
    parser.add_argument("--plugin-dll", help="Path to BmfContentBrowser.dll / .dylib")
    parser.add_argument("--server-dir", help="PyInstaller output dir (default: dist/vab)")
    parser.add_argument("--model-dir", help="ONNX model dir (default: models/bge-large-en-v1.5)")
    parser.add_argument("--version", default="1.0.0", help="Package version (default: 1.0.0)")
    parser.add_argument("--out-dir", help="Output directory (default: dist)")
    parser.add_argument("--platform", choices=["windows", "macos"], help="Target platform")
    args = parser.parse_args()

    plat = args.platform or detect_platform()
    version = args.version
    out_dir = Path(args.out_dir) if args.out_dir else DIST

    server_dir = Path(args.server_dir) if args.server_dir else ROOT / "dist" / "vab"
    model_dir = Path(args.model_dir) if args.model_dir else ROOT / "models" / MODEL_NAME

    plugin_dll = find_plugin_dll(plat, args.plugin_dll)

    # Validate inputs
    errors = []
    if not server_dir.exists():
        errors.append(f"Server bundle not found: {server_dir}\n  → Run: make release-exe")
    if not model_dir.exists():
        errors.append(f"ONNX model not found: {model_dir}\n  → Run: python export_model.py")
    if errors:
        print("ERROR: missing build inputs:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / "dim-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()

    out_zip = out_dir / "IM00999101-01_BmfDazContentNLBrowser.zip"

    print(f"Building DIM package  version={version}  platform={plat}")
    print(f"  plugin:  {plugin_dll}")
    print(f"  server:  {server_dir}")
    print(f"  model:   {model_dir}")
    print()

    staged = stage_files(staging, plugin_dll, server_dir, model_dir)
    write_manifest(staging, staged, plat)
    write_supplement(staging)
    zip_staging(staging, out_zip)
    shutil.rmtree(staging)


if __name__ == "__main__":
    main()
