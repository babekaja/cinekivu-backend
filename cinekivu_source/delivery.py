"""Verified MP4 and DASH/HLS delivery for the local CineKivu API."""
import asyncio
import hashlib
import ipaddress
import re
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

try:
    from .adaptive import build_presentation, ManifestError, MAX_MANIFEST_BYTES
except ImportError:
    from adaptive import build_presentation, ManifestError, MAX_MANIFEST_BYTES

API_REVISION = "cinekivu-tui-adaptive-20261026-r2"
_playback_lock = threading.Lock()
_playback_sources = {}
_adaptive_sources = {}
_download_sources = {}
_download_resolution_lock = threading.Lock()


def is_notice_video(url):
    parsed = urlsplit(url)
    path = parsed.path.lower()
    return (any(marker in path for marker in (
        "1c7de0bd3393702d9191801f15f88f8d", "9a0461bc39da389663bf3dbb17091d3f",
        "b164fbfb4347792950bdfbfb563d39d9", "80da52722ee7de7a4303290af6783424",
    )) or path.endswith("/notice.mp4")
        or ((parsed.hostname or "").lower() == "macdn.aoneroom.com" and path.startswith("/other/")))


def _public_https_media_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (ValueError, OSError):
        return False


def _media_headers(stream):
    headers = {k: v for k, v in (stream.get("headers") or {}).items()
               if k in ("User-Agent", "Referer", "Origin") and isinstance(v, str)
               and "\r" not in v and "\n" not in v}
    # These values originate in the validated request context; revalidate at
    # the delivery boundary before forwarding to the media origin.
    for name in ("X-Forwarded-For", "X-Real-IP"):
        value = (stream.get("headers") or {}).get(name)
        try:
            address = ipaddress.ip_address(value or "")
            if address.is_global and not address.is_multicast:
                headers[name] = str(address)
        except ValueError:
            pass
    cookie = stream.get("cookie")
    if isinstance(cookie, str) and cookie and len(cookie) <= 16384 and "\r" not in cookie and "\n" not in cookie:
        headers["Cookie"] = "; ".join(part.strip() for part in cookie.split(";") if part.strip())
    headers["Accept-Encoding"] = "identity"
    return headers


def _sample(response):
    result = b""
    for chunk in response.iter_raw(32):
        result += chunk
        if len(result) >= 32:
            break
    return result


def _cdn_refusal(response, resource):
    details = f"HTTP {response.status_code}"
    if response.status_code == 426:
        version = str(response.http_version)
        details += f" ; transport {version}"
        required = response.headers.get("upgrade", "")
        if re.fullmatch(r"[A-Za-z0-9./+_, -]{1,80}", required):
            details += f" ; protocole requis {required}"
    return f"Le CDN refuse {resource} ({details})."


def _probe_mp4_playback(stream, expected_duration):
    if not _public_https_media_url(stream.get("url")):
        return "Adresse du CDN non autorisée pour la lecture locale."
    if is_notice_video(stream["url"]):
        return "L’API mobile renvoie une vidéo d’avertissement, sans source complète."
    try:
        duration = float(stream.get("duration_sec") or 0)
    except (ValueError, TypeError):
        duration = 0
    if expected_duration >= 180 and 0 < duration * 3 < expected_duration:
        return f"La source disponible est un extrait de {int(duration)} s, pas la vidéo complète."
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, http2=True) as client:
            with client.stream("GET", stream["url"], headers={**_media_headers(stream), "Range": "bytes=0-31"}) as response:
                if response.status_code != 206:
                    return _cdn_refusal(response, "cette source vidéo")
                mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if mime not in ("video/mp4", "application/octet-stream"):
                    return "La source ne renvoie pas un fichier MP4."
                if not re.fullmatch(r"bytes 0-31/\d+", response.headers.get("content-range", "")):
                    return "Le CDN ne fournit pas les plages vidéo nécessaires au lecteur."
                sample = _sample(response)
                if len(sample) < 8 or sample[4:8] != b"ftyp":
                    return "La réponse du CDN n’est pas un MP4 lisible."
    except (httpx.HTTPError, KeyError, ValueError):
        return "Le CDN vidéo ne répond pas."
    return None


