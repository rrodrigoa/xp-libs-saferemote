"""Verify the exact wheel and source distribution available on an index."""

import hashlib
import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import urlopen

INDEXES = {
    "pypi": ("pypi.org", "files.pythonhosted.org"),
    "testpypi": ("test.pypi.org", "test-files.pythonhosted.org"),
}


def fetch(host, path):
    parsed = urlsplit(path)
    if parsed.scheme != "https" or parsed.netloc != host:
        raise ValueError("Unexpected index host")
    with urlopen(path, timeout=30) as response:
        if urlsplit(response.geturl()).netloc != host:
            raise ValueError("Unexpected index redirect")
        data = response.read(20_000_001)
        if len(data) > 20_000_000:
            raise ValueError("Index response too large")
        return data


def main():
    index, files_host = INDEXES[sys.argv[1]]
    version = os.environ["GITHUB_REF_NAME"].removeprefix("v")
    paths = sorted(Path("dist").iterdir())
    expected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
    if len(expected) != 2:
        raise SystemExit("Expected one wheel and one source distribution")
    for attempt in range(6):
        try:
            data = json.loads(
                fetch(index, f"https://{index}/pypi/xp-libs-saferemote/{version}/json")
            )
        except OSError:
            if attempt == 5:
                raise
            time.sleep(10)
        else:
            break
    files = {entry["filename"]: entry for entry in data["urls"]}
    if set(files) != set(expected):
        raise SystemExit("Published file set does not match release")
    with TemporaryDirectory(prefix="rir-index-smoke-") as temp:
        folder = Path(temp)
        for name, sha in expected.items():
            entry = files[name]
            remote = fetch(files_host, entry["url"])
            if (
                entry["digests"]["sha256"] != sha
                or hashlib.sha256(remote).hexdigest() != sha
            ):
                raise SystemExit("Index artifact digest mismatch")
            (folder / name).write_bytes(remote)
        venv.EnvBuilder(with_pip=True).create(folder / "venv")
        python = str(folder / "venv/bin/python")
        wheel = next(folder.glob("*.whl"))
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                f"{wheel}[mcp]",
            ],
            check=True,
        )
        subprocess.run(
            [python, "-I", str(Path("scripts/smoke.py").resolve()), "--mcp"],
            check=True,
            cwd=folder,
        )


if __name__ == "__main__":
    main()
