# pysaka API Reference

## Authentication

### `BrowserAuth`
Handles interactive login via Playwright.

#### `login(group: Union[Group, str], headless: bool = False, user_data_dir: str = None, channel: str = None) -> Optional[LoginCredentials]`
- **group**: The target group (e.g., `Group.NOGIZAKA46` or `"nogizaka46"`).
- **headless**: Run browser in background (default `False`). Interactive login
  needs a visible window — a fresh cookie-less headless context has no way to
  complete OAuth and times out. Use headless only for `refresh_token_headless`
  with an existing persistent profile.
- **user_data_dir**: Path to persist the browser profile. Required to keep a
  reusable session for `refresh_token_headless`; a fresh non-persistent context
  is launched when omitted.
- **channel**: System browser channel (e.g., `'msedge'`, `'chrome'`).
- **Returns**: A `LoginCredentials` dict with `access_token`, `refresh_token`
  (`None` for web-only logins that never returned one), `cookies`, `app_id`,
  `user_agent`; or `None` if login failed. `refresh_token` is the only path to
  mobile-mode (`platform="android"`) token refresh.

#### `refresh_token_headless(group: Group, auth_dir: Union[str, Path], auto_install: bool = True, channel: str = None) -> Optional[LoginCredentials]`
Silently refresh an access token via a headless persistent context (no user
interaction). Reuses the browser session stored in `auth_dir`.
- **auth_dir**: Path to the persistent browser context directory (must already
  exist with a logged-in session).
- **auto_install**: If `True`, download Playwright's bundled Chromium when it is
  missing. Ignored when a system `channel` is in effect.
- **channel**: System browser channel to drive (e.g. `'chrome'`, `'msedge'`);
  may also be supplied via the `PYSAKA_BROWSER_CHANNEL` environment variable.
  When set, the refresh reuses the user's installed browser and never downloads
  Chromium at runtime (that download pops a console window in a packaged GUI
  app). Tries the requested channel, then Edge, then fails closed.
- **Returns**: A `LoginCredentials` dict (with `refresh_token` set to `None`), or
  `None` if the refresh failed/timed out.

## Client

### `Client`
Main API client supporting all Sakamichi groups.

#### `__init__(group: Union[Group, str] = Group.HINATAZAKA46, access_token: str = None, refresh_token: str = None, cookies: dict = None, app_id: str = None, user_agent: str = None, auth_dir: Union[str, Path] = None, use_token_storage: bool = False, platform: str = "web")`
- **group**: Target group, enum or string (default `Group.HINATAZAKA46`).
- **access_token** / **refresh_token** / **cookies**: Credentials from
  `BrowserAuth.login`. `cookies` are required for the web-session refresh path.
- **app_id**: `X-Talk-App-ID` header value (defaults to the group config).
- **user_agent**: `User-Agent` header value (defaults to the platform profile).
- **auth_dir**: Persistent browser-profile directory used by the Plan-C headless
  refresh (`refresh_token_headless`) when cookie/refresh-token refresh fails.
- **use_token_storage**: If `True`, attempts to auto-load credentials from the
  system keyring (or opt-in file fallback).
- **platform**: Request profile — `"web"` (default, browser-like) or `"android"`
  (Flutter app: Dart UA, per-group mobile host, `x-talk-app-platform=android`).
  Android mode refreshes via `refresh_token` only (never web cookies).
- **Raises**: `ValueError` if an invalid group string is provided.

#### `get_groups(session: aiohttp.ClientSession, include_inactive: bool = False) -> List[dict]`
- **session**: Active aiohttp session.
- **include_inactive**: If True, returns `expired` and `suspended` subscriptions too.
- **Returns**: List of group objects.

#### `get_members(session, group_id: int) -> List[dict]`
- **group_id**: Target group ID.
- **Returns**: List of member objects.

#### `get_messages(session, group_id: int, since_id: int = None, since_ts: str = None, max_id: int = None, progress_callback = None, clear_unread: bool = False) -> List[dict]`
- **group_id**: Target group ID.
- **since_ts**: (Primary cursor, since 0.3.0) ISO timestamp — fetch messages
  published after this time. Prefer this for incremental sync.
- **since_id**: (Deprecated) Message ID cursor. Ignored when `since_ts` is set;
  when only `since_id` is given, the timestamp is discovered via a slower
  fallback scan.
