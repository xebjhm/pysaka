# Complete room archives — pysaka 0.4.6 design

Status: approved in discussion 2026-11-03. Scope: pysaka 0.4.6, with matching changes
in SakaDesk and saka-cli.

## 1. Problem

Sync builds one task per member returned by `GET /groups/{id}/members`. It fetches the
room timeline once per room, then filters it per task by `member_id`. A message whose
author is not in that list is fetched and then dropped. The API list is the room's
*current* lineup, not everyone who ever posted:

- Rotating-lineup rooms reuse one room for many events. For example, hinatazaka46 79
  "18th Single ひなた坂46 LIVE" also holds the 16th and 17th lineups.
- Official rooms list only the official account. For example, hinatazaka46 43
  「日向坂46」 lists only member 79, yet 22 members posted there between 2021 and 2022.

A read-only comparison of the server timeline with the user's archive (2026-11-03)
found these messages missing. All of them are `state: published`.

| Room | Missing | Authors |
|---|---|---|
| hinatazaka46 43 日向坂46 | ~1,240 + 33 without `member_id` | 22, 24–30, 32, 58–66, 89, 96–98 (2021-09 → 2022-12) |
| hinatazaka46 79 18th Single LIVE | 66 | 29, 63, 89, 97, 98, 137 (2024-10 → 2026-02) |
| sakurazaka46 33 櫻坂46 | 38 + 1 without `member_id` | 130–138 (2026-05) |
| hinatazaka46 93 新参者 五期生 | 1 without `member_id` | — |

nogizaka46 and yodel rooms have no gaps. Every other gap in single-member rooms was a
`state: canceled` message (section 6).

Checked against the official app (screenshots, Taiwan time = UTC+8):

- Each message is shown under its own author's name and avatar (`member_id` 89, 98, 25
  and 60 matched).
- A message without `member_id` is shown as the room itself: room name and room logo.
  Example: 2022/4/1 11:00 in 日向坂46.

## 2. Goals and non-goals

Goals:

- Archive every message in a room, whoever posted it, the way the official app shows it.
- Recover the missing history once, with media.
- Keep messages a member withdrew (`canceled`), including content we captured before
  the withdrawal.
- Never let a rate-limited response look like "no data".

Non-goals:

- Detecting new messages from authors outside the API list in SakaDesk's
  "check for new messages" path. The user's rule is that anyone posting new messages is
  in the API list.
- Showing canceled messages in SakaDesk. The data is kept for a future always-on sync
  service.
- Changing the per-member cursor computation.

## 3. Room roster (pysaka)

New `pysaka.roster` module (pure helpers) plus `SyncManager` methods.

`SyncManager.room_tasks(session, group, api_members, messages, name_hints) -> list[dict]`
returns the member dicts to sync for one room:

1. `api_members` (unchanged order).
2. Every distinct `member_id` in `messages` that is not in `api_members`. These are
   called *extra authors*.
3. A *room author* (`id` 0) if any message in `messages` has no `member_id`.

Resolving an extra author's member dict (name, thumbnail, phone_image), first match
wins:

1. `name_hints`: id → member dict from every room's API member list already fetched in
   this sync. Sync fetches every room's list anyway.
2. The member block of an existing local `messages/<gid> */<mid> */messages.json`.
3. `Client.get_member(session, member_id)` → `GET /members/{id}` (new; returns None on
   failure). Called at most once per id per `SyncManager` instance (in-memory cache).
4. Fallback `{"id": mid, "name": str(mid)}`. A message is never dropped because a name
   is unknown. A later sync that resolves the name renames the folder (step 5 below).

Room author:

- Member dict `{"id": 0, "name": group["name"], "thumbnail": group["thumbnail"]}`.
- Folder `messages/<gid> <room>/0 <room>/`.
- In `sync_member`'s filter, a message without `member_id` belongs to member 0.
- Profile-image archiving and letters are skipped for member 0.

Folder renames:

- An extra author's folder is `<mid> <name>`.
- If a folder `<mid> <other name>` already exists in the room (an old fallback, or the
  member renamed), it is renamed first. This uses the same approach as
  `_ensure_group_dir`, with a new `_ensure_member_dir`.

Extra authors use the normal `sync_member` with the prefetched timeline. Their cursor
is None the first time, so they take every prefetched message of theirs. Their own
`sync_state` cursor, `messages.json` and media then work like any member's.

The extra authors are found only after the room fetch, so they never lower the fetch
window.

Why no extra fetch is needed for an extra author:

- After the room's history scan (section 4), every incremental fetch starts at or
  before the previous fetch's end.
- So any extra author's messages since the scan are in some fetch, and that fetch makes
  them a task.

## 4. One-time history scan

- `sync_state["room_<gid>"]["history_scanned_at"]` (UTC ISO). The key never collides
  with `<gid>_<mid>` entries.
- While it is missing, that room's fetch is a full fetch (`since_ts=None`), and
  `room_tasks` runs on the full timeline.
- The flag is set only when all of these hold:
  - the full fetch returned without raising;
  - every task of that room returned without raising;
  - every task's cursor was written.
- A failure leaves the flag unset, and the next sync scans again. Merging is
  idempotent.

Pacing. The scan re-reads whole rooms; 2026-11-03's unpaced probes hit HTTP 429.

- `Client.get_messages(..., page_delay: float = 0.0)` sleeps `page_delay` seconds
  between page requests.
- Scans use `page_delay=1.0`.
- In SakaDesk, rooms that need a scan run one at a time behind an `asyncio.Semaphore(1)`.
  Rooms that only need an incremental fetch keep running concurrently as today.

