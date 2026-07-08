"""Regression tests for credential namespacing (pysaka/credentials.py KeyringStore).

Root cause these lock in: keyring's Windows backend always stores the newest
credential for a given *service* at the bare ``service`` target, migrating the
previous occupant to a ``username@service`` target (see keyring
backends/Windows.py set_password). When every credential is stored under one
shared service name (``SERVICE_NAME = "pysaka"``) and separated only by the
keyring *username*, all login sessions AND the LLM API key churn through that
single bare target and interfere with each other — the API key ends up sharing
a target with a login session and gets clobbered by routine session re-saves
(login / token refresh around an app update or reinstall).

The fix gives every credential its own keyring *service* so exactly one
credential ever lives per service and the bare-target churn cannot happen.
"""

import base64
import json
import zlib

import pytest

from pysaka.credentials import SERVICE_NAME, KeyringStore, _service_for
from pysaka.exceptions import SakaError


class FakeWinVault:
    """Faithful in-memory model of keyring's WinVaultKeyring target semantics.

    Mirrors backends/Windows.py: the bare ``service`` target holds the most
    recently written credential; a prior occupant is relocated to
    ``username@service`` on the next write to that service.
    """

    def __init__(self):
        # target -> {"UserName": str, "value": str}
        self.storage: dict[str, dict[str, str]] = {}

    @staticmethod
    def _compound(username, service):
        return f"{username}@{service}"

    def get_password(self, service, username):
        res = self.storage.get(service)
        if res is None or (username and res["UserName"] != username):
            res = self.storage.get(self._compound(username, service))
        return res["value"] if res else None

    def set_password(self, service, username, password):
        existing = self.storage.get(service)
        if existing:
            self.storage[self._compound(existing["UserName"], service)] = existing
        self.storage[service] = {"UserName": username, "value": str(password)}

    def delete_password(self, service, username):
        deleted = False
        for target in (service, self._compound(username, service)):
            entry = self.storage.get(target)
            if entry and entry["UserName"] == username:
                del self.storage[target]
                deleted = True
        if not deleted:
            raise KeyError(service)


def _store_with(fake) -> KeyringStore:
    """A KeyringStore backed by ``fake``, bypassing the real keyring probe."""
    store = KeyringStore.__new__(KeyringStore)
    store._keyring = fake
    return store


def test_saving_a_second_credential_leaves_the_first_untouched():
    """Saving credential B must not relocate/rewrite credential A's storage.

    This is the root-cause invariant: with a shared service name, saving the
    API key churns an existing login session off the bare target. Each
    credential must occupy an isolated target so cross-credential saves can't
    interfere.
    """
    fake = FakeWinVault()
    store = _store_with(fake)

    store.save("session_hinatazaka46", {"access_token": "T1"})
    snapshot = {t: dict(e) for t, e in fake.storage.items()}

    # A completely unrelated credential is saved (e.g. the LLM API key).
    store.save("llm_provider_api_key", {"api_key": "K"})

    for target, entry in snapshot.items():
        assert fake.storage.get(target) == entry, (
            f"saving 'llm_provider_api_key' churned the session credential at "
            f"target {target!r} — credentials are sharing a keyring target"
        )


def test_save_load_delete_roundtrip():
    """Basic contract still holds under the new namespacing."""
    fake = FakeWinVault()
    store = _store_with(fake)

    store.save("llm_provider_api_key", {"api_key": "secret-123"})
    assert store.load("llm_provider_api_key") == {"api_key": "secret-123"}

    store.delete("llm_provider_api_key")
    assert store.load("llm_provider_api_key") is None


def test_legacy_credentials_are_migrated_and_readable():
    """Credentials written by the old layout (service=SERVICE_NAME,
    username=group) must remain readable after the change, so existing users
    don't lose their logins/keys on upgrade."""
    fake = FakeWinVault()
    # Seed a credential exactly as the OLD KeyringStore would have written it.
    legacy_store = KeyringStore.__new__(KeyringStore)
    legacy_store._keyring = fake
    import base64
    import json
    import zlib

    payload = json.dumps({"api_key": "legacy-key"})
    compressed = base64.b64encode(zlib.compress(payload.encode(), 9)).decode("ascii")
    fake.set_password(SERVICE_NAME, "llm_provider_api_key", compressed)

    store = _store_with(fake)
    assert store.load("llm_provider_api_key") == {"api_key": "legacy-key"}


