#!/usr/bin/env python3
"""Build the Claude Desktop .mcpb bundle for mcp-server-matrix.

The bundle carries mcpb/manifest.json plus the project sources. The manifest
uses server.type "uv": Claude Desktop installs Python and the dependencies
from pyproject.toml (and uv.lock) itself, so users need nothing preinstalled.
The bundle version always comes from pyproject.toml.

Usage: python scripts/build_mcpb.py [--out dist/] [-o path/to/file.mcpb]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SOURCE = ROOT / "mcpb" / "manifest.json"
ICON = ROOT / "mcpb" / "icon.png"
PYPROJECT = ROOT / "pyproject.toml"
ROOT_FILES = ["README.md", "README.ru.md", "LICENSE"]
PACKAGE_PATHS = ["src/mcp_server_matrix"]
SKIP_DIRS = {"__pycache__", "tests"}


def read_pyproject_value(key: str) -> str:
    """Extract a scalar `key = "value"` entry from pyproject.toml."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]+)"$', text, re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find {key!r} in pyproject.toml")
    return match.group(1)


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST_SOURCE.read_text(encoding="utf-8"))
    manifest["version"] = read_pyproject_value("version")

    # Claude Desktop leaves "${user_config.x}" verbatim when an optional field
    # has no value and no default, and the server would get that literal.
    for key, option in manifest.get("user_config", {}).items():
        if not option.get("required") and "default" not in option:
            raise ValueError(f"Optional user_config {key!r} needs a default")
    if ICON.is_file():
        manifest["icon"] = "icon.png"
    return manifest


def bundle_files() -> list[Path]:
    files = [PYPROJECT]
    for relpath in ROOT_FILES:
        path = ROOT / relpath
        if path.is_file():
            files.append(path)

    for relpath in PACKAGE_PATHS:
        base = ROOT / relpath
        if base.is_file():
            files.append(base)
            continue
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file()
            and not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
            and path.suffix not in {".pyc", ".pyo"}
        )

    return files


def write_bundle(output_path: Path) -> tuple[Path, str]:
    manifest = load_manifest()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        if ICON.is_file():
            archive.write(ICON, "icon.png")

        for file_path in sorted(bundle_files()):
            arcname = file_path.relative_to(ROOT).as_posix()
            archive.write(file_path, arcname)

    sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return output_path, sha256


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="Output directory")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Path to the output .mcpb file (overrides --out)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    name = json.loads(MANIFEST_SOURCE.read_text(encoding="utf-8"))["name"]
    output_path = args.output or args.out / f"{name}-{read_pyproject_value('version')}.mcpb"
    bundle_path, sha256 = write_bundle(output_path)
    print(bundle_path)
    print(sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
