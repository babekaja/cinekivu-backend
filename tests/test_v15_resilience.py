"""Network-free regression tests for malformed payloads, outages and caching policy."""
import time

import pytest
from fastapi import HTTPException

import cinekivu_web as web


class Reply:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self.payload = payload if payload is not None else {"code": 0, "data": {"items": []}}
    def json(self):
        return self.payload


def test_home_tolerates_wrong_list_shapes():
    result = web.normalize_home({"data": {"operatingList": "bad", "platformList": False}})
    assert result["sections"] == [] and result["banners"] == []


def test_home_tolerates_invalid_banner_and_subjects():
    payload = {"data": {"operatingList": [
        {"type": "BANNER", "banner": "not-object"},
        {"type": "SUBJECTS_MOVIE", "subjects": 50},
        {"type": "SUBJECTS_MOVIE", "subjects": [None, 5, {"title": "No identifier"}]},
    ]}}
    result = web.normalize_home(payload)
    assert result["sectionCount"] == 0


def test_home_propagates_stale_metadata_marker():
    result = web.normalize_home({"_fallback_stale": True, "data": {"operatingList": []}})
    assert result["stale"] is True


def test_outage_never_replays_a_previous_success(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return Reply({'code': 0, 'data': {'operatingList': []}})
        raise OSError('network disconnected')
    monkeypatch.setattr(web.cine.engine.session, 'request', request)
    assert web.upstream('GET', '/home')['code'] == 0
    with pytest.raises(HTTPException) as caught:
        web.upstream('GET', '/home')
    assert caught.value.status_code == 502
    assert len(calls) == 2


def test_rejected_requests_do_not_use_stale_cache(monkeypatch):
    monkeypatch.setattr(web.cine.engine.session, "request", lambda *a, **k: Reply(status=403))
    with pytest.raises(HTTPException) as caught:
        web.upstream("GET", "/home", params={"host": "movieboxhd.net"})
    assert caught.value.status_code == 403


def test_play_list_metadata_not_cached(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        return Reply({"code": 0, "data": {"url": f"ephemeral-{len(calls)}"}})
    monkeypatch.setattr(web.cine.engine.session, "request", request)
    a = web.upstream("GET", "/platform/play-list")
    b = web.upstream("GET", "/platform/play-list")
    assert a["data"]["url"] != b["data"]["url"]
    assert len(calls) == 2


def test_invalid_upstream_envelope_not_cached(monkeypatch):
    monkeypatch.setattr(web.cine.engine.session, "request", lambda *a, **k:
                        Reply({"code": 503, "data": {}}))
    with pytest.raises(HTTPException) as caught:
        web.upstream("GET", "/home")
    assert caught.value.status_code == 502


def test_invalid_shape_in_provider_not_cached(monkeypatch):
    monkeypatch.setattr(web.cine.engine.session, "request", lambda *a, **k:
                        Reply({"code": 0, "data": {"items": []}, "unexpected": []}))
    assert web.upstream("GET", "/home")["code"] == 0