Force resync:

- SakaDesk's force resync deletes `sync_state.json`, which holds the flag.
- The next sync therefore scans every room again.
- A test pins this.

Media for recovered messages goes through the normal media queue and the existing
pending-media cursor hold.

## 5. HTTP 429

`Client.fetch_json` returns None for 429 today (the "unexpected status" branch). So:

- `get_groups` returns `[]`;
- `get_members` returns `[]`;
- `get_messages` returns `[]` or stops at the failed page;
- a scan could be recorded as complete.

Change:

- Add `RateLimitedError(ApiError)` (status 429, `retry_after` seconds when the header
  is present).
- `fetch_json` raises it for 429 on the first response and on the retry after a 401.

Callers already treat `ApiError` as a failed request. The per-group `except Exception`
in SakaDesk logs and skips the room, and the cursor does not move.

## 6. Canceled messages

Facts (2026-11-03):

- A canceled message comes back with `state: canceled` and no `text`/`file`.
- `updated_at` is the cancel time. Recent withdrawals came 17–54 seconds after posting.
- `utils.normalize_message` already records `state`.
- `_merge_messages` prefers the fresh record. A fresh canceled record therefore
  overwrites `content` with None. Media is safe: `media_file` is in
  `_LOCAL_DERIVED_FIELDS`.

Change:

- In `_merge_messages`, when the fresh record's state is canceled (`_MEDIA_STRIPPING_STATES`)
  and the stored record has `content`, the stored content is kept.
  `canceled_at` is set to the fresh `updated_at`. It is recorded on first sight only;
  later syncs keep it.
- A canceled message seen for the first time (nothing stored) is stored as a skeleton:
  - `id`, `timestamp`, `type`, `state`, `canceled_at`, `content: null`;
  - no media.
- If the full-history scan shows such skeletons are currently dropped anywhere before
  the merge, that path is fixed too. The plan verifies this with real payload shapes.
- `canceled_at` is added to `_LOCAL_DERIVED_FIELDS` semantics (kept when the fresh
  record omits it).
- SakaDesk keeps hiding non-published messages (`content.py`), so the UI is unchanged.

## 7. SakaDesk

Sync:

- `sync_group` passes the room's fetched timeline and `name_hints` to
  `manager.room_tasks`. It then syncs the extra tasks after the API tasks, with the same
  backfill, letters and profile steps. Backfill, letters and profile images are skipped
  for member 0.
- Rooms with no `history_scanned_at` are fetched in full behind the scan semaphore with
  `page_delay=1.0`.
- `metadata["groups"]` entries for extra authors and member 0 get `"listed": false`.
  The check-new-messages path (`members_by_group`) skips `listed: false` entries. Their
  2021–2022 cursors would otherwise widen every check back to 2021.

Progress:

- `progress` gains `extend_total(n)`. `sync_group` calls it with the number of extra
  tasks, so "members" never exceeds its total.
- While a room is scanned, the detail line reads `sync.scanningHistory`, a new i18n key
  in en/ja/zh-TW/zh-CN/yue. Suggested zh-TW: 「首次完整掃描聊天室歷史…」.
- No new components.

New-message accounting:

- Messages written for an extra author on that author's first write
  (`initial_sync=True`) and every message written during a history scan are excluded
  from `new_messages` totals and notifications.
- They are indexed for search the same way backfilled messages are (`full_member_scan`).

Display:

- A room with more than one member folder is already a group chat (`content.py`
  `is_group_chat`), so 日向坂46 and 櫻坂46 become group chats with per-message authors.
- Member 0 renders with the room name and room thumbnail. Its avatar opens no profile
  card, pills or context menu.
- `profile_images_service` returns no entries for member 0.

## 8. saka-cli

`cli.py` Phase 2 builds tasks from `get_members`. It switches to `manager.room_tasks`
after the room fetch, with the same scan flag and `page_delay`. No new CLI flags.

## 9. Testing

pysaka (TDD):

- `room_tasks`:
  - union and order;
  - name resolution order, including an unknown id falling back to `str(mid)` and then
    the folder renamed once resolved;
  - member 0;
  - `get_member` called once per id.
- `sync_member` with a member-0 filter.
- Scan flag:
  - set only on full success;
  - unset on a raised fetch or a failed task;
  - absent after `sync_state.json` is deleted.
- `page_delay` spacing (time-machine or a patched sleep).
- 429 → `RateLimitedError`, including the retry-after-401 path. `get_groups`,
  `get_members` and `get_messages` propagate it.
- Canceled:
  - content kept;
  - `canceled_at` set once;
  - skeleton stored;
  - a published message is never marked canceled;
  - media still inherited.
- Regression: existing cursor tests unchanged and passing.

SakaDesk:

- `sync_group` adds extra tasks and extends progress.
- `listed: false` entries are skipped by the check-new path.
- Scan rooms run sequentially.
- Recovered messages are excluded from new counts and included in search.
- Member 0 gets no profile entries.
- Force resync rescans.

Windows check on the archive copy `C:\D\saka-verify`:

- The first sync recovers the counts in section 1 (43, 79, 33, 93) with media.
- A second sync fetches only the incremental window.
- The UI shows the official rooms as group chats with correct authors and the room
  author for null-sender messages.
- No 429.

## 10. Release

- pysaka 0.4.6 (minor additive API: `room_tasks`, `get_member`, `RateLimitedError`,
  `get_messages(page_delay=)`). The 429 behaviour change goes in the changelog under
  Changed.
- SakaDesk pins `pysaka==0.4.6`.
- saka-cli follows when it next releases.
