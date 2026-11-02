# pysaka 0.4.5: Archive Integrity & Completeness — Design

**Date:** 2026-11-03
**Repo:** `pysaka` (consumed by SakaDesk sub-project A, and later C)
**Status:** Approved in brainstorming 2026-11-03 — awaiting written-spec review
**Release policy:** everything SakaDesk's current roadmap needs from pysaka ships in this one release, so the SDK version is bumped once.

---

## 1. Scope

| # | Item | Why | Ships in 0.4.5 |
|---|------|-----|----------------|
| 1 | Profile-image archiving (`profile_images/`) | Member images are never stored locally; a changed image is lost forever. Enables SakaDesk's avatar history. | Yes |
| 2 | Locked read-modify-write for `messages.json` + public `update_message_fields` | Confirmed lost-update race (§3) can permanently drop freshly synced messages. | Yes |
| 3 | `on_new_messages` callback on `sync_member` | SakaDesk C (per-member notifications, incoming-call popup) needs to know which messages are genuinely new and their type. | Yes |
| 4 | Pre-subscription backfill via `/groups/{id}/past_messages` | Spike (2026-11-03, live, read-only) found 98 messages in 8 rooms missing from the archive. | Yes |
| 5 | "History is an exact multiple of 200" frozen-chat bug (`SakaDesk/docs/KNOWN_BUGS.md` #1) | Known rare bug in pagination. | **Conditional** — see §6 exit criteria |
| 7 | Sent-letter archiving (`letters.json` + letter-card images) | Letters are only ever fetched live; SakaDesk will show them inline in the chat like the official app (added 2026-11-03). | Yes — §9 |
| 8 | Own profile (nickname + avatar) archiving | Needed to show the user's own avatar beside their letters in the chat (added 2026-11-03). | Yes — §10 |

Non-goals: any change to how the forward cursor (`last_sync_ts` / `last_message_id`) is computed or advanced; any UI concern.

---

## 2. Profile-image archiving

### Data
The member block written into `messages.json` already carries three CDN URLs; their basenames embed an upload timestamp and change when the member changes the image (verified on live data):

| kind | source field | example basename |
|------|--------------|------------------|
| `thumbnail` | `member.thumbnail` | `134-20260705-113257.jpeg` |
| `phone_image` | `member.phone_image` | `134-20260223-125348.jpeg` |
| `group_thumbnail` | `group.thumbnail` | `79-20261001-010023.jpg` |

### Layout
```
<member_dir>/profile_images/
    thumbnail/<basename>
    phone_image/<basename>
    group_thumbnail/<basename>
    index.json
```
`index.json` (UTF-8, atomic write via the existing `_atomic_write_json`):
```json
{
  "version": 1,
  "current": {"thumbnail": "https://…/134-20260705-113257.jpeg", "phone_image": "https://…", "group_thumbnail": "https://…"},
  "images": [
    {"kind": "thumbnail", "file": "thumbnail/134-20260705-113257.jpeg",
     "url": "https://…/134-20260705-113257.jpeg", "first_seen": "2026-11-03T04:12:00Z"}
  ]
}
```
- Identity of an image = `(kind, basename)`. A new basename for a kind appends an entry; history accumulates naturally.
- `first_seen` is the UTC time pysaka first archived it — the ordering key that survives a future change of CDN naming.
- `current` maps each kind to the URL from the fresh API data of the latest sync (recorded even before its download succeeds); consumers treat the image whose `url` matches as current and fall back to the remote URL otherwise.
- Basename is sanitized (`sanitize_name`) and must stay inside the kind directory (reject path separators / `..`).

### Behaviour
- New public method `SyncManager.archive_profile_images(session, group, member, member_dir) -> dict` returning `{"downloaded": n, "skipped": n, "failed": n}`.
- Called at the end of **every** `sync_member` invocation that did not raise an auth error — including the "no new messages" early return, since images change independently of messages. URLs come from the `member` / `group` dicts passed in (fresh API data). Wrapped so that **any** exception is logged and swallowed — it can never change `sync_member`'s return value, raise, or affect the cursor. Cost when nothing changed: three stat calls and one small `index.json` write only if `current` changed.
- Idempotent: a kind whose file already exists on disk with non-zero size is skipped (reuses `media_file_is_present`). Downloads use the existing retrying `download_file` path (temp file + rename), so an interruption cannot leave a stub that masquerades as complete.
- Missing/empty URL for a kind → that kind is skipped silently (some members have no `phone_image`).
- A failed download is retried naturally on the next sync (file absent ⇒ retried). No attempt cap needed: one request per kind per sync, at most 3.
- History starts from the first sync after upgrade; earlier images cannot be discovered (timestamped basenames are unguessable). Documented, not worked around.

---

## 3. Locked read-modify-write for `messages.json`

### The race (confirmed by code reading)
SakaDesk's favorite toggle (`backend/api/favorites.py:110-123`) runs in a worker thread (`asyncio.to_thread`): read whole file → flip `is_favorite` → atomic write. `sync_member` reads the file (`manager.py` ~L247), awaits API work, then writes the merged file in a worker thread and **advances the cursor**. Two threads, no lock:

```
favorite thread: read (old) ─────────────────────────────── write(old + fav)   ← overwrites
sync:                    read (old) → merge new → write(old + new) → cursor++
```
Result: the new messages are gone from disk but the cursor has moved past them — permanent loss. The reverse interleaving silently drops the favorite. Window is small (milliseconds) but the consequence is unrecoverable.

### Design
- A process-wide registry of `threading.Lock` keyed by the resolved path of each `messages.json` (thread locks, because favorites and the sync write both execute in worker threads; an `asyncio.Lock` would not serialise them).
- One private primitive, `_locked_update(path, mutate) -> T`, **synchronous**, always invoked via `asyncio.to_thread` from async code:
  1. acquire the path's lock;
  2. read the current file (missing file ⇒ `None`; corrupt ⇒ raise, caller decides);
  3. `data, result = mutate(current)`; if `data is None` nothing is written;
  4. atomic write; release; return `result`.
- Every writer of `messages.json` goes through it:
  - `sync_member`: the merge moves **inside** `mutate`, against the freshly read file, not the copy read before the API call. The earlier read is still used for the corrupt/count-mismatch recovery decision only. `_merge_messages` is unchanged (it is already a pure function).
  - `update_message_metadata`: same pattern.
  - New public `async update_message_fields(messages_file, message_id, fields: dict) -> bool` — sets the given keys on one message; returns `False` if the id is absent. SakaDesk's favorites switches to this (SakaDesk A).
- Cursor update stays where it is (after the write, on the event loop), computed from what `mutate` actually wrote.
- Cross-process writers are already excluded by SakaDesk's `DataDirLock`; this lock is in-process only.

---

## 4. `on_new_messages` callback

- `sync_member(..., on_new_messages: Optional[Callable[[dict, list[dict]], Any]] = None)` — keyword-only, default `None`; the `int` return value is **unchanged**, so saka-cli and existing callers are unaffected.
- "New" = message ids present in `processed` but absent from the file as read **inside** `_locked_update` (exact, race-free). The current `int` return (`len(processed)`) also counts re-fetched overlap; it stays as-is for compatibility, and the docstring is corrected to say "processed", not "new".
- Payload: `member_info` (`group_id`, `member_id`, names, `phone_image`) and the list of new normalized messages (id, type, `published_at`, `media_file`, text). Called once per `sync_member` after the write succeeds, only if the list is non-empty. Sync or async callables are both accepted. Exceptions from the callback are logged and swallowed.
- Not fired during recovery re-fetches (messages that were lost locally are not "new" to the user) or by backfill (§5).

---

## 5. Pre-subscription backfill

### Spike evidence (2026-11-03, read-only GETs with the user's existing Windows session)
`/groups/{id}/past_messages` returns the ~1–1.5 days immediately before the account's **first** subscription to that room, and keeps returning it after expiry, lapse and re-subscription. Every returned id was missing locally:

| room | returned | window | local archive starts |
|------|----------|--------|----------------------|
| 櫻坂 79 中川 智尋 (active) | 9 | 2026-06-14 | 2026-06-15 |
| 櫻坂 62 石森 璃花 | 3 (2 voice) | 2026-01-16 | 2026-01-17 |
| 日向坂 53 上村 ひなの | 27 (3 voice) | 2020-05-27..28 | 2020-05-28 |
| …5 more rooms | 6–20 each | | |
| rooms subscribed at opening | 0 | — | — |

Total 98 messages / 8 rooms. Response items carry the same fields as timeline messages.

### Design
- New public `async SyncManager.backfill_past_messages(session, group, member) -> int`:
  1. Skip (return 0) if the member has no sync_state entry yet (backfill runs only after a first successful `sync_member`) or `past_backfilled_at` is set.
  2. `client.get_past_messages(session, gid)`, filter by `member_id == mid` (same rule as `sync_member`).
  3. `prepare_messages(...)` into a **private** queue, then `process_media_queue` + `update_message_metadata` inline. Backfill is small (≤ ~30 items per room, once), and it must own its media: Verify & Fix (`reconcile_member_media`) takes fresh URLs from the timeline, which never contains pre-subscription messages, so it could not repair them.
  4. `_locked_update` merge (same `_merge_messages`; ids already present are upserted as usual).
  5. Update `total_messages` in sync_state **without touching** `last_message_id` / `last_sync_ts` (new helper `update_total_messages`). Set `past_backfilled_at` (UTC ISO) **only if** the API answered and every queued media file is present on disk afterwards. Persist with `save_sync_state`.
  6. Return the number of messages added.
- An empty response is a valid final answer (room subscribed at opening) → flag is set. An API error or any missing media → flag **not** set; the next sync retries (merge is idempotent, `download_file` skips files already present).
- No cursor involvement: backfilled messages are older than the cursor.
- Does **not** fire `on_new_messages`. Backfilled ids are smaller than any read pointer, so they do not appear as unread in SakaDesk.
- Called by consumers (SakaDesk sync orchestration, once per member per sync; the flag makes later calls free). saka-cli may adopt it later; not required for this release.

---

## 6. Conditional: exact-multiple-of-200 frozen chat

Investigate the pagination end-of-history decision in `Client.get_messages` (the 0.4.4 "fail closed on full or replayed last page" logic). Ship a fix in 0.4.5 **only if all hold**:
1. A definitive end-of-history signal exists in the API response (e.g. continuation token absence on a full page) — verified on recorded responses, not assumed.
2. A regression test reproduces the frozen case with a 200 / 400-message fixture and passes after the fix.
3. Every existing pagination test still passes unchanged.

Otherwise the item is dropped from 0.4.5 and `KNOWN_BUGS.md` stays as-is. Either outcome is reported back before release.

---

## 9. Sent-letter archiving

### Spike evidence (2026-11-03, read-only)
- `GET /groups/{id}/letters` still returns letters for **expired** subscriptions (room 66: 3, room 85: 1); active rooms: 90 → 189, 83 → 57.
- Items: `id, group_id, member_id, text, file` (letter-card image, signed URL), `thumbnail`, `thumbnail_width/height, is_favorite, client_token, created_at, updated_at`. Every letter has `updated_at > created_at` (server-side processing after sending).
- Ordering: ascending by id; `count` takes the oldest first; `updated_from` is inclusive (`>=`; pivot test returned exactly the 95 expected letters).

### Design
- New public `async SyncManager.sync_letters(session, group, member) -> int` (letters newly added):
  1. Cursor `letters_updated_at` in the member's sync_state entry (separate from the message cursor; preserved by the merging `update_sync_state`).
  2. Page: `get_letters(group_id, updated_from=cursor, count=200)`; while a page has 200 items, request again with `updated_from` = the page's last item's `updated_at` (inclusive overlap, deduped by id); stop when a page adds no new id or after 50 pages.
  3. Keep only `member_id == member.id`. Store under `<member_dir>/letters.json` via `_locked_update`: `{"version": 1, "letters": [{id, member_id, text, created_at, updated_at, is_favorite, image_file, width, height}]}` sorted by `created_at`; upsert by id (fresh fields win; `image_file` kept). `client_token` is never stored.
  4. Download each letter's `file` to `<member_dir>/letters/<id>.<ext>` with `download_file` (skips files already present).
  5. Advance `letters_updated_at` to the max `updated_at` across **all** letters fetched for the room, **only if** the fetch was complete (it ended on a short page that is either the first page or non-empty; a mid-pagination empty page means a swallowed error, and hitting the page cap or a full page with no new id are also incomplete) **and** every downloaded image of this member is on disk (same rule as the message cursor's media hold: an image that failed must be re-fetched with a fresh signed URL next run). A member with no letters in the room still advances after a complete fetch. Letters whose `id` is not an integer are skipped; `letters.json` is not rewritten when unchanged.
- Called by consumers for every non-closed room, including expired subscriptions. Never touches the message cursor, never fires `on_new_messages`. Auth errors propagate; other errors are logged by type and leave the cursor unchanged.

## 10. Own profile archiving

- New public `async SyncManager.archive_account_profile(session) -> dict` (`{"downloaded","skipped","failed"}`): `client.get_profile()`; keeps **only** `name` and `thumbnail` — birthday, sex, subdivision and FC account ids are personal data the app does not need and are neither stored nor logged.
- Writes `<output_dir>/account/profile.json` (`{"version": 1, "name", "updated_at"}`) and archives the avatar with the same mechanism and index format as §2 (`<output_dir>/account/profile_images/thumbnail/<basename>` + `index.json`, kind `thumbnail`), so avatar history comes for free.
- `output_dir` is per service, so each group's own avatar is kept separately (users can set different ones). Failures are logged by type and never raise except auth errors.

## 7. Testing

- `respx` for all HTTP; `time-machine` for `first_seen` / `past_backfilled_at`; CJK + emoji names in fixtures.
- Race test for §3: two threads hammer `update_message_fields` and a simulated sync merge on the same file; assert no message id and no field update is lost across many iterations.
- Profile images: new / unchanged / changed basename / missing kind / failed download → retried next run / path-traversal basename rejected / exception inside archiving does not change `sync_member` return or cursor.
- Callback: fires with exactly the new ids; not on overlap re-fetch, recovery or backfill; callback exception swallowed.
- Backfill: flag lifecycle (set on success incl. empty, not set on error), cursor fields untouched, `total_messages` updated, idempotent second call makes no request.
- 100 % coverage of new code (`uv run pytest --cov`), `ruff`, `mypy` clean.

## 8. Docs & release
- Docstrings updated in the same commits (Zero Drift), `docs/API.md` gains the three public methods and the callback.
- CHANGELOG `[Unreleased]` per `docs/DOC_FORMATS.md`; released as 0.4.5 via the existing `/release pysaka 0.4.5` flow, after SakaDesk A is verified against the branch build.
