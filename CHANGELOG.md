# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `pysaka.knowledge`: a new, UI-agnostic engine for grounded, cited member
  Q&A over ingested blogs and messages. Includes document cleaning/ingestion
  (`html_to_text`, `normalize_text`, `ingest_blog`, `ingest_messages`),
  member/alias resolution and mention detection (`MemberRegistry`,
  `AliasTable`, `MentionDetector`), chunking (`chunk_documents`), a pure
  lexical index (`PureLexicalIndex`) and hybrid RRF retriever
  (`HybridRetriever`) behind `Embedder`/`VectorStore`/`LexicalIndex`
  protocols, a bounded tool-using `KnowledgeAgent`, and an evidence
  `validate()` gate that drops any answer sentence not grounded in the
  retrieved citations.
- New optional `pysaka[embeddings]` extra (`numpy`, `onnxruntime`,
  `tokenizers`) providing `pysaka.knowledge.backends`: an ONNX-based
  `OnnxEmbedder` and a numpy-backed `NumpyVectorStore`. Not imported by
  `pysaka.knowledge` itself, so the default install stays lean.

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

[Unreleased]: https://github.com/xebjhm/pysaka/compare/v0.4.3...HEAD
[0.4.3]: https://github.com/xebjhm/pysaka/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/xebjhm/pysaka/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/xebjhm/pysaka/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/xebjhm/pysaka/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/xebjhm/pysaka/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/xebjhm/pysaka/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/xebjhm/pysaka/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/pysaka/releases/tag/v0.1.0
