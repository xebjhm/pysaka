from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhako import Client, Group


@pytest.fixture
def mock_session():
    session = MagicMock()
    # Mock context manager
    session.get.return_value.__aenter__.return_value = AsyncMock()
    session.get.return_value.__aexit__.return_value = None
    session.post.return_value.__aenter__.return_value = AsyncMock()
    session.post.return_value.__aexit__.return_value = None
    return session

@pytest.fixture
def client():
    return Client(group=Group.HINATAZAKA46, access_token="test_token")
