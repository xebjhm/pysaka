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

**Event loops.** A `Client` may be reused one after another across event loops (for example
successive `asyncio.run` calls): its internal locks (token refresh, timeline pacing) are rebuilt
for each loop. It must not be used from several event loops running at the same time (for
example in different threads).

**Errors of the GET methods.** Every method built on `fetch_json` (`get_groups`, `get_members`,
`get_messages`, `get_profile`, `get_news`, `get_tags`, `get_fc_contents`, `get_organizations`,
`get_products`, `get_member`, `get_account`, `get_letters`, `get_past_messages`,
`get_subscription_streak`, `mark_group_read`) raises `RateLimitedError` (a subclass of `ApiError`)
on HTTP 429 instead of returning its failure value (`None`, `[]` or `False`). They also raise
`ApiError` on a server error (5xx) or a network error / timeout, and `SessionExpiredError` /
`RefreshFailedError` on auth failures. The failure value is returned only when the request failed
without raising: a 4xx other than 429, a 401 the token refresh could not fix, or an unexpected
error while handling the response.

#### `__init__(group: Union[Group, str] = Group.HINATAZAKA46, access_token: str = None, refresh_token: str = None, cookies: dict = None, app_id: str = None, user_agent: str = None, auth_dir: Union[str, Path] = None, use_token_storage: bool = False, platform: str = "web", timeline_min_interval: float = 0.0)`
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
- **timeline_min_interval**: Minimum seconds between the starts of two
  `/groups/{id}/timeline` requests made through this client (default `0.0` = off).
  Also available as the settable attribute `Client.timeline_min_interval`; see
  "Timeline pacing" below.
- **Raises**: `ValueError` if an invalid group string is provided, or
  `timeline_min_interval` is negative, NaN or infinite.

#### Timeline pacing: `Client.timeline_min_interval: float`
Only `/groups/{id}/timeline` has been observed to return HTTP 429, and only when it is hit
hundreds of times in a few minutes, as during a one-time history scan. While the attribute is
above `0`, every HTTP request to a group's timeline made through this client waits so that
consecutive request **starts** are at least that many seconds apart (spacing at the client,
not on the wire). That covers every
`get_messages` page, its `order=asc` probe, `mark_group_read`, and the retry after a 401 inside
`fetch_json`. Other endpoints (`/groups/{id}/letters`, `/members/{id}`, ...) and CDN downloads
are never paced.
- The default `0.0` never sleeps. The value can be changed at any time (for example, set `3.0`
  before a history scan and back to `0.0` in a `finally`); a negative, NaN or infinite value
  raises `ValueError`.
- Safe under concurrent callers: waits are serialised per client, so tasks that share a client
  also share the spacing. Each client (one per service) paces independently.
- `get_messages(page_delay=)` is unchanged and adds to the pacing; pass `page_delay=0` when
  the interval is enabled so the pause is not doubled.
- Logging: `Timeline pacing set` (debug, `interval`) whenever the value changes, and one
  `Timeline pacing summary` (info, `requests`, `waited_seconds`) when pacing is switched
  back to `0.0` after at least one paced request.

**Fail-fast after a timeline 429.** Independently of `timeline_min_interval` (also at `0.0`),
once any `/groups/{id}/timeline` request through a client returns HTTP 429, that client raises
`RateLimitedError` for every further timeline request **without sending it**, until the cooldown
has passed. The cooldown is the response's `Retry-After` when it is a finite number >= 0, else
`TIMELINE_429_COOLDOWN` (`150.0` seconds, `pysaka.client`). It covers requests that were already
waiting in the pacing gate (checked again after their wait), `get_messages` pages, the
`order=asc` probe, `mark_group_read` and the 401 retry. The `retry_after` of the raised error is
the remaining cooldown in seconds. Other endpoints (`/groups/{id}/letters`, `/members/{id}`, ...)
are unaffected, the block ends by itself, and it is per client (one per service). The monotonic
deadline is readable as `Client.timeline_blocked_until` (`None` when not blocked). Logging:
`Timeline requests blocked` (warning, `cooldown`) once when blocking starts, and a debug
`Timeline request short-circuited` line (`endpoint`, `retry_after`, `short_circuited` count) per
refused request.

