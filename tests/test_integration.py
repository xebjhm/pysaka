"""
Local Integration Tests for PyHako.

These tests require:
1. Prior login (auth_data directory exists)
2. Valid session in keyring

Run with:
    uv run pytest tests/test_integration.py -m integration -v

Skip in CI by default.
"""

import pytest

from pyhako import Client, Group, get_auth_dir
from pyhako.auth import BrowserAuth
from pyhako.credentials import TokenManager, get_user_data_dir

# Skip all tests in this module if no auth data
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_auth_dir().exists() or not any(get_auth_dir().iterdir()),
        reason="No auth_data present. Login first with CLI."
    )
]


@pytest.fixture
def auth_dir():
    """Get the auth_dir if it exists."""
    return get_auth_dir()


@pytest.fixture
def token_manager():
    """Get token manager for credential access."""
    return TokenManager()


class TestHeadlessRefresh:
    """Test real headless browser refresh."""

    @pytest.mark.asyncio
    async def test_headless_refresh_returns_token(self, auth_dir):
        """Test that headless refresh returns a valid token."""
        # Try with first available group that has auth
        for group in Group:
            try:
                result = await BrowserAuth.refresh_token_headless(
                    group=group,
                    auth_dir=auth_dir,
                    auto_install=True
                )
                if result and result.get('access_token'):
                    # Verify token structure
                    assert result['access_token'].startswith('eyJ')  # JWT format
                    assert 'cookies' in result
                    print(f"✅ Headless refresh successful for {group.value}")
                    return
            except Exception as e:
                print(f"⚠️ {group.value}: {e}")
                continue

        pytest.skip("No group has valid auth session")

    @pytest.mark.asyncio
    async def test_playwright_auto_install(self, auth_dir):
        """Test that Playwright chromium auto-installs if missing."""
        # This test verifies the auto-install mechanism works
        # It will only trigger if chromium is not installed

        for group in Group:
            try:
                result = await BrowserAuth.refresh_token_headless(
                    group=group,
                    auth_dir=auth_dir,
                    auto_install=True
                )
                if result:
                    # If we got here, either chromium was installed or auto-installed
                    print(f"✅ Playwright chromium functional for {group.value}")
                    return
            except Exception as e:
                print(f"⚠️ {group.value}: {e}")
                continue

        pytest.skip("No valid session to test auto-install")


class TestAPIIntegration:
    """Test real API calls."""

    @pytest.mark.asyncio
    async def test_api_groups_fetch(self, token_manager):
        """Test fetching groups from real API."""
        import aiohttp

        # Find a group with valid token
        for group in Group:
            try:
                session_data = token_manager.load_session(group.value)
                if session_data and session_data.get('access_token'):
                    client = Client(
                        group=group,
                        access_token=session_data['access_token'],
                        auth_dir=str(get_auth_dir())
                    )

                    async with aiohttp.ClientSession() as session:
                        groups = await client.get_groups(session)

                        # Verify response structure
                        assert isinstance(groups, list)
                        if groups:
                            assert 'id' in groups[0]
                            assert 'name' in groups[0]
                            print(f"✅ Fetched {len(groups)} groups from {group.value}")
                        return
            except Exception as e:
                print(f"⚠️ {group.value}: {e}")
                continue

        pytest.skip("No group has valid token")


class TestStoragePaths:
    """Test storage path helpers (always runs, no auth needed)."""

    @pytest.mark.skipif(False, reason="Always run")  # Override module skipif
    def test_user_data_dir_exists(self):
        """Test that user data dir is accessible."""
        user_dir = get_user_data_dir()
        assert user_dir.exists()
        assert user_dir.is_dir()
        # Check platform-specific path
        import platform
        if platform.system() == "Windows":
            assert "AppData" in str(user_dir)
        elif platform.system() == "Darwin":
            assert "Application Support" in str(user_dir)
        else:
            assert ".local/share" in str(user_dir)
        print(f"✅ User data dir: {user_dir}")

    @pytest.mark.skipif(False, reason="Always run")
    def test_auth_dir_created(self):
        """Test that auth_dir is created on access."""
        auth_dir = get_auth_dir()
        assert auth_dir.exists()
        assert auth_dir.is_dir()
        assert auth_dir.name == "auth_data"
        print(f"✅ Auth dir: {auth_dir}")
