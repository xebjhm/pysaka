# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `Client.timeline_min_interval` (also the `Client(..., timeline_min_interval=0.0)`
  keyword) paces `/groups/{id}/timeline` requests: while above 0, consecutive
  request starts through that client are at least that many seconds apart,
  covering every `get_messages` page, its `order=asc` probe, `mark_group_read`
  and the 401 retry in `fetch_json`, also under concurrent callers. Other
  endpoints and CDN downloads are never paced. Default `0.0` keeps today's
  behavior (no pacing); a negative, NaN or infinite value raises `ValueError`. Set it
  (e.g. `3.0`) for history scans, which can hit HTTP 429 after ~170 timeline
  requests in 5 minutes, and pass `page_delay=0` to avoid doubling the pause.
- Timeline fail-fast after HTTP 429, with or without `timeline_min_interval`:
  once a `/groups/{id}/timeline` request returns 429, the `Client` raises
  `RateLimitedError` immediately, without sending, for every further timeline
  request (`get_messages` pages, `mark_group_read`, `fetch_json`, the 401 retry,
  and requests already waiting in the pacing gate), with `retry_after` set to the
  remaining cooldown. The cooldown is the response's `Retry-After` (finite, >= 0,
  at most `pysaka.client.TIMELINE_429_MAX_COOLDOWN` = 900 s) or else
  `pysaka.client.TIMELINE_429_COOLDOWN` (150 s); a later 429 never shortens it.
  It ends by itself, is per client, and other endpoints are unaffected. The new
  read-only `Client.timeline_blocked_remaining` returns the seconds left (`None`
  when not blocked). Before, queued requests kept going out and each got another
  429, so a caller's circuit breaker could not stop them.
- `Client.get_messages(..., page_delay=0.5)` makes the pause between page
  requests configurable (default unchanged).
- `pysaka.roster` (`ROOM_AUTHOR_ID`, `author_id`, `extra_author_ids`,
  `room_author`) and `SyncManager.sync_member(..., ignore_cursor=True)`, which merges a
  member's rows older than the cursor and does not call `on_new_messages` (not
  even for posts that arrived since the last sync). Only messages at or after
  the previous cursor can hold the cursor back: an old message with missing
  media or a failed normalize no longer pins it to that old date.
- `SyncManager.prepare_messages(..., cursor_floor=None)`: when set, only
  messages published at or after it count toward the returned earliest
  failed / pending-media timestamps.
- `SyncManager.extra_room_members(session, group, api_members, messages, name_hints=None)`
  returns the authors of a room's timeline missing from `/groups/{id}/members`
  (earlier lineups, members posting in official rooms) plus member 0 for
  messages without `member_id`. Sync them with `sync_member` so their messages
  are no longer dropped. Rows that sync skips as empty (non-canceled text with
  no text and no media) do not create an author, so a 1:1 room gets no empty
  member 0 folder.
- `SyncManager.history_scanned`, `mark_history_scanned` and `room_archive_complete`
  let callers re-read each room's whole timeline once (flag in `sync_state.json`
  as `room_<gid>`), recovering messages from authors that earlier versions
  dropped. `room_archive_complete` does not expect the messages sync skips
  (empty text messages that are not canceled).
- `pysaka.utils.CANCELED_STATES`: the server states (`canceled`, `cancelled`) of a
  message the member withdrew. A canceled message now carries `canceled_at`
  (the API's `updated_at`, kept from the first time it was seen; omitted, never
  `null`, while the API row has no `updated_at`).
- Log breadcrumbs for the room-roster and scan decisions, for diagnosing bug
  reports from `debug.log`. `info`: `Extra room members`, `Room history scan
  recorded`, `Stored message turned canceled`. `warning`: unknown extra-author
  names, `Messages with unusable member_id attributed to room author`, `Local
  member block unreadable`, `Member lookup returned a non-object body`; the
  `API rate limited` warning also says why an unusable `Retry-After` was
  ignored (`retry_after_ignored`, `error_type`). `debug`: per-author name
  source, `Room archive complete`, cursor exclusions during a scan, canceled
  skeletons and skipped member 0 calls. Ids, counts and error types only,
  never message text.