- **max_id**: Accepted for compatibility; ignored by the API.
- **progress_callback**: Async or sync function `(date_str, count)` called during pagination.
- **clear_unread**: If `True`, the server clears the account's unread badge for
  this group (the official app's chat-open signal). Defaults to `False` so a
  background sync does **not** zero the user's unread count on the official
  mobile app.
- **Returns**: List of message objects (sorted by ID ascending).

#### `download_file(session, url: str, filepath: Path, timestamp: str = None) -> bool`
- **url**: Signed media URL.
- **filepath**: Local destination path.
- **timestamp**: (Optional) Timestamp metadata.
- **Returns**: `True` if success/exists.

#### `get_profile(session) -> Optional[dict]`
- **Returns**: Dict containing profile info (nickname, etc.) or `None` if failed.

#### `get_news(session, count: int = 20) -> List[dict]`
- **count**: Number of items to fetch (default 20).
- **Returns**: List of news/announcement items.

#### `get_tags(session) -> List[dict]`
- **Returns**: List of tags.

#### `get_fc_contents(session, organization_id: int = 1) -> List[dict]`
- **organization_id**: Organization ID (default 1).
- **Returns**: List of Fan Club content items.

#### `get_organizations(session) -> List[dict]`
- **Returns**: List of organizations.

#### `get_products(session, product_type: str = None) -> List[dict]`
- **product_type**: Optional filter (e.g. `'subscription'`, `'fc_subscription'`).
- **Returns**: List of products.

#### `get_member(session, member_id: int) -> Optional[dict]`
- **member_id**: The ID of the member.
- **Returns**: Member details dict or `None` if failed.

#### `get_account(session) -> Optional[dict]`
- **Returns**: Account info dict or `None` if failed.

#### `get_letters(session, group_id: int, updated_from: str = None, count: int = 200) -> List[dict]`
- **group_id**: Target group ID.
- **updated_from**: ISO timestamp to fetch letters updated after.
- **count**: Number of letters to fetch (default 200).
- **Returns**: List of letter objects.

#### `get_past_messages(session, group_id: int) -> List[dict]`
- **group_id**: Target group ID.
- **Returns**: List of historical message objects (before subscription start date).

#### `get_subscription_streak(session, group_id: int) -> Optional[dict]`
- **group_id**: Target group ID.
- **Returns**: Dict with streak information or `None` if failed.

#### `post_json(session, endpoint: str, data: dict = None) -> Optional[dict]`
- **endpoint**: API endpoint path (e.g. `"/messages/123/favorite"`).
- **data**: Request body as dict (can be `None` for empty body).
- **Returns**: JSON response as dict or `None` if failed.

#### `delete_json(session, endpoint: str) -> bool`
- **endpoint**: API endpoint path.
- **Returns**: `True` if successful (2xx), `False` otherwise.

#### `add_favorite(session, message_id: int) -> bool`
- **message_id**: The ID of the message to favorite.
- **Returns**: `True` if successful, `False` otherwise.

#### `remove_favorite(session, message_id: int) -> bool`
- **message_id**: The ID of the message to unfavorite.
- **Returns**: `True` if successful, `False` otherwise.

#### `mark_group_read(session, group_id: int) -> bool`
- **group_id**: Target group ID.
- Explicitly clears the room's unread count on the server (the official app's
  room-open signal). Opt-in; background sync does not call this.
- **Returns**: `True` if successful, `False` otherwise.

#### `fetch_json(session, endpoint: str, params: dict = None) -> Optional[dict]`
Low-level authenticated GET returning parsed JSON (used internally by the
higher-level `get_*` methods). Handles token refresh and raises `ApiError` on
non-recoverable HTTP errors. See the `ApiError` example below.

#### `refresh_access_token(session) -> bool`
- **session**: Active aiohttp session.
- **Returns**: `True` if refresh succeeded, `False` if no credentials configured.
- **Raises**:
  - `SessionExpiredError` if session is invalid server-side (e.g., logged in elsewhere).
  - `RefreshFailedError` if all refresh attempts failed unexpectedly.

#### `refresh_if_needed(session, min_seconds_remaining: int = 300) -> bool`
- **session**: Active aiohttp session.
- **min_seconds_remaining**: Threshold in seconds. Refresh if token expires within this time. Default: 300 (5 minutes).
- **Returns**: `True` if refresh happened, `False` if skipped (token still valid).

#### `get_token_expiry_seconds() -> Optional[int]`
- **Returns**: Seconds remaining until token expiry (can be negative if expired), or `None` if no token is set.

#### `save_session() -> None`
Manually save current session to storage if configured.

## Credentials

### `get_token_manager(allow_plaintext_fallback: bool = False) -> TokenManager`
Get the singleton `TokenManager` instance. Avoids repeated keyring probe
operations when accessed from multiple modules. `allow_plaintext_fallback` is
applied only when the singleton is first created (also honored via the
`PYSAKA_ALLOW_PLAINTEXT_KEYRING` environment variable); on a host with no secure
backend and no opt-in, the first call raises `NoSecureKeyringError`.

