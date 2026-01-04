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

## Enums
### `Group`
- `Group.NOGIZAKA46`
- `Group.SAKURAZAKA46`
- `Group.HINATAZAKA46`

