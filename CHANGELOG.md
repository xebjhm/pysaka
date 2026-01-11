# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Blog backup support with parallel downloading
- Media dimension extraction (width/height) for images and videos
- `SessionExpiredError` exception for proper session handling
- `TokenManager` class for secure credential storage via keyring
- Support for Windows Credential Manager integration

### Changed
- Improved token refresh logic with expiry-aware checks

### Fixed
- Session expiration detection and proper error propagation

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

[Unreleased]: https://github.com/xebjhm/PyHako/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/xebjhm/PyHako/releases/tag/v0.1.0