```python
from pysaka.credentials import get_token_manager

tm = get_token_manager()
tm.save_session("hinatazaka46", access_token, refresh_token, cookies)
```

## Sync Manager

### `SyncManager`
High-level manager for syncing messages and media.

#### `__init__(client: Client, output_dir: Path)`
- **client**: Authenticated `Client` instance.
- **output_dir**: Base directory for downloaded content (already service-specific).

#### `sync_member(session, group: dict, member: dict, media_queue: list, progress_callback = None, prefetched_messages: list = None) -> int`
Sync one member's messages and append their media downloads to `media_queue`.
- **group** / **member**: Group and member object dicts (as returned by
  `get_groups` / `get_members`).
- **media_queue**: A caller-owned `list` that this method **appends** media
  download descriptors to (dicts — see `process_media_queue`); it is drained
  separately after syncing all members.
- **prefetched_messages**: (Optional) A pre-fetched group timeline. When given,
  the per-member API call is skipped and messages are filtered from this list.
- **Returns**: The number of new messages processed for this member.
- On a corrupt or count-short `messages.json`, `sync_member` re-fetches the
  member's true full history from the API (bypassing `prefetched_messages`) and
  merges add-only, so recovery never truncates the archive.

#### `process_media_queue(session, queue: list, concurrency: int = 5, progress_callback = None) -> dict[Path, dict[int, dict]]`
Download every media item in `queue` and extract media metadata.
- **queue**: A list of media descriptor **dicts**, each with keys `url`, `path`
  (`Path`), `timestamp`, `message_id`, `media_type`, and `member_dir` (`Path`).
  (These are the dicts `sync_member` appended.)
- **concurrency**: Deprecated and ignored — concurrency is governed by the
  caller's session/pool wrapper.
- **Returns**: A dict mapping each `member_dir` (`Path`) to
  `{message_id: metadata}`, where `metadata` may contain `width`, `height`,
  `media_duration`, and `is_muted`. Feed the per-member slice to
  `update_message_metadata` to persist it.

#### `update_message_metadata(messages_file: Path, metadata: dict[int, dict])`
Write extracted media metadata back into a member's `messages.json` (atomic write).
- **messages_file**: Path to the member's `messages.json` (not the member dir).
- **metadata**: `{message_id: {width?, height?, media_duration?, is_muted?}}`,
  typically one member's slice of the `process_media_queue` result.

#### `scan_member_media(member_dir: Path) -> dict`
Offline scan of a member's `messages.json` for missing media (no network).
- **Returns**: `{"checked": int, "missing": list[dict], "unresolved": list[dict],
  "error": str | None}`. `missing` items are `{message_id, media_type, path,
  timestamp}`; `unresolved` items (media-type messages with no recorded
  `media_file`) are `{message_id, media_type, timestamp}`. `error` is `None` for
  a readable manifest, else one of `"manifest_missing"` / `"manifest_unreadable"`
  / `"manifest_invalid"` so an unsynced member is distinguishable from a fully
  present one.

#### `reconcile_member_media(session, member_dir: Path, missing: list, timeline_messages: list, progress_callback = None) -> dict`
Backfill a member's missing media using fresh URLs from a re-fetched timeline,
then re-check disk truth and persist any new dimension metadata.
- **missing**: The `missing` list from `scan_member_media`.
- **timeline_messages**: A freshly fetched member/group timeline supplying new
  signed URLs keyed by `message_id`.
- **Returns**: `{"repaired": int, "failed": int, "still_missing": int}`.

## Credentials

### `TokenManager`
Secure credential storage using system keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service).

#### `__init__(allow_plaintext_fallback: bool = False)`
Initialize the token manager, backed by a secure OS keyring.
- **allow_plaintext_fallback**: If `True` (or the
  `PYSAKA_ALLOW_PLAINTEXT_KEYRING` environment variable is set to a truthy
  value), fall back to the insecure `keyrings.alt` plaintext store when no secure
  backend is available. This only obfuscates data (base64+zlib), it is **not**
  encryption, so it is off by default.
- **Raises**: `NoSecureKeyringError` when no secure backend exists and the
  plaintext fallback has not been opted in — e.g. on headless/CI/container hosts.
  Callers on such systems must opt in explicitly or handle this exception.

Each credential is isolated under its own keyring service (`pysaka:<group>`);
credentials written under the pre-0.4.2 shared-service layout are transparently
migrated on first read.

