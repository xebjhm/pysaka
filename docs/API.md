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

##### `get_blog_detail(blog_id: str) -> BlogEntry`
- **blog_id**: The unique identifier of the blog post.
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

## Enums
### `Group`
- `Group.NOGIZAKA46`
- `Group.SAKURAZAKA46`
- `Group.HINATAZAKA46`