### Changed
- `sync_member` assigns messages without `member_id` to member 0 (the room
  itself) instead of no member. `sync_member` and `sync_letters` rename
  `messages/<gid> */<mid> <old name>` to the member's current name instead of
  creating a second folder. If a member or room folder rename fails
  (`OSError`, e.g. a file locked on Windows), `sync_member` and `sync_letters`
  log a warning and return 0 without writing or moving a cursor (before: the
  error escaped `sync_member`).
- `Client.get_member(session, member_id)` now returns `None` when the response
  body is not a JSON object (before: the raw body). It also works for members
  who are no longer in any room's member list (e.g. graduated members who
  posted in an official room).
- **Breaking:** HTTP 429 now raises `RateLimitedError` (a subclass of `ApiError`)
  from every `Client` method built on `fetch_json`: `fetch_json`, `get_groups`,
  `get_members`, `get_messages`, `get_profile`, `get_account`, `get_member`,
  `get_news`, `get_tags`, `get_fc_contents`, `get_organizations`,
  `get_products`, `get_letters`, `get_past_messages`, `get_subscription_streak`
  and `mark_group_read`. Before, they returned their failure value while rate
  limited (`None`, `[]` or `False`), which callers could not tell apart from
  "nothing there". `retry_after` holds the `Retry-After` header in seconds, or
  `None` when it is absent, not a number, not finite or negative. Callers that
  already handle `ApiError` need no change; others should catch
  `RateLimitedError` and back off.

### Fixed
- Error logs no longer carry exception text, which could quote message content,
  signed URLs or local paths: `Prepare error`, `Error syncing member`,
  `Failed to update message metadata`, `Unexpected error fetching` and `Message
  media download error` now log `error_type` plus ids. `Error syncing member`
  logs a text-free `origin` (`file:line:function`, `pysaka_frame`, `cause_type`)
  instead of a traceback. Network, timeout and download failures log the
  redacted URL, and the `ApiError` raised for a network error or timeout reads
  `Network error: <ExceptionType>` / `Timeout: <ExceptionType>` instead of
  embedding the exception text. Media probe and file-error logs show
  `parent/name` instead of the absolute path (no user name).
- Renaming a room folder (`messages/<gid> <old room name>`) or a member folder
  (`<mid> <old name>`) left every stored `media_file` pointing into the old
  folder, so the app showed broken media and Verify & Fix re-downloaded into
  the old path, recreating it. `sync_member` and `sync_letters` now rewrite the
  `media_file` values that start with the old folder in the affected
  `messages.json` files, keeping each value's `\` or `/` separators; other
  values and fields are left as they are.
- **Withdrawn messages are now archived instead of dropped**: a canceled text
  message was skipped before (the withdrawal was never recorded) and a canceled
  photo/video lost its archived caption on the next re-fetch. `sync_member` now
  stores canceled messages with `state` and `canceled_at` (the server's
  `updated_at`, first sight), keeping any content captured before the
  withdrawal. A text message with no text and no media in any state other
  than canceled (not only `published`) is still skipped.

## [0.4.5] - 2026-11-03

### Added
- `SyncManager.update_message_fields(messages_file, message_id, fields)` (and the
  blocking `update_message_fields_sync`) update one message under a per-file
  lock, so the edit is not lost to a concurrent sync. A later sync that
  re-fetches the message still replaces its record: fields the API returns
  (e.g. `is_favorite`) are overwritten, and only `width`, `height`,
  `media_duration`, `is_muted` and `media_file` are carried over.
- `SyncManager.sync_member(..., on_new_messages=callback)` reports the messages
  that were not on disk before, with the member's `phone_image`, for
  notifications. Exceptions raised by the callback are logged by type only and
  swallowed.