#### `save_session(group: str, access_token: str, refresh_token: str = None, cookies: dict = None)`
- **group**: Group identifier (e.g., `"hinatazaka46"`).
- **access_token**: OAuth access token.
- **refresh_token**: (Optional) OAuth refresh token.
- **cookies**: (Optional) Session cookies.

#### `load_session(group: str) -> Optional[dict]`
- **group**: Group identifier.
- **Returns**: Dictionary with `access_token`, `refresh_token`, `cookies` or `None`.

#### `delete_session(group: str)`
- **group**: Group identifier.
Remove stored credentials for this group.

## Media Utilities

### `get_media_dimensions(filepath: Path, media_type: str) -> tuple[Optional[int], Optional[int]]`
Extract dimensions from a media file.

- **filepath**: Path to the media file.
- **media_type**: Type of media (`'picture'`, `'video'`, or other).
- **Returns**: Tuple of `(width, height)` or `(None, None)` for non-visual media.

```python
from pathlib import Path
from pysaka.media import get_media_dimensions

# Image
width, height = get_media_dimensions(Path("photo.jpg"), "picture")
# Returns: (1920, 1080)

# Video
width, height = get_media_dimensions(Path("clip.mp4"), "video")
# Returns: (1280, 720)

# Audio (no dimensions)
width, height = get_media_dimensions(Path("voice.m4a"), "voice")
# Returns: (None, None)
```

### `get_image_dimensions(filepath: Path) -> tuple[Optional[int], Optional[int]]`
Extract dimensions from an image file using Pillow.

### `get_video_dimensions(filepath: Path) -> tuple[Optional[int], Optional[int]]`
Extract dimensions from a video file using pymediainfo.

### `get_audio_metadata(filepath: Path, media_type: str) -> dict[str, float | bool | None]`
Extract audio metadata from a video/voice file using pymediainfo.
- **media_type**: `'video'` or `'voice'` (any other type returns the empty result).
- **Returns**: `{"duration": <seconds | None>, "is_muted": <bool | None>}`.
  `duration` is the General-track duration converted from ms to seconds;
  `is_muted` is set only for `'video'` (`True` when the file has no audio track).

## Blog Scrapers

Public blog scrapers for official member blogs. No authentication required.

### `BaseBlogScraper`
Abstract base class for all blog scrapers.

### `NogizakaBlogScraper(session: aiohttp.ClientSession)`
Scraper for Nogizaka46 official blog (www.nogizaka46.com).

### `SakurazakaBlogScraper(session: aiohttp.ClientSession)`
Scraper for Sakurazaka46 official blog (sakurazaka46.com).

### `HinatazakaBlogScraper(session: aiohttp.ClientSession)`
Scraper for Hinatazaka46 official blog (www.hinatazaka46.com).

#### Common Methods
All scrapers implement:

##### `get_members() -> dict[str, str]`
- **Returns**: Dictionary mapping `member_id` to `member_name` for active members.

##### `get_members_with_thumbnails() -> list[MemberInfo]`
- **Returns**: List of `MemberInfo` (id, name, thumbnail_url) for active members.

##### `get_blogs_metadata(member_id: str, since_date: datetime = None, max_pages: int = 3, member_name: str = None) -> AsyncIterator[BlogEntry]`
Fast metadata-only listing (parses list pages only, **no** detail fetches).
- **member_id**: The member's unique identifier.
- **since_date**: (Optional) Only yield blogs published after this date.
- **max_pages**: Maximum list pages to fetch per member (default 3).
- **member_name**: (Optional) Filter to this member (some sites list "featured"
  blogs from others).
- **Yields**: `BlogEntry` objects with metadata only (`content` is empty).

##### `get_blogs(member_id: str, since_date: datetime = None) -> AsyncIterator[BlogEntry]`
Full-content listing (**slow** — fetches detail per blog; for indexing use
`get_blogs_metadata`).
- **member_id**: The member's unique identifier.
- **since_date**: (Optional) Stop when reaching blogs before this date.
- **Yields**: `BlogEntry` objects for each blog post.

##### `get_blog_detail(blog_id: str, member_id: str = None) -> BlogEntry`
- **blog_id**: The unique identifier of the blog post.
- **member_id**: (Optional) The member's identifier, used by some scrapers for URL construction.
- **Returns**: A `BlogEntry` with full content.
- **Raises**: `BlogGoneError` if the post was permanently removed (404/410);
  `ValueError` if not found.

##### `get_blog_detail_metadata(blog_id: str) -> tuple[str | None, datetime | None, str | None]`
- Fetch authoritative `(thumbnail_url, published_at, title)` from a blog's detail
  page — used when list pages have incomplete data.

