# Mixtape Bug Hunt — Submission

All 5 issues fixed, each as its own commit on `bugfix/mixtape`. Quick links: [AI Usage](#ai-usage) · [Codebase Map](#codebase-map) · [Root Cause Analyses](#root-cause-analyses) · [Regression Test](#regression-test) · [Commits](#commits)

**At a glance:**

| # | Issue | Root cause in one line | Fixed in |
|---|---|---|---|
| 1 | Listening streak keeps resetting | `weekday() != 6` silently blocked Sunday from counting as a consecutive day | `streak_service.py` |
| 2 | *(stretch)* "Listening Now" shows people from yesterday | Recency window was 24h — a history window, not a live-status window | `feed_service.py` |
| 3 | Same song shows twice in search | An unused join to `song_tags` fanned out one row per tag | `search_service.py` |
| 4 | *(stretch)* No notification when a friend rates my song | The notify-the-sharer step was never written for ratings (unlike playlist adds) | `notification_service.py` |
| 5 | Last song in a playlist never shows up | `songs[:-1]` unconditionally dropped the last song | `playlist_service.py` |

---

## AI Usage

I used Claude Code throughout this project. Here's specifically how, and where I double-checked it myself instead of taking its word for it:

- **Orientation first.** Before opening any issue, I had Claude read `models.py` and everything in `routes/` and `services/`, and summarize each module's responsibility. That's what the codebase map below is built from.
- **Tracing the "working" version of a pattern.** For the two example flows in the README, I asked Claude to trace route → service → model so I understood what a *correct* flow looks like before comparing it to a broken one. This mattered most for Issue #4, where the fix is essentially "make the broken path match the working path."
- **Reproduce before diagnosing.** Instead of asking "what's wrong with this file," I ran the existing tests first (`pytest tests/ -q`) to see what already failed, and read the hints seed_data.py leaves inline (e.g. *"should NOT appear in listening now after fix"*, *"Bug causes this to return 4"*). I used Claude to help interpret those signals and to write small `app.app_context()` scripts to query the seeded DB directly, confirming each bug's real behavior before touching code.
- **Verifying instead of trusting.** For Issue #3, my first read of the code assumed `search_songs` would return literal duplicate rows. Run against real data, it returned 1 result, not 3. Claude's explanation — that this SQLAlchemy version dedupes fully-loaded ORM entities by primary key before returning them, even though the join produces 3 raw rows underneath — turned out to be correct, but I didn't take that on faith. I verified it myself by running the raw SQL directly (`db.session.connection().execute(query.statement)`) and counting 3 actual rows before deciding how to fix it.
- **Where I already had a hypothesis.** For Issue #5 (`songs[:-1]`), I spotted the likely bug on first read. I still ran `test_playlists.py` to confirm before changing anything — its docstring literally says *"Bug causes this to return 4."*
- **The verification loop for every fix:** reproduce the bug against real seeded data → watch the relevant test fail → make the smallest change that addresses the root cause → re-run that test file and the full suite.

---

## Codebase Map

### Main files

- **`app.py`** — the Flask app factory (`create_app`). Sets up the SQLAlchemy instance (`db`), registers the four blueprints (`songs`, `playlists`, `users`, `feed`), and configures SQLite. Every model and service imports `db` from here rather than creating its own.
- **`models.py`** — all SQLAlchemy models: `User`, `Song`, `Tag`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`, plus three association tables:
  - `friendships` — symmetric many-to-many, populated in both directions
  - `song_tags` — many-to-many between songs and tags
  - `playlist_entries` — many-to-many between playlists and songs, but *not* a plain association table: it carries `position`, `added_by`, and `added_at`, which is what makes ordered playlists possible

  Every model has a `to_dict()` that doubles as the JSON response shape — there's no separate serializer layer.
- **`routes/songs.py`, `routes/playlists.py`, `routes/users.py`, `routes/feed.py`** — thin Flask blueprints. Each route parses the request, calls exactly one service function, and turns a `ValueError` into a 400/404. No business logic lives in routes — a consistent pattern that made it fast to find real logic once I knew a route's name.
- **`services/streak_service.py`** — `record_listening_event` (log a `ListeningEvent`, then update the streak) and `update_listening_streak` (the state machine: unchanged if already listened today, +1 if listened yesterday, reset to 1 if a day was skipped).
- **`services/feed_service.py`** — `get_friends_listening_now` (a live "who's online" feed, filtered to a recency window) and `get_activity_feed` (unfiltered, most-recent-N history). Similar-looking, deliberately different purposes: one is "right now," one is "recent history."
- **`services/search_service.py`** — `search_songs` (title/artist substring match) and `get_song` (fetch by ID).
- **`services/notification_service.py`** — `create_notification` (the one low-level insert every notification-producing action uses), `add_to_playlist` (adds a song to a playlist *and* notifies the sharer), `rate_song` (upserts a `Rating`), `get_notifications`, `mark_as_read`.
- **`services/playlist_service.py`** — `create_playlist`, `get_playlist_songs` (ordered by `position`), `get_playlist`, `get_user_playlists`.
- **`seed_data.py`** — realistic sample data: 5 users with a friendship graph, ~25 songs with varying tag counts (songs with 3+ tags exist specifically to expose Issue #3), 3 playlists, and listening events spread from ~10 minutes to 58 hours ago (shaped to test the Issue #2 recency window). Inline comments like *"Bug causes this to return 4"* are direct hints toward expected behavior.
- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py` already encode the *intended* correct behavior for three of the five issues. Running them first was the fastest way to confirm two of the bugs without reading a line of service code.

### Data flow: rating a song → notifying the sharer

1. Client sends `POST /songs/<song_id>/rate` with `{user_id, score}`.
2. `routes/songs.py::rate()` parses the body and calls `notification_service.rate_song(user_id, song_id, score)`.
3. `rate_song` looks up the `Song` and rating `User`, checks for an existing `Rating` for that `(user_id, song_id)` pair (unique constraint in `models.py`), updates or inserts it, then commits.
4. **This is where the flow should have branched off to notify the sharer** — the same shape as `add_to_playlist`, which calls `create_notification(user_id=song.shared_by, ...)` after its commit, guarded by `if song.shared_by != added_by_user_id`. `rate_song` had no such branch — it just returned the `Rating`. That gap is Issue #4: a `commit()` with no side effect where its sibling function has one.
5. `create_notification` (shared by both flows) is a thin insert into `Notification`, later surfaced via `GET /users/<user_id>/notifications` → `get_notifications`.

### Pattern I noticed

Every route is a pure adapter — parse request, call one service function, shape response — and all business logic, including side effects like notifications, lives in `services/`. That made bug-hunting mostly a `services/` exercise. It also meant the asymmetry between `add_to_playlist` (notifies) and `rate_song` (doesn't) was visible immediately once I read both side by side, rather than something I had to hunt for.

---

## Root Cause Analyses

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** Ran `tests/test_streaks.py` before touching code. `test_streak_increments_on_sunday` — listens Saturday then Sunday, expects streak 1→2 — failed with `assert 1 == 2`. Deterministic reproduction: the streak resets on the Sunday call instead of incrementing.

**How I found the root cause:** `update_listening_streak` in `streak_service.py` is the only place `listening_streak` is mutated. Its branches: no-op if `days_since_last == 0`, increment if `days_since_last == 1 and today.weekday() != 6`, else reset to 1. The `and today.weekday() != 6` clause stood out immediately — the function's own docstring says only *"if the user listened yesterday, streak increments by 1,"* with no day-of-week exception. Confirming that Python's `weekday()` returns `6` for Sunday made clear this clause is `False` specifically and only on Sundays.

**The root cause:** The increment branch required *both* "listened exactly one day after the last listen" *and* "today is not a Sunday." There's no product reason a Sunday listen should behave differently — so on a Sunday, even a perfectly consecutive listen (yesterday was Saturday) fails the `!= 6` check and falls into `else: user.listening_streak = 1`, silently resetting a streak that should have grown. This matches the reported symptom exactly: it happens *every* Sunday, not intermittently.

**My fix and side-effect check:** Removed `and today.weekday() != 6`, leaving `elif days_since_last == 1: user.listening_streak += 1`. Re-ran all 5 tests in `test_streaks.py` (same-day no-op, consecutive-day increment, skipped-day reset, new-user-starts-at-1, Sunday case) — all pass. The change only removes a condition from one branch, so same-day and skipped-day behavior is untouched.

---

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:** `tests/test_search.py` has a fixture with a 3-tag song and asserts it appears once in results. That test actually *passed* before my fix — I didn't trust that at face value (see AI Usage). Running the exact query construction from `search_service.py` directly against the raw connection (bypassing `Query.all()`) returned 3 identical rows for the one 3-tag song. The bug is real at the SQL level; it was invisible in the test only because this SQLAlchemy version dedupes fully-loaded ORM entities by primary key before returning them.

**How I found the root cause:** `search_songs` builds `db.session.query(Song).outerjoin(song_tags, Song.id == song_tags.c.song_id).filter(...)`. That join is a classic fan-out — a song with 3 tags produces 3 joined rows. The moment I noticed `Song.to_dict()` already builds its `tags` list independently via the `Song.tags` relationship, I was confident the join served no purpose: it isn't used to filter, sort, or select by tag. It only fanned out rows.

**The root cause:** `search_songs` joined `Song` to `song_tags` with no `.distinct()` and no actual use of the joined table, so any song with more than one tag produced one duplicate row per tag. Any DB/ORM setup that doesn't dedupe full entities by identity — which is what the assignment's issue describes — surfaces this as literal duplicate results.

**My fix and side-effect check:** Removed the `.outerjoin(song_tags, ...)` clause and the now-unused `song_tags`/`Tag` imports — search filters directly on `Song.title`/`Song.artist`, no join needed. Re-ran `test_search.py` (all 5 pass) and added a regression test, `test_search_query_does_not_join_song_tags`, that checks the raw row count of a join-based query directly (bypassing ORM dedup) to prove the duplication would reappear if the join were reintroduced, plus asserts `search_songs` returns exactly one result with all 3 tags. Also checked `get_song` — doesn't touch `song_tags`, unaffected.

---

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** `test_playlists.py::test_playlist_returns_all_songs` creates a 5-song playlist and asserts `len(songs) == 5` (docstring: *"Bug causes this to return 4"*). Ran it — failed with exactly 4 songs returned.

**How I found the root cause:** `get_playlist_songs` in `playlist_service.py` queries songs ordered by `position` ascending, then returns `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice unconditionally drops the last element of whatever comes back, regardless of playlist size. The function's own docstring — *"this function returns all songs in the playlist"* — directly contradicts the slice, which is what made me confident this was the actual bug rather than, say, an off-by-one in the SQL filter.

**The root cause:** `get_playlist_songs` truncates its correctly-ordered result by one element right before returning it, so the last song in every playlist is silently omitted from every response — the playlist detail view and any consumer of `GET /playlists/<id>/songs` never sees it.

**My fix and side-effect check:** Changed `songs[:-1]` to `songs`. Re-ran `test_playlists.py`: `test_playlist_returns_all_songs` (now 5), `test_playlist_returns_songs_in_order` (all 5, correct order), `test_empty_playlist_returns_empty_list` (still passes — confirms no new off-by-one on the empty case). Also checked `get_user_playlists` and `get_playlist` — neither touches the songs list, unaffected.

---

### Issue #2 (stretch) — Friends Listening Now shows people from yesterday

**How I reproduced it:** Seeded the DB and called `get_friends_listening_now` for `darius`, whose friends are `nova` and `simone`. `simone` had a genuinely live event (~16 min old) and correctly appeared. `nova`'s most recent event was ~2 hours old with no live event — and `nova` still appeared, with that 2-hour-old timestamp reported as `listened_at`.

**How I found the root cause:** `feed_service.py` defines `RECENT_THRESHOLD = timedelta(hours=24)`, and `get_friends_listening_now` filters `listened_at >= now - RECENT_THRESHOLD`. Comparing to the sibling `get_activity_feed`, whose docstring says it's explicitly *not* recency-filtered (a long-history feed) — while `get_friends_listening_now`'s docstring promises "friends who have listened to something recently," describing live/near-real-time status, not a 24-hour window.

**The root cause:** `RECENT_THRESHOLD` was set to a "recent activity" window (24h) rather than a "currently listening" window. That distinction is exactly what's supposed to separate the two feed functions, but the constant didn't reflect it — so anyone active any time in the last full day shows up as "listening now," including people whose last play was the evening before.

**My fix and side-effect check:** Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)` — keeps the seed data's genuinely-live events (10–20 min old) visible while excluding the next tier (2+ hours old). Re-ran the reproduction: `darius`'s feed now shows only `simone`; `nova` is correctly excluded. Confirmed `get_activity_feed` doesn't reference `RECENT_THRESHOLD` at all and still returns both friends' full history, since it's intentionally unfiltered.

---

### Issue #4 (stretch) — Missing notification when a friend rates my song

**How I reproduced it:** Had `darius` rate a song shared by `nova` via `notification_service.rate_song(darius.id, song.id, 5)`, then checked `get_notifications(nova.id)` before and after. Count stayed at 1 (the pre-existing "added to playlist" notification) — nothing new was created for the rating.

**How I found the root cause:** Compared `rate_song` to `add_to_playlist`, since both are "friend does X to my song → I get notified" flows and one of them is confirmed working. `add_to_playlist` ends with `if song.shared_by != added_by_user_id: create_notification(...)` after its commit. `rate_song` has the identical shape up through `db.session.commit()` — then just `return rating`. No call to `create_notification` anywhere in the function, and nothing else in the file calls it for a rating event either.

**The root cause:** Not a typo or off-by-one — a missing feature. `rate_song` never had the notify-the-sharer step implemented at all, unlike its sibling `add_to_playlist`. The rating itself saves correctly (ratings work fine in the app), but nothing downstream of the commit ever creates a `Notification` row, so the sharer has no way to learn their song was rated.

**My fix and side-effect check:** Added, right after `db.session.commit()` in `rate_song`:
```python
if song.shared_by != user_id:
    create_notification(
        user_id=song.shared_by,
        notification_type="song_rated",
        body=f"{rater.username} rated your song '{song.title}' {score}/5.",
    )
```
Same self-notification guard and `create_notification` call `add_to_playlist` uses, just with a `song_rated` type and rating-specific message. Re-ran the reproduction: notification count now goes 1→2 with the correct body text. Also confirmed the guard works — `nova` rating their own song creates no notification, matching `add_to_playlist`'s existing self-notification exclusion. Ran the full suite afterward: all 14 tests (13 original + 1 new regression test) pass, confirming this didn't disturb rating upsert behavior or anything else.

---

## Regression Test

Added `test_search_query_does_not_join_song_tags` in `tests/test_search.py`. It exists because the pre-existing `test_search_no_duplicates_multi_tag_song` didn't actually fail against the buggy code in this environment — this SQLAlchemy version's entity-identity dedup on `Query.all()` masked the row-level duplication caused by the `song_tags` join. The new test executes the join-based query directly against the raw DB connection (bypassing ORM dedup) to prove the duplication would reproduce if the join were ever reintroduced — plus asserts `search_songs` itself returns exactly one result with the correct tag list.

---

## Commits

`git log --oneline` on `bugfix/mixtape`:

1. `fix: stop excluding Sunday from consecutive-day streak increments` — Issue #1
2. `fix: stop dropping the last song from playlist song lists` — Issue #5
3. `fix: remove unneeded song_tags join causing duplicate search results` — Issue #3
4. `fix: shrink Friends Listening Now window from 24 hours to 30 minutes` — Issue #2
5. `fix: notify song sharer when their song is rated` — Issue #4
6. `test: add regression test for search duplication bug`

Screenshot: [git_log_screenshot.png](git_log_screenshot.png). Live app verification of the fixes running end-to-end: [app_running_screenshot.png](app_running_screenshot.png).
