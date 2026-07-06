# Mixtape 🎧

Mixtape is a social music app — friends share songs, build playlists together, rate what they hear, and keep tabs on who's listening right now. This repo is my submission for **Project 5: Mixtape Bug Hunt**, where the app shipped with five real bugs and the job was to find them, fix them, and explain why they happened.

**Status: all 5 bugs fixed.** See [`submission.md`](submission.md) for the full write-up — codebase map, root cause analysis for each bug, and how AI tools were used along the way.

---

## What's in here

```
mixtape-bughunt/
├── app.py                      # Flask app factory + DB setup
├── models.py                   # SQLAlchemy models (User, Song, Playlist, Notification, ...)
├── routes/                     # Thin HTTP layer — parses requests, calls a service, shapes the response
│   ├── songs.py                # share / search / rate songs
│   ├── playlists.py            # create playlists, add & list songs
│   ├── users.py                # profiles, streaks, notifications
│   └── feed.py                 # "friends listening now" + activity feed
├── services/                   # All the actual business logic lives here
│   ├── streak_service.py       # listening streak math
│   ├── feed_service.py         # who's listening now, recent activity
│   ├── search_service.py       # song search
│   ├── notification_service.py # notification creation + delivery
│   └── playlist_service.py     # playlist song retrieval
├── tests/                      # pytest suite, including a regression test I added
├── seed_data.py                # populates the DB with realistic sample data
└── requirements.txt
```

The pattern across the app: **routes are dumb, services are smart.** If something's broken at an endpoint, the fix is almost always in the matching `services/` file, not the route itself.

---

## Running it locally

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate.bat       # Windows (cmd)
# source .venv/Scripts/activate    # Windows (Git Bash)

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed the database with sample data
python seed_data.py

# 4. Run the app
FLASK_APP=app:create_app flask run
```

The app will be live at **http://127.0.0.1:5000**.

> ⚠️ **Don't run `python app.py`** — it triggers a SQLAlchemy double-import error. Always start it with `FLASK_APP=app:create_app flask run`.
>
> 🍎 **On macOS**, use `127.0.0.1` instead of `localhost` — `localhost` can resolve to IPv6 and make requests hang.

Run the test suite:

```bash
pytest tests/
```

---

## The five bugs (all fixed)

| # | What users saw | Where it lived | Fixed in |
|---|---|---|---|
| 1 | Listening streaks kept resetting | `streak_service.py` | Sunday was wrongly excluded from the "consecutive day" check |
| 2 | "Friends Listening Now" showed people from a day ago | `feed_service.py` | Recency window was 24 hours instead of a real "right now" window |
| 3 | The same song showed up twice in search results | `search_service.py` | An unnecessary join fanned out one row per tag |
| 4 | No notification when a friend rated your song (only when they added it to a playlist) | `notification_service.py` | The notify-the-sharer step was simply never written for ratings |
| 5 | The last song in a playlist never appeared | `playlist_service.py` | An off-by-one slice (`songs[:-1]`) silently dropped it |

Full root cause analysis — how each bug was reproduced, traced, fixed, and verified — is in [`submission.md`](submission.md).

---

## How a request flows through the app

Two examples, route → service → model:

- **Rating a song:** `POST /songs/<id>/rate` → `routes/songs.py` → `notification_service.rate_song()` → updates/creates a `Rating` row, then (after the fix) notifies the original sharer.
- **Viewing a playlist:** `GET /playlists/<id>/songs` → `routes/playlists.py` → `playlist_service.get_playlist_songs()` → queries songs ordered by their `position` in the playlist.

---

## Branch & commits

All fixes live on `bugfix/mixtape`, one commit per bug, using conventional commit messages (`fix: ...`), plus one `test:` commit for the added regression test.
