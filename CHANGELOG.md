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

[Unreleased]: https://github.com/xebjhm/pysaka/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/xebjhm/pysaka/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/xebjhm/pysaka/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/xebjhm/pysaka/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/pysaka/releases/tag/v0.1.0
