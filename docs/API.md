# PyHako API Reference

## Authentication

### `BrowserAuth`
Handles interactive login via Playwright.

#### `login(group: Union[Group, str], headless: bool = False, user_data_dir: str = None, channel: str = None) -> Optional[dict]`
- **group**: The target group (e.g., `Group.NOGIZAKA46` or `"nogizaka46"`).
- **headless**: Run browser in background (default `False`).
- **user_data_dir**: Path to persist browser profile.
- **channel**: Browser channel (e.g., `'msedge'`, `'chrome'`).
- **Returns**: Dictionary with `access_token`, `cookies`, `app_id`, `user_agent`.

## Client

### `Client`
Main API client supporting all Sakamichi groups.

#### `__init__(group: Group, access_token: str = None, refresh_token: str = None, cookies: dict = None, use_token_storage: bool = False)`
- **group**: Target group (e.g., `Group.HINATAZAKA46`).
- **use_token_storage**: If `True`, attempts to auto-load credentials from system keyring/file.

#### `get_groups(session: aiohttp.ClientSession, include_inactive: bool = False) -> List[dict]`
- **session**: Active aiohttp session.
- **include_inactive**: If True, returns `expired` and `suspended` subscriptions too.
- **Returns**: List of group objects.

#### `get_members(session, group_id: int) -> List[dict]`
- **group_id**: Target group ID.
- **Returns**: List of member objects.

#### `get_messages(session, group_id: int, since_id: int = None, progress_callback = None) -> List[dict]`
- **group_id**: Target group ID.
- **since_id**: (Optional) Only fetch messages newer than this ID.
- **progress_callback**: Async or sync function `(date_str, count)` called during pagination.
- **Returns**: List of message objects (sorted ascending).

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

### `get_token_manager() -> TokenManager`
Get the singleton `TokenManager` instance. Avoids repeated keyring probe operations when accessed from multiple modules.

```python
from pyhako.credentials import get_token_manager

tm = get_token_manager()
tm.save_session("hinatazaka46", access_token, refresh_token, cookies)
```

## Sync Manager

### `SyncManager`
High-level manager for syncing messages and media.

#### `__init__(client: Client, output_dir: Path)`
- **client**: Authenticated `Client` instance.
- **output_dir**: Base directory for downloaded content.

#### `sync_messages(session, group_id: int, since_id: int = None) -> List[dict]`
Sync messages for a group member.

#### `process_media_queue(session, media_queue: List[tuple]) -> dict[str, dict]`
Download media files and extract dimensions.
- **Returns**: Dictionary mapping `member_dir` to dimension updates for each message.

#### `update_message_dimensions(member_dir: Path, dimensions: dict)`
Write extracted dimensions back to messages.json.

## Credentials

### `TokenManager`
Secure credential storage using system keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service).

#### `__init__()`
Initialize the token manager. Uses keyring for secure storage.

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
from pyhako.media import get_media_dimensions

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

##### `get_blogs(member_id: str, since_date: datetime = None) -> AsyncIterator[BlogEntry]`
- **member_id**: The member's unique identifier.
- **since_date**: (Optional) Stop when reaching blogs before this date.
- **Yields**: `BlogEntry` objects for each blog post.

##### `get_blog_detail(blog_id: str, member_id: str = None) -> BlogEntry`
- **blog_id**: The unique identifier of the blog post.
- **member_id**: (Optional) The member's identifier, used by some scrapers for URL construction.
- **Returns**: A `BlogEntry` with full content.

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
from pyhako.blog import get_scraper
from pyhako import Group

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
from pyhako.blog import MemberInfo
```

## Exceptions

### `HakoError`
Base exception for all PyHako errors.

### `AuthError`
Authentication related errors. Subclass of `HakoError`.

### `ApiError`
API request errors. Subclass of `HakoError`.

- **status_code**: `Optional[int]` - HTTP status code that caused the error.

```python
from pyhako import ApiError

try:
    data = await client.fetch_json(session, "/endpoint")
except ApiError as e:
    print(f"API error (status {e.status_code}): {e}")
```

### `BlogGoneError`
Raised when a blog post has been permanently removed (HTTP 404/410).

```python
from pyhako.blog import BlogGoneError

try:
    entry = await scraper.get_blog_detail(blog_id)
except BlogGoneError:
    print("Blog post has been removed")
```

### `SessionExpiredError`
Raised when the session has been invalidated server-side (e.g., user logged in from another device).

```python
from pyhako import Client, SessionExpiredError

try:
    await client.refresh_access_token(session)
except SessionExpiredError:
    # Expected scenario - session revoked server-side
    print("Session expired. Please log in again.")
```

### `RefreshFailedError`
Raised when all token refresh attempts fail unexpectedly. This indicates a potential bug or unexpected server behavior.

```python
from pyhako import Client, RefreshFailedError

try:
    await client.refresh_access_token(session)
except RefreshFailedError:
    # Unexpected failure - consider reporting
    print("Refresh failed unexpectedly. Please log in again.")
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
from pyhako.client import GROUP_CONFIG, Group

config = GROUP_CONFIG[Group.HINATAZAKA46]
print(config["display_name"])  # "日向坂46"
```
