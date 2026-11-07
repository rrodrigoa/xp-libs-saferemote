import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_intent import ActionRegistry, IntentError
from remote_intent.catalog import (
    publish_catalog,
    verify_catalog,
    verify_installed_actions,
)
from tests.agent_sim.test_protocol import fixture


def test_signed_catalog_is_bound_to_exact_target_and_installed_contract(tmp_path):
    _, controller, _, calls = fixture(tmp_path / "catalog.sqlite", 44)
    registry = controller.compiler.registry
    key = Ed25519PrivateKey.generate()
    data, signature = publish_catalog(registry, "fake", "fake-endpoint", 50, key)

    def verify(raw=data, target_id="fake-endpoint", now=25):
        return verify_catalog(
            raw, signature, publisher_key=key.public_key(), target_id=target_id, now=now
        )

    snapshot = verify()
    verify_installed_actions(snapshot, registry, "fake")
    assert (
        snapshot.epoch
        == controller.catalog("fixture-user", "fake-endpoint")["catalog_epoch"]
    )
    assert len(snapshot.actions) == 2
    for options, code in [
        ({"raw": data + b" "}, "ACTION_PACK_UNTRUSTED"),
        ({"target_id": "other"}, "TARGET_MISMATCH"),
        ({"now": 50}, "CATALOG_STALE"),
    ]:
        with pytest.raises(IntentError) as error:
            verify(**options)
        assert error.value.code == code
    with pytest.raises(IntentError) as error:
        verify_installed_actions(snapshot, ActionRegistry(), "fake")
    assert error.value.code == "ACTION_SCHEMA_MISMATCH"
    assert calls == []


def test_catalog_checks_size_canonicality_and_publisher_epoch(tmp_path, monkeypatch):
    import json

    from remote_intent.contracts import canonical

    _, controller, _, calls = fixture(tmp_path / "limits.sqlite", 70)
    registry = controller.compiler.registry
    private = Ed25519PrivateKey.generate()
    data, _ = publish_catalog(registry, "fake", "fake-endpoint", 100, private)
    oversized = b"x" * 1_048_577
    wrong_epoch = json.loads(data)
    wrong_epoch["epoch"] = "0" * 64
    for bad_data, code in [
        (oversized, "RESOURCE_QUOTA_EXCEEDED"),
        (data + b" ", "ACTION_PACK_UNTRUSTED"),
        (canonical(wrong_epoch), "ACTION_SCHEMA_MISMATCH"),
    ]:
        signature = private.sign(b"rir.catalog/v1\x00" + bad_data)
        with pytest.raises(IntentError) as error:
            verify_catalog(
                bad_data,
                signature,
                publisher_key=private.public_key(),
                target_id="fake-endpoint",
                now=1,
            )
        assert error.value.code == code
    entry = registry.catalog("fake")[0].model_copy(
        update={"description": "x" * 1_048_577}
    )
    monkeypatch.setattr(registry, "catalog", lambda _: [entry])
    with pytest.raises(IntentError) as error:
        publish_catalog(registry, "fake", "fake-endpoint", 100, private)
    assert error.value.code == "RESOURCE_QUOTA_EXCEEDED"
    assert calls == []
