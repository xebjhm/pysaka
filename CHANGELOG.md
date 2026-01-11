# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/xebjhm/PyHako/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/xebjhm/PyHako/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/xebjhm/PyHako/releases/tag/v0.1.0
