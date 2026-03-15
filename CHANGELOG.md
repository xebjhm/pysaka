# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- Initial PyHako core library release
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

[Unreleased]: https://github.com/xebjhm/PyHako/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/xebjhm/PyHako/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/xebjhm/PyHako/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/PyHako/releases/tag/v0.1.0
