"""A working web MP4 must not wait for optional mobile mirror resolution."""
from types import SimpleNamespace
from cinekivu_source import api as cine


def engine_with_web(monkeypatch):
    monkeypatch.setattr(cine, 'WEB_DOMAINS', ['https://web.example.test'])
    engine = object.__new__(cine.MovieBoxEngine)
    engine.bootstrap_token = lambda: 'test'
    engine.session = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {'data': {'streams': [{
            'url': 'https://cdn.example.test/film.mp4', 'format': 'MP4',
            'resolutions': '480', 'duration': 3600}], 'dash': []}}))
    engine._resource_streams = lambda *a: ([], None)
    return engine


def test_verified_web_source_skips_mobile_fallback(monkeypatch):
    engine = engine_with_web(monkeypatch)
    monkeypatch.setattr(cine, '_probe_mp4_playback', lambda source, duration: None)
    def unexpected(*args):
        raise AssertionError('Verified MP4 must be returned before mobile resolution')
    engine.native_play_info = unexpected
    streams = engine.get_streams('123', detail_path='film', prefer_web_mp4=True)
    assert len(streams) == 1 and streams[0]['quality'] == '480p'


def test_unreadable_web_source_keeps_mobile_fallback(monkeypatch):
    engine = engine_with_web(monkeypatch)
    calls = []
    monkeypatch.setattr(cine, '_probe_mp4_playback', lambda *a: 'CDN refused')
    def native(subject, season, episode):
        calls.append((subject, season, episode))
        return {'streams': [{'url': 'https://cdn.example.test/episode.mp4',
                             'format': 'MP4', 'resolutions': '720', 'duration': 3600}]}
    engine.native_play_info = native
    streams = engine.get_streams('123', season=2, episode=3, detail_path='series', prefer_web_mp4=True)
    assert calls == [('123', 2, 3)]
    assert any(s['source'] == 'Mobile Native CDN' for s in streams)