def _prepare_adaptive(stream):
    if not _public_https_media_url(stream.get("url")):
        return None, "Adresse du manifeste DASH non autorisée."
    try:
        headers = _media_headers(stream)
        with httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False, http2=True) as client:
            with client.stream("GET", stream["url"], headers=headers) as response:
                if response.status_code != 200:
                    return None, _cdn_refusal(response, "le manifeste DASH")
                manifest = bytearray()
                for chunk in response.iter_bytes(8192):
                    manifest.extend(chunk)
                    if len(manifest) > MAX_MANIFEST_BYTES:
                        return None, "Manifeste DASH trop volumineux."
        presentation = build_presentation(bytes(manifest), stream["url"])
        compatible = [t for t in presentation.tracks if t.hls_compatible]
        selected = compatible if presentation.playlists else presentation.tracks
        probes = []
        for kind in ("video", "audio"):
            choices = [t for t in selected if t.kind == kind]
            if choices:
                track = min(choices, key=lambda t: t.bandwidth)
                probes += [track.init, track.segments[0][0]]
        def check_fragment(local):
            url = presentation.assets[local.removeprefix("asset/")]
            if not _public_https_media_url(url):
                return "Adresse d’un fragment DASH non autorisée."
            with httpx.Client(timeout=httpx.Timeout(6, connect=3), follow_redirects=False, http2=True) as client:
                with client.stream("GET", url, headers={**headers, "Range": "bytes=0-31"}) as response:
                    if response.status_code not in (200, 206):
                        return _cdn_refusal(response, "un fragment DASH")
                    sample = _sample(response)
                    if len(sample) < 8 or sample[4:8] not in (b"ftyp", b"styp", b"sidx", b"moof"):
                        return "Un fragment DASH ne contient pas de média MP4 lisible."
            return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            issues = list(pool.map(check_fragment, probes))
        if any(issues):
            return None, next(issue for issue in issues if issue)
        return presentation, None
    except ManifestError as exc:
        return None, str(exc)
    except ValueError:
        return None, "Manifeste DASH non pris en charge."
    except (httpx.HTTPError, KeyError):
        return None, "Le manifeste ou les fragments DASH ne répondent pas."


def _register_mp4_playback(stream):
    if str(stream.get("format", "")).lower() != "mp4" or not _public_https_media_url(stream.get("url")):
        return None
    token = secrets.token_urlsafe(24)
    with _playback_lock:
        now = time.monotonic()
        for old in [k for k, v in _playback_sources.items() if v["expires"] <= now]:
            del _playback_sources[old]
        _playback_sources[token] = {"url": stream["url"], "headers": _media_headers(stream), "expires": now + 1800}
    return f"/api/play/{token}"


def _register_adaptive(stream, presentation):
    token = secrets.token_urlsafe(24)
    with _playback_lock:
        now = time.monotonic()
        for old in [k for k, v in _adaptive_sources.items() if v["expires"] <= now]:
            del _adaptive_sources[old]
        if len(_adaptive_sources) >= 8:
            del _adaptive_sources[min(_adaptive_sources, key=lambda k: _adaptive_sources[k]["expires"])]
        _adaptive_sources[token] = {"presentation": presentation, "headers": _media_headers(stream), "expires": now + 1800}
    return f"/api/adaptive/{token}"


