"""Verify version and release tag metadata."""

import os
import re
import tomllib
from pathlib import Path

version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
    raise SystemExit("Expected a final semantic version")
if os.environ.get("GITHUB_REF") != f"refs/tags/v{version}":
    raise SystemExit("Version tag does not match package metadata")
