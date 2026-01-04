
import asyncio

import pytest

from pyhako.utils import normalize_message


@pytest.mark.benchmark(group="utils")
def test_normalize_message_speed(benchmark):
    """Benchmark the normalization logic speed."""
    raw = {
        "id": 12345,
        "published_at": "2023-01-01T12:00:00Z",
        "type": "image",
        "text": "Hello World",
        "is_favorite": True
    }
    benchmark(normalize_message, raw)


@pytest.mark.benchmark(group="io")
def test_async_io_simulation(benchmark):
    """Simulate async I/O overhead (no actual network/disk)."""
    async def pseudo_io():
        await asyncio.sleep(0.0001)
        return True

    def run_sync():
        asyncio.run(pseudo_io())

    benchmark(run_sync)
