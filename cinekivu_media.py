"""Read-only live media details for the subject IDs used by Compose.

The eleven-title path map only resolves missing public detail URLs. Scores,
casting, trailers and comments come from current provider responses.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from cinekivu_media_source import source_client
from cinekivu_source import api as cine
from cinekivu_web import live_title

log = logging.getLogger(__name__)
SUBJECT_ID = re.compile(r"^[0-9]{1,40}$")
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "media_subject_paths.json"


def _image(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) and value.startswith("https://") else None


def _number(value: Any, convert):
    try:
        return convert(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def _known_paths() -> dict[str, str]:
    """Known detail paths, never a source for displayed catalogue metadata."""
    with FIXTURE.open(encoding="utf-8") as handle:
        return {sid: row['detailPath'] for sid, row in json.load(handle).items()
                if SUBJECT_ID.fullmatch(sid) and isinstance(row.get('detailPath'), str)}


async def _base(subject_id: str) -> dict:
    if not SUBJECT_ID.fullmatch(subject_id):
        raise HTTPException(422, "subjectId invalide")
    try:
        live = await run_in_threadpool(cine.engine.get_details, subject_id)
    except Exception as exc:
        log.info("Media detail lookup failed for %s: %s", subject_id, type(exc).__name__)
        live = None
    if isinstance(live, dict) and str(live.get("subject_id")) == subject_id:
        path = live.get('detail_path') or _known_paths().get(subject_id)
        return {**live, 'detail_path': path, '_live': True}
    try:
        current = await run_in_threadpool(live_title, subject_id)
    except HTTPException:
        current = None
    if current:
        return {**current, 'detailPath': current.get('detailPath') or _known_paths().get(subject_id),
                '_live': True}
    path = _known_paths().get(subject_id)
    if path:
        return {'subjectId': subject_id, 'detailPath': path, '_pathOnly': True}
    raise HTTPException(502, "Fiche actuelle indisponible")


async def _metadata(subject_id: str, *, live: bool) -> tuple[dict, dict | None]:
    base = await _base(subject_id)
    rich = None
    path = base.get("detailPath") or base.get("detail_path")
    if live and isinstance(path, str) and path:
        try:
            candidate = await source_client.detail(path)
            if candidate.get("subjectId") == subject_id:
                rich = candidate
            else:
                log.warning("SSR subject ID did not match requested title %s", subject_id)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.info("Public metadata unavailable for %s: %s", subject_id, type(exc).__name__)
    if base.get('_pathOnly') and rich is None:
        raise HTTPException(502, "Métadonnées actuelles indisponibles")
    return base, rich


def _score(base: dict, rich: dict | None) -> dict:
    reported = rich.get("rating") if rich else None
    value = (reported or {}).get("value") if reported else None
    count = (reported or {}).get("count") if reported else None
    return {
        "value": _number(value if value is not None else
                         base.get("imdbRatingValue", base.get("imdb_rating", base.get("score"))), float),
        "count": _number(count if count is not None else
                         base.get("imdbRatingCount", base.get("ratingCount")), int),
        "scale": 10,
        "source": "moviebox-public-ssr" if rich else "cinekivu-live",
        "label": "Note affichée par MovieBox (IMDb déclaré, non vérifié)",
    }


def _trailer(base: dict, rich: dict | None) -> dict:
    if rich:
        result = dict(rich.get("trailer") or {})
    else:
        trailer = base.get("trailer") if isinstance(base.get("trailer"), dict) else {}
        video = trailer.get("videoAddress") if isinstance(trailer.get("videoAddress"), dict) else {}
        cover = trailer.get("cover")
        result = {"url": _image(video.get("url")), "posterUrl": _image(cover),
                  "durationSeconds": _number(video.get("duration"), int),
                  "videoId": str(video.get("videoId")) if video.get("videoId") else None}
    result["url"] = _image(result.get("url"))
    result["posterUrl"] = _image(result.get("posterUrl"))
    trailer_url = result["url"]
    parsed = urlparse(trailer_url) if trailer_url else None
    result["playbackAllowed"] = bool(parsed and parsed.hostname == "macdn.aoneroom.com"
                                     and parsed.path.startswith("/media/vone/")
                                     and parsed.path.lower().endswith(".mp4"))
    return result


def _cast(base: dict, rich: dict | None) -> list[dict]:
    people = rich.get("cast") if rich else (base.get("staffList") or [])
    if not isinstance(people, list):
        return []
    result, seen = [], set()
    for item in people[:150]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("staffName") or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        result.append({"id": str(item.get("externalId") or item.get("staffId") or "") or None,
                       "name": name[:140], "character": item.get("character"),
                       "avatarUrl": _image(item.get("avatarUrl") or item.get("avatar")),
                       "role": item.get("role")})
    return result


def _gallery(base: dict) -> list[str]:
    stills = base.get("stills") or []
    if isinstance(stills, dict):
        stills = [stills]
    if not isinstance(stills, list):
        return []
    return list(dict.fromkeys(x for row in stills if (x := _image(row))))[:24]


def install_media_routes(app):
    router = APIRouter(prefix="/api/v1/media", tags=["CineKivu · notes et avis"])

    @router.get("/{subject_id}/overview")
    async def overview(subject_id: str):
        base, rich = await _metadata(subject_id, live=True)
        return {"subjectId": subject_id, "title": rich.get("title") if rich else base.get("title", ""),
                "rating": _score(base, rich), "trailer": _trailer(base, rich), "cast": _cast(base, rich),
                "gallery": _gallery(base), "genre": base.get("genre"),
                "country": base.get("countryName") or base.get("country"),
                "releaseDate": base.get("releaseDate") or base.get("release_date"),
                "source": "moviebox-public-ssr" if rich else "cinekivu-live",
                "stale": False, "observedAt": None,
                "hasResource": bool(base.get("hasResource")), "availabilityStatus": "unknown",
                "audioVerified": False, "subtitlesVerified": False,
                "accessNote": "La fiche actuelle ne confirme pas les droits ou la disponibilité de lecture."}

    @router.get("/{subject_id}/rating")
    async def rating(subject_id: str, external: bool = False):
        base, rich = await _metadata(subject_id, live=external)
        return {"subjectId": subject_id, "external": _score(base, rich),
                "cinekivu": {"value": None, "count": 0, "scale": 10}}

    @router.get("/{subject_id}/trailer")
    async def trailer(subject_id: str, external: bool = False):
        base, rich = await _metadata(subject_id, live=external)
        return {"subjectId": subject_id, "trailer": _trailer(base, rich),
                "notice": "Lien public de bande-annonce fourni par le catalogue lorsqu'il est disponible."}

    @router.get("/{subject_id}/cast")
    async def cast(subject_id: str, external: bool = False):
        base, rich = await _metadata(subject_id, live=external)
        return {"subjectId": subject_id, "source": "moviebox-public-ssr" if rich else "cinekivu-live",
                "items": _cast(base, rich)}

    @router.get("/{subject_id}/reviews")
    async def reviews(subject_id: str, source: str = Query("moviebox", pattern="^(moviebox|cinekivu)$"),
                      limit: int = Query(10, ge=1, le=30), cursor: str | None = None):
        if not SUBJECT_ID.fullmatch(subject_id):
            raise HTTPException(422, "subjectId invalide")
        if source == "cinekivu":
            return {"subjectId": subject_id, "source": source, "items": [], "hasMore": False,
                    "nextCursor": None, "filteredOut": 0,
                    "note": "Aucun compte ou avis CineKivu local n'est configuré."}
        if cursor is not None and (not cursor.isdigit() or len(cursor) > 30):
            raise HTTPException(422, "Curseur invalide")
        try:
            page = await source_client.reviews(subject_id, limit, cursor)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.info("Public reviews unavailable for %s: %s", subject_id, type(exc).__name__)
            raise HTTPException(502, "Commentaires du fournisseur indisponibles") from exc
        return {"subjectId": subject_id, **page}

    app.include_router(router)
