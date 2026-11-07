import asyncio

import pytest

from remote_intent_evals.run import configuration, run_fixture


@pytest.mark.live_llm
@pytest.mark.parametrize("access", ["allow", "deny", "requires_approval"])
def test_live_model(access):
    result = asyncio.run(run_fixture(*configuration(True), 42, access, 12))
    assert result["passed"]