### `BlogEntry`
Dataclass representing a blog post.

#### Attributes
- **id**: `str` - Unique blog identifier.
- **title**: `str` - Blog post title.
- **content**: `str` - HTML content of the blog.
- **published_at**: `datetime` - Publication timestamp (JST).
- **url**: `str` - Full URL to the blog post.
- **images**: `list[str]` - List of image URLs.
- **member_id**: `str` - Member identifier.
- **member_name**: `str` - Member display name.

### `get_scraper(group: Group, session: aiohttp.ClientSession) -> BaseBlogScraper`
Factory function to get the appropriate scraper for a group.

```python
from pysaka.blog import get_scraper
from pysaka import Group

async with aiohttp.ClientSession() as session:
    scraper = get_scraper(Group.HINATAZAKA46, session)
    members = await scraper.get_members()
```

### `MemberInfo`
Dataclass representing a member with profile image.

#### Attributes
- **id**: `str` - Member ID (ct parameter for blogs).
- **name**: `str` - Member name in Japanese.
- **thumbnail_url**: `str` - URL to member's profile image on CDN.

```python
from pysaka.blog import MemberInfo
```

## Exceptions

### `SakaError`
Base exception for all pysaka errors.

### `AuthError`
Authentication related errors. Subclass of `SakaError`.

### `ApiError`
API request errors. Subclass of `SakaError`.

- **status_code**: `Optional[int]` - HTTP status code that caused the error.

```python
from pysaka import ApiError

try:
    data = await client.fetch_json(session, "/endpoint")
except ApiError as e:
    print(f"API error (status {e.status_code}): {e}")
```

### `BlogGoneError`
Raised when a blog post has been permanently removed (HTTP 404/410).

```python
from pysaka.blog import BlogGoneError

try:
    entry = await scraper.get_blog_detail(blog_id)
except BlogGoneError:
    print("Blog post has been removed")
```

### `SessionExpiredError`
Raised when the session has been invalidated server-side (e.g., user logged in from another device).

```python
from pysaka import Client, SessionExpiredError

try:
    await client.refresh_access_token(session)
except SessionExpiredError:
    # Expected scenario - session revoked server-side
    print("Session expired. Please log in again.")
```

### `RefreshFailedError`
Raised when all token refresh attempts fail unexpectedly. This indicates a potential bug or unexpected server behavior.

```python
from pysaka import Client, RefreshFailedError

try:
    await client.refresh_access_token(session)
except RefreshFailedError:
    # Unexpected failure - consider reporting
    print("Refresh failed unexpectedly. Please log in again.")
```

### `NoSecureKeyringError`
Subclass of `SakaError`. Raised by `TokenManager` / `get_token_manager` /
`KeyringStore` when no secure OS keyring backend is available and the insecure
plaintext fallback has not been opted in (headless/CI/container hosts, or a
locked Secret Service). Opt in via `allow_plaintext_fallback=True` or the
`PYSAKA_ALLOW_PLAINTEXT_KEYRING` environment variable, or install the
`pysaka[headless]` extra and handle the exception.

```python
from pysaka.credentials import get_token_manager, NoSecureKeyringError

try:
    tm = get_token_manager()
except NoSecureKeyringError:
    # No secure keyring (e.g. server/container). Opt in explicitly if acceptable:
    tm = get_token_manager(allow_plaintext_fallback=True)
```

## Enums

### `Group`
- `Group.NOGIZAKA46`
- `Group.SAKURAZAKA46`
- `Group.HINATAZAKA46`
- `Group.YODEL`

## Configuration

### `GROUP_CONFIG`
Dictionary containing group-specific configuration including `display_name` for localized folder names.

```python
from pysaka.client import GROUP_CONFIG, Group

config = GROUP_CONFIG[Group.HINATAZAKA46]
print(config["display_name"])  # "日向坂46"
```

### Environment variables

| Variable | Effect |
|----------|--------|
| `HAKO_ENV` | `development` (default): pretty console logs. `production`: JSON logs with secret redaction. |
| `PYSAKA_ALLOW_PLAINTEXT_KEYRING` | Truthy (`1`/`true`/`yes`/`on`) opts in to the insecure `keyrings.alt` plaintext fallback when no secure keyring backend exists (see `NoSecureKeyringError`). Off by default. Requires the `pysaka[headless]` extra. |
| `PYSAKA_BROWSER_CHANNEL` | System browser channel (e.g. `chrome`, `msedge`) for `refresh_token_headless`. When set, the silent refresh drives the installed browser and never downloads Chromium at runtime. |