- `sync_member` now archives the member's list icon, chat icon and call-screen
  image into `profile_images/` with an `index.json` history
  (`SyncManager.archive_profile_images`). URLs in `index.json` are stored
  without their query string, so CDN signatures are never written to disk. An
  unreadable `index.json` is kept as `index.json.corrupt-<UTC timestamp>` and
  rebuilt, with a warning logged.
- `SyncManager.backfill_past_messages(session, group, member)` adds the room's
  pre-subscription messages and their media once per member, without moving the
  cursor.
- `SyncManager.sync_letters(session, group, member)` archives the account's sent
  letters (text + letter-card image) into `letters.json` and `letters/`, with
  its own `letters_updated_at` cursor; works for expired subscriptions. A
  missing `letters.json` triggers a full refetch, and an unreadable one is kept
  as `letters.json.corrupt-<UTC timestamp>` and rebuilt in the same call. When
  a complete fetch returns letters but none to this member, an empty
  `letters.json` is written so the cursor persists instead of the whole room
  being refetched on every call.
- `Client.get_past_messages(..., strict=True)` returns None on a failed request,
  or on a response whose `messages` is missing or not a list, instead of `[]`.
  The default (`strict=False`) is unchanged and typed as `list[dict]`; only
  `strict=True` is typed `Optional[list[dict]]`.
- `member_info` passed to `on_new_messages` has `initial_sync`, True when the
  member's `messages.json` was created by that sync (the callback then gets the
  whole fetched history); ignore those calls when sending notifications.
- `SyncManager.archive_account_profile(session)` stores the account's own
  nickname and avatar (with avatar history) under `account/`; other profile
  fields (birthday, sex, region, FC accounts) are not stored. An unreadable
  `account/profile.json` is kept as `profile.json.corrupt-<UTC timestamp>` and
  rebuilt.

### Fixed
- `Client.download_file` failure logs no longer include the signed query string of
  the media URL, nor any `user:password@` in it (only scheme, host, port and
  path), nor exception text that may embed it.
- `Client.get_messages`: when the `order=asc` probe does not confirm the start of
  the timeline, the `ApiError` text and a `Timeline start probe rejected` warning
  now include the probed first id and the oldest collected id, so a room that
  stops syncing can be diagnosed.
- **A favorite (or any local `messages.json` edit) made while a sync was writing
  the same file could drop the newly synced messages permanently**, because the
  cursor had already advanced past them. `sync_member` and
  `update_message_metadata` now merge under the same per-file lock as
  `update_message_fields`.
- `SyncManager.update_sync_state` keeps keys it does not own in the member's
  entry (such as `past_backfilled_at`) instead of replacing the whole entry;
  corrupt-file or count-mismatch recovery clears `past_backfilled_at` so
  `backfill_past_messages` merges the pre-subscription messages again.
- `Client.get_messages` no longer raises `ApiError` forever for a room whose
  whole history is an exact multiple of 200 messages: a full last page without a
  continuation is accepted when an `order=asc` probe returns the smallest id
  collected, i.e. the room's first message.

## [0.4.4] - 2026-11-02

### Fixed
- `Client.get_messages` no longer raises `ApiError` when the timeline ends on a
  partial page (fewer than a full page of 200 messages; no continuation, or one
  echoing its continuation) before reaching the cursor. That is the start of
  the room's timeline, not a gap — e.g. a caller's overlap window subtracted
  from a cursor pinned at the room's oldest message. Since 0.4.3 it raised on
  every sync for such rooms, so `SyncManager` callers never advanced the cursor
  and the room stopped receiving new messages permanently. It now returns the
  messages and logs a `Timeline starts after cursor` warning.
- `Client.get_messages` still fails closed while the cursor is unreached when
  pagination ends on a full page (missing or repeated continuation) or the
  server replays already-collected messages, and this now also applies to a
  legacy `since_id` cursor whose message has not been seen yet (previously it
  returned only the newest pages, letting callers skip older messages).

## [0.4.3] - 2026-07-08

