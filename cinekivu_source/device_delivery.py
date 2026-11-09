"""Opt-in native MP4 candidates. Only the device can mark these playable."""
import math
import re
from urllib.parse import urlsplit

from fastapi import HTTPException

from .delivery import is_notice_video
from .transport_probe import USER_AGENT


def allowed_device_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment
                and host.endswith('.hakunaymatata.com')
                and parsed.path.lower().endswith('.mp4') and not is_notice_video(url)
                and '\\' not in url)
    except ValueError:
        return False


def candidates(streams, subject_id, season, episode, detail_path=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', subject_id):
        return []
    durations = []
    for stream in streams:
        try:
            value = float(stream.get('duration_sec') or 0)
            if math.isfinite(value):
                durations.append(value)
        except (ValueError, TypeError):
            pass
    expected = max(durations, default=0)
    result, seen = [], set()
    for stream in streams:
        url = stream.get('url')
        if (str(stream.get('format', '')).lower() != 'mp4' or not allowed_device_url(url)
                or stream.get('cookie') or any(k.lower() in ('cookie', 'authorization')
                    for k in (stream.get('headers') or {}))):
            continue
        try:
            quality = int(stream.get('quality_value') or 0)
            duration = float(stream.get('duration_sec') or 0)
        except (ValueError, TypeError):
            continue
        if quality not in (240, 360, 480, 720, 1080, 1440, 2160) or quality in seen:
            continue
        if not math.isfinite(duration) or (expected >= 180 and 0 < duration * 3 < expected):
            continue
        seen.add(quality)
        media_id = f'md-{subject_id}-s{season}e{episode}-q{quality}'
        download_url = f'/api/download/device/{media_id}'
        if isinstance(detail_path, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,319}', detail_path):
            download_url += '?detail_path=' + detail_path
        result.append({'id': media_id, 'subject_id': subject_id, 'season': season, 'episode': episode,
                       'url': url, 'format': 'MP4', 'quality': f'{quality}p', 'quality_value': quality,
                       'codec': stream.get('codec'), 'duration': duration,
                       'validation': 'required_on_device',
                       'headers': {'User-Agent': USER_AGENT, 'Referer': 'https://moviebox.ph/',
                                   'Origin': 'https://moviebox.ph', 'Accept': '*/*', 'Accept-Encoding': 'identity'},
                       'download': {'id': media_id, 'url': download_url, 'resume_url': download_url}})
    return sorted(result, key=lambda item: item['quality_value'])[:7]


def resolve_download(media_id, engine, detail_path=None):
    match = re.fullmatch(r'md-([A-Za-z0-9_-]{1,40})-s([0-9]{1,3})e([0-9]{1,4})-q(240|360|480|720|1080|1440|2160)', media_id)
    if not match or len(media_id) > 80:
        raise HTTPException(422, 'Identifiant de téléchargement invalide')
    subject_id, season, episode = match[1], int(match[2]), int(match[3])
    options = {'detail_path': detail_path} if detail_path else {}
    streams = engine.get_streams(subject_id, season=season, episode=episode, device_delivery=True, **options)
    selected = next((item for item in candidates(streams, subject_id, season, episode)
                     if item['id'] == media_id), None)
    if selected is None:
        raise HTTPException(409, 'Cette qualité n’est plus disponible. Rouvrez la fiche pour réessayer.')
    return {'code': 0, 'data': selected}
