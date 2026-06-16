#!/usr/bin/env python3
"""Build an MCPB bundle for mcp-server-matrix."""

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
PYPROJECT = ROOT / "pyproject.toml"
DEFAULT_OUTPUT = ROOT / "dist"
ROOT_FILES = ["README.md", "README.ru.md", "LICENSE"]
PACKAGE_DIRS = ["src"]


def read_pyproject_value(key: str) -> str:
    """Extract a scalar `key = "value"` entry from pyproject.toml."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]+)"$', text, re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find {key!r} in pyproject.toml")
    return match.group(1)


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST_SOURCE.read_text(encoding="utf-8"))
    project_name = read_pyproject_value("name")
    project_version = read_pyproject_value("version")

    if manifest.get("name") != project_name:
        raise ValueError(
            f"Manifest name {manifest.get('name')!r} does not match pyproject name {project_name!r}"
        )
    if manifest.get("version") != project_version:
        raise ValueError(
            f"Manifest version {manifest.get('version')!r} does not match pyproject version {project_version!r}"
        )

    return manifest


def bundle_files() -> list[Path]:
    files = [PYPROJECT]
    for relpath in ROOT_FILES:
        path = ROOT / relpath
        if path.is_file():
            files.append(path)

    for relpath in PACKAGE_DIRS:
        base = ROOT / relpath
        if not base.exists():
            continue
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )

    return files


def write_bundle(output_path: Path) -> tuple[Path, str]:
    manifest = load_manifest()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")

        for file_path in sorted(bundle_files()):
            arcname = file_path.relative_to(ROOT).as_posix()
            archive.write(file_path, arcname)

    sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return output_path, sha256


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Path to the output .mcpb file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    project_version = read_pyproject_value("version")
    project_name = read_pyproject_value("name")
    output_path = args.output or DEFAULT_OUTPUT / f"{project_name}-{project_version}.mcpb"
    bundle_path, sha256 = write_bundle(output_path)
    print(bundle_path)
    print(sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
