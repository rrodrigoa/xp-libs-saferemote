"""Smoke test a clean distribution install, optionally verifying resource digests."""

import hashlib
import sys
from importlib.resources import files
from importlib.util import find_spec

from remote_intent import ActionRegistry, WorkflowCompiler
from remote_intent.errors import get_error_info

package = files("remote_intent")
resources = [package.joinpath("errors", "catalog.json"), package.joinpath("SKILL.md")]
actual = [hashlib.sha256(resource.read_bytes()).hexdigest() for resource in resources]
expected = [arg for arg in sys.argv[1:] if arg != "--mcp"]
if expected:
    assert expected == actual
assert get_error_info("ACTION_UNKNOWN")["retryable"] is False
assert WorkflowCompiler(ActionRegistry(), "fake").registry.catalog("fake") == []
assert find_spec("azure") is None

if "--mcp" in sys.argv:
    import asyncio
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from fastmcp import Client

    from remote_intent.mcp import SKILL_URI
    from remote_intent_evals.fixture import TARGET, create_fixture

    async def check_protocol():
        with TemporaryDirectory(prefix="rir-installed-mcp-") as directory:
            server, calls = create_fixture(Path(directory) / "smoke.sqlite", 90)
            async with Client(server) as client:
                skill = await client.read_resource(SKILL_URI)
                assert skill[0].text == resources[1].read_text(encoding="utf-8")
                result = await client.call_tool("list_actions", {"target_id": TARGET})
                assert len(result.structured_content["data"]["actions"]) == 2
            assert calls == []

    asyncio.run(check_protocol())