#### `get_groups(session: aiohttp.ClientSession, include_inactive: bool = False) -> List[dict]`
- **session**: Active aiohttp session.
- **include_inactive**: If True, returns `expired` and `suspended` subscriptions too.
- **Returns**: List of group objects (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_members(session, group_id: int) -> List[dict]`
- **group_id**: Target group ID.
- **Returns**: List of member objects (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_messages(session, group_id: int, since_id: int = None, since_ts: str = None, max_id: int = None, progress_callback = None, clear_unread: bool = False, page_delay: float = 0.5) -> List[dict]`
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
- **page_delay**: Seconds to wait between page requests (default `0.5`). To stay under the
  API rate limit during long full-history fetches, set `Client.timeline_min_interval` instead
  (and pass `page_delay=0` so the pause is not doubled).
- **Returns**: List of message objects (sorted by ID ascending). A full last
  page without a continuation is accepted as the start of the timeline when the
  room's first message (an `order=asc` probe) has the smallest id among the
  collected messages; merely being collected is not enough.
- **Raises**: `ApiError` if pagination stops in a way that could hide unfetched
  messages: a failed page fetch or an empty page carrying a continuation after
  earlier pages were collected, or, while the cursor is still unreached, a full
  page with a repeated continuation (always), a full page with a missing
  continuation (unless the `order=asc` probe confirms the timeline start), or a
  page that replays already-collected messages. On HTTP 429 it raises
  `RateLimitedError` instead of returning an empty list.

#### `download_file(session, url: str, filepath: Path, timestamp: str = None) -> bool`
- **url**: Signed media URL. Failure logs carry only its scheme, host, port and path (never the
  query string or `user:password@`).
- **filepath**: Local destination path.
- **timestamp**: (Optional) Timestamp metadata.
- **Returns**: `True` if success/exists.

#### `get_profile(session) -> Optional[dict]`
- **Returns**: Dict containing profile info (nickname, etc.), or `None` when the request failed
  without raising.
- **Raises**: `RateLimitedError` on HTTP 429 (not `None`); see "Errors of the GET methods".

#### `get_news(session, count: int = 20) -> List[dict]`
- **count**: Number of items to fetch (default 20).
- **Returns**: List of news/announcement items (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_tags(session) -> List[dict]`
- **Returns**: List of tags (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_fc_contents(session, organization_id: int = 1) -> List[dict]`
- **organization_id**: Organization ID (default 1).
- **Returns**: List of Fan Club content items (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_organizations(session) -> List[dict]`
- **Returns**: List of organizations (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_products(session, product_type: str = None) -> List[dict]`
- **product_type**: Optional filter (e.g. `'subscription'`, `'fc_subscription'`).
- **Returns**: List of products (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_member(session, member_id: int) -> Optional[dict]`
- **member_id**: The member's id.
- **Returns**: The member dict (`id`, `name`, `thumbnail`, `phone_image`, ...) from
  `GET /members/{id}`, or `None` when the request failed without raising (e.g. a 4xx other than
  429) or the body is not a JSON object. Works for members who are no
  longer in any room's member list.
- **Raises**: `ApiError` on a server (5xx) or network error; `RateLimitedError`
  on HTTP 429; `SessionExpiredError` / `RefreshFailedError` on auth failures
  (propagated from `fetch_json`).

#### `get_account(session) -> Optional[dict]`
- **Returns**: Account info dict, or `None` when the request failed without raising.
- **Raises**: `RateLimitedError` on HTTP 429 (not `None`); see "Errors of the GET methods".

#### `get_letters(session, group_id: int, updated_from: str = None, count: int = 200) -> List[dict]`
- **group_id**: Target group ID.
- **updated_from**: ISO timestamp to fetch letters updated after.
- **count**: Number of letters to fetch (default 200).
- **Returns**: List of letter objects (`[]` when the request failed without raising).
- **Raises**: `RateLimitedError` on HTTP 429 (not an empty list); see "Errors of the GET methods".

#### `get_past_messages(session, group_id: int, *, strict: bool = False) -> List[dict]  # Optional[List[dict]] when strict=True`
Messages from the ~1–1.5 days before the account's first subscription to the room (still returned
after the subscription lapses or is renewed). With `strict=True`, returns None when the request failed
or the response has no `messages` list.
- **group_id**: Target group ID.
- **strict**: If True, a failed request, or a response whose `messages` is missing or not a list,
  returns `None` instead of `[]`, so it can be told apart from a room with no earlier messages. Type
  checkers see `list[dict]` for `strict=False` and `Optional[list[dict]]` for `strict=True`.
- **Returns**: List of historical message objects; `None` only when `strict=True` and the request
  failed without raising or returned no `messages` list.
- **Raises**: `RateLimitedError` on HTTP 429 (not `[]` or `None`); see "Errors of the GET methods".

#### `get_subscription_streak(session, group_id: int) -> Optional[dict]`
- **group_id**: Target group ID.
- **Returns**: Dict with streak information, or `None` when the request failed without raising.
- **Raises**: `RateLimitedError` on HTTP 429 (not `None`); see "Errors of the GET methods".

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
- **Returns**: `True` if successful, `False` when the request failed without raising.
- **Raises**: `RateLimitedError` on HTTP 429 (not `False`); see "Errors of the GET methods".

#### `fetch_json(session, endpoint: str, params: dict = None) -> Optional[dict]`
Low-level authenticated GET returning parsed JSON (used internally by the
higher-level `get_*` methods). Handles token refresh. Returns `None` for a 4xx other than 429, a
401 the refresh could not fix, or an unexpected error while handling the response. Raises
`ApiError` on a server error (5xx) or a network error / timeout, `RateLimitedError` (a subclass of
`ApiError`) on HTTP 429 instead of returning `None` (`retry_after` is `None` unless the header is a
finite, non-negative number; a timeline request during the cooldown after a timeline 429 raises it
without being sent), and `SessionExpiredError` / `RefreshFailedError` on auth failures.
See the `ApiError` example below.

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

#### `sync_member(session, group: dict, member: dict, media_queue: list, progress_callback = None, prefetched_messages: list = None, *, on_new_messages = None, ignore_cursor: bool = False) -> int`
Sync one member's messages, queue their media, and archive the member's profile images.
- **group** / **member**: Group and member object dicts (as returned by
  `get_groups` / `get_members`).
- **media_queue**: A caller-owned `list` that this method **appends** media
  download descriptors to (dicts — see `process_media_queue`); it is drained
  separately after syncing all members.
- **prefetched_messages**: (Optional) A pre-fetched group timeline. When given,
  the per-member API call is skipped and messages are filtered from this list.
- **on_new_messages**: (Optional) `callback(member_info, new_messages)`, sync or
  async, called once after a successful write when messages that were not on
  disk before were added. `member_info` has `group_id`, `group_name`,
  `member_id`, `member_name`, `member_dir` (`Path`), `phone_image` and
  `initial_sync` (`bool`). Not called during corrupt-file/count-mismatch recovery,
  when `ignore_cursor=True`, or by `backfill_past_messages`. It is awaited inline,
  so a slow callback delays that member's sync (hand heavy work off, e.g. schedule a task). Its exceptions
  are logged (exception type only, never their text) and swallowed.
  - `initial_sync` is True when `messages.json` did not exist before this write;
    `new_messages` is then the member's whole fetched history, not fresh posts, so
    consumers sending notifications should ignore calls with `initial_sync=True`.
  - The callback runs before the profile-image step, so a just-changed
    `phone_image` may not be archived yet; use the `phone_image` URL from
    `member_info`.
- **ignore_cursor**: (Optional, default `False`) Take all of this member's rows from
  `prefetched_messages` (or a full fetch) instead of only those at/after the cursor. Used by a
  room's one-time history scan so rows older than the cursor (e.g. canceled ones) are merged.
  Merging is idempotent; the cursor is recomputed from the merged file as usual, except that only
  messages published at or after the previous cursor can hold it back: an older message whose media
  is still missing, or that failed to normalize, is still queued (or logged) but does not pull the
  cursor back to its date (`room_archive_complete` and Verify & Fix cover it). Without a previous
  cursor every message can hold it back, as in a normal sync. `on_new_messages` is not called for
  any row merged when `ignore_cursor=True`, including genuinely new posts that arrived since the last
  sync, so a consumer that notifies should not rely on scan runs.
- **Returns**: The number of messages **processed** (re-fetched overlap
  included, so not a count of new messages).
- A message the member withdrew has `state: "canceled"` (or `"cancelled"`, see
  `pysaka.utils.CANCELED_STATES`) and `canceled_at`, the API's `updated_at`, kept from the first
  time it was seen (omitted, never `null`, while the API row has no `updated_at`; recorded once a
  later row has one). The API returns it without text or media, so `content` captured before the
  withdrawal is kept, also when the fresh `content` is an empty string (and `media_file`, see
  `update_message_fields`). A canceled text message never seen before is stored with `content` null
  instead of being skipped; a text message with no text and no media in any state other than
  canceled (not only `published`) is still skipped.
  Published messages have neither `state` nor `canceled_at`.
- On a corrupt or count-short `messages.json`, `sync_member` re-fetches the
  member's true full history from the API (bypassing `prefetched_messages`) and
  merges add-only, so recovery never truncates the archive.
- Messages without a `member_id` belong to member 0, the room author (`pysaka.roster`). Member 0
  has no profile images, so none are archived for it, and `backfill_past_messages` and
  `sync_letters` return 0 for it.
- A member folder left under an old name (`messages/<gid> */<mid> <old name>`) is renamed to the
  member's current name instead of creating a second folder, and so is a room folder left under an
  old room name (`messages/<gid> <old name>`). When both the old and the new folder exist, neither
  is touched. After a rename, every `media_file` that starts with the old folder is rewritten to the
  new one in the affected `messages.json` (the member's, or every member folder's for a room
  rename), keeping the value's own `\` or `/` separators (`media_file` is relative to
  `output_dir` with the separator of the OS that wrote it). Other values and fields are untouched.
  A failure to rewrite is logged (file and error type) and does not fail the sync. If the rename
  itself fails (`OSError`, e.g. a file locked on Windows), a warning with group id, member id and
  error type is logged and `sync_member` returns 0: nothing is written, the cursor is untouched and
  profile images are skipped, so the next sync retries the rename.

#### `update_message_fields(messages_file: Path, message_id: int, fields: dict) -> bool` (async, static) / `update_message_fields_sync(messages_file, message_id, fields) -> bool` (blocking, static)
Set fields on one message under the file's lock. Use this for every local edit of `messages.json`
(e.g. favorites) so a concurrent sync cannot lose either change. The lock does not protect the
change from later syncs: a sync that re-fetches the message replaces its record with the API's, so a
field the API returns (e.g. `is_favorite`) is overwritten and any other field you set is dropped,
except `width`, `height`, `media_duration`, `is_muted` and `media_file` (`_LOCAL_DERIVED_FIELDS`),
which are carried over when the fresh record lacks them. The blocking form is for callers
already in a worker thread; the async form runs it in a thread. `message_id` is compared as-is with
the stored ids, so pass an `int`. Returns False if the file is missing, the id is absent or the write
failed. Raises `ValueError` if the file is unreadable JSON and `OSError` on read errors.

#### `archive_profile_images(session, group: dict, member: dict, member_dir: Path) -> dict`
Download the current list icon (`group.thumbnail`), chat icon (`member.thumbnail`) and call-screen
image (`member.phone_image`) into `member_dir/profile_images/<kind>/<basename>` and record them in
`profile_images/index.json`:
`{"version": 1, "current": {kind: url}, "images": [{"kind", "file", "url", "first_seen"}]}`
(`current` maps each kind to its latest URL; `images` is the history of distinct files). URLs are
stored without their query string and fragment, so CDN signatures never reach the disk. Kinds are
`thumbnail`, `phone_image` and `group_thumbnail`. Called automatically by `sync_member`, where a
failure is logged and never affects the result or the cursor. Returns `{"downloaded", "skipped", "failed"}`.
- An unreadable `index.json` is renamed to `index.json.corrupt-<UTC timestamp>` (kept for
  inspection; a warning with the file name is logged) and a fresh index is built: the old history
  entries and `first_seen` values are not carried over (the image files stay on disk).
- The current image of a kind is the `images[]` entry where
  `entry["kind"] == kind and entry["file"] == profile_images.relative_file(kind, index["current"][kind])`
  — match by file, never by full URL (CDN URLs can carry changing query strings). If no entry
  matches, the current image has not been archived yet; use the URL in `current[kind]` (this works for
  public CDN URLs only — a signed private URL, such as the account avatar, needs a fresh API URL).

#### `extra_room_members(session, group: dict, api_members: list[dict], messages: list[dict], name_hints: dict[int, dict] = None) -> list[dict]`
Members to sync for a room in addition to `api_members` (the `/groups/{id}/members` lineup): one
member dict per author of `messages` missing from `api_members` (ascending id), then member 0
(`roster.room_author(group)`) when a message has no `member_id`. Rows that sync skips as empty
(non-canceled `text` messages with no text and no media) are ignored, so they never create an
author (no empty folder). Sync each with `sync_member` so their messages are no longer dropped. Each dict has at least `id` and `name`, plus `thumbnail`,
`phone_image` and `portrait` when known. The name is resolved from, first hit wins:
1. `name_hints[id]` (a member dict with a `name`), returned as given.
2. The `member` block of a local `messages/* */<id> */messages.json` whose `name` is a non-empty
   string other than `str(id)`.
3. `Client.get_member` (cached per `SyncManager`, including a `None` result for unknown ids).
4. `{"id": id, "name": str(id)}`; a later sync that learns the name renames the folder.
- **Raises**: `ApiError` from `Client.get_member` (e.g. `RateLimitedError`) propagates and is not
  cached, so isolate each room in the caller.

#### `backfill_past_messages(session, group: dict, member: dict) -> int`
Merge the room's pre-subscription messages (`Client.get_past_messages`) into the member's archive
and download their media. Requires a prior successful `sync_member` for the member. Runs once per
member: `past_backfilled_at` is set in sync_state only when the request succeeded, no message failed
to normalize and every queued media file is on disk; otherwise the next call retries (merging is
idempotent). A corrupt-file or count-mismatch recovery in `sync_member` clears the flag, so the
next call merges the pre-subscription messages again. Never moves the cursor and never fires
`on_new_messages`. Returns the number of messages added.
- **Raises**: `SessionExpiredError` / `RefreshFailedError` propagate. `OSError` (and other
  unexpected errors) after the fetch can propagate too, so wrap the call. These do not raise and
  leave the flag unset: a failed fetch (a warning is logged, returns 0) and, when the member has
  past messages to merge, a `messages.json` that is unreadable JSON or not a JSON object (left
  untouched; a warning, with the found type for a non-object, is logged; returns 0) or whose write
  failed (logged by the write; returns 0).

#### `sync_letters(session, group: dict, member: dict) -> int`
Archive the account's sent letters to this member into `<member_dir>/letters.json` and each
letter-card image into `<member_dir>/letters/<id>.<ext>`. `client_token` is never stored. Works for
expired subscriptions (the server still returns their letters). Uses its own cursor
`letters_updated_at` in the member's sync_state entry; it never touches the message cursor and
never fires `on_new_messages`. The cursor advances (to the newest `updated_at` among all letters
fetched for the room) only when the fetch was complete (it ended on a short page; an empty page
after a full one, hitting the page cap, or a page with no new letter count as incomplete) and every
image of this member's letters is on disk, so a failed image is re-fetched (with a fresh signed
URL) on the next call. When a complete fetch returned letters but none to this member and
`letters.json` does not exist, an empty `{"version": 1, "letters": []}` is written so the cursor
persists; an incomplete fetch writes nothing. A letter whose `id` is not an integer (a string such as `"12"`, a float, a bool or a missing id) is
skipped, and only the id's type name is logged. A non-string `created_at`/`updated_at` counts as missing (stored as `null`).
The cursor is used only while `letters.json` exists: if the file is missing the cursor is dropped and
all letters are fetched again; an unreadable file is renamed to `letters.json.corrupt-<UTC timestamp>`
(kept for inspection), the cursor is dropped, and the letters are rebuilt from a full fetch in the same
call. Fetches up to 50 pages of 200 letters per
call, chaining each page from the last letter's `created_at` (not `updated_at`, which a later
favorite would push past letters not yet fetched); `letters.json` is written only when its content
changes or it is created. Before that write (and only when there is something to write), a room
folder left under an old room name and a member folder left under an old member name are renamed,
with their `media_file` paths rewritten (as `sync_member` does), so letters and messages share one
folder.