def prepare_streams(streams):
    durations = []
    for stream in streams:
        try:
            durations.append(float(stream.get("duration_sec") or 0))
        except (TypeError, ValueError):
            pass
    expected = max(durations, default=0)
    candidates = [s for s in streams if str(s.get("format", "")).lower() in ("mp4", "dash") and not s.get("playback_issue")]
    def check(stream):
        return _prepare_adaptive(stream) if str(stream.get("format", "")).lower() == "dash" else (None, _probe_mp4_playback(stream, expected))
    with ThreadPoolExecutor(max_workers=4) as pool:
        checks = list(pool.map(check, candidates))
    derived = []
    for stream, (presentation, issue) in zip(candidates, checks):
        if issue:
            stream["playback_issue"] = issue
        elif presentation is not None:
            base = _register_adaptive(stream, presentation)
            stream["play_url"] = base + "/manifest.mpd"
            if presentation.playlists:
                derived.append({"source": stream.get("source", "DASH") + " · HLS", "format": "HLS",
                                "quality": f"{presentation.hls_height}p (HLS)" if presentation.hls_height else "Adaptatif (HLS)",
                                "quality_value": presentation.hls_height, "url": base + "/master.m3u8",
                                "play_url": base + "/master.m3u8", "codec": "fMP4", "headers": {}, "size_mb": 0})
        else:
            local_url = _register_mp4_playback(stream)
            if local_url:
                stream["play_url"] = local_url
            else:
                stream["playback_issue"] = "Adresse du CDN non autorisée pour la lecture locale."
    streams.extend(derived)
    return streams


def _session(registry, token):
    with _playback_lock:
        source = registry.get(token)
        if source and source["expires"] <= time.monotonic():
            del registry[token]
            source = None
        if source:
            source["expires"] = time.monotonic() + 1800
    if source is None:
        raise HTTPException(status_code=404, detail="Session vidéo expirée : rechargez le lecteur")
    return source


async def play_stream(token: str, request: Request):
    source = _session(_playback_sources, token)
    return await _relay_media(source["url"], source["headers"], request, source)


def register_download(stream, subject_id, season, episode):
    """Bind a verified MP4 session to one title, episode and rendition."""
    if str(stream.get("format", "")).lower() != "mp4" or stream.get("playback_issue"):
        return None
    match = re.fullmatch(r"/api/play/([A-Za-z0-9_-]+)", stream.get("play_url", ""))
    if not match or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", subject_id):
        return None
    identity = repr((stream.get("quality"), stream.get("quality_value"), stream.get("codec"), stream.get("source")))
    rendition = hashlib.sha256(identity.encode()).hexdigest()[:12]
    media_id = f"mb-{subject_id}-s{season}e{episode}-{rendition}"
    if len(media_id) > 80:
        return None
    with _playback_lock:
        source = _playback_sources.get(match[1])
        if not source or source["expires"] <= time.monotonic():
            return None
        source["download_id"] = media_id
        for old in [key for key, value in _download_sources.items() if value["expires"] <= time.monotonic()]:
            del _download_sources[old]
        _download_sources[media_id] = source
    return {"id": media_id, "url": f"/api/download/{match[1]}/{media_id}",
            "resume_url": f"/api/download/by-id/{media_id}"}


async def download_stream(token: str, media_id: str, request: Request):
    source = _session(_playback_sources, token)
    if source.get("download_id") != media_id:
        raise HTTPException(status_code=403, detail="Ce téléchargement ne correspond pas au média")
    response = await _relay_media(source["url"], source["headers"], request, source)
    response.headers["content-disposition"] = f'attachment; filename="cinekivu-{media_id}.mp4"'
    response.headers["content-type"] = "video/mp4"
    return response


def _resolve_download(media_id, engine):
    match = re.fullmatch(r"mb-([A-Za-z0-9_-]{1,40})-s([0-9]{1,3})e([0-9]{1,4})-([a-f0-9]{12})", media_id)
    if not match or len(media_id) > 80:
        raise HTTPException(422, "Identifiant de téléchargement invalide")
    # Resolve once for concurrent HEAD/GET attempts; never block the ASGI loop.
    with _download_resolution_lock:
        with _playback_lock:
            source = _download_sources.get(media_id)
            if source and source["expires"] > time.monotonic():
                return source
        entries = prepare_streams(engine.get_streams(match[1], season=int(match[2]),
            episode=int(match[3]), prefer_web_mp4=True))
        for entry in entries:
            register_download(entry, match[1], int(match[2]), int(match[3]))
        with _playback_lock:
            source = _download_sources.get(media_id)
        if not source:
            raise HTTPException(409, "Cette qualité n’est plus disponible. Rouvrez la fiche pour choisir une autre source.")
        return source


