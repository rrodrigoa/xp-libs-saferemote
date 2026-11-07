import pytest

from remote_intent import IntentError
from remote_intent.journal import Journal


def test_interrupted_execution_is_quarantined_and_never_reclaimed(tmp_path):
    path = tmp_path / "recovery.sqlite"
    journal = Journal(path)
    assert journal.accept("job", "nonce", "digest", "owner")
    journal.record("job", "running")
    journal.record("job", "running", step="measure")
    reopened = Journal(path)
    assert reopened.recover_exclusive() == 1
    assert reopened.recover_exclusive() == 0
    assert reopened.status("job", "owner")["status"] == "needs_reconciliation"
    assert not reopened.accept("job", "nonce", "digest", "owner")
    assert not reopened.cancel("job", "owner")
    with pytest.raises(IntentError, match="conflicting"):
        reopened.accept("other", "nonce", "different", "owner")
    with pytest.raises(IntentError) as error:
        reopened.results("job", "someone-else")
    assert error.value.code == "SECURITY_DENIED"


def test_recovery_checks_schema_version_and_execution_owner(tmp_path):
    path = tmp_path / "migration.sqlite"
    journal = Journal(path)
    with pytest.raises(IntentError) as error:
        journal.status("absent", "owner")
    assert error.value.code == "SECURITY_DENIED"
    with journal.connection() as db:
        db.execute("PRAGMA user_version=900")
    with pytest.raises(IntentError) as error:
        Journal(path)
    assert error.value.code == "INTERNAL_ERROR"
