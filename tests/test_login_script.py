"""Tests for scripts/login.py (PY-AUX-01).

The login script is not an importable package (no scripts/__init__.py), so it is
loaded by file path. These tests pin the credential-persistence contract that the
script broke: it must call the real ``TokenManager.save_session`` API (there is no
``store_tokens``), key by ``group.value`` (not the ``Group`` enum), persist the
captured cookies, and drive ``BrowserAuth.login`` with a per-group
``user_data_dir`` so the integration-test skip gate is actually satisfied.
"""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "login.py"


def _load_login_module():
    spec = importlib.util.spec_from_file_location("_login_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_login_persists_via_save_session(monkeypatch):
    login_mod = _load_login_module()

    creds = {
        "access_token": "acc-token-1234567890",
        "refresh_token": "ref-token-1234567890",
        "cookies": {"session": "abc"},
        "app_id": "app",
        "user_agent": "ua",
    }

    fake_tm = MagicMock()
    captured_login_kwargs: dict = {}

    async def fake_login(group, headless=False, user_data_dir=None, channel=None):
        captured_login_kwargs["group"] = group
        captured_login_kwargs["user_data_dir"] = user_data_dir
        return creds

    monkeypatch.setattr(login_mod.BrowserAuth, "login", staticmethod(AsyncMock(side_effect=fake_login)))
    monkeypatch.setattr(login_mod, "TokenManager", MagicMock(return_value=fake_tm))
    monkeypatch.setattr("sys.argv", ["login.py", "--group", "hinatazaka46"])

    await login_mod.main()

    # There is no store_tokens on TokenManager — the real API is save_session.
    assert not hasattr(fake_tm, "store_tokens") or not fake_tm.store_tokens.called
    fake_tm.save_session.assert_called_once()
    args, kwargs = fake_tm.save_session.call_args
    call = {**dict(zip(("group", "access_token", "refresh_token", "cookies"), args)), **kwargs}

    # Keyed by the string group value, not the Group enum.
    assert call["group"] == "hinatazaka46"
    assert call["access_token"] == "acc-token-1234567890"
    assert call["refresh_token"] == "ref-token-1234567890"
    # Cookies (needed by the web refresh path) must be persisted.
    assert call["cookies"] == {"session": "abc"}

    # login() must receive a persistent per-group user_data_dir so the
    # integration-test skip gate (auth_data non-empty) can be satisfied.
    assert captured_login_kwargs["user_data_dir"]
    assert "hinatazaka46" in str(captured_login_kwargs["user_data_dir"])
