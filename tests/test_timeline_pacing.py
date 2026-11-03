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


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), float("-inf")])
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


async def test_non_timeline_call_not_delayed_while_timeline_waiter_sleeps(paced_client, clock):
    """A queued timeline waiter holds only the timeline lock, never other requests."""
    starts: list[tuple[str, float]] = []
    session = _ok_session(starts, clock)
    await paced_client.fetch_json(session, "/groups/1/timeline")

    gate = asyncio.Event()
    real_sleep = clock._real_sleep

    async def blocked_sleep(delay: float) -> None:
        clock.sleeps.append(delay)
        await gate.wait()
        clock.now += delay

    client_module.asyncio.sleep = blocked_sleep  # restored by the clock fixture's monkeypatch
    waiter = asyncio.create_task(paced_client.fetch_json(session, "/groups/1/timeline"))
    await real_sleep(0)
    await real_sleep(0)
    assert clock.sleeps == [pytest.approx(3.0)]  # waiter is parked in its wait

    t0 = clock.now
    await paced_client.fetch_json(session, "/groups/1/letters")
    await paced_client.fetch_json(session, "/members/1")
    assert clock.now == t0  # not delayed
    assert not waiter.done()

    gate.set()
    await waiter


def test_timeline_lock_survives_event_loop_change():
    """Reusing one client across asyncio.run() calls must not lose pages."""
    c = Client(group=Group.HINATAZAKA46, access_token="t", timeline_min_interval=0.01)

    async def one_round() -> list[object]:
        session = MagicMock()

        def get(url, **kwargs):
            resp = AsyncMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"ok": True})
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        session.get.side_effect = get
        return await asyncio.gather(*(c.fetch_json(session, "/groups/1/timeline") for _ in range(3)))

    for _ in range(2):
        assert asyncio.run(one_round()) == [{"ok": True}] * 3


def test_refresh_lock_survives_event_loop_change():
    c = Client(group=Group.HINATAZAKA46, access_token="t")

    async def one_round() -> int:
        inside = 0
        peak = 0

        async def critical() -> None:
            nonlocal inside, peak
            async with c._get_refresh_lock():
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.01)
                inside -= 1

        await asyncio.gather(*(critical() for _ in range(3)))
        return peak

    assert asyncio.run(one_round()) == 1
    assert asyncio.run(one_round()) == 1


# --- Fail-fast after a timeline 429 ------------------------------------------------------


def _status_session(statuses, clock: FakeClock, headers=None, in_flight_yield=False) -> MagicMock:
    """Session answering the given statuses in order (the last one repeats); 429s carry ``headers``.

    With ``in_flight_yield`` the response takes one event-loop turn, so other tasks run meanwhile.
    """
    session = MagicMock()
    seq = list(statuses)
    session.sent: list[tuple[str, float]] = []

    def get(url, **kwargs):
        session.sent.append((url, clock.now))
        resp = AsyncMock()
        resp.status = seq.pop(0) if len(seq) > 1 else seq[0]
        resp.headers = headers or {}
        resp.json = AsyncMock(return_value={"ok": True})
        cm = MagicMock()

        async def enter(*_a):
            if in_flight_yield:
                await clock._real_sleep(0)
            return resp

        cm.__aenter__ = AsyncMock(side_effect=enter)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session.get.side_effect = get
    return session


async def _trip(client, session):
    with pytest.raises(client_module.RateLimitedError):
        await client.fetch_json(session, "/groups/1/timeline")


def test_cooldown_constants():
    assert client_module.TIMELINE_429_COOLDOWN == 150.0
    assert client_module.TIMELINE_429_MAX_COOLDOWN == 900.0


async def test_after_429_timeline_calls_raise_without_sending(paced_client, clock):
    session = _status_session([429], clock)
    await _trip(paced_client, session)
    assert len(session.sent) == 1

    for endpoint in ("/groups/1/timeline", "/groups/2/timeline"):
        with pytest.raises(client_module.RateLimitedError) as exc:
            await paced_client.fetch_json(session, endpoint)
        assert exc.value.retry_after == pytest.approx(150.0)
    with pytest.raises(client_module.RateLimitedError):
        await paced_client.get_messages(session, 1, page_delay=0)
    with pytest.raises(client_module.RateLimitedError):
        await paced_client.mark_group_read(session, 1)
    assert len(session.sent) == 1


async def test_blocked_without_pacing_enabled(clock):
    c = Client(group=Group.HINATAZAKA46, access_token="t")
    session = _status_session([429], clock)
    await _trip(c, session)
    await _trip(c, session)
    assert len(session.sent) == 1


