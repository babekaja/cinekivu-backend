"""Read-only public metadata adapter. Intended for authorised research/integration only.

No playback manifests, session cookies, authentication, or media downloads.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
import json
import re
import time
import httpx
from cinekivu_text import synopsis

UPSTREAM_SITE = "https://movieboxhd.net"
UPSTREAM_API = "https://h5-api.aoneroom.com/wefeed-h5api-bff"
VALID_DETAIL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,319}$")

@dataclass(frozen=True)
class SourceRef:
    work_id: int
    external_subject_id: str
    external_detail_path: str
    source_rating: float | None = None
    source_rating_count: int | None = None
    trailer_url: str | None = None
    trailer_cover_url: str | None = None
    trailer_duration_seconds: int | None = None

class NuxtScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.value = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NUXT_DATA__":
            self.active = True

    def handle_data(self, data):
        if self.active:
            self.value += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

def deref(seq: list, value: Any) -> Any:
    """Nuxt devalue-flattened payload uses positive integer array references."""
    if type(value) is int:
        return seq[value] if 0 <= value < len(seq) else None
    return value

def field(seq: list, obj: dict, name: str, fallback=None):
    return deref(seq, obj[name]) if name in obj else fallback

def parse_public_detail(html: str) -> dict:
    parser = NuxtScriptParser()
    parser.feed(html)
    if not parser.value:
        raise ValueError("Données SSR __NUXT_DATA__ absentes")
    payload = json.loads(parser.value)
    root = next((x for x in payload if isinstance(x, dict) and "postList" in x and "subject" in x), None)
    if root is None:
        raise ValueError("Aucun objet subject/postList trouvé dans le SSR")
    subject = deref(payload, root["subject"])
    if not isinstance(subject, dict):
        raise ValueError("Format du sujet non reconnu")

    trailer = field(payload, subject, "trailer") or {}
    if not isinstance(trailer, dict):
        trailer = {}
    video = field(payload, trailer, "videoAddress") or {}
    poster = field(payload, trailer, "cover") or {}
    if not isinstance(video, dict): video = {}
    if not isinstance(poster, dict): poster = {}

    stars = field(payload, root, "stars") or []
    cast = []
    for ref in stars if isinstance(stars, list) else []:
        star = deref(payload, ref)
        if isinstance(star, dict):
            cast.append({
                "externalId": str(field(payload, star, "staffId", "")),
                "name": field(payload, star, "name", ""),
                "character": field(payload, star, "character", ""),
                "avatarUrl": field(payload, star, "avatarUrl"),
                "detailPath": field(payload, star, "detailPath"),
            })
    def number(val, kind):
        try: return kind(val)
        except (ValueError, TypeError): return None

    return {
        "subjectId": str(field(payload, subject, "subjectId", "")),
        "title": field(payload, subject, "title", ""),
        "description": synopsis(*(field(payload, subject, key) for key in ('description', 'synopsis', 'overview', 'desc', 'introduction'))),
        "rating": {
            "value": number(field(payload, subject, "imdbRatingValue"), float),
            "count": number(field(payload, subject, "imdbRatingCount"), int),
            "scale": 10,
            "label": "IMDb (étiquette d'origine; provenance directe non vérifiée)",
        },
        "trailer": {
            "url": field(payload, video, "url"),
            "durationSeconds": number(field(payload, video, "duration"), int),
            "posterUrl": field(payload, poster, "url"),
            "videoId": str(field(payload, video, "videoId", "")),
            "playbackAllowed": False,  # Requires an independent rights check.
        },
        "cast": cast,
    }

def to_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None

def normalize_comments(payload: dict, limit: int = 10) -> dict:
    """Map textual comments and rated reviews, excluding non-comment media posts.

    Live cases: Gannibal VF returns pages of IMAGE-only records with blank content
    and status=0. Those are not reviews and must not become empty chat bubbles.
    Preserve the upstream nextPage even when all records were filtered out.
    """
    if payload.get("code") != 0:
        raise ValueError(f"L'API source a retourné le code {payload.get('code')}")
    d = payload.get("data") or {}
    pager = d.get("pager") or {}
    rows = []
    skipped = 0
    for post in (d.get("items") or [])[:limit]:
        if not isinstance(post, dict):
            skipped += 1
            continue
        content = post.get("content")
        text = content.strip()[:4000] if isinstance(content, str) else ""
        try:
            rating = float(post.get("subjectRate"))
            if not 0 <= rating <= 10:
                rating = None
        except (TypeError, ValueError):
            rating = None
        is_rating = post.get("isSubjectRate") is True and rating is not None
        if post.get("status") not in (None, 1) or (not text and not is_rating):
            skipped += 1
            continue
        post_id = str(post.get("postId") or "")
        if not post_id:
            skipped += 1
            continue
        user = post.get("user") if isinstance(post.get("user"), dict) else {}
        stat = post.get("stat") if isinstance(post.get("stat"), dict) else {}
        rows.append({
            "id": post_id,
            "text": text,
            "author": {"displayName": str(user.get("nickname") or "Utilisateur")[:120],
                       "avatarUrl": user.get("avatar") if str(user.get("avatar") or "").startswith("https://") else None},
            "createdAt": to_iso(post.get("createdAt")),
            "likes": stat.get("likeCount", 0),
            "replies": stat.get("commentCount", 0),
            "rating": rating if is_rating else None,
            "isRatedReview": is_rating,
        })
    has_more = bool(pager.get("hasMore"))
    next_cursor = str(pager.get("nextPage")) if has_more and pager.get("nextPage") else None
    return {"source": "moviebox", "items": rows,
            "hasMore": has_more and bool(next_cursor), "nextCursor": next_cursor,
            "filteredOut": skipped}

class PublicMetadataClient:
    """Read-only; call only where allowed by third-party terms and data rights."""
    def __init__(self, ttl_seconds: int = 600):
        self._cache: dict[str, tuple[float,dict]] = {}
        self._ttl=ttl_seconds

    async def detail(self, path: str) -> dict:
        if not VALID_DETAIL.fullmatch(path):
            raise ValueError("Chemin de détail invalide")
        now=time.monotonic()
        cached=self._cache.get(path)
        if cached and now-cached[0]<self._ttl:
            return cached[1]
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            r=await client.get(f"{UPSTREAM_SITE}/moviedetail/{path}",headers={"Accept":"text/html","User-Agent":"CineKivuMetadataResearch/0.1"})
            r.raise_for_status()
        snapshot=parse_public_detail(r.text)
        self._cache[path]=(now,snapshot)
        return snapshot

    async def reviews(self, subject_id: str, limit: int, cursor: str | None) -> dict:
        if not subject_id.isdigit() or len(subject_id)>40:
            raise ValueError("subjectId invalide")
        if cursor is not None and (not cursor.isdigit() or len(cursor)>30):
            raise ValueError("Curseur invalide")
        params={"id":subject_id,"perPage":limit,"page":cursor or "0"}
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            r=await client.get(f"{UPSTREAM_API}/post/list/subject",params=params,
                headers={"Accept":"application/json","User-Agent":"CineKivuMetadataResearch/0.1"})
            r.raise_for_status()
        return normalize_comments(r.json(), limit=limit)

source_client=PublicMetadataClient()
