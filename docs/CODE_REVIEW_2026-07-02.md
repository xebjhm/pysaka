# pysaka — Code Review (2026-07-02)

Full read-only review of `src/pysaka/`. Findings ranked by severity with exact
`file:line`, failure scenario, and suggested fix. IDs (`PY-*`) are referenced by
the fix branch.

Legend: **Critical** = data loss / hang / account impact; **Important** = wrong
behavior in realistic conditions; **Minor** = latent trap or correctness edge.

---

## Critical

### PY-C1 — `get_messages` infinite refresh loop on any non-401 failure
- `src/pysaka/client.py:623-627`
- On page 0, any non-401 failure (404 / 403 / empty body → `fetch_json` returns
  `None`) triggers a cookie refresh; the refresh succeeds and `continue` retries
  the same request forever. No attempt counter.
- **Scenario:** syncing a *closed* group (its own docstring at `client.py:534-535`
  says closed-group timelines return 404) → tight network-bound loop that never
  terminates, hammers `/update_token`, rotates the server session cookie and
  writes the keyring every iteration, and hangs the consuming app (SakaDesk).
- **Fix:** allow at most one refresh-and-retry (boolean flag); treat non-401
  statuses as terminal (`break` — a refresh cannot fix a 404); distinguish `{}`
  from `None`.

### PY-C2 — Rotated session cookie persisted *before* it is captured → forced re-login
- `src/pysaka/client.py:360` vs `366-369` (save at `203-212`)
- On cookie-based refresh, `update_token → save_session` runs **before** the
  rotated cookies from the response are copied into `self.cookies`, and no save
  happens afterward. Storage always holds the pre-rotation (already-consumed)
  cookie.
- **Scenario:** `use_token_storage=True` web client refreshes; restart → loads the
  stale cookie → refresh returns `400 invalid_parameter` → `SessionExpiredError`
  ("logged in from another browser") → user must re-login after every restart
  that followed a refresh.
- **Fix:** update `self.cookies` from `resp.cookies` **first**, then call
  `update_token` (or call `save_session()` again after the cookie-capture block).

---

## Important

### PY-I1 — `fetch_json` / `delete_json` swallow auth-refresh failures
- `src/pysaka/client.py:274-276` (fetch_json), `912-914` (delete_json)
- Catch-all `except Exception: return None` swallows `RefreshFailedError` in
  `fetch_json`; `delete_json` swallows both `RefreshFailedError` and
  `SessionExpiredError`. `post_json` propagates both → three inconsistent
  contracts. Consumers see "no data" instead of "re-login required".
- **Fix:** `except RefreshFailedError: raise` in `fetch_json`; re-raise `AuthError`
  subclasses in `delete_json`.

### PY-I2 — `download_file` leaves 0-byte file on mid-download failure, never re-downloaded
- `src/pysaka/client.py:686-694`
- Opens the destination before the body is read and does not clean up on failure;
  the `filepath.exists()` guard then permanently skips re-download of the
  truncated file (`manager.py:324` has the same guard).
- **Fix:** download to a `.tmp` path and `os.replace` on success; unlink the
  partial file in the exception path.

### PY-I3 — Corruption-recovery paths are no-ops → permanent message loss
- `src/pysaka/manager.py:217-218, 232-233` (defeated by `275`)
- Both recovery mechanisms pop the sync state, but `last_ts` was already read at
  `172`, so only *new* messages are fetched; the file is overwritten with just
  those, and `update_sync_state` (275) unconditionally re-creates a valid cursor.
  Entire message history is silently and permanently lost. No test covers this.
- **Fix:** on corruption, set a flag and re-fetch with `last_ts=None` in the same
  call, or return early without calling `update_sync_state`.

### PY-I4 — `sync_member` swallows session/refresh errors → returns 0
- `src/pysaka/manager.py:279-281`
- Catch-all swallows `SessionExpiredError` / `RefreshFailedError`, making auth
  failure indistinguishable from "no new messages" — SakaDesk shows a successful
  empty sync instead of prompting re-login.
- **Fix:** `except (SessionExpiredError, RefreshFailedError): raise` before the
  generic handler.

### PY-I5 — `refresh_access_token` has no lock; concurrent 401s race
- `src/pysaka/client.py:278` (whole method)
- Async callers (SakaDesk) issuing concurrent expired-token requests → both POST
  `/update_token` with the same cookie → the second uses the now-rotated cookie →
  `400 invalid_parameter` → spurious `SessionExpiredError`. Also races
  `self.cookies` mutation at `368`.
- **Fix:** guard refresh with an `asyncio.Lock` (single-flight); re-check token
  validity after acquiring. *(This also addresses SakaDesk SVC-M11.)*