### Added
- `BrowserAuth.refresh_token_headless` accepts a `channel` argument (also read
  from the `PYSAKA_BROWSER_CHANNEL` env var) to drive the user's installed system
  browser (e.g. `"chrome"`, `"msedge"`) for the silent token refresh. When set,
  it tries that channel, falls back to Edge, and never downloads Chromium at
  runtime — the download otherwise spawns a Node subprocess that pops a console
  window inside a packaged GUI app. Unset (the default) preserves the previous
  bundled-Chromium behaviour with `auto_install` as the fallback, so headless
  server usage is unchanged.

### Changed
- **Breaking:** `TokenManager`, `get_token_manager`, and `KeyringStore` now raise
  `NoSecureKeyringError` on hosts with no secure keyring backend (headless / CI /
  container, or a locked Secret Service) instead of silently writing an
  obfuscated plaintext file. **Migration:** opt in with
  `allow_plaintext_fallback=True` or set `PYSAKA_ALLOW_PLAINTEXT_KEYRING=1` (and
  install the `pysaka[headless]` extra) to keep the previous fallback behaviour,
  or handle the exception and prompt for secure storage.
- `SyncManager.scan_member_media` now returns an `error` field
  (`None` / `"manifest_missing"` / `"manifest_unreadable"` / `"manifest_invalid"`)
  so callers can distinguish an unsynced or corrupt member manifest from a
  fully-present one. Previously an unreadable/missing manifest collapsed into a
  clean-looking empty result that read as "all media present".
- **Breaking:** `Client.refresh_access_token` and `Client.refresh_if_needed` now
  raise `RefreshFailedError` when the token is expired and there is no usable
  refresh path (no `refresh_token`/cookies/`auth_dir`), instead of returning
  `False`. An unrefreshable session now surfaces as an auth error a caller can act
  on rather than silently yielding empty results. **Migration:** proactive-refresh
  callers should catch `RefreshFailedError` in addition to `SessionExpiredError`.

### Fixed
- **Data loss:** `SyncManager` no longer truncates a member's message history
  during recovery. A readable-but-short or corrupt `messages.json` is now rebuilt
  from the member's TRUE full history (fetched from the API, bypassing the
  incremental `prefetched_messages` window) and merged add-only, instead of being
  overwritten with just the incremental window.
- **Data loss:** locally-derived media metadata (`width`, `height`,
  `media_duration`, `is_muted`, and the `media_file` pointer) is now preserved
  across re-syncs. These fields — which the message API never returns — were
  dropped on every re-sync's whole-object upsert; `media_file` in particular is
  now inherited so a withdrawn post (state `canceled`, URL stripped) keeps its
  already-downloaded media reachable instead of orphaning the file.
- `Client.refresh_access_token` is now single-flight: concurrent 401s no longer
  each `POST /update_token` with the same soon-to-be-rotated cookie, and the
  rotated cookie is persisted before the next request uses it.
- `get_messages` also fails closed on a continuation gap detected *before* the
  cursor is reached (in addition to the incomplete-pagination guard), so a
  partial timeline can no longer advance the caller's cursor past unfetched
  messages.
- A failed legacy-credential migration is now surfaced (raises) instead of
  returning the credential as if migrated, and corrupt/undecodable stored data
  logs a greppable `saka.cred.load_failed` id distinct from the never-stored case.
- Blog sync skips an individual deleted blog post (`BlogGoneError`) instead of
  aborting the whole member's backfill.
- `Client.refresh_access_token` now refreshes a server-revoked-but-unexpired token
  instead of early-exiting on the remaining `exp`, so a rejected token is no longer
  re-sent indefinitely and a consumer's proactive refresh is no longer silently
  no-oped.
- Blog incremental sync no longer permanently skips a second same-day post: a
  date-only list date is now compared at day granularity against the
  time-precision cursor. A date parse failure logs and returns `None` instead of
  fabricating the current time (which had poisoned stored publish dates and the
  cursor), and the page-cap failsafe is applied uniformly across all scrapers,
  logging when it — rather than an empty page — ends pagination.
- Hardened media download and logging: the on-disk media filename is derived from
  a validated integer message id (path-traversal safety), secret redaction now
  recurses nested structures, and the token-refresh error path no longer logs the
  raw response body.

