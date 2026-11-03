"""Per-client pacing of ``/groups/{id}/timeline`` requests (0.4.6 addendum, Task A)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

import pysaka.client as client_module
from pysaka import Client, Group


class FakeClock:
    """Virtual monotonic clock; ``sleep`` advances it and records the wait."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []
        self._real_sleep = asyncio.sleep

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await self._real_sleep(0)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=fake.monotonic))
    monkeypatch.setattr(client_module.asyncio, "sleep", fake.sleep)
    return fake


@pytest.fixture
def paced_client(clock):
    return Client(group=Group.HINATAZAKA46, access_token="t", timeline_min_interval=3.0)


def _ok_session(starts: list[tuple[str, float]], clock: FakeClock) -> MagicMock:
    """Session whose GET records (url, virtual time) and answers 200 ``{}``."""
    session = MagicMock()

    def get(url, **kwargs):
        starts.append((url, clock.now))
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session.get.side_effect = get
    return session


def test_default_is_off():
    assert Client(group=Group.HINATAZAKA46).timeline_min_interval == 0.0


def test_constructor_keyword_and_setter():
    c = Client(group=Group.HINATAZAKA46, timeline_min_interval=2.5)
    assert c.timeline_min_interval == 2.5
    c.timeline_min_interval = 0.0
    assert c.timeline_min_interval == 0.0


@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_invalid_interval_rejected(bad):
    with pytest.raises(ValueError):
        Client(group=Group.HINATAZAKA46, timeline_min_interval=bad)
    c = Client(group=Group.HINATAZAKA46)
    with pytest.raises(ValueError):
        c.timeline_min_interval = bad
    assert c.timeline_min_interval == 0.0


