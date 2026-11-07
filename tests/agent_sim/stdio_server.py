"""Disposable stdio MCP server for the independent protocol-client test."""

from pathlib import Path
from tempfile import TemporaryDirectory

from remote_intent_evals.fixture import create_fixture


def main():
    with TemporaryDirectory(prefix="rir-stdio-") as folder:
        server, _ = create_fixture(Path(folder) / "test.sqlite", 83)
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
