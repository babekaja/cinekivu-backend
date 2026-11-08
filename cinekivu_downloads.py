"""SDK-inspired movie/episode downloads using CineKivu's verified relay.

This adapts workflows from moviebox-api 0.2.4 (Unlicense); it does not install
its conflicting moviebox_api namespace or send provider credentials to clients.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import cinekivu_bridge as bridge
from cinekivu_source import api as cine, delivery
from moviebox_api.edition_search import base_title
from cinekivu_recommendations import normalized

QUALITIES = ('BEST', 'WORST', '360p', '480p', '720p', '1080p', '1440p', '2160p')
SUBJECT = r'^[A-Za-z0-9_-]{1,40}$'
Episode = Annotated[int, Field(ge=1, le=9999, strict=True)]
_assets: OrderedDict[str, dict] = OrderedDict()
_asset_lock = threading.RLock()
ASSET_TTL = 1800
MAX_ASSETS = 512
LANGUAGES = {'french': 'fr', 'francais': 'fr', 'english': 'en', 'anglais': 'en',
             'swahili': 'sw', 'kiswahili': 'sw', 'spanish': 'es', 'espagnol': 'es',
             'portuguese': 'pt', 'arabic': 'ar', 'japanese': 'ja', 'german': 'de',
             'hindi': 'hi', 'italian': 'it', 'korean': 'ko', 'chinese': 'zh'}


def language_code(value: str) -> str:
    value = normalized(value.strip()).replace('_', '-')
    value = LANGUAGES.get(value, value)
    if not re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})?', value):
        raise ValueError('Code de langue invalide')
    return value


class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quality: str = 'BEST'
    caption_languages: list[str] = Field(default_factory=list, max_length=5)
    caption_format: Literal['srt', 'vtt'] = 'vtt'
    captions_only: bool = False

    @field_validator('quality')
    @classmethod
    def valid_quality(cls, value):
        value = value.upper() if value.upper() in ('BEST', 'WORST') else value.lower()
        if value not in QUALITIES:
            raise ValueError('Qualité inconnue')
        return value

    @field_validator('caption_languages')
    @classmethod
    def valid_languages(cls, values):
        return list(dict.fromkeys(language_code(v) for v in values))

    @model_validator(mode='after')
    def caption_requirements(self):
        if self.captions_only and not self.caption_languages:
            raise ValueError('captions_only exige au moins une langue')
        return self


class ResolveRequest(Options):
    subject_id: str | None = Field(None, pattern=SUBJECT)
    query: str | None = Field(None, min_length=2, max_length=120)
    subject_type: Literal[1, 2] = 1
    year: int | None = Field(None, ge=1878, le=2200, strict=True)
    season: int = Field(1, ge=1, le=999, strict=True)
    episode: Episode = 1

    @field_validator('query')
    @classmethod
    def clean_query(cls, value):
        if value is not None:
            value = ' '.join(value.split())
            if len(value) < 2:
                raise ValueError('Titre trop court')
        return value

    @model_validator(mode='after')
    def one_identifier(self):
        if (self.subject_id is None) == (self.query is None):
            raise ValueError('Fournir subject_id ou query, exclusivement')
        return self


class BatchRequest(Options):
    subject_id: str = Field(pattern=SUBJECT)
    season: int = Field(1, ge=1, le=999, strict=True)
    episodes: list[Episode] | None = Field(None, min_length=1, max_length=20)
    start_episode: Episode = 1
    limit: int = Field(1, ge=1, le=20, strict=True)

    @model_validator(mode='after')
    def valid_episodes(self):
        if self.episodes is not None:
            if len(set(self.episodes)) != len(self.episodes):
                raise ValueError('Les épisodes doivent être distincts')
            if 'start_episode' in self.model_fields_set or 'limit' in self.model_fields_set:
                raise ValueError('Utiliser episodes ou start_episode/limit, exclusivement')
        elif self.start_episode + self.limit - 1 > 9999:
            raise ValueError('Fin du lot hors limites')
        return self


def filename(title: str, year: str, season: int, episode: int, suffix: str, ext: str) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', title).strip(' .')[:110] or 'CineKivu'
    label = f' S{season:02}E{episode:02}' if season else (f' ({year})' if year else '')
    return re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', f'{title}{label} - {suffix}.{ext}')


def register_asset(**value) -> str:
    token = secrets.token_urlsafe(24)
    with _asset_lock:
        now = time.monotonic()
        for key in list(_assets):
            if _assets[key]['expires'] <= now:
                del _assets[key]
        while len(_assets) >= MAX_ASSETS:
            _assets.popitem(last=False)
        _assets[token] = {**value, 'expires': now + ASSET_TTL}
    return f'/api/v1/downloads/assets/{token}'


def get_asset(token: str) -> dict:
    with _asset_lock:
        item = _assets.get(token)
        if item is None or item['expires'] <= time.monotonic():
            _assets.pop(token, None)
            raise HTTPException(404, 'Lien de téléchargement expiré ou inconnu')
        return item.copy()


def select_movie(body: ResolveRequest) -> dict:
    if body.subject_id:
        raw = cine.engine.get_details(body.subject_id)
        selected = bridge._subject(raw)
        if selected is None:
            raise HTTPException(404, 'Titre introuvable')
        if selected['subjectId'] != body.subject_id:
            raise HTTPException(502, 'Identifiant fournisseur incohérent')
        candidates = [selected]
    else:
        # The current native provider accepts 20; the archived MovieAuto's 30
        # is rejected with HTTP 400 (verified against the live provider).
        candidates = [item for raw in cine.engine.search(body.query, page=1, per_page=20)
                      if (item := bridge._subject(raw)) is not None]
    candidates = [item for item in candidates if item['subjectType'] == body.subject_type
                  and (body.year is None or item['releaseTime'] == str(body.year))]
    if not candidates:
        raise HTTPException(404, 'Aucun titre ne correspond au type et à l’année demandés')
    term = normalized(base_title(body.query or ''))
    candidates.sort(key=lambda item: (
        bool(term) and normalized(base_title(item['title'])) != term,
        bridge.language_rank(item)))
    selected = candidates[0]
    bridge.valid_id(selected['subjectId'])
    if len(selected['subjectId']) > 40:
        raise HTTPException(502, 'Identifiant fournisseur non pris en charge')
    return selected


def choose_quality(entries: list[dict], quality: str) -> tuple[dict, list[int]]:
    entries = sorted(entries, key=lambda e: int(e.get('quality_value') or 0), reverse=True)
    available = sorted({int(e.get('quality_value') or 0) for e in entries if int(e.get('quality_value') or 0) > 0})
    if not entries:
        raise HTTPException(409, {'code': 'no_downloadable_mp4', 'availableQualities': []})
    if quality == 'BEST':
        return entries[0], available
    if quality == 'WORST':
        return entries[-1], available
    selected = next((e for e in entries if int(e.get('quality_value') or 0) == int(quality[:-1])), None)
    if selected is None:
        raise HTTPException(409, {'code': 'quality_unavailable', 'availableQualities': available})
    return selected, available


def prepare(selected: dict, options: Options, season: int, episode: int) -> dict:
    sid = selected['subjectId']
    season, episode = (0, 0) if selected['subjectType'] == 1 else (season, episode)
    file = None
    qualities = []
    if not options.captions_only:
        upstream = cine.engine.get_streams(sid, season=season, episode=episode,
                                           detail_path=selected.get('detail_path'), prefer_web_mp4=True)
        downloadable = []
        for entry in delivery.prepare_streams(upstream):
            download = delivery.register_download(entry, sid, season, episode)
            if download:
                downloadable.append({**entry, '_download': download})
        entry, qualities = choose_quality(downloadable, options.quality)
        download = entry['_download']
        name = filename(selected['title'], selected['releaseTime'], season, episode,
                        str(entry.get('quality') or 'video'), 'mp4')
        file = {'mediaId': download['id'], 'filename': name, 'mimeType': 'video/mp4',
                'quality': entry.get('quality'), 'qualityValue': int(entry.get('quality_value') or 0),
                'url': register_asset(kind='video', media_id=download['id'], filename=name),
                'resumeUrl': download['resume_url'], 'rangeProbeVerified': True}
    captions, missing = [], []
    if options.caption_languages:
        raw = cine.engine.get_subtitles(sid, season=season, episode=episode)
        registered = bridge._register_captions(raw)
        for requested in options.caption_languages:
            def match_code(item):
                try:
                    return language_code(item.get('lang') or item.get('language') or '')
                except ValueError:
                    return ''
            caption = next((c for c in registered if match_code(c) == requested), None)
            if caption is None:
                caption = next((c for c in registered if match_code(c).split('-')[0] == requested.split('-')[0]), None)
            if caption is None:
                missing.append(requested)
                continue
            token = caption['url'].rsplit('/', 1)[-1].removesuffix('.vtt')
            name = filename(selected['title'], selected['releaseTime'], season, episode,
                            requested, options.caption_format)
            captions.append({'language': match_code(caption), 'requestedLanguage': requested,
                             'filename': name, 'format': options.caption_format,
                             'url': register_asset(kind='caption', caption_token=token,
                                                   format=options.caption_format, filename=name)})
        if options.captions_only and not captions:
            raise HTTPException(409, {'code': 'captions_unavailable', 'missingCaptionLanguages': missing})
    return {'subjectId': sid, 'title': selected['title'], 'year': selected['releaseTime'] or None,
            'subjectType': selected['subjectType'], 'season': season, 'episode': episode,
            'file': file, 'captions': captions, 'availableQualities': qualities,
            'missingCaptionLanguages': missing, 'partial': bool(missing), 'audioVerified': False}


def convert_to_srt(raw: bytes) -> bytes:
    vtt = bridge.convert_subtitle(raw).decode('utf-8')
    cues = []
    for block in re.split(r'\n\s*\n', vtt):
        lines = block.splitlines()
        if not lines or lines[0].startswith(('NOTE', 'STYLE', 'REGION')):
            continue
        for index, line in enumerate(lines):
            if '-->' not in line:
                continue
            start, end = (bridge._vtt_timestamp(t) for t in line.split('-->', 1))
            if start and end and lines[index+1:]:
                cues.append(f'{len(cues)+1}\n{start.replace(".", ",")} --> {end.replace(".", ",")}\n'
                            + '\n'.join(lines[index+1:]))
            break
    if not cues:
        raise HTTPException(502, 'Aucune légende convertible en SRT')
    return ('\n\n'.join(cues) + '\n').encode('utf-8')


def install_download_routes(app: FastAPI):
    router = APIRouter(prefix='/api/v1/downloads', tags=['CineKivu / téléchargements SDK'])

    @router.get('/capabilities')
    def capabilities():
        return {'code': 0, 'data': {'qualities': QUALITIES, 'subtitleFormats': ['vtt', 'srt'],
                'maxBatchEpisodes': 20, 'httpModes': ['start', 'resume', 'auto'],
                'storage': 'client', 'mirrors': list(cine.WEB_DOMAINS), 'mirrorsProbed': False}}

    @router.post('/resolve')
    def resolve(body: ResolveRequest):
        try:
            return {'code': 0, 'data': prepare(select_movie(body), body, body.season, body.episode)}
        except Exception as exc:
            raise bridge._remote_error(exc) from exc

    @router.post('/batch')
    def batch(body: BatchRequest):
        try:
            selected = select_movie(ResolveRequest(subject_id=body.subject_id, subject_type=2))
            seasons = cine.engine.get_episodes(body.subject_id)
            current = next((s for s in seasons if int(s.get('season') or 0) == body.season), None)
            if current is None:
                raise HTTPException(404, 'Saison inconnue')
            episodes = body.episodes if body.episodes is not None else list(range(body.start_episode, body.start_episode + body.limit))
            if any(ep > int(current.get('episode_count') or 0) for ep in episodes):
                raise HTTPException(422, 'Un épisode demandé dépasse la saison disponible')
            def one(ep):
                try:
                    return {'episode': ep, 'status': 'ready', 'data': prepare(selected, body, body.season, ep)}
                except Exception as exc:
                    error = bridge._remote_error(exc)
                    return {'episode': ep, 'status': 'unavailable', 'error': {
                        'status': error.status_code, 'detail': error.detail}}
            with ThreadPoolExecutor(max_workers=3) as pool:
                items = list(pool.map(one, episodes))
            ready = sum(i['status'] == 'ready' for i in items)
            return {'code': 0, 'data': {'subjectId': body.subject_id, 'season': body.season,
                    'items': items, 'readyCount': ready, 'total': len(items),
                    'partial': ready != len(items) or any(i.get('data', {}).get('partial') for i in items)}}
        except Exception as exc:
            raise bridge._remote_error(exc) from exc

    @router.get('/assets/{token}')
    async def asset(token: str, request: Request):
        item = get_asset(token)
        if item['kind'] == 'video':
            result = await delivery.stable_download_stream(item['media_id'], request, cine.engine)
        else:
            raw = await bridge.read_caption(item['caption_token'])
            try:
                raw = convert_to_srt(raw) if item['format'] == 'srt' else bridge.convert_subtitle(raw)
            except ValueError as exc:
                raise HTTPException(502, 'Format de sous-titres non compatible') from exc
            mime = 'application/x-subrip' if item['format'] == 'srt' else 'text/vtt'
            result = Response(raw if request.method != 'HEAD' else b'', media_type=mime,
                              headers={'content-length': str(len(raw)), 'cache-control': 'private, no-store'})
        result.headers['content-disposition'] = "attachment; filename=\"cinekivu-download\"; filename*=UTF-8''" + quote(item['filename'], safe='')
        return result

    router.add_api_route('/assets/{token}', asset, methods=['HEAD'], include_in_schema=False)
    app.include_router(router)
