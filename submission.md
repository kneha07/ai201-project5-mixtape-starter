# Mixtape Bug Hunt — Submission

## AI Usage

I used Claude Code (an AI coding agent) throughout this project. Specific ways it helped and where I verified things myself:

- **Codebase orientation:** I had Claude read `models.py`, all of `routes/`, and all of `services/` end-to-end and summarize each module's responsibility, before looking at any issue. This built the codebase map below.
- **Tracing call chains:** For each of the two example flows in the README, I asked Claude to trace the route → service → model path so I understood the intended shape of a "working" flow before comparing it to a broken one (this was especially useful for Issue #4, where the fix is "make the broken path look like the working path").
- **Reproducing bugs, not diagnosing them blind:** Rather than asking "what's wrong with this file," I ran the existing test suite first (`pytest tests/ -q`) to see which tests already failed, and inspected the seed data comments (e.g. `seed_data.py` explicitly says "should NOT appear in listening now after fix" and "Bug causes this to return 4"), which are strong hints left by the assignment author. I used Claude to help me read those signals correctly and to write small one-off Python scripts (using `flask shell`-equivalent `app.app_context()` blocks) that queried the seeded database directly to confirm each bug's actual behavior before touching any code.
- **Verifying my diagnosis, not trusting it blindly:** For Issue #3 (search duplicates), my first assumption from reading the code was that `search_songs` would return literal duplicate `Song` dicts. When I ran it against the real seeded database, it returned only 1 result — not 3. I asked Claude to help me understand why, and the answer (SQLAlchemy's `Query.all()` deduplicates fully-loaded ORM entities by primary key identity in this installed version, even though the underlying join produces 3 raw rows) was something I verified myself by dropping to raw SQL execution (`db.session.connection().execute(query.statement)`) and counting the actual rows returned, which did show 3 duplicates. This is a case where the AI's first answer was correct but I didn't take it on faith — I reproduced the row-level duplication independently before deciding how to fix it and before writing the regression test.
- **Boundary/off-by-one bugs:** For Issue #5 (`songs[:-1]`), I already had a hypothesis on first read of the file, but I still ran the existing `test_playlists.py` suite to confirm it before changing anything, since the test file's docstring literally says "Bug causes this to return 4."
- Every fix in this submission was verified by (a) reproducing the buggy behavior against the real seeded SQLite database with a small script, (b) running the relevant pytest file to see it fail, (c) making the smallest change that addressed the root cause, and (d) re-running both the specific test file and the full suite to confirm nothing else broke.

---

## Codebase Map

### Main files

- **`app.py`** — Flask application factory (`create_app`). Configures the SQLAlchemy instance (`db`), registers the four blueprints (`songs`, `playlists`, `users`, `feed`), and wires up the SQLite database. This is the only place the app is constructed; `models.py` and every service import `db` from here rather than instantiating their own.
- **`models.py`** — All SQLAlchemy models: `User`, `Song`, `Tag`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`, plus three association tables: `friendships` (symmetric many-to-many between users, populated in both directions), `song_tags` (many-to-many between songs and tags), and `playlist_entries` (many-to-many between playlists and songs, but *not* a pure association table — it carries extra columns `position`, `added_by`, and `added_at`, which is what makes ordered playlists possible). Every model has a `to_dict()` used directly as the JSON response shape — there's no separate serializer layer.
- **`routes/songs.py`, `routes/playlists.py`, `routes/users.py`, `routes/feed.py`** — Thin Flask blueprints. Every route does request parsing (pulling fields off `request.get_json()`/`request.args`), calls exactly one service function, and converts a `ValueError` from the service into a 400/404 JSON response. No business logic lives in routes — this is a consistent pattern across the whole app, which made it fast to find the real logic once I knew a route's name.
- **`services/streak_service.py`** — Owns `record_listening_event` (create a `ListeningEvent`, then update the streak) and `update_listening_streak` (the actual streak state machine: unchanged if already listened today, +1 if listened yesterday, reset to 1 if a day was skipped).
- **`services/feed_service.py`** — `get_friends_listening_now` (a live "who's online" style feed filtered to a recency window) and `get_activity_feed` (an unfiltered, most-recent-N history feed for the same friend group). These two functions look similar but serve deliberately different purposes — one is "right now," one is "recent history."
- **`services/search_service.py`** — `search_songs` (title/artist substring match) and `get_song` (fetch by ID).
- **`services/notification_service.py`** — `create_notification` (the single low-level insert used by every notification-producing action), `add_to_playlist` (adds a song to a playlist *and* notifies the sharer), `rate_song` (upsert a `Rating`), `get_notifications`, `mark_as_read`.
- **`services/playlist_service.py`** — `create_playlist`, `get_playlist_songs` (ordered by the `position` column on `playlist_entries`), `get_playlist`, `get_user_playlists`.
- **`seed_data.py`** — Populates a realistic dataset: 5 users with a friendship graph, 25(ish) songs with 0/1/3+ tags each (the 3+ tag songs are specifically there to expose Issue #3), 3 playlists, listening events spread from ~10 minutes ago to 58 hours ago (specifically shaped to test the recency window in Issue #2), and existing streak/notification state. The inline comments in this file (e.g. "Bug causes this to return 4") are direct hints toward the bugs' expected behavior.
- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py` already exist and encode the *intended* correct behavior for three of the five issues (streaks, search duplicates, playlist truncation) — running them first was the fastest way to confirm two of the bugs without reading a line of service code.

### Data flow: rating a song → notifying the sharer (and where it was broken)

1. Client sends `POST /songs/<song_id>/rate` with `{user_id, score}`.
2. `routes/songs.py::rate()` parses the body and calls `notification_service.rate_song(user_id, song_id, score)`.
3. `rate_song` looks up the `Song` and the rating `User`, checks for an existing `Rating` row for that `(user_id, song_id)` pair (there's a unique constraint on that pair in `models.py`), and either updates the existing rating's `score` or inserts a new `Rating`, then commits.
4. **This is where the intended flow should have branched off to notify the song's original sharer** — the exact same shape as `add_to_playlist`, which (step 4 in that flow) calls `create_notification(user_id=song.shared_by, ...)` after committing, guarded by `if song.shared_by != added_by_user_id` so you don't notify yourself. `rate_song` had no such branch at all; it just returned the `Rating`. That's Issue #4 — a `commit()` with no following side effect where its sibling function has one.
5. `create_notification` (used by both flows) is a thin insert-and-commit into the `Notification` table, later surfaced via `GET /users/<user_id>/notifications` → `notification_service.get_notifications`.

### Pattern I noticed across the app

Every route is a pure adapter (parse request → call one service function → shape response), and every piece of business logic — including side effects like notifications — lives in `services/`. This made bug-hunting mostly a `services/` exercise, exactly as the README says. It also meant that when one service function (`add_to_playlist`) had a notification side effect and its sibling (`rate_song`) didn't, that asymmetry was immediately visible once I'd read both functions side by side, rather than something I had to guess at.

---

## Root Cause Analyses

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** I ran the existing `tests/test_streaks.py` suite before touching any code. `test_streak_increments_on_sunday` — which listens on a Saturday then a Sunday and expects the streak to go from 1 to 2 — failed with `assert 1 == 2`. That's a confirmed, deterministic reproduction: streak resets to 1 on the Sunday call instead of incrementing.

**How I found the root cause:** `services/streak_service.py::update_listening_streak` is the only place `listening_streak` is mutated. Reading it top to bottom, the branch structure is: no-op if `days_since_last == 0`, increment if `days_since_last == 1 and today.weekday() != 6`, else reset to 1. The `and today.weekday() != 6` clause immediately stood out as unrelated to the streak rule described in the function's own docstring ("If the user listened yesterday: streak increments by 1" — no day-of-week exception is mentioned). I confirmed `weekday()` returns `6` for Sunday (Monday=0 ... Sunday=6) with a quick check, which meant this clause is `False` specifically and only on Sundays.

**The root cause:** The increment branch required *both* "listened exactly one day after the last listen" *and* "today is not a Sunday." Since there's no legitimate product reason a Sunday listen should be treated differently from any other consecutive day, this second condition was pure sabotage of the correct case: on a Sunday, even a perfectly consecutive listen (yesterday was Saturday) fails the `!= 6` check and falls through to the `else: user.listening_streak = 1` branch, silently resetting a streak that should have incremented. This reproduces the reported symptom exactly — "my streak keeps resetting" — because it happens every single Sunday, not intermittently.

**My fix and side-effect check:** Removed the `and today.weekday() != 6` clause entirely, leaving `elif days_since_last == 1: user.listening_streak += 1`. I re-ran all 5 tests in `test_streaks.py` (same-day no-op, consecutive-day increment, skipped-day reset, new-user-starts-at-1, and the Sunday case) — all pass. Since the change only removes a condition from one branch and doesn't touch the `== 0` or `else` branches, same-day and skipped-day behavior is provably unaffected.

---

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:** `tests/test_search.py` already has a fixture that creates a song with 3 tags, plus a test asserting it appears exactly once in `search_songs("Crown Heights")` results. Running the test suite, that test actually *passed* even before my fix — but I didn't trust that at face value (see AI Usage section). I dropped into an `app.app_context()` script and ran the *exact* underlying query construction from `search_service.py` (join `Song` to `song_tags`, filter by title, `.execute()` the raw SQL statement instead of going through `Query.all()`), and got back 3 identical rows for the one 3-tag song. So the bug is real at the SQL level; it was only invisible in the existing test because this installed version of SQLAlchemy's `Query.all()` deduplicates fully-loaded ORM entity results by primary key identity before returning them, papering over the row-level duplication.

**How I found the root cause:** `services/search_service.py::search_songs` builds its query as `db.session.query(Song).outerjoin(song_tags, Song.id == song_tags.c.song_id).filter(...)`. The join is a classic "fan-out" — for a song with 3 tags, the join produces 3 rows (one per matching `song_tags` row), all with the same `Song`. The moment I noticed that `Song.to_dict()` (in `models.py`) already builds its `tags` list independently via the `Song.tags` relationship (`lazy="subquery"`), completely separate from anything in the search query, I was confident the join in `search_songs` served no purpose at all — it isn't used to filter by tag, sort by tag, or select any tag column. It only existed to fan out rows.

**The root cause:** `search_songs` joined `Song` to `song_tags` without any corresponding `.distinct()` or actual use for the joined table, so any song with more than one tag produced one duplicate SQL row per tag. In a database/ORM configuration where full-entity results aren't deduplicated by identity (which is what the assignment's Issue #3 describes, and what the raw-SQL check above proves happens regardless of ORM version), this returns the same song multiple times in the API response.

**My fix and side-effect check:** Removed the `.outerjoin(song_tags, ...)` clause entirely (and the now-unused `song_tags`/`Tag` imports) — the query filters directly on `Song.title`/`Song.artist` with no join needed, since tags are populated by `to_dict()` independently. I re-ran `tests/test_search.py` (all 5 pre-existing tests pass) and added a new regression test, `test_search_query_does_not_join_song_tags`, that checks the raw row count of a join-based query directly against the connection (bypassing ORM entity dedup) to prove the duplication would exist if the join were ever reintroduced, and separately asserts `search_songs` itself returns exactly one result with all 3 tags correctly populated. I also checked `get_song` (the only other function in the file) — it doesn't touch `song_tags` and was unaffected.

---

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** `tests/test_playlists.py::test_playlist_returns_all_songs` creates a playlist with 5 songs and asserts `len(songs) == 5`; its docstring even says "Bug causes this to return 4." Running the suite, it failed with exactly that: 4 songs returned instead of 5.

**How I found the root cause:** `services/playlist_service.py::get_playlist_songs` queries songs ordered by `position` ascending, then returns `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice is the entire root cause — it's applied after the ordered query, unconditionally dropping the last element of whatever list comes back, regardless of playlist size. The function's own docstring says "Note: This function returns all songs in the playlist," which directly contradicts the slice — that mismatch between the doc and the code was what made me confident this was the actual bug and not, say, an off-by-one in the SQL `position` filter itself.

**The root cause:** `get_playlist_songs` truncates its correctly-ordered result list by one element right before returning it, via `songs[:-1]`, so the song in the last position of every playlist is silently omitted from every response — the playlist detail view, and any consumer of `GET /playlists/<id>/songs`, never sees it.

**My fix and side-effect check:** Changed `songs[:-1]` to `songs`. Re-ran `tests/test_playlists.py`: `test_playlist_returns_all_songs` (now returns 5), `test_playlist_returns_songs_in_order` (now returns all 5 in correct position order, not just the first 4), and `test_empty_playlist_returns_empty_list` (unaffected — slicing an empty list was never the issue, this confirms the fix doesn't introduce an off-by-one on the empty case). I also checked `get_user_playlists` and `get_playlist` in the same file — neither touches the songs list, so they're unaffected by this change.

---

### Issue #2 (stretch) — Friends Listening Now shows people from yesterday

**How I reproduced it:** I seeded the database (`python seed_data.py`) and called `get_friends_listening_now` directly for the user `darius`, whose friends are `nova` and `simone`. `simone` had a genuinely live event (~16 minutes old) and correctly appeared. `nova` had no live event, only an older one — about 2 hours old — and `nova` still appeared in the "listening now" feed, with the 2-hour-old timestamp returned as the `listened_at` value. That's the bug reproduced concretely: a friend whose last listen was hours old is presented as if they're currently listening.

**How I found the root cause:** `services/feed_service.py` defines `RECENT_THRESHOLD = timedelta(hours=24)` and `get_friends_listening_now` filters `ListeningEvent.listened_at >= now - RECENT_THRESHOLD`, then deduplicates to the single most recent event per friend. The threshold is the only thing gating what counts as "listening now." I compared this to the sibling function `get_activity_feed` right below it, whose docstring explicitly says it is "not filtered by recency — it returns the most recent N events regardless of when they happened," i.e. it's meant to be the long-history feed. `get_friends_listening_now`'s docstring, by contrast, promises "friends who have listened to something recently," describing a live/near-real-time status, not a 24-hour history window.

**The root cause:** `RECENT_THRESHOLD` was set to 24 hours, which is a "recent activity" window, not a "currently listening" window — that distinction is exactly what separates `get_friends_listening_now` from `get_activity_feed` in this codebase, but the constant didn't reflect it. As a result, anyone who listened to anything at any point in the last full day is shown in the "listening now" feed, including someone whose most recent play was in the evening of the previous calendar day — which is precisely the "shows people from yesterday" symptom in the issue title.

**My fix and side-effect check:** Changed `RECENT_THRESHOLD` from `timedelta(hours=24)` to `timedelta(minutes=30)`, which keeps the seed data's genuinely-live events (10–20 minutes old) visible while excluding the next-oldest tier of events (2+ hours old) that were previously leaking through. Re-ran the reproduction script: `darius`'s feed now shows only `simone` (recent), and `nova` (2-hour-old) is correctly excluded. I also re-checked `get_activity_feed` for `darius` and confirmed it's untouched by this change (it doesn't reference `RECENT_THRESHOLD` at all) and still returns both friends' full history, since that function is intentionally not recency-filtered.

---

### Issue #4 (stretch) — Missing notification when a friend rates my song

**How I reproduced it:** With the seeded database, I had `darius` rate a song shared by `nova` via `notification_service.rate_song(darius.id, song.id, 5)`, then checked `get_notifications(nova.id)` before and after. The notification count stayed at 1 (the pre-existing seeded "added to playlist" notification) — no new notification was created for the rating.

**How I found the root cause:** I compared `rate_song` to `add_to_playlist` in the same file, since both are "friend does X to my song → I get notified" flows and one of them (adding to playlist) is confirmed working (there's even a seeded notification for it, and the README/seed comments call it out as "the correct pattern" to compare against). `add_to_playlist` ends with an `if song.shared_by != added_by_user_id: create_notification(...)` block after its `db.session.commit()`. `rate_song` has the same shape up through `db.session.commit()` — it looks up the `Song`, looks up the rater `User`, upserts the `Rating`, commits — and then just `return rating`. There is no call to `create_notification` anywhere in `rate_song`, and no other code path in the file calls it for a rating event either (`get_notifications` only reads).

**The root cause:** This isn't a typo or an off-by-one — it's a missing feature/branch. `rate_song` never had the notify-the-sharer step implemented in the first place, unlike its sibling `add_to_playlist`, which does. The Rating is saved correctly (which is why rating scores themselves work fine in the app), but nothing downstream of the commit ever creates a `Notification` row for it, so the song's original sharer has no way to learn their song was rated.

**My fix and side-effect check:** Added, immediately after `db.session.commit()` in `rate_song`: `if song.shared_by != user_id: create_notification(user_id=song.shared_by, notification_type="song_rated", body=f"{rater.username} rated your song '{song.title}' {score}/5.")` — the same self-notification guard and `create_notification` call used by `add_to_playlist`, just with a `song_rated` type and a rating-specific message. Re-ran the reproduction script: notification count now goes from 1 to 2 after a friend rates the song, with the correct body text. I also tested the guard directly: having `nova` (the sharer) rate their own song does not create a notification, matching the existing self-notification exclusion pattern in `add_to_playlist`. Ran the full test suite (`pytest tests/`) afterward — all 14 tests (13 pre-existing + 1 new regression test) pass, confirming this addition didn't disturb rating upsert behavior or anything else.

---

## Regression Test

Added `test_search_query_does_not_join_song_tags` in `tests/test_search.py`. It exists because the pre-existing `test_search_no_duplicates_multi_tag_song` test did not actually fail against the buggy code in this environment — this installed SQLAlchemy version deduplicates full-entity `Query.all()` results by primary key identity, which happened to mask the row-level duplication caused by the `song_tags` join. The new test executes the join-based query directly against the raw DB connection (bypassing ORM entity dedup) to assert the duplication would reproduce if the join were ever reintroduced (e.g. by someone "helpfully" adding it back to filter by tag), independent of ORM version behavior — and also asserts `search_songs` itself returns exactly one result with the correct tag list.

---

## Commits

See `git log --oneline` on `bugfix/mixtape`:

1. `fix: stop excluding Sunday from consecutive-day streak increments` (Issue #1)
2. `fix: stop dropping the last song from playlist song lists` (Issue #5)
3. `fix: remove unneeded song_tags join causing duplicate search results` (Issue #3)
4. `fix: shrink Friends Listening Now window from 24 hours to 30 minutes` (Issue #2)
5. `fix: notify song sharer when their song is rated` (Issue #4)
6. `test: add regression test for search duplication bug`