`letters.json` shape, sorted by `created_at`:
`{"version": 1, "letters": [{"id", "member_id", "text", "created_at", "updated_at", "is_favorite",
"image_file", "width", "height"}]}`. `image_file` is relative to the member dir
(`letters/<id>.<ext>`) or `None` for a letter without an image; `width`/`height` are the card
thumbnail's size. Returns the number of letters that were not in `letters.json` before (all of them after a corrupt or
missing file).
- **Raises**: `SessionExpiredError` / `RefreshFailedError` propagate. A failed fetch does not raise:
  it logs and returns 0 (a corrupt `letters.json` is already set aside by then, so the next call
  rebuilds it). A failed folder rename does not raise either: a warning with group id, member id
  and error type is logged and 0 returned, nothing is written and the letters cursor is untouched.
  `OSError` from file I/O after the fetch can propagate (like `backfill_past_messages`), so callers
  should wrap the call.

#### `archive_account_profile(session) -> dict`
Archive the account's own nickname and avatar for this service. Writes
`<output_dir>/account/profile.json` = `{"version": 1, "name": str, "updated_at": str}` and archives
the avatar (kind `thumbnail`) into `<output_dir>/account/profile_images/` with the same `index.json`
history as member profile images, so an app can show the user's avatar beside their letters.
The same current-image rule as for `archive_profile_images` applies to `account/profile_images/index.json`:
match the `images[]` entry by `kind` and `file` (via `profile_images.relative_file`), never by full URL.
Birthday, sex, subdivision (region) and FC accounts are returned by `/profile` too but are
deliberately **not** stored or logged. A missing profile writes nothing. A missing, non-string or
empty `name`, and a missing or non-string `updated_at`, keep the stored value (`""`/`null` on the
first write), and only a non-empty string `thumbnail` is archived. An unreadable `profile.json` (or
avatar `index.json`) is renamed to `<name>.corrupt-<UTC timestamp>` (kept for inspection; a warning
with the file name is logged) and rebuilt. Idempotent: an avatar already on disk is skipped, a
changed avatar adds a history entry. Returns `{"downloaded", "skipped", "failed"}` for the avatar.
Not called by `sync_member`; call it once per service.
- **Raises**: `SessionExpiredError` / `RefreshFailedError` propagate. Any other error (the fetch,
  or the file and avatar work after it, e.g. `OSError`) returns all-zero counts, and this method's
  own error log carries the exception type only (never the nickname). A failed avatar download is
  logged by `Client.download_file` with the URL's scheme, host, port and path (query string and any
  `user:password@` redacted).

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
Write extracted media metadata back into a member's `messages.json` (atomic write, under the same
per-file lock as `update_message_fields` and `sync_member`).
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