## [0.4.2] - 2026-07-04

### Fixed
- Credentials no longer clobber each other in the OS keyring. Every credential
  (login sessions and stored API keys) was written under a single keyring
  service, separated only by the keyring username; keyring's Windows backend
  routes the newest write to the bare `service` target, so credentials churned
  through one slot and a routine session re-save (e.g. the re-auth around an app
  update or reinstall) could silently wipe a stored API key. Each credential is
  now isolated under its own service (`pysaka:<group>`), and existing credentials
  are transparently migrated on first read so upgrades keep sessions and keys.
- `get_messages` now fails closed on an incomplete timeline pagination. If a
  continuation page fails to fetch (a non-raising error path), or the server
  returns an empty page that still carries a continuation, before the cursor or
  the end of the timeline is reached, it raises instead of returning the partial
  newest-only set — otherwise the caller would advance its timestamp cursor past
  the un-fetched older-but-still-new messages, silently losing them. (An empty
  page with no continuation remains a normal end-of-timeline.)

### Changed
- Messages now record a non-"published" server `state` (e.g. `canceled` — the
  member withdrew the post, which strips its media). `normalize_message` keeps it
  on disk; `scan_member_media` excludes only genuinely-withdrawn states
  (`canceled`) from `unresolved` since a withdrawn post has no media by design
  (not a completeness gap). Any other unexpected non-published state (e.g. a
  transient `processing`) is still surfaced in `unresolved`, so it can't silently
  hide a real gap. Published messages stay lean (no `state` field).
- `SyncManager.scan_member_media` now also reports `unresolved` — a list of
  media-type messages (each `{message_id, media_type, timestamp}`) that have no
  recorded media file because the media URL was absent at sync time (e.g. media
  removed on the server). These were previously skipped entirely, which could let
  a completeness check report "all media present" while such media was genuinely
  missing and unrecoverable.

## [0.4.1] - 2026-07-03

### Fixed
- Sync no longer strands message media when a media download is interrupted: the
  per-member cursor is held behind any message whose media is not yet on disk, and
  `download_file` now writes atomically with validation and retries.

### Added
- `SyncManager.scan_member_media` / `reconcile_member_media` primitives for
  detecting and backfilling missing message media.

## [0.4.0] - 2026-07-01

### Added
- Mobile auth mode: `platform` parameter with an Android header/host profile,
  verified per-group `mobile_api_base`, and `refresh_token` capture from the
  signin response so mobile mode can refresh via `/update_token`.
- Absolute Android purity — in android mode, web session cookies are never used
  for token refresh (refresh_token grant only); web behavior unchanged.