def _legacy_blob(payload: dict) -> str:
    """Encode a credential exactly as the OLD KeyringStore.save would have."""
    return base64.b64encode(zlib.compress(json.dumps(payload).encode(), 9)).decode("ascii")


class FailingSetVault(FakeWinVault):
    """FakeWinVault whose set_password fails for a chosen set of services,
    modelling an isolated-service write that the backend rejects."""

    def __init__(self, fail_services):
        super().__init__()
        self.fail_services = set(fail_services)

    def set_password(self, service, username, password):
        if service in self.fail_services:
            raise RuntimeError("backend write rejected")
        super().set_password(service, username, password)


class FailingDeleteVault(FakeWinVault):
    """FakeWinVault whose delete_password fails for a chosen service, modelling a
    legacy delete that the backend rejects, leaving the legacy entry lingering."""

    def __init__(self, fail_services):
        super().__init__()
        self.fail_services = set(fail_services)

    def delete_password(self, service, username):
        if service in self.fail_services:
            raise RuntimeError("backend delete rejected")
        super().delete_password(service, username)


def test_failed_isolated_write_is_not_treated_as_completed_migration():
    """If the isolated-service write fails during migration, the credential was NOT
    migrated: _migrate_legacy must surface the failure (raise) rather than return the
    credential as if migration succeeded — and it must NOT delete the legacy entry,
    so the still-valid credential isn't lost and the migration can be retried."""
    group = "session_hinatazaka46"
    fake = FailingSetVault(fail_services={_service_for(group)})
    # Seed a legacy credential under the shared service (old layout).
    FakeWinVault.set_password(fake, SERVICE_NAME, group, _legacy_blob({"access_token": "T1"}))
    store = _store_with(fake)

    with pytest.raises(SakaError):
        store._migrate_legacy(group)

    # Legacy entry preserved (delete must not have run after the failed write).
    assert fake.get_password(SERVICE_NAME, group) is not None
    # Nothing was written to the isolated service.
    assert fake.get_password(_service_for(group), "credential") is None


def test_lingering_legacy_after_failed_delete_is_not_clobbered_by_other_group_save():
    """If the legacy delete fails, the legacy entry lingers under the shared service.
    That residue must not be clobbered by another group's save — i.e. saves must stay
    isolated per service so the shared-service collision this release fixed cannot
    reappear via a leftover legacy entry."""
    group_a = "session_hinatazaka46"
    fake = FailingDeleteVault(fail_services={SERVICE_NAME})
    FakeWinVault.set_password(fake, SERVICE_NAME, group_a, _legacy_blob({"access_token": "A"}))
    store = _store_with(fake)

    # Migration reads + rewrites to the isolated service, then the legacy delete
    # fails: the credential is still readable and the legacy entry lingers.
    assert store.load(group_a) == {"access_token": "A"}
    legacy_after = fake.get_password(SERVICE_NAME, group_a)
    assert legacy_after is not None

    # A completely unrelated group is saved.
    store.save("llm_provider_api_key", {"api_key": "K"})

    # The lingering legacy entry for group A is untouched by group B's save.
    assert fake.get_password(SERVICE_NAME, group_a) == legacy_after
    # Group B lives in its own isolated service, never the shared one.
    assert fake.get_password(_service_for("llm_provider_api_key"), "credential") is not None


def test_load_logs_greppable_error_id_on_corrupt_data():
    """Corrupt stored data must be distinguishable in logs from 'never stored'
    (which returns None with no error). load must emit a stable, greppable error id
    on the failure path so corruption isn't silently collapsed into 'no credential'."""
    from structlog.testing import capture_logs

    group = "session_hinatazaka46"
    fake = FakeWinVault()
    # Stored value that is neither valid compressed data nor valid JSON.
    fake.set_password(_service_for(group), "credential", "@@@not-valid-json@@@")
    store = _store_with(fake)

    with capture_logs() as logs:
        result = store.load(group)

    assert result is None
    assert any(e.get("event") == "saka.cred.load_failed" for e in logs), (
        f"expected a 'saka.cred.load_failed' log event, got {[e.get('event') for e in logs]}"
    )