async def stable_download_stream(media_id, request, engine):
    for attempt in range(2):
        source = await asyncio.to_thread(_resolve_download, media_id, engine)
        try:
            response = await _relay_media(source["url"], source["headers"], request, source)
            response.headers["content-disposition"] = f'attachment; filename="cinekivu-{media_id}.mp4"'
            response.headers["content-type"] = "video/mp4"
            return response
        except HTTPException as error:
            if attempt or error.status_code not in (401, 403, 404):
                raise
            with _playback_lock:
                _download_sources.pop(media_id, None)


async def adaptive_media(token: str, resource: str, request: Request):
    source = _session(_adaptive_sources, token)
    presentation = source["presentation"]
    body = presentation.dash if resource == "manifest.mpd" else presentation.playlists.get(resource)
    if body is not None:
        mime = "application/dash+xml" if resource == "manifest.mpd" else "application/vnd.apple.mpegurl"
        return Response(body if request.method != "HEAD" else b"", media_type=mime,
                        headers={"cache-control": "private, no-store", "content-length": str(len(body))})
    url = presentation.assets.get(resource.removeprefix("asset/")) if resource.startswith("asset/") else None
    if not url:
        raise HTTPException(status_code=404, detail="Fragment inconnu")
    if not _public_https_media_url(url):
        raise HTTPException(status_code=502, detail="Adresse du fragment non autorisée")
    return await _relay_media(url, source["headers"], request, source)


async def _relay_media(url, source_headers, request, activity):
    requested_range = request.headers.get("range")
    if requested_range and not re.fullmatch(r"bytes=\d*-\d*", requested_range):
        raise HTTPException(status_code=416, detail="Range invalide")
    headers = {**source_headers, "Accept-Encoding": "identity"}
    if requested_range:
        headers["Range"] = requested_range
        if request.headers.get("if-range"):
            headers["If-Range"] = request.headers["if-range"]
    client = getattr(request.app.state, "cdn_client", None)
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8), follow_redirects=False, http2=True)
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        if owned: await client.aclose()
        raise HTTPException(status_code=502, detail="CDN vidéo inaccessible") from exc
    if upstream.status_code not in (200, 206):
        status = upstream.status_code
        await upstream.aclose()
        if owned: await client.aclose()
        raise HTTPException(status_code=status if status in (401, 403, 404, 416, 429) else 502, detail="Le CDN refuse ce flux vidéo")
    mime = upstream.headers.get("content-type", "video/mp4").split(";", 1)[0].lower()
    if mime not in ("video/mp4", "audio/mp4", "video/iso.segment", "application/octet-stream"):
        await upstream.aclose()
        if owned: await client.aclose()
        raise HTTPException(status_code=502, detail="Le CDN n’a pas renvoyé une vidéo MP4")
    response_headers = {k: v for k in ("content-length", "content-range", "accept-ranges", "etag", "last-modified") if (v := upstream.headers.get(k)) is not None}
    response_headers["cache-control"] = "private, no-store"
    if request.method == "HEAD":
        await upstream.aclose()
        if owned: await client.aclose()
        return Response(status_code=upstream.status_code, headers=response_headers, media_type=mime)
    async def chunks():
        try:
            async for chunk in upstream.aiter_raw(65536):
                with _playback_lock:
                    activity["expires"] = time.monotonic() + 1800
                yield chunk
        finally:
            await upstream.aclose()
            if owned: await client.aclose()
    return StreamingResponse(chunks(), status_code=upstream.status_code, headers=response_headers, media_type=mime)
