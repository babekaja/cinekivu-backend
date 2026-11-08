"""Local-only HTTP fixture for the SDK Postman folder.

The real CineKivu app, download service, Range probe and relay run unchanged.
Only provider metadata and its CDN origin are replaced in this test process.
No production switch enables this fixture.
"""
import os
import re
import tempfile
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

_data = tempfile.TemporaryDirectory(prefix='cinekivu-sdk-http-')
os.environ['MOVIEBOX_DB_PATH'] = os.path.join(_data.name, 'library.sqlite3')
os.environ['CINEKIVU_FEEDBACK_DB'] = os.path.join(_data.name, 'feedback.sqlite3')

from moviebox_api_server import app as backend_app
import moviebox_api_server as backend
from cinekivu_source import api as cine, delivery

BASE = 'http://127.0.0.1:8001'
# Binary transport fixture, not a playable movie or an assertion of AVPlayer support.
MEDIA = b'\x00\x00\x00\x18ftypisom' + b'CineKivu-HTTP-Range-fixture-' * 64
SRT = '1\n00:00:01,000 --> 00:00:03,000\nBonjour CineKivu\n'.encode()
ETAG = '"cinekivu-sdk-fixture-v1"'
app = FastAPI(title='CineKivu SDK HTTP test environment')
_counts = {'streamCalls': 0, 'cdnCalls': 0}
_lock = threading.Lock()
_session_id = '00000000-0000-4000-8000-000000000801'
backend.sessions[_session_id] = {'id': _session_id}
backend.get_session = lambda sid=None: backend.sessions[_session_id]


def details(sid):
    if sid not in ('fixture-old', 'fixture-new', 'fixture-series', 'fixture-caption-missing'):
        raise HTTPException(404, 'Titre de test inconnu')
    return {'subject_id': sid, 'title': 'CineKivu Démo' if sid != 'fixture-series' else 'Série Démo',
            'release_date': '2009-12-10' if sid == 'fixture-old' else '2022-12-10',
            'subject_type': 2 if sid == 'fixture-series' else 1,
            'detail_path': sid}


def search(term, page=1, per_page=30):
    if per_page > 20:
        raise HTTPException(502, 'API returned HTTP 400')
    return [details('fixture-new'), details('fixture-old')]


def streams(sid, season=0, episode=0, **kwargs):
    with _lock:
        _counts['streamCalls'] += 1
    if episode == 3:
        return []
    return [{'format': 'MP4', 'quality': f'{q}p', 'quality_value': q, 'codec': 'h264',
             'source': 'synthetic-http-fixture', 'duration_sec': 9,
             'url': f'{BASE}/__sdk_fixture/video/{q}.mp4'} for q in (360, 720, 1080)]


def subtitles(sid, **kwargs):
    fr = 'missing' if sid == 'fixture-caption-missing' else 'fr'
    return [{'id': 'fr', 'code': 'fr', 'language': 'Français', 'url': f'{BASE}/__sdk_fixture/{fr}.srt'},
            {'id': 'en', 'code': 'en', 'language': 'English', 'url': f'{BASE}/__sdk_fixture/en.srt'}]


cine.engine.get_details = details
cine.engine.search = search
cine.engine.get_streams = streams
cine.engine.get_subtitles = subtitles
cine.engine.get_episodes = lambda sid: [{'season': 1, 'episode_count': 4}]
delivery._public_https_media_url = lambda url: isinstance(url, str) and url.startswith(BASE + '/__sdk_fixture/')


@app.get('/__sdk_fixture/status')
def status():
    return {'fixture': 'cinekivu-sdk-http-v1', 'mediaBytes': len(MEDIA), **_counts}


@app.post('/__sdk_fixture/reset-sources')
def reset_sources():
    with delivery._playback_lock:
        delivery._playback_sources.clear()
        delivery._download_sources.clear()
    with _lock:
        _counts.update(streamCalls=0, cdnCalls=0)
    return {'reset': True}


@app.api_route('/__sdk_fixture/video/{quality}.mp4', methods=['GET', 'HEAD'])
def video(quality: int, request: Request):
    with _lock:
        _counts['cdnCalls'] += 1
    if quality not in (360, 720, 1080):
        raise HTTPException(404)
    start, end, status = 0, len(MEDIA)-1, 200
    requested = request.headers.get('range')
    if requested and request.headers.get('if-range', ETAG) == ETAG:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
        if not match or not any(match.groups()):
            return Response(status_code=416, headers={'content-range': f'bytes */{len(MEDIA)}'})
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right), end) if right else end
        else:
            start = max(0, len(MEDIA)-int(right))
        if start > end or start >= len(MEDIA):
            return Response(status_code=416, headers={'content-range': f'bytes */{len(MEDIA)}'})
        status = 206
    headers = {'accept-ranges': 'bytes', 'etag': ETAG,
               'last-modified': 'Sun, 08 Nov 2026 00:00:00 GMT',
               'content-length': str(end-start+1)}
    if status == 206:
        headers['content-range'] = f'bytes {start}-{end}/{len(MEDIA)}'
    return Response(MEDIA[start:end+1] if request.method != 'HEAD' else b'',
                    status_code=status, media_type='video/mp4', headers=headers)


@app.get('/__sdk_fixture/{lang}.srt')
def caption(lang: str):
    if lang not in ('fr', 'en'):
        raise HTTPException(404)
    return Response(SRT, media_type='application/x-subrip')


app.mount('/', backend_app)
