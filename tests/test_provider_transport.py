from types import SimpleNamespace
from cinekivu_source import api as cine


def test_provider_uses_current_browser_profile_when_transport_installed(monkeypatch):
    calls = []
    session = object()
    monkeypatch.setattr(cine, 'cr', SimpleNamespace(Session=lambda **kwargs: calls.append(kwargs) or session))
    engine = cine.MovieBoxEngine()
    assert engine.session is session
    assert calls == [{'impersonate': 'chrome'}]


def test_development_httpx_fallback_remains_available(monkeypatch):
    monkeypatch.setattr(cine, 'cr', None)
    engine = cine.MovieBoxEngine()
    assert isinstance(engine.session, cine.httpx.Client)
    engine.session.close()
