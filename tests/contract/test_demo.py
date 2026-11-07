import asyncio

from examples.demo import run


def test_signed_artifact_example(tmp_path):
    assert asyncio.run(run(tmp_path / "demo.sqlite")) == {"summary": {"System": 3}}
