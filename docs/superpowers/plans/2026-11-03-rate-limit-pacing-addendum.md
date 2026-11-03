# Addendum: timeline pacing for history scans and a 429 circuit breaker (0.4.6)

## Evidence

Taken from the Windows dev-build log on 2026-11-03 (`C:\D\saka-verify\appdata\logs\debug.log`) and the live app logs.

- **Only `/groups/{id}/timeline` ever returned 429.** In the same minutes, `/groups/{id}/letters` (56 OK), `/groups/{id}/past_messages` (32 OK), `/members/{id}` (20 OK), `/groups/{id}/members`, `/groups` and `/profile` all succeeded. The afternoon probes showed the same split.
- **Timeline requests before each 429:**

  | 429 at | last 60 s | last 5 min |
  |---|---|---|
  | 12:20:53 | 37 | 172 (155 continuation pages) |
  | 12:27:13 | 35 | 189 (161 continuation pages) |

- **Live app** (incremental syncs only, since July): no 429. Its busiest window was 100 timeline requests in 5 minutes.
- **Cooldown:** under about 2.5 minutes. The next sync at 12:23 succeeded.
- **Limits are per service.** Each service has its own API host, and the 429s hit hinatazaka only.

## Decisions (user, 2026-11-03)

- Pace only timeline requests, per service: at most one request start every **3.0 s** (20/min, 100 per 5 min, the live app's proven-safe level).
- Pace only during syncs that contain a history scan. Normal incremental syncs are unchanged.
- Add a 429 circuit breaker per sync as the safety valve.
- Both go into 0.4.6.
- Every decision and failure path logs a structured breadcrumb (ids, counts, elapsed time, error types only).

## Task A (pysaka): `Client.timeline_min_interval`

- `Client.timeline_min_interval: float`, default `0.0` (off). It is settable at any time, and the constructor also accepts the keyword.
- When the interval is above 0, every HTTP request to `/groups/{id}/timeline` made through this client waits so that consecutive request **starts** are at least that many seconds apart. That covers all `get_messages` pages, the `order=asc` probe (`_timeline_first_id`) and the 401 retry inside `fetch_json` for those endpoints.
- Other endpoints and CDN downloads are never paced.
- Concurrency-safe: one `asyncio.Lock` per client and event loop plus `time.monotonic()` of the last start. Use `await asyncio.sleep`, never `time.sleep`.
- `get_messages(page_delay=)` stays. Callers that enable the interval may pass `page_delay=0`, so the pause isn't doubled.
- Log: debug `Timeline pacing set` (`interval`) whenever the value changes; debug per paced wait is unnecessary. Optionally, one info line at the end of a paced period with the total requests paced and the wait time. Keep the logging minimal.
- Tests:
  - three sequential timeline calls with interval 3.0 wait the expected amounts (patch `asyncio.sleep` and `monotonic`, or use time-machine);
  - concurrent timeline calls are serialised;
  - non-timeline calls (`/groups/1/letters`, `/members/1`) are never delayed;
  - interval 0 never sleeps;
  - the asc probe is paced.
- Docs: docstring, `docs/API.md`, and a CHANGELOG `### Added` entry.

## Task B (SakaDesk) — `backend/services/sync_service.py`

**Pacing**

- When any room in this sync needs a history scan, before phase 2:
  - set `client.timeline_min_interval = SCAN_TIMELINE_INTERVAL` (module constant `3.0`);
  - log `sync_timeline_pacing` with `scan_rooms` and `interval`.
- Reset it to `0.0` in a `finally` that runs however the sync ends.
- The scan fetch passes `page_delay=0`.
- Syncs with no scan rooms never touch the interval.

**Circuit breaker**

- Keep a per-sync `rate_limited` flag (an `asyncio.Event` created in `start_sync`).
- Set the flag when any call in `sync_group` raises `RateLimitedError`. That covers the scan fetch, the incremental fetch, `extra_room_members`, backfill and letters (add flag-setting to their existing per-member handlers).
- Log `sync_rate_limited` once per sync, with `group_id` and `retry_after`.
- A room that has not started its timeline fetch when the flag is set skips the fetch. Check the flag both before the fetch and after acquiring `scan_sem`.
  - Log `room_sync_deferred` with `group_id`, `reason="rate_limited"` and `scan: bool`.
  - The room returns no results and is not a `group_sync_failed`.
  - Move no cursors and set no scan mark. The next sync retries the room.
- Rooms already past their fetch finish normally. The sync still completes, and phase 3 downloads media for the rooms that did sync.
- At the end of the sync, log `sync_rate_limit_summary` with the count of deferred rooms and their `group_ids`.

**Tests**

- Pacing: set for a scan sync and reset afterwards, including when the sync raises; never set for an incremental-only sync; the scan fetch gets `page_delay=0`.
- Circuit breaker:
  - a 429 in a scan room means later rooms make no `get_messages` call and each logs `room_sync_deferred`;
  - a room already past its fetch still completes;
  - deferred rooms change no cursors and get no mark;
  - a 429 in an incremental room also defers the remaining rooms.

## Task C (saka-cli) — `src/saka_cli/cli.py` `_sync_rooms` / `_sync_room`

- Same as Task B: set `timeline_min_interval=3.0` while any room needs a scan, reset it in a `finally`, pass `page_delay=0` for scans.
- Use a per-run `rate_limited` flag. Rooms that haven't fetched yet are skipped with `room_sync_deferred`, and a summary log is written at the end.
- Mirror the Task B tests.
- Add no new user-facing strings.

## Spec update (pysaka)

Add a §4a to `docs/superpowers/specs/2026-11-03-room-roster-v046-design.md` covering:
- the evidence above;
- the 3.0 s timeline pacing during scan syncs;
- the circuit breaker;
- the fact that normal syncs are unaffected.