async def test_retry_after_reports_remaining_seconds(paced_client, clock):
    session = _status_session([429], clock)
    await _trip(paced_client, session)
    clock.now += 40.0
    with pytest.raises(client_module.RateLimitedError) as exc:
        await paced_client.fetch_json(session, "/groups/1/timeline")
    assert exc.value.retry_after == pytest.approx(110.0)


async def test_queued_waiters_raise_after_their_wait(paced_client, clock):
    """Requests parked in the pacing gate when a 429 lands never go out."""
    session = _status_session([429], clock, in_flight_yield=True)
    results = await asyncio.gather(
        *(paced_client.fetch_json(session, f"/groups/{i}/timeline") for i in (1, 2, 3, 4)),
        return_exceptions=True,
    )
    assert all(isinstance(r, client_module.RateLimitedError) for r in results)
    assert len(session.sent) == 1  # only the first one was ever sent
    # The waiter holding the gate when the 429 landed raised after its wait; the ones behind
    # it were drained at once, without sleeping.
    assert clock.sleeps == [pytest.approx(3.0)]


async def test_waiter_blocked_while_waiting_does_not_send(paced_client, clock):
    session = _status_session([200], clock)
    await paced_client.fetch_json(session, "/groups/1/timeline")

    gate = asyncio.Event()

    async def blocked_sleep(delay: float) -> None:
        await gate.wait()
        clock.now += delay

    client_module.asyncio.sleep = blocked_sleep  # restored by the clock fixture
    waiter = asyncio.create_task(paced_client.fetch_json(session, "/groups/1/timeline"))
    await clock._real_sleep(0)
    await clock._real_sleep(0)
    assert not waiter.done()

    paced_client._timeline_blocked_until = clock.now + 100.0  # a 429 elsewhere lands now
    gate.set()
    with pytest.raises(client_module.RateLimitedError):
        await waiter
    assert len(session.sent) == 1


async def test_401_retry_path_checks_block(paced_client, clock):
    session = _status_session([401, 200], clock)

    async def refresh(_session):
        paced_client._timeline_blocked_until = clock.now + 100.0  # 429 from a concurrent call
        return True

    paced_client.refresh_access_token = refresh
    with pytest.raises(client_module.RateLimitedError):
        await paced_client.fetch_json(session, "/groups/1/timeline")
    assert len(session.sent) == 1


async def test_429_on_401_retry_blocks(paced_client, clock):
    session = _status_session([401, 429], clock)
    paced_client.refresh_access_token = AsyncMock(return_value=True)
    await _trip(paced_client, session)
    assert len(session.sent) == 2
    await _trip(paced_client, session)
    assert len(session.sent) == 2


async def test_non_timeline_calls_still_go_out(paced_client, clock):
    session = _status_session([429], clock)
    await _trip(paced_client, session)
    ok = _ok_session([], clock)
    for endpoint in ("/groups/1/letters", "/members/1", "/groups/1/past_messages", "/groups"):
        assert await paced_client.fetch_json(ok, endpoint) == {}
    assert ok.get.call_count == 4


async def test_non_timeline_429_does_not_block_timeline(paced_client, clock):
    session = _status_session([429], clock)
    with pytest.raises(client_module.RateLimitedError):
        await paced_client.fetch_json(session, "/groups/1/letters")
    assert paced_client.timeline_blocked_remaining is None
    ok = _ok_session([], clock)
    assert await paced_client.fetch_json(ok, "/groups/1/timeline") == {}


async def test_block_expires_after_cooldown(paced_client, clock):
    session = _status_session([429], clock)
    await _trip(paced_client, session)
    clock.now += 149.0
    await _trip(paced_client, session)
    assert len(session.sent) == 1

    clock.now += 1.0  # cooldown over
    ok = _ok_session([], clock)
    assert await paced_client.fetch_json(ok, "/groups/1/timeline") == {}
    assert ok.get.call_count == 1
    assert paced_client.timeline_blocked_remaining is None


async def test_retry_after_header_sets_cooldown(paced_client, clock):
    session = _status_session([429], clock, headers={"Retry-After": "20"})
    await _trip(paced_client, session)
    clock.now += 19.0
    await _trip(paced_client, session)
    assert len(session.sent) == 1
    clock.now += 1.5
    ok = _ok_session([], clock)
    assert await paced_client.fetch_json(ok, "/groups/1/timeline") == {}


async def test_retry_after_zero_does_not_block(paced_client, clock):
    session = _status_session([429, 200], clock, headers={"Retry-After": "0"})
    await _trip(paced_client, session)
    assert await paced_client.fetch_json(session, "/groups/1/timeline") == {"ok": True}