### One-time history scan

Earlier versions dropped messages from authors missing from `/groups/{id}/members`, so callers
re-read each room's whole timeline once. The flag lives in `sync_state.json` as
`room_<gid>: {"history_scanned_at": "<UTC %Y-%m-%dT%H:%M:%SZ>"}`; it never collides with the
`<gid>_<mid>` member keys, and deleting `sync_state.json` (a force resync) clears it.

#### `history_scanned(group_id: int) -> bool`
True when `room_<gid>.history_scanned_at` is set.

#### `mark_history_scanned(group_id: int) -> None`
Set `history_scanned_at` to the current UTC time and save `sync_state.json`. Call it only after
`room_archive_complete` returned True for the scan's timeline.

#### `room_archive_complete(group: dict, messages: list[dict]) -> bool` (async)
True when every message id in `messages` that `sync_member` stores is in `messages.json` of its
author (`roster.author_id`, so member 0 for messages without `member_id`) under
`messages/<gid> */<author> */`. Messages that sync skips (a text message with no text and no media
that is not canceled), entries without an `id` and non-dict entries are not expected. An empty list
is complete, and a stored `"messages": null` counts as empty. Returns False when an author has no
stored folder or misses an id (a warning with group id, member id and the number missing is
logged), or when a `messages.json` is unreadable JSON, not a JSON object, or has a `messages` value
that is not a list (a warning with group id, member id and the reason — `unreadable`,
`not_an_object` or `messages_not_a_list` — is logged). It catches what a raised exception would
not: a write that failed (`sync_member` returned 0) or a message that failed to normalize. Reads
files in a worker thread.

