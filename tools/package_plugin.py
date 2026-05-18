#!/usr/bin/env python3
"""Package the plugin into an upload-ready zip for plugins.qgis.org.

Layout produced (mirrors the format the Plugin Manager expects):

    dist/align2qgis-<version>.zip
        align2qgis/
            __init__.py
            align2qgis_plugin.py
            metadata.txt
            ...

Reads the version from ``plugin/metadata.txt`` so the artifact name
tracks the metadata source of truth. Excludes ``__pycache__``,
``*.pyc``, and other dev-only cruft.

Usage:
    python tools/package_plugin.py            # writes dist/align2qgis-<version>.zip
    python tools/package_plugin.py --out foo  # writes foo/align2qgis-<version>.zip

Adopted from the opengeos/qgis-plugin-template approach.
"""
from __future__ import annotations

import argparse
import configparser
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SRC = ROOT / "plugin"
PLUGIN_FOLDER_NAME = "align2qgis"

# Anything matching these patterns is dropped from the zip. Keep the
# uploaded artifact lean — plugins.qgis.org rejects oversized uploads.
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def read_version(metadata_path: Path) -> str:
    parser = configparser.ConfigParser()
    parser.read(metadata_path, encoding="utf-8")
    return parser["general"]["version"].strip()


def iter_plugin_files(src: Path):
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(src).parts):
            continue
        yield path


def build_zip(out_dir: Path) -> Path:
    metadata = PLUGIN_SRC / "metadata.txt"
    if not metadata.exists():
        sys.exit(f"metadata.txt not found at {metadata}")

    version = read_version(metadata)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{PLUGIN_FOLDER_NAME}-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()

    files = list(iter_plugin_files(PLUGIN_SRC))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = Path(PLUGIN_FOLDER_NAME) / path.relative_to(PLUGIN_SRC)
            zf.write(path, arcname)

    print(f"wrote {zip_path} ({len(files)} files, {zip_path.stat().st_size} bytes)")
    return zip_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="dist", help="output directory (default: dist)")
    ap.add_argument(
        "--clean", action="store_true", help="remove the output directory first",
    )
    args = ap.parse_args()

    out_dir = (ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    build_zip(out_dir)


if __name__ == "__main__":
    main()