- `mark_group_read()` — opt-in clear of a room's unread count on the server
  (the official app's room-open signal).

### Changed
- Announcements query sends `platform=android` in android mode to match the app.
- Large `messages.json` / metadata writes are offloaded via `asyncio.to_thread`
  so the blocking write + retry no longer stalls the event loop.

### Fixed
- **Data loss:** a failed `messages.json` write no longer advances the sync
  cursor — previously a swallowed write error moved the cursor past messages
  that never hit disk, permanently skipping them on the next sync.
- **Data loss:** a message that fails to normalize no longer lets the cursor
  advance past it; it is re-fetched on the next sync instead of being dropped.
- `fetch_json` now propagates `RefreshFailedError` instead of swallowing it into
  `None`, so a dead session surfaces as a re-login prompt rather than empty data.
- Background sync no longer clears the official app's unread badge; read state is
  now controlled explicitly via the timeline `clear_unread` flag.
- Browser login closes exactly once, so a teardown error can no longer discard a
  fully successful login (previously `login()` could return `None` on success).
- Infinite retry loop in `get_messages()` when the first page returns `None`.
- `SessionExpiredError` / `RefreshFailedError` propagate from `delete_json`
  instead of being swallowed into `return False`.

### Security
- `sanitize_name()` strips path-traversal sequences (`..`, `/`, `\`), including
  `..` hidden across a control byte; control characters are removed first.
- JWT decoding uses urlsafe base64 with correct padding.

## [0.3.0] - 2026-03-21

### Changed
- **Breaking:** Package renamed from `pyhako`/`pyzaka` to `pysaka`
- Incremental sync now uses timestamp-based cursor instead of message ID
- `get_messages()` accepts `since_ts` parameter for cursor-based fetching
- `sync_member()` accepts `prefetched_messages` for group-level batching
- Integrity check resets on any message count drop (stricter)

### Added
- Log rotation with separate `error.log` file
- Thread name included in structlog output

### Fixed
- Message loss prevented during force-close with atomic temp file writes
- Overly aggressive low-ID message warning removed

## [0.2.0] - 2026-03-15

### Added
- Yodel service with multi-organization support
- `RefreshFailedError` exception and `is_active` single source of truth
- OAuth cookie preservation for Google, Apple, and LINE providers
- `get_token_manager()` singleton factory for credential management
- File handler support and configurable log levels
- API methods for official app feature parity (media duration, is_muted)
- Lazy token refresh to reduce unnecessary API calls
- JWT parsing shared utilities
- `BlogGoneError` for permanently removed blogs (404/410)
- Blog URL normalization and Sakurazaka thumbnail support
- `keyrings.alt` declared as optional dependency for headless environments

### Changed
- Client now uses `get_token_manager()` singleton instead of creating new instances
- Blog `MemberInfo` moved to `base.py` for shared access
- `get_blog_thumbnail` renamed to `get_blog_detail_metadata`
- Removed internal semaphore from `process_media_queue`

### Fixed
- TLS certificate verification enabled on all API calls (removed `ssl=False`)
- Session cookie value no longer logged in plaintext
- Token prefix removed from debug logs to prevent leakage
- Incremental sync correctly handles non-sequential message IDs
- Nogizaka blog content truncation and path issues resolved
- Sakurazaka blog timestamp parsing uses correct `.blog-foot .date` selector
- Sakurazaka `og:title` site suffix stripped during parsing
- Group official accounts excluded from member lists
- Group rename detection prevents duplicate directories
- Dead `max_id is not None` guard removed from sync manager

### Security
- Removed `ssl=False` from all 9 aiohttp API calls — TLS verification now active
- Session cookie values redacted from debug logs
- mypy overrides documented with rationale for remaining suppressions

## [0.1.1] - 2026-01-11

### Added
- Blog backup support with parallel downloading for all three groups
- Media dimension extraction (width/height) for images and videos
- `SessionExpiredError` exception for proper session handling
- `display_name` configuration option
- Git Flow workflow documentation (CONTRIBUTING.md, PR template)

### Changed
- Improved browser mimicry with proper headers (Accept, Accept-Language, Origin, Platform)
- Optimized headless refresh wait condition

### Fixed
- Token refresh now sends `refresh_token: null` to match browser behavior
- Added `x-talk-app-platform` header for correct web token refresh
- Removed Authorization header that caused refresh failures

### Documentation
- Added official Terms of Service links and warnings
- Added blog scraper documentation

## [0.1.0] - 2026-01-11

### Added
- Initial pysaka core library release
- Multi-group support: Hinatazaka46, Nogizaka46, Sakurazaka46
- OAuth browser authentication flow
- Message synchronization with incremental updates
- Media downloading with progress tracking
- Member information and avatar management
- SQLite database for sync state persistence
- Async/await API design
- Comprehensive type hints
- Property-based testing with Hypothesis

### Security
- Secure credential storage via system keyring
- Token refresh without storing plaintext credentials

[Unreleased]: https://github.com/xebjhm/pysaka/compare/v0.4.5...HEAD
[0.4.5]: https://github.com/xebjhm/pysaka/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/xebjhm/pysaka/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/xebjhm/pysaka/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/xebjhm/pysaka/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/xebjhm/pysaka/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/xebjhm/pysaka/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/xebjhm/pysaka/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/xebjhm/pysaka/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/xebjhm/pysaka/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/pysaka/releases/tag/v0.1.0