Recommended caller sequence per room when `history_scanned(gid)` is False:
1. `Client.get_messages(session, gid, page_delay=0)` for the whole timeline, with
   `client.timeline_min_interval = 3.0` set for the scan (reset it to `0.0` in a `finally`).
2. `sync_member(..., prefetched_messages=timeline, ignore_cursor=True)` for each API member and
   each member from `extra_room_members`.
3. `room_archive_complete(group, timeline)`.
4. `mark_history_scanned(gid)` only if step 3 returned True; otherwise the next run scans again.

## Roster helpers

### `pysaka.roster`
Derives who authored a room's messages. `GET /groups/{id}/members` lists only a room's current
lineup, so rotating-lineup and official rooms hold messages from authors missing from it. No I/O.

- `ROOM_AUTHOR_ID: int = 0`: member id for messages the API returns without `member_id` (the room
  itself, shown by the official app with the room name and logo).
- `author_id(message: dict) -> int`: the message's `member_id` when it is an int (bool excluded),
  the int value of a decimal-digit string, else `ROOM_AUTHOR_ID`.
- `extra_author_ids(api_member_ids: Iterable[int], messages: Iterable[dict]) -> list[int]`: the
  authors of `messages` missing from `api_member_ids`, real ids ascending, then `ROOM_AUTHOR_ID`
  last if any message has no author.
- `room_author(group: dict) -> dict`: the member dict standing for the room itself,
  `{"id": 0, "name": group["name"] or str(group["id"]), "thumbnail": group.get("thumbnail")}`.

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

### `RateLimitedError`
Subclass of `ApiError`, raised when the API answers HTTP 429 Too Many Requests.
Raised by `fetch_json` and by every `Client` method built on it (see "Errors of the GET methods"
under `Client`), instead of returning `None`, `[]` or `False`. It is also raised, without a
request being sent, for a timeline request made while the client's timeline cooldown after an
earlier 429 is running (see "Timeline pacing"); `retry_after` is then the remaining cooldown.

- **status_code**: `429`.
- **retry_after**: `Optional[float]` - seconds from the `Retry-After` header, or
  `None` when the header is absent, not a number, not finite (`nan`, `inf`) or negative.
  For a request refused during the timeline cooldown it is always set: the remaining seconds.

```python
import asyncio

from pysaka import RateLimitedError

try:
    members = await client.get_members(session, group_id)
except RateLimitedError as e:
    # Back off; retry_after is None when the server sent no usable header
    await asyncio.sleep(e.retry_after or 60)
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