### PY-I6 — JWT payload decoded with `b64decode` instead of `urlsafe_b64decode`
- `src/pysaka/utils.py:77`
- Payloads containing `-`/`_` (base64url — near-certain for real tokens) fail →
  `parse_jwt_expiry` returns `None` → `refresh_if_needed` (`client.py:495-498`)
  refreshes conservatively on **every** call → constant `/update_token` traffic,
  amplifying PY-I5. Tests pass only because their short payloads avoid `-`/`_`.
- **Fix:** `base64.urlsafe_b64decode`.

### PY-I7 — Credential store silently downgrades to plaintext on disk *(= SEC-3)*
- `src/pysaka/credentials.py:92-109`
- When the OS keyring backend is unavailable (headless/CI/container, locked
  Secret Service), auto-switches to `keyrings.alt.file.PlaintextKeyring`
  (base64+zlib, **not** encryption) with only a `logger.warning`. Refresh tokens
  and session cookies become recoverable by any user-level read.
- **Fix:** make the plaintext fallback opt-in (constructor flag / env var); refuse
  to persist otherwise; verify restrictive file permissions when used; hard error,
  not a warning.

### PY-I8 — `get_blogs` aborts the whole member on `BlogGoneError`
- `src/pysaka/blog/hinatazaka.py:408-413`; `src/pysaka/blog/sakurazaka.py:362-367`
- The per-blog loop catches only `ValueError`, but `get_blog_detail` raises
  `BlogGoneError` (a plain `Exception`, `hinatazaka.py:438`) for 404/410. One blog
  deleted between list and detail fetch kills the member's generator; remaining
  older blogs are never yielded.
- **Fix:** catch `(ValueError, BlogGoneError)` and skip the gone blog.

---

## Minor

- **PY-M1** `blog/config.py:52-53` — `parse_jst_datetime` fabricates `now()` on
  parse failure → wrong `published_at`, full re-crawl every sync. Return `None`/
  raise + warn.
- **PY-M2** `blog/nogizaka.py:419-421` — `main_img` inserted without
  `normalize_url()` (metadata path at `222` normalizes) → unusable thumbnail URL +
  duplicate image. Insert `normalize_url(main_img)`, dedupe normalized.
- **PY-M3** `blog/base.py:96-101` (via `normalize_html_urls:103-127`) —
  `normalize_url` prefixes `base_url` onto `data:` / `mailto:` / `tel:` / `#anchor`
  → broken content. Skip URLs with a scheme + fragments.
- **PY-M4** `manager.py:97, 252` — `isoformat() + "Z"` yields malformed
  `...+00:00Z` (double tz). Use `isoformat()` alone or `strftime('%Y-%m-%dT%H:%M:%SZ')`.
- **PY-M5** `manager.py:466-467` — `update_message_metadata` rewrites
  `messages.json` without the tmp+`os.replace` used in `sync_member` (267-270) →
  crash mid-write = permanent corruption. Use temp file + `os.replace`.
- **PY-M6** `utils.py:13-23` — `sanitize_name` only replaces `/`, leaving
  Windows-invalid chars (`\ : * ? " < > |`, trailing dots/spaces) → `mkdir`
  `OSError` (swallowed) → member never syncs on Windows. Strip the full reserved set.
- **PY-M7** `logging.py:164-169` — `_redact_secrets` mutates nested dicts in-place;
  passing a live `client.headers`/`cookies` would set `***REDACTED***` on the real
  dict → 401s. Latent trap. Copy before redacting.
- **PY-M8** `manager.py:69` — `save_sync_state` uses blocking `time.sleep` in an
  async path → Windows file-lock contention blocks the event loop ~0.75s. Use
  `asyncio.sleep` / `asyncio.to_thread`.
- **PY-M9** `client.py:114-120` — stored `refresh_token`/`cookies` override explicit
  constructor args (`saved.get(...) or arg`) → stale stored creds win over a fresh
  login → spurious `SessionExpiredError`. Invert precedence.
- **PY-M10** `manager.py:432` — `process_media_queue` uses unbounded
  `asyncio.gather`; the `concurrency` param is honored only by SakaDesk's pooled
  session wrapper → plain-session library users fire thousands of simultaneous
  requests → bans. Honor `concurrency` with a local `asyncio.Semaphore`.
- **PY-M11** `auth.py:270` — `refresh_token_headless` permanently sets
  `os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"` process-wide as a side effect.
  Set only around the install call and restore.
- **PY-M12** `client.py:278-284` (docstring) vs `435-439` — documented
  True/False return contract is wrong (it raises). Update the docstring / `Raises:`.
