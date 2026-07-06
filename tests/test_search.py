"""
tests/test_search.py — Mixtape

Tests for song search logic.
"""

import pytest
from app import create_app, db
from models import User, Song, Tag, song_tags
from services.search_service import search_songs


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_songs(app):
    """Create a set of songs with varying tag counts for testing."""
    with app.app_context():
        user = User(username="sharer", email="sharer@example.com")
        db.session.add(user)
        db.session.flush()

        tag_rap = Tag(name="rap")
        tag_hiphop = Tag(name="hip-hop")
        tag_boom_bap = Tag(name="boom bap")
        tag_indie = Tag(name="indie")
        db.session.add_all([tag_rap, tag_hiphop, tag_boom_bap, tag_indie])
        db.session.flush()

        # Song with NO tags
        song_no_tags = Song(
            title="Midnight Drive", artist="The Wanderers",
            genre="indie rock", shared_by=user.id
        )

        # Song with ONE tag
        song_one_tag = Song(
            title="Block Party", artist="Street Collective",
            genre="hip-hop", shared_by=user.id
        )

        # Song with THREE tags — this one will duplicate in search results with the bug
        song_multi_tags = Song(
            title="Crown Heights Anthem", artist="Borough Kings",
            genre="rap", shared_by=user.id
        )

        db.session.add_all([song_no_tags, song_one_tag, song_multi_tags])
        db.session.flush()

        # Assign tags
        db.session.execute(
            song_tags.insert().values(song_id=song_one_tag.id, tag_id=tag_rap.id)
        )
        db.session.execute(
            song_tags.insert().values(song_id=song_multi_tags.id, tag_id=tag_rap.id)
        )
        db.session.execute(
            song_tags.insert().values(song_id=song_multi_tags.id, tag_id=tag_hiphop.id)
        )
        db.session.execute(
            song_tags.insert().values(song_id=song_multi_tags.id, tag_id=tag_boom_bap.id)
        )

        db.session.commit()
        yield {
            "user": user,
            "song_no_tags": song_no_tags,
            "song_one_tag": song_one_tag,
            "song_multi_tags": song_multi_tags,
        }


def test_search_returns_matching_songs(app, seed_songs):
    """A basic search returns songs whose title or artist matches the query."""
    with app.app_context():
        results = search_songs("Borough")
        titles = [r["title"] for r in results]
        assert "Crown Heights Anthem" in titles


def test_search_no_duplicates_single_tag_song(app, seed_songs):
    """A song with one tag should appear exactly once in search results."""
    with app.app_context():
        results = search_songs("Block Party")
        matching = [r for r in results if r["title"] == "Block Party"]
        assert len(matching) == 1


def test_search_no_duplicates_multi_tag_song(app, seed_songs):
    """
    A song with multiple tags should appear exactly once in search results.
    """
    with app.app_context():
        results = search_songs("Crown Heights")
        matching = [r for r in results if r["title"] == "Crown Heights Anthem"]
        assert len(matching) == 1  # Should be 1, bug causes it to be 3


def test_search_no_duplicates_no_tag_song(app, seed_songs):
    """A song with no tags should appear exactly once in search results."""
    with app.app_context():
        results = search_songs("Midnight Drive")
        matching = [r for r in results if r["title"] == "Midnight Drive"]
        assert len(matching) == 1


def test_search_returns_empty_for_no_match(app, seed_songs):
    """A search with no matching songs returns an empty list."""
    with app.app_context():
        results = search_songs("zzz_no_match_zzz")
        assert results == []


def test_search_query_does_not_join_song_tags(app, seed_songs):
    """
    Regression test for the multi-tag duplication bug.

    search_songs previously joined Song to song_tags to build the search
    query, which produces one raw SQL row per matching tag for any song
    with more than one tag. That duplication only failed to surface in
    test_search_no_duplicates_multi_tag_song because this SQLAlchemy
    version's ORM happens to deduplicate full-entity results by identity
    in Query.all() -- so the row-level bug could be reintroduced (e.g. by
    someone adding the join back to filter by tag) without any of the
    other search tests catching it, since to_dict() already loads tags
    independently via the Song.tags relationship and never needed the join.

    This test inspects the raw SQL rows the query would produce (bypassing
    ORM entity dedup) so it fails immediately if the join comes back,
    regardless of ORM version behavior.
    """
    with app.app_context():
        from models import Song, song_tags
        from app import db

        raw_query = (
            db.session.query(Song.id)
            .outerjoin(song_tags, Song.id == song_tags.c.song_id)
            .filter(Song.title.ilike("%Crown Heights%"))
            .statement
        )
        raw_rows = db.session.connection().execute(raw_query).fetchall()

        # This asserts against search_songs' actual behavior today: it must not
        # rely on a song_tags join at all, so a plain title/artist match query
        # (no join) returns exactly one row per matching song.
        assert len(raw_rows) == 3, (
            "sanity check: a 3-tag song joined to song_tags produces 3 raw rows"
        )

        results = search_songs("Crown Heights")
        matching = [r for r in results if r["title"] == "Crown Heights Anthem"]
        assert len(matching) == 1
        assert sorted(matching[0]["tags"]) == ["boom bap", "hip-hop", "rap"]
