"""The Compose media routes use subject IDs, without requiring a MySQL work ID."""
import pytest
from fastapi.testclient import TestClient

import cinekivu_media
from cinekivu_media_source import normalize_comments
from moviebox_api_server import app


@pytest.fixture(autouse=True)
def live_mobile_details(monkeypatch):
    def details(subject_id):
        return {"subject_id": subject_id, "title": "Titre actuel",
                "imdb_rating": "6.0" if subject_id == "5580730815148868536" else "8.0",
                "detail_path": "", "staffList": []}
    monkeypatch.setattr(cinekivu_media.cine.engine, "get_details", details)


def test_eleven_title_rating_and_empty_local_reviews_are_distinct():
    client = TestClient(app)
    rating = client.get("/api/v1/media/3620534210064175096/rating")
    assert rating.status_code == 200
    assert rating.json()["external"]["value"] == 8.0
    assert rating.json()["external"]["source"] == "cinekivu-live"
    assert rating.json()["external"]["count"] is None
    assert rating.json()["cinekivu"]["value"] is None
    local = client.get("/api/v1/media/3620534210064175096/reviews?source=cinekivu")
    assert local.status_code == 200
    assert local.json()["items"] == []
    assert "Aucun compte" in local.json()["note"]


def test_live_overview_cast_and_trailer_match_the_requested_edition(monkeypatch):
    async def detail(path):
        assert path == "reacher-SRJiPjDHQ06"
        return {"subjectId": "5047303543502293824", "title": "Reacher VF",
                "rating": {"value": 8.1, "count": 27},
                "trailer": {"url": "https://example.org/trailer.mp4", "posterUrl": None},
                "cast": [{"externalId": "345", "name": "Actrice", "character": "Rôle"}]}
    monkeypatch.setattr(cinekivu_media.source_client, "detail", detail)
    client = TestClient(app)
    overview = client.get("/api/v1/media/5047303543502293824/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["subjectId"] == "5047303543502293824"
    assert body["rating"]["value"] == 8.1
    assert body["cast"][0]["name"] == "Actrice"
    assert body["trailer"]["playbackAllowed"] is False
    assert body["stale"] is False
    cast = client.get("/api/v1/media/5047303543502293824/cast?external=true")
    assert cast.status_code == 200
    assert cast.json()["items"][0]["id"] == "345"


def test_public_trailer_mp4_is_playable_without_enabling_other_hosts(monkeypatch):
    async def detail(path):
        assert path == "reacher-SRJiPjDHQ06"
        return {"subjectId": "5047303543502293824", "title": "Reacher VF",
                "rating": {}, "cast": [], "trailer": {
                    "url": "https://macdn.aoneroom.com/media/vone/2024/03/19/trailer-sd.mp4",
                    "posterUrl": "https://pbcdnw.aoneroom.com/poster.jpg"}}
    monkeypatch.setattr(cinekivu_media.source_client, "detail", detail)
    body = TestClient(app).get("/api/v1/media/5047303543502293824/overview").json()
    assert body["trailer"]["playbackAllowed"] is True


def test_public_reviews_do_not_require_a_full_subject_detail(monkeypatch):
    async def unavailable(subject_id):
        raise AssertionError("Review lookup must not load the film detail")
    async def reviews(subject_id, limit, cursor):
        return {"source": "moviebox", "items": [{"id": "1", "text": "Avis actuel"}],
                "hasMore": False, "nextCursor": None, "filteredOut": 0}
    monkeypatch.setattr(cinekivu_media, "_base", unavailable)
    monkeypatch.setattr(cinekivu_media.source_client, "reviews", reviews)
    body = TestClient(app).get("/api/v1/media/5047303543502293824/reviews")
    assert body.status_code == 200
    assert body.json()["items"][0]["text"] == "Avis actuel"


def test_missing_trailer_remains_absent_when_public_detail_fails(monkeypatch):
    async def unavailable(path):
        raise ValueError("SSR unavailable")
    monkeypatch.setattr(cinekivu_media.source_client, "detail", unavailable)
    result = TestClient(app).get("/api/v1/media/5580730815148868536/overview")
    assert result.status_code == 200
    body = result.json()
    assert body["stale"] is False
    assert body["trailer"]["url"] is None
    assert body["rating"]["value"] == 6.0


def test_missing_ssl_certificates_do_not_turn_optional_metadata_into_500(monkeypatch):
    async def missing_certificate(path):
        raise FileNotFoundError("CA bundle missing")
    monkeypatch.setattr(cinekivu_media.source_client, "detail", missing_certificate)
    result = TestClient(app).get("/api/v1/media/5047303543502293824/overview")
    assert result.status_code == 200
    assert result.json()["source"] == "cinekivu-live"


def test_missing_ssl_certificates_make_reviews_explicitly_unavailable(monkeypatch):
    async def missing_certificate(subject_id, limit, cursor):
        raise FileNotFoundError("CA bundle missing")
    monkeypatch.setattr(cinekivu_media.source_client, "reviews", missing_certificate)
    result = TestClient(app).get("/api/v1/media/5047303543502293824/reviews")
    assert result.status_code == 502


def test_image_only_posts_do_not_become_comments_and_cursor_is_preserved(monkeypatch):
    upstream = {"code": 0, "data": {"items": [
        {"postId": "1", "status": 0, "mediaType": "IMAGE", "content": ""},
        {"postId": "2", "status": 1, "content": "  Bon film  ",
         "user": {"nickname": "Lectrice"}, "stat": {"likeCount": 3}},
    ], "pager": {"hasMore": True, "nextPage": "1792495523489"}}}
    async def reviews(subject_id, limit, cursor):
        assert subject_id == "1392037807669085720"
        assert cursor is None
        return normalize_comments(upstream, limit)
    monkeypatch.setattr(cinekivu_media.source_client, "reviews", reviews)
    result = TestClient(app).get("/api/v1/media/1392037807669085720/reviews?source=moviebox")
    assert result.status_code == 200
    body = result.json()
    assert [row["text"] for row in body["items"]] == ["Bon film"]
    assert body["filteredOut"] == 1
    assert body["nextCursor"] == "1792495523489"
    upstream["data"]["items"] = upstream["data"]["items"][:1]
    empty = TestClient(app).get("/api/v1/media/1392037807669085720/reviews?source=moviebox")
    assert empty.json()["items"] == []
    assert empty.json()["hasMore"] is True
    assert empty.json()["nextCursor"] == "1792495523489"


def test_review_cursor_is_validated():
    response = TestClient(app).get("/api/v1/media/3620534210064175096/reviews?cursor=abc")
    assert response.status_code == 422


def test_textless_rated_review_is_kept_when_rating_is_numeric():
    payload = {"code": 0, "data": {"items": [
        {"postId": "91", "status": 1, "content": "", "isSubjectRate": True,
         "subjectRate": "8.5"},
        {"postId": "92", "status": 1, "content": "", "isSubjectRate": True,
         "subjectRate": "invalid"},
    ], "pager": {"hasMore": False}}}
    page = normalize_comments(payload)
    assert len(page["items"]) == 1
    assert page["items"][0]["rating"] == 8.5
    assert page["filteredOut"] == 1