async def test_sequential_timeline_calls_spaced(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    for _ in range(3):
        await paced_client.fetch_json(session, "/groups/1/timeline", {"count": 200})

    times = [t for _, t in starts]
    assert times[1] - times[0] == pytest.approx(3.0)
    assert times[2] - times[1] == pytest.approx(3.0)
    # First request is never delayed.
    assert clock.sleeps == [pytest.approx(3.0), pytest.approx(3.0)]


async def test_wait_is_only_the_remainder(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    await paced_client.fetch_json(session, "/groups/1/timeline")
    clock.now += 2.0  # caller spent 2 s elsewhere
    await paced_client.fetch_json(session, "/groups/1/timeline")
    assert clock.sleeps == [pytest.approx(1.0)]

    clock.now += 10.0  # longer than the interval: no wait at all
    await paced_client.fetch_json(session, "/groups/1/timeline")
    assert clock.sleeps == [pytest.approx(1.0)]


async def test_concurrent_timeline_calls_are_serialised(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    await asyncio.gather(*(paced_client.fetch_json(session, f"/groups/{i}/timeline") for i in (1, 2, 3, 4)))

    times = sorted(t for _, t in starts)
    assert len(times) == 4
    assert [round(b - a, 6) for a, b in zip(times, times[1:])] == [3.0, 3.0, 3.0]


@pytest.mark.parametrize("endpoint", ["/groups/1/letters", "/members/1", "/groups/1/past_messages", "/groups"])
async def test_non_timeline_endpoints_never_paced(paced_client, clock, endpoint):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    for _ in range(3):
        await paced_client.fetch_json(session, endpoint)

    assert clock.sleeps == []
    assert len(starts) == 3


async def test_non_timeline_calls_do_not_wait_behind_timeline(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    await paced_client.fetch_json(session, "/groups/1/timeline")
    await paced_client.fetch_json(session, "/groups/1/letters")
    await paced_client.fetch_json(session, "/groups/1/timeline")

    assert clock.sleeps == [pytest.approx(3.0)]  # only the second timeline call waited


async def test_interval_zero_never_sleeps(clock):
    c = Client(group=Group.HINATAZAKA46, access_token="t")
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    for _ in range(3):
        await c.fetch_json(session, "/groups/1/timeline")

    assert clock.sleeps == []
    assert len(starts) == 3


async def test_interval_can_be_enabled_and_disabled_at_any_time(clock):
    c = Client(group=Group.HINATAZAKA46, access_token="t")
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    await c.fetch_json(session, "/groups/1/timeline")
    c.timeline_min_interval = 3.0
    await c.fetch_json(session, "/groups/1/timeline")
    await c.fetch_json(session, "/groups/1/timeline")
    c.timeline_min_interval = 0.0
    await c.fetch_json(session, "/groups/1/timeline")

    assert clock.sleeps == [pytest.approx(3.0)]


async def test_asc_probe_is_paced(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    await paced_client.fetch_json(session, "/groups/1/timeline")
    await paced_client._timeline_first_id(session, 1)

    assert clock.sleeps == [pytest.approx(3.0)]


async def test_get_messages_pages_and_probe_are_paced(paced_client, clock):
    """Every page of get_messages (page_delay=0) plus the asc probe is spaced."""
    starts: list[tuple[str, float]] = []
    pages = [
        {"messages": [{"id": 30, "published_at": "2026-01-03T00:00:00Z"}], "continuation": {"max_id": 29}},
        {"messages": [{"id": 20, "published_at": "2026-01-02T00:00:00Z"}], "continuation": {"max_id": 19}},
        {"messages": [{"id": 10, "published_at": "2026-01-01T00:00:00Z"}]},
    ]
    call = {"n": 0}
    session = MagicMock()

    def get(url, **kwargs):
        starts.append((url, clock.now))
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=pages[min(call["n"], len(pages) - 1)])
        call["n"] += 1
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session.get.side_effect = get
    await paced_client.get_messages(session, 1, page_delay=0)

    times = [t for _, t in starts]
    assert len(times) >= 3
    assert all(b - a >= 3.0 - 1e-9 for a, b in zip(times, times[1:]))


async def test_401_retry_is_paced(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = MagicMock()
    statuses = iter([401, 200])

    def get(url, **kwargs):
        starts.append((url, clock.now))
        resp = AsyncMock()
        resp.status = next(statuses)
        resp.json = AsyncMock(return_value={"ok": True})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session.get.side_effect = get
    paced_client.refresh_access_token = AsyncMock(return_value=True)

    assert await paced_client.fetch_json(session, "/groups/1/timeline") == {"ok": True}

    assert len(starts) == 2
    assert starts[1][1] - starts[0][1] >= 3.0 - 1e-9


async def test_pacing_is_per_client(clock):
    a = Client(group=Group.HINATAZAKA46, access_token="t", timeline_min_interval=3.0)
    b = Client(group=Group.SAKURAZAKA46, access_token="t", timeline_min_interval=3.0)
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    await a.fetch_json(session, "/groups/1/timeline")
    await b.fetch_json(session, "/groups/1/timeline")

    assert clock.sleeps == []


async def test_logs_interval_change_and_period_summary(paced_client, clock):
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)

    with capture_logs() as logs:
        paced_client.timeline_min_interval = 1.5
        paced_client.timeline_min_interval = 1.5  # unchanged: no second line
        for _ in range(3):
            await paced_client.fetch_json(session, "/groups/1/timeline")
        paced_client.timeline_min_interval = 0.0

    set_logs = [e for e in logs if e["event"] == "Timeline pacing set"]
    assert [(e["interval"], e["log_level"]) for e in set_logs] == [(1.5, "debug"), (0.0, "debug")]

    summary = [e for e in logs if e["event"] == "Timeline pacing summary"]
    assert len(summary) == 1
    assert summary[0]["log_level"] == "info"
    assert summary[0]["requests"] == 3
    assert summary[0]["waited_seconds"] == pytest.approx(3.0)
    # nothing but ids/counts/intervals: no URLs or tokens
    assert set(summary[0]) <= {"event", "log_level", "requests", "waited_seconds"}