@pytest.mark.parametrize("raw", ["inf", "-5", "nan", "soon"])
async def test_unusable_retry_after_falls_back_to_150(paced_client, clock, raw):
    session = _status_session([429], clock, headers={"Retry-After": raw})
    await _trip(paced_client, session)
    clock.now += 149.0
    await _trip(paced_client, session)
    assert len(session.sent) == 1
    clock.now += 1.5
    ok = _ok_session([], clock)
    assert await paced_client.fetch_json(ok, "/groups/1/timeline") == {}


async def test_block_is_per_client(clock):
    a = Client(group=Group.HINATAZAKA46, access_token="t")
    b = Client(group=Group.SAKURAZAKA46, access_token="t")
    await _trip(a, _status_session([429], clock))
    ok = _ok_session([], clock)
    assert await b.fetch_json(ok, "/groups/1/timeline") == {}


async def test_block_logging(paced_client, clock):
    session = _status_session([429], clock, headers={"Retry-After": "30"})
    with capture_logs() as logs:
        await _trip(paced_client, session)
        for _ in range(3):
            await _trip(paced_client, session)

    blocked = [e for e in logs if e["event"] == "Timeline requests blocked"]
    assert len(blocked) == 1
    assert blocked[0]["log_level"] == "warning"
    assert blocked[0]["cooldown"] == 30.0
    skipped = [e for e in logs if e["event"] == "Timeline request short-circuited"]
    assert [e["short_circuited"] for e in skipped] == [1, 2, 3]
    assert all(e["log_level"] == "debug" for e in skipped)
    # only the 429 itself goes through the sender-facing warning; no extra warnings per skipped call
    assert len([e for e in logs if e["log_level"] == "warning" and e["event"] != "API rate limited"]) == 1


async def test_remaining_property_is_read_only_and_expires(paced_client, clock):
    assert paced_client.timeline_blocked_remaining is None
    await _trip(paced_client, _status_session([429], clock, headers={"Retry-After": "30"}))
    assert paced_client.timeline_blocked_remaining == pytest.approx(30.0)
    clock.now += 12.0
    assert paced_client.timeline_blocked_remaining == pytest.approx(18.0)
    with pytest.raises(AttributeError):
        paced_client.timeline_blocked_remaining = 0.0
    clock.now += 18.0
    assert paced_client.timeline_blocked_remaining is None


async def test_later_429_never_shortens_active_block(paced_client, clock):
    await _trip(paced_client, _status_session([429], clock, headers={"Retry-After": "600"}))
    # a request that was already in flight comes back with a short Retry-After
    err = client_module.RateLimitedError("x", 5.0)
    paced_client._block_timeline("/groups/1/timeline", err)
    assert paced_client.timeline_blocked_remaining == pytest.approx(600.0)
    # a longer one does extend it
    paced_client._block_timeline("/groups/1/timeline", client_module.RateLimitedError("x", 700.0))
    assert paced_client.timeline_blocked_remaining == pytest.approx(700.0)


async def test_short_then_long_extends(paced_client, clock):
    await _trip(paced_client, _status_session([429], clock, headers={"Retry-After": "5"}))
    # the longer 429 comes from a request that was already in flight
    paced_client._block_timeline("/groups/1/timeline", client_module.RateLimitedError("x", 60.0))
    assert paced_client.timeline_blocked_remaining == pytest.approx(60.0)


@pytest.mark.parametrize("raw, expected", [("900", 900.0), ("901", 900.0), ("86400", 900.0), ("899", 899.0)])
async def test_retry_after_is_capped(paced_client, clock, raw, expected):
    await _trip(paced_client, _status_session([429], clock, headers={"Retry-After": raw}))
    assert paced_client.timeline_blocked_remaining == pytest.approx(expected)


async def test_zero_cooldown_blocks_nothing_and_logs_nothing(paced_client, clock):
    with capture_logs() as logs:
        await _trip(paced_client, _status_session([429], clock, headers={"Retry-After": "0"}))
    assert paced_client.timeline_blocked_remaining is None
    assert not [e for e in logs if e["event"] == "Timeline requests blocked"]


async def test_drained_waiter_leaves_pacing_state_untouched(paced_client, clock):
    session = _ok_session([], clock)
    await paced_client.fetch_json(session, "/groups/1/timeline")
    last_start = paced_client._timeline_last_start
    counted = paced_client._timeline_paced_requests
    paced_client._timeline_blocked_until = clock.now + 100.0

    with pytest.raises(client_module.RateLimitedError):
        await paced_client._pace_timeline("/groups/1/timeline")

    assert clock.sleeps == []
    assert paced_client._timeline_last_start == last_start
    assert paced_client._timeline_paced_requests == counted
