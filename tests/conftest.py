import os
from pathlib import Path

from hypothesis import settings

settings.register_profile("security-fuzz", max_examples=1000)


def pytest_addoption(parser):
    parser.addoption("--live-llm", action="store_true", default=False)


def pytest_ignore_collect(collection_path: Path, config):
    if collection_path.name == "agent_live":
        return not (
            config.getoption("--live-llm")
            and os.environ.get("RIR_ENABLE_LIVE_LLM") == "1"
        )
    return None
