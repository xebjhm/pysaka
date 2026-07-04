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

import pytest

from pysaka.credentials import SERVICE_NAME, KeyringStore


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
