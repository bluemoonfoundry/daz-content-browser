#!/usr/bin/env python
"""Build a DAZ Install Manager (DIM) package for BMF Content Browser.

Supports building multiple package variants by enabling/disabling components:

Base package (~162 MB) — default:
  - Plugin DLL
  - PyInstaller server bundle (dist/vab/)

Export-tool package (~2 GB) — pass --no-plugin --no-server --export-tool-dir dist/vab_export:
  - Model export tool (dist/vab_export/)

Model package — pass --include-model:
  - Plugin DLL + server + pre-exported ONNX model

Install layout inside the DIM zip (zip root = DAZ Studio application directory):
  plugins/BmfContentBrowser/BmfContentBrowser.dll
  resources/BlueMoonFoundry/ContentBrowser/server/vab/
  resources/BlueMoonFoundry/ContentBrowser/server/vab_export/  (when included)
  resources/BlueMoonFoundry/ContentBrowser/models/<model-name>/  (when included)

DIM installs everything relative to the application directory (InstallTypes=Application).
The plugin locates the server via QCoreApplication::applicationDirPath() +
"resources/BlueMoonFoundry/ContentBrowser/server/vab/vab.exe".

Usage:
    python build_dim.py [options]

Options:
    --plugin-dll PATH      Path to BmfContentBrowser.dll (Windows) or .dylib (macOS)
    --server-dir PATH      Path to PyInstaller output directory (default: dist/vab)
    --export-tool-dir PATH Path to export tool directory (optional, not included by default)
    --include-model PATH   Include ONNX model dir in this package (default: omitted)
    --no-plugin            Exclude the plugin DLL (for export-tool-only packages)
    --no-server            Exclude the server bundle (for export-tool-only packages)
    --product-id ID        Product store ID for Supplement.dsx (default: 999101-1)
    --product-name NAME    Product name for Supplement.dsx (default: BMF Content Browser)
    --out-file NAME        Output zip filename (default: auto-generated from product-id)
    --version VERSION      Package version string (default: 1.0.0)
    --out-dir PATH         Output directory (default: dist)
    --platform PLATFORM    windows or macos (default: auto-detect)

Makefile: make release-dim
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

# DIM install-target directory name and version compatibility string.
# All content files are nested under this directory inside the zip; Manifest.dsx
# and Supplement.dsx sit at the zip root.  The version string matches DAZ Studio 4.5+.
DIM_APP_DIR = "DAZ Studio_4.5;4.x Public Build;4.x Publishing Build_(64-Bit)"
DIM_VERSION = "4.5;4.x Public Build;4.x Publishing Build"

# Destination paths relative to the DAZ Studio application directory.
# plugins/ and resources/ are siblings; DAZ only scans plugins/ for plugin DLLs.
PLUGIN_DEST = f"plugins/{PLUGIN_NAME}"
SERVER_DEST = f"resources/{VENDOR}/{PRODUCT}/server"
MODELS_DEST = f"resources/{VENDOR}/{PRODUCT}/models"


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


def stage_files(
    staging: Path,
    plugin_dll: Path | None,
    server_dir: Path | None,
    model_dir: Path | None,
    export_tool_dir: Path | None,
) -> list[str]:
    """Copy all files into staging/ and return a list of zip-relative paths (for the manifest).

    Content is nested under DIM_APP_DIR inside the zip; Manifest.dsx and Supplement.dsx
    are written directly to staging/ (zip root) by their own functions.
    """
    staged_paths: list[str] = []

    def copy_file(src: Path, rel_dest: str):
        # rel_dest is relative to the DAZ Studio application directory
        zip_path = f"{DIM_APP_DIR}/{rel_dest}"
        dst = staging / zip_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        staged_paths.append(zip_path)

    def copy_tree(src: Path, rel_dest_prefix: str):
        for item in src.rglob("*"):
            if item.is_file():
                rel = item.relative_to(src).as_posix()
                copy_file(item, f"{rel_dest_prefix}/{rel}")

    # Plugin DLL (optional — omitted for export-tool-only packages)
    if plugin_dll is not None:
        dll_dest = f"{PLUGIN_DEST}/{plugin_dll.name}"
        copy_file(plugin_dll, dll_dest)
        print(f"  [dll]    {plugin_dll.name} -> {PLUGIN_DEST}/")
    else:
        print("  [dll]    skipped (--no-plugin)")

    # PyInstaller server bundle (optional — omitted for export-tool-only packages)
    if server_dir is not None:
        server_bundle_name = server_dir.name  # typically "vab"
        server_dest_prefix = f"{SERVER_DEST}/{server_bundle_name}"
        n_before = len(staged_paths)
        copy_tree(server_dir, server_dest_prefix)
        print(f"  [server] {server_dir.name}/ ({len(staged_paths) - n_before} files) -> {SERVER_DEST}/")
    else:
        print("  [server] skipped (--no-server)")

    # Model export tool (optional — large bundle, includes torch/transformers)
    if export_tool_dir is not None:
        export_bundle_name = export_tool_dir.name  # typically "vab_export"
        export_dest_prefix = f"{SERVER_DEST}/{export_bundle_name}"
        n_before = len(staged_paths)
        copy_tree(export_tool_dir, export_dest_prefix)
        print(f"  [export] {export_tool_dir.name}/ ({len(staged_paths) - n_before} files) -> {SERVER_DEST}/")
    else:
        print("  [export] skipped (pass --export-tool-dir dist/vab_export to include)")

    # ONNX model directory (optional — omitted from base package, downloaded on demand)
    if model_dir is not None:
        model_dest_prefix = f"{MODELS_DEST}/{model_dir.name}"
        n_before = len(staged_paths)
        copy_tree(model_dir, model_dest_prefix)
        print(f"  [model]  {model_dir.name}/ ({len(staged_paths) - n_before} files) -> resources/.../models/")
    else:
        print("  [model]  skipped (pass --include-model <dir> to bundle a model)")

    return staged_paths


def write_manifest(staging: Path, staged_paths: list[str], plat: str):
    # PLATFORM values per DIM spec: "PC" (Windows) or "Mac" (macOS)
    plat_tag = "PC" if plat == "windows" else "Mac"
    # Use a stable UUID derived from product name so re-builds produce the same GlobalID
    global_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bluemoonfoundry.{PRODUCT}"))

    lines = ['<DAZInstallManifest VERSION="0.1">']
    lines.append(f' <GlobalID VALUE="{global_id}"/>')
    for p in sorted(staged_paths):
        lines.append(
            f' <File TARGET="Application" VERSION="{DIM_VERSION}" PLATFORM="{plat_tag}"'
            f' ACTION="Install" BITARCH="64" TYPE="DAZ Studio" VALUE="{p}"/>'
        )
    lines.append("</DAZInstallManifest>")

    manifest_path = staging / "Manifest.dsx"
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [manifest] Manifest.dsx ({len(staged_paths)} entries)")


def write_supplement(staging: Path, product_name: str = "BMF Content Browser", product_id: str = "999101-1"):
    supplement = dedent(f"""\
        <ProductSupplement VERSION="0.1">
          <ProductName VALUE="{product_name}"/>
          <ProductStoreIDX VALUE="{product_id}"/>
          <InstallTypes VALUE="Application"/>
          <ProductTags VALUE="DAZStudio4_5"/>
        </ProductSupplement>
    """)
    (staging / "Supplement.dsx").write_text(supplement, encoding="utf-8")


# Extensions that are already binary/compressed and gain little from deflate.
# Storing them uncompressed makes DIM installation fast (no decompression step).
_STORE_EXTENSIONS = {".onnx", ".bin", ".pt", ".pth"}


def zip_staging(staging: Path, out_path: Path):
    with zipfile.ZipFile(out_path, "w") as zf:
        for item in sorted(staging.rglob("*")):
            if not item.is_file():
                continue
            if item.suffix.lower() in _STORE_EXTENSIONS:
                zf.write(item, item.relative_to(staging).as_posix(),
                         compress_type=zipfile.ZIP_STORED)
            else:
                zf.write(item, item.relative_to(staging).as_posix(),
                         compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"\nCreated {out_path} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Build BMF Content Browser DIM package")
    parser.add_argument("--plugin-dll",    help="Path to BmfContentBrowser.dll / .dylib")
    parser.add_argument("--server-dir",    help="PyInstaller output dir (default: dist/vab)")
    parser.add_argument("--export-tool-dir", help="Export tool dir to include (optional)")
    parser.add_argument("--include-model", metavar="MODEL_DIR",
                        help="Include this ONNX model dir in the package (optional, not in base package)")
    parser.add_argument("--no-plugin",     action="store_true", help="Exclude the plugin DLL")
    parser.add_argument("--no-server",     action="store_true", help="Exclude the server bundle")
    parser.add_argument("--product-id",   default="999101-1",
                        help="Product store ID for Supplement.dsx (default: 999101-1)")
    parser.add_argument("--product-name", default="BMF Content Browser",
                        help="Product name for Supplement.dsx")
    parser.add_argument("--out-file",     help="Output zip filename (default: auto from product-id)")
    parser.add_argument("--version",      default="1.0.0", help="Package version (default: 1.0.0)")
    parser.add_argument("--out-dir",      help="Output directory (default: dist)")
    parser.add_argument("--platform",     choices=["windows", "macos"], help="Target platform")
    args = parser.parse_args()

    plat = args.platform or detect_platform()
    version = args.version
    out_dir = Path(args.out_dir) if args.out_dir else DIST

    include_plugin = not args.no_plugin
    include_server = not args.no_server

    plugin_dll: Path | None = None
    if include_plugin:
        plugin_dll = find_plugin_dll(plat, args.plugin_dll)

    server_dir: Path | None = None
    if include_server:
        server_dir = Path(args.server_dir) if args.server_dir else ROOT / "dist" / "vab"

    model_dir: Path | None = Path(args.include_model) if args.include_model else None
    export_tool_dir: Path | None = Path(args.export_tool_dir) if args.export_tool_dir else None

    # Validate that the paths that are supposed to exist actually do.
    errors = []
    if include_server and server_dir and not server_dir.exists():
        errors.append(f"Server bundle not found: {server_dir}\n  → Run: make release-exe")
    if model_dir and not model_dir.exists():
        errors.append(f"ONNX model not found: {model_dir}\n  → Run: python export_model.py")
    if export_tool_dir and not export_tool_dir.exists():
        errors.append(f"Export tool not found: {export_tool_dir}\n  → Run: make release-export-exe")
    if errors:
        print("ERROR: missing build inputs:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Use the output filename as the staging dir name so concurrent builds don't collide.
    staging = out_dir / (out_zip.stem + "-staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    if args.out_file:
        out_zip = out_dir / args.out_file
    elif args.product_id == "999101-1":
        out_zip = out_dir / "IM00999101-01_BmfDazContentNLBrowser.zip"
    elif args.product_id == "999101-2":
        out_zip = out_dir / "IM00999101-02_BmfDazContentNLBrowserExportTool.zip"
    else:
        safe_name = args.product_name.replace(" ", "")
        out_zip = out_dir / f"IM{args.product_id.replace('-', '')}_{safe_name}.zip"

    print(f"Building DIM package  version={version}  platform={plat}")
    print(f"  product: {args.product_name} ({args.product_id})")
    print(f"  plugin:  {plugin_dll or '(not included)'}")
    print(f"  server:  {server_dir or '(not included)'}")
    print(f"  export:  {export_tool_dir or '(not included)'}")
    print(f"  model:   {model_dir or '(not included)'}")
    print()

    staged = stage_files(staging, plugin_dll, server_dir, model_dir, export_tool_dir)
    write_manifest(staging, staged, plat)
    write_supplement(staging, args.product_name, args.product_id)
    zip_staging(staging, out_zip)
    shutil.rmtree(staging)


if __name__ == "__main__":
    main()
