"""Inspect wheel/sdist bytes and run the smoke test against clean installs."""

import hashlib
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "remote_intent/errors/catalog.json": ROOT / "src/remote_intent/errors/catalog.json",
    "remote_intent/SKILL.md": ROOT / "skills/remote-intent/SKILL.md",
}


def main():
    (wheel,) = (ROOT / "dist").glob("*.whl")
    (sdist,) = (ROOT / "dist").glob("*.tar.gz")
    expected = [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES.values()
    ]
    with zipfile.ZipFile(wheel) as archive:
        for resource, source in SOURCES.items():
            assert archive.read(resource) == source.read_bytes(), resource
        assert "remote_intent/py.typed" in archive.namelist()
    with tarfile.open(sdist) as archive:
        for source in SOURCES.values():
            member = next(
                name
                for name in archive.getnames()
                if name.endswith(f"/{source.relative_to(ROOT).as_posix()}")
            )
            stream = archive.extractfile(member)
            assert stream is not None and stream.read() == source.read_bytes()
    for artifact in (wheel, sdist):
        with TemporaryDirectory(prefix="rir-smoke-") as temp:
            folder = Path(temp)
            venv.EnvBuilder(with_pip=True).create(folder)
            python = folder / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "-c",
                    str(ROOT / "requirements-lock.txt"),
                    str(artifact),
                ],
                check=True,
                cwd=folder,
            )
            subprocess.run(
                [str(python), "-I", str(ROOT / "scripts/smoke.py"), *expected],
                check=True,
                cwd=folder,
            )
            if artifact == wheel:
                subprocess.run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "-c",
                        str(ROOT / "requirements-lock.txt"),
                        f"{wheel}[mcp]",
                    ],
                    check=True,
                    cwd=folder,
                )
                subprocess.run(
                    [
                        str(python),
                        "-I",
                        str(ROOT / "scripts/smoke.py"),
                        "--mcp",
                        *expected,
                    ],
                    check=True,
                    cwd=folder,
                )
        print(artifact.name, hashlib.sha256(artifact.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
