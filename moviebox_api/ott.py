"""Versioned OTT application API.

Stable public response shapes are deliberately separate from the upstream MovieBox
contract. Remote calls use existing client methods; local library/progress/settings
are SQLite-backed and do not imply cloud synchronization or streaming rights.
"""
from __future__ import annotations

import json
import copy
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Cookie, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from .edition_search import search_editions, related_editions, base_title, is_french_edition

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
GENRES = (
    {"id": "action", "name": "Action"}, {"id": "comedy", "name": "Comédie"},
    {"id": "drama", "name": "Drame"}, {"id": "thriller", "name": "Thriller"},
    {"id": "horror", "name": "Horreur"}, {"id": "romance", "name": "Romance"},
    {"id": "animation", "name": "Animation"}, {"id": "documentary", "name": "Documentaire"},
    {"id": "science-fiction", "name": "Science-fiction"}, {"id": "fantasy", "name": "Fantastique"},
)
TABS = (
    {"id": "home", "name": "Accueil", "tab_id": 1},
    {"id": "movies", "name": "Films", "tab_id": 2},
    {"id": "series", "name": "Séries", "tab_id": None},
    {"id": "new", "name": "Nouveautés", "tab_id": None},
    {"id": "top10", "name": "Les mieux notés", "tab_id": None},
)


def _id(value: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail="Invalid content ID")
    return value


def _unwrap(value: Any, depth: int = 0):
    """Bounded, envelope-tolerant extraction. Never treat an arbitrary dict as a title."""
    if depth > 7:
        return
    if isinstance(value, list):
        for v in value[:200]:
            yield from _unwrap(v, depth + 1)
    elif isinstance(value, dict):
        subject = value.get("subject")
        if isinstance(subject, dict):
            yield from _unwrap(subject, depth + 1)
            return
        if value.get("subjectId") or (value.get("id") and (value.get("title") or value.get("name") or value.get("subjectName"))):
            yield value
            return
        for key in ("data", "items", "list", "subjects", "rows", "sections", "lists", "movieList", "results", "recommendations", "info"):
            if key in value:
                yield from _unwrap(value[key], depth + 1)


def _items(raw: Any, mapper: Callable[[dict], dict], limit: int = 100) -> list[dict]:
    found: dict[str, dict] = {}
    for record in _unwrap(raw):
        try:
            mapped = mapper(record)
            sid = str(mapped.get("subjectId") or "")
            if sid and sid != "None" and mapped.get("title") not in ("", "Unknown", None):
                found.setdefault(sid, mapped)
        except (ValueError, TypeError, AttributeError):
            pass
        if len(found) >= limit:
            break
    return list(found.values())


def _remote(raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise HTTPException(502, detail="Invalid upstream response")
    code = raw.get("code", 0)
    if code not in (0, 200, "0", "200", None):
        # Provide the upstream error message when available instead of a generic label.
        msg = raw.get("msg") or raw.get("message") or "Catalogue provider unavailable"
        raise HTTPException(502, detail=f"Catalogue provider unavailable ({msg})")
    return raw


class LocalStore:
    """Per-guest-session local state; no cross-guest 'default' history."""

    def __init__(self, db_path: str | None = None):
        path = db_path or os.getenv("MOVIEBOX_DB_PATH", "data/moviebox_ott.sqlite3")
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS library (
                    sid TEXT NOT NULL, subject_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    cover TEXT NOT NULL DEFAULT '', subject_type INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, PRIMARY KEY (sid, subject_id)
                );
                CREATE TABLE IF NOT EXISTS progress (
                    sid TEXT NOT NULL, subject_id TEXT NOT NULL, season INTEGER NOT NULL DEFAULT 1,
                    episode INTEGER NOT NULL DEFAULT 1, position_seconds INTEGER NOT NULL DEFAULT 0,
                    duration_seconds INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '',
                    cover TEXT NOT NULL DEFAULT '', subject_type INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL, PRIMARY KEY (sid, subject_id, season, episode)
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    sid TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at INTEGER NOT NULL
                );
            """)

    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock, self.connection() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args: tuple = ()):
        with self.lock, self.connection() as db:
            db.execute(sql, args)
            db.commit()


class TitleSnapshot(BaseModel):
    title: str = Field(default="", max_length=240)
    cover: str = Field(default="", max_length=2048)
    subject_type: int = Field(default=1, ge=1, le=5)


class ProgressUpdate(TitleSnapshot):
    position_seconds: int = Field(ge=0, le=2592000)
    duration_seconds: int = Field(default=0, ge=0, le=2592000)
    season: int = Field(default=1, ge=1, le=999)
    episode: int = Field(default=1, ge=1, le=9999)


class PreferencesUpdate(BaseModel):
    language: Optional[str] = Field(default=None, pattern=r"^(fr|en|sw)$")
    audio_language: Optional[str] = Field(default=None, pattern=r"^(fr|en|original)$")
    subtitle_language: Optional[str] = Field(default=None, pattern=r"^(fr|en|off)$")
    autoplay: Optional[bool] = None
    reduced_motion: Optional[bool] = None


DEFAULT_PREFS = {"language": "fr", "audio_language": "fr", "subtitle_language": "fr", "autoplay": False, "reduced_motion": False}


def install_ott_routes(app: FastAPI, get_session: Callable, mapper: Callable,
                       format_sections: Callable, extract_items: Callable,
                       store: LocalStore | None = None, home_loader=None, catalog_loader=None, search_loader=None):
    db = store or LocalStore()
    router = APIRouter(prefix="/api/v1", tags=["OTT / application"])
    search_cache: dict[tuple, tuple[float, dict]] = {}
    search_cache_lock = threading.RLock()
    # Keep each session's last successful feed briefly so a transient provider
    # outage does not blank an already loaded page or mix account-specific feeds.
    feed_cache: dict[tuple, tuple[float, dict]] = {}
    feed_cache_lock = threading.RLock()

    def public_feed(cache_key: tuple, load: Callable[[], dict]) -> dict:
        # Prefer the last known feed immediately during an outage. A cold cache
        # gets one short retry, which also covers intermittent DNS failures.
        for attempt in range(2):
            try:
                data = load()
                if data.get("sections") or data.get("items"):
                    with feed_cache_lock:
                        if len(feed_cache) >= 128:
                            feed_cache.pop(next(iter(feed_cache)))
                        feed_cache[cache_key] = (time.monotonic(), copy.deepcopy(data))
                return data
            except HTTPException as exc:
                if exc.status_code != 502:
                    raise
                with feed_cache_lock:
                    cached = feed_cache.get(cache_key)
                if cached and time.monotonic() - cached[0] < 3600:
                    return {**copy.deepcopy(cached[1]), "stale": True}
                if attempt:
                    raise
                time.sleep(0.35)
        raise HTTPException(502, detail="Catalogue provider unavailable")

    def indexed_search(s: dict, q: str, page: int = 1, subject_type: int = 0) -> dict:
        # Cache by authenticated/guest session: never cross account boundaries.
        cache_key = (s["id"], q.casefold(), page, subject_type)
        now = time.monotonic()
        with search_cache_lock:
            entry = search_cache.get(cache_key)
            if entry and entry[0] > now:
                return copy.deepcopy(entry[1])
        value = search_editions(s["content"], mapper, _items, q, page=page,
                                subject_type=subject_type)
        with search_cache_lock:
            # Prevent unbounded cache retention if many guests or distinct searches.
            if len(search_cache) >= 256:
                expired = [key for key, (until, _) in search_cache.items() if until <= now]
                for key in expired:
                    search_cache.pop(key, None)
                if len(search_cache) >= 256:
                    search_cache.pop(next(iter(search_cache)))
            search_cache[cache_key] = (time.monotonic() + 180, copy.deepcopy(value))
        return value

    def session(sid: Optional[str]) -> dict:
        return get_session(sid)

    def key(sid: Optional[str]) -> str:
        return session(sid)["id"]

    def result(data: Any, source: str = "upstream", **meta):
        return {"code": 0, "data": data, "meta": {"source": source, **meta}}

    def remote_items(s, remote_call, *, page=1, max_items=100):
        return _items(_remote(remote_call()), mapper, limit=max_items)

    @router.get("/config")
    def config():
        return result({"name": "CineKivu Streaming", "version": "2.0.0", "local_profiles": True,
                       "tabs": list(TABS), "genres": list(GENRES),
                       "note": "Availability, audio language and stream rights depend on the provider."}, "app")

    @router.get("/home")
    def home(page: int = Query(1, ge=1, le=200), session_id: Optional[str] = Cookie(None)):
        s = session(session_id)
        def load():
            if home_loader is not None:
                return home_loader(page)
            res = _remote(s["content"].get_categories(category_id=1, page=page))
            data = res.get("data") or {}
            source_rows = data.get("list") or data.get("items") or data.get("subjects") or [] if isinstance(data, dict) else data
            sections = []
            if isinstance(source_rows, list):
                try:
                    for row in format_sections(source_rows):
                        mapped = _items(row.get("items", []), mapper)
                        if mapped:
                            sections.append({"title": row.get("title") or "Découvrir", "items": mapped})
                except (TypeError, ValueError):
                    pass
            if not sections:
                fallback_items = _items(data, mapper)
                if fallback_items:
                    sections.append({"title": "À découvrir", "items": fallback_items})
            featured = next((m for row in sections for m in row["items"] if m.get("cover")), None)
            return {"featured": featured, "sections": sections, "page": page}
        data = public_feed((s["id"], "home", page), load)
        return result(data, "cache" if data.get("stale") else "upstream")

    @router.get("/catalog/{category}")
    def catalog(category: str, page: int = Query(1, ge=1, le=100_000),
                session_id: Optional[str] = Cookie(None)):
        s = session(session_id)
        name = category.lower()
        def load():
            if catalog_loader is not None:
                return catalog_loader(name, page)
            if name == "movies":
                raw = s["content"].get_subject_list(page=page, category_type="1")
            elif name == "series":
                raw = s["content"].get_subject_list(page=page, category_type="2")
            elif name in ("new", "recommended"):
                raw = s["content"].get_discovery()
            elif name == "trending":
                raw = s["content"].get_trending()
            elif name == "live":
                raw = s["content"].get_live_channels(page=page)
            elif name == "top10":
                raw = s["content"].get_rankings(tab_id=1)
            else:
                tab = next((t for t in TABS if t["id"] == name and t["tab_id"] is not None), None)
                if not tab:
                    raise HTTPException(404, detail="Unknown catalogue category")
                raw = s["content"].get_categories(tab["tab_id"], page=page)
            titles = _items(_remote(raw), mapper)
            return {"items": titles, "page": page, "category": name}
        data = public_feed((s["id"], "catalog", name, page), load)
        return result(data, "cache" if data.get("stale") else "upstream", kind="provider-feed")

    @router.get("/titles/{subject_id}")
    def title(subject_id: str, session_id: Optional[str] = Cookie(None)):
        s = session(session_id)
        raw = s["content"].get_movie_detail(_id(subject_id))
        # Auto-recover from 441 (miss token) by bootstrapping a fresh token.
        if isinstance(raw, dict) and raw.get("code") in (401, 441):
            try:
                from cinekivu_source.api import MovieBoxEngine
                token = MovieBoxEngine.get_instance().bootstrap_token(force=True)
                if token:
                    s["auth"].update_session(token)
                    raw = s["content"].get_movie_detail(_id(subject_id))
            except Exception:
                pass
        raw = _remote(raw)
        data = raw.get("data") or {}
        if not isinstance(data, dict) or not data:
            raise HTTPException(404, detail="Title not found in the provider response")
        return result({"title": mapper(data), "raw": data})

    @router.get("/titles/{subject_id}/seasons")
    def seasons(subject_id: str, session_id: Optional[str] = Cookie(None)):
        s = session(session_id)
        raw = _remote(s["content"].get_episode_list(_id(subject_id)))
        data = raw.get("data") or {}
        return result(data, contract="upstream-structure-may-vary")

    @router.get("/genres")
    def genres():
        return result(list(GENRES), "app")

    @router.get("/genres/{genre_id}")
    def genre(genre_id: str, page: int = Query(1, ge=1, le=200), session_id: Optional[str] = Cookie(None)):
        if genre_id not in {g["id"] for g in GENRES}:
            raise HTTPException(404, detail="Unknown genre")
        s = session(session_id)
        raw = _remote(s["content"].filter_items({"genre": genre_id}, page=page))
        return result({"items": _items(raw, mapper), "page": page, "genre": genre_id},
                      filter_scope="upstream-unverified")

    @router.get("/search")
    def search(q: str = Query(min_length=1, max_length=120), page: int = Query(1, ge=1, le=200),
               subject_type: int = Query(0, ge=0, le=2),
               edition: str = Query("all", pattern=r"^(all|fr)$"),
               session_id: Optional[str] = Cookie(None)):
        s = session(session_id)
        if search_loader is not None:
            try:
                cine_data = search_loader(q.strip(), page=page, subject_type=subject_type)
            except HTTPException:
                cine_data = None
            # Avoid up to six unnecessary Internal probes if a CineKivu VF was
            # already found. Internal is still used to fill missing French editions.
            internal_needed = not cine_data or not cine_data.get("french_editions")
            try:
                other_data = indexed_search(s, q.strip(), page=page, subject_type=subject_type) if internal_needed else None
            except Exception:
                if not cine_data:
                    raise HTTPException(502, detail="Les sources de recherche sont indisponibles")
                other_data = None
            if cine_data is None:
                if not other_data:
                    raise HTTPException(502, detail="Les sources de recherche sont indisponibles")
                data = other_data
            elif other_data is None:
                data = cine_data
                data["partial"] = cine_data.get("partial", False) or internal_needed
            else:
                # CineKivu titles are first, internal French-edition discovery enriches them.
                found = {item["subjectId"]: item for item in cine_data["items"]}
                for item in other_data.get("items", []):
                    found.setdefault(item["subjectId"], item)
                from cinekivu_bridge import language_rank
                ordered = sorted(found.values(), key=language_rank)
                fr_ids = {item["subjectId"] for item in (cine_data.get("french_editions", []) + other_data.get("french_editions", []))}
                data = {**other_data, "items": ordered,
                        "french_editions": [it for it in ordered if it["subjectId"] in fr_ids],
                        "searched_queries": list(dict.fromkeys(cine_data.get("searched_queries", []) + other_data.get("searched_queries", []))),
                        "partial": cine_data.get("partial", False) or other_data.get("partial", False),
                        "source": "cinekivu+internal"}
        else:
            data = indexed_search(s, q.strip(), page=page, subject_type=subject_type)
        if edition == "fr":
            data["items"] = data["french_editions"]
            data["edition_filter"] = "fr"
        return result(data, search_strategy="french-edition-expansion", audio_verified=False)

    @router.get("/titles/{subject_id}/editions")
    def title_editions(subject_id: str, session_id: Optional[str] = Cookie(None)):
        """Discover separate catalog entries; do not claim that streams contain VF."""
        s = session(session_id)
        sid = _id(subject_id)
        raw = _remote(s["content"].get_movie_detail(sid))
        source = raw.get("data") or {}
        if not isinstance(source, dict) or not source:
            raise HTTPException(404, detail="Title not found")
        current = mapper(source)
        if not current.get("title") or current["title"] == "Unknown":
            raise HTTPException(404, detail="Title unavailable")
        current["subjectId"] = sid
        current["edition"] = "fr" if is_french_edition(current["title"]) else "unknown"
        current["audioVerified"] = False
        editions = indexed_search(s, base_title(current["title"]), page=1)
        siblings = related_editions(editions["items"], current)
        return result({"current": current, "items": siblings,
                       "french_editions": [i for i in siblings if i["edition"] == "fr"],
                       "partial": editions["partial"], "searched_queries": editions["searched_queries"],
                       "audio_verified": False,
                       "note": "Les éditions sont identifiées par les noms du catalogue. La bande-son reste à vérifier."})

    @router.get("/suggestions")
    def suggestions(q: str = Query("", max_length=120), session_id: Optional[str] = Cookie(None)):
        """Legacy string-array contract retained; CineKivu-first, not Hindi rankings."""
        term = q.strip()
        if len(term) < 2:
            return result([], source="cinekivu")
        from cinekivu_discovery import cine_autocomplete, normalize_suggestions
        values: list[str] = []
        try:
            values = [item['query'] for item in cine_autocomplete(term)]
        except Exception:
            pass
        if len(values) < 8:
            try:
                provider = session(session_id)['content']
                parsed = normalize_suggestions(provider.get_search_autocomplete(term), term, 12)
                values += [item['query'] for item in parsed if item['query'] not in values]
            except Exception:
                pass
        return result(values[:12], source="cinekivu+internal")

    @router.get("/titles/{subject_id}/similar")
    def similar(subject_id: str, session_id: Optional[str] = Cookie(None)):
        """Backward-compatible title array, with CineKivu results preceding Internal."""
        sid = _id(subject_id)
        cine_items = []
        try:
            from cinekivu_discovery import cine_similar, _dedupe_titles
            cine_items = cine_similar(sid)
        except Exception:
            pass
        internal_items = []
        try:
            s = session(session_id)
            raw = _remote(s['content'].get_recommendations(sid))
            internal_items = _items(raw, mapper)
        except Exception:
            pass
        if not cine_items and not internal_items:
            try:
                raw = _remote(session(session_id)['content'].get_related_playback(sid))
                internal_items = _items(raw, mapper)
            except Exception:
                pass
        from cinekivu_discovery import _dedupe_titles
        return result({"items": _dedupe_titles(cine_items + internal_items, sid)})

    @router.get("/collections/{playlist_id}")
    def collection(playlist_id: str, page: int = Query(1, ge=1, le=200), session_id: Optional[str] = Cookie(None)):
        s = session(session_id)
        raw = _remote(s["content"].get_playlist_content(_id(playlist_id), page=page))
        return result({"playlist_id": playlist_id, "page": page, "items": _items(raw, mapper)})

    @router.get("/library")
    def get_library(session_id: Optional[str] = Cookie(None)):
        rows = db.all("SELECT subject_id, title, cover, subject_type, created_at FROM library WHERE sid=? ORDER BY created_at DESC", (key(session_id),))
        return result({"items": [{"subjectId": r["subject_id"], "id": r["subject_id"], "title": r["title"],
                                  "cover": r["cover"], "poster": r["cover"], "subjectType": r["subject_type"], "addedAt": r["created_at"]} for r in rows]}, "local")

    @router.put("/library/{subject_id}")
    def add_library(subject_id: str, item: TitleSnapshot, session_id: Optional[str] = Cookie(None)):
        subject_id = _id(subject_id)
        db.execute("INSERT INTO library(sid,subject_id,title,cover,subject_type,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(sid,subject_id) DO UPDATE SET title=excluded.title, cover=excluded.cover,subject_type=excluded.subject_type",
                   (key(session_id), subject_id, item.title, item.cover, item.subject_type, int(time.time())))
        return result({"subjectId": subject_id, "saved": True}, "local")

    @router.delete("/library/{subject_id}")
    def remove_library(subject_id: str, session_id: Optional[str] = Cookie(None)):
        db.execute("DELETE FROM library WHERE sid=? AND subject_id=?", (key(session_id), _id(subject_id)))
        return result({"subjectId": subject_id, "saved": False}, "local")

    @router.get("/progress/{subject_id}")
    def get_progress(subject_id: str, season: int = Query(1, ge=1), episode: int = Query(1, ge=1),
                     session_id: Optional[str] = Cookie(None)):
        row = db.one("SELECT subject_id,season,episode,position_seconds,duration_seconds,updated_at FROM progress WHERE sid=? AND subject_id=? AND season=? AND episode=?",
                     (key(session_id), _id(subject_id), season, episode))
        return result(row, "local")

    @router.put("/progress/{subject_id}")
    def put_progress(subject_id: str, entry: ProgressUpdate, session_id: Optional[str] = Cookie(None)):
        subject_id = _id(subject_id)
        position = min(entry.position_seconds, entry.duration_seconds) if entry.duration_seconds else entry.position_seconds
        db.execute("""INSERT INTO progress(sid,subject_id,season,episode,position_seconds,duration_seconds,title,cover,subject_type,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sid,subject_id,season,episode)
                   DO UPDATE SET position_seconds=excluded.position_seconds, duration_seconds=excluded.duration_seconds,
                   title=excluded.title,cover=excluded.cover,subject_type=excluded.subject_type,updated_at=excluded.updated_at""",
                   (key(session_id), subject_id, entry.season, entry.episode, position,
                    entry.duration_seconds, entry.title, entry.cover, entry.subject_type, int(time.time())))
        return result({"subjectId": subject_id, "position_seconds": position, "saved": True}, "local")

    @router.delete("/progress/{subject_id}")
    def clear_progress(subject_id: str, session_id: Optional[str] = Cookie(None)):
        db.execute("DELETE FROM progress WHERE sid=? AND subject_id=?", (key(session_id), _id(subject_id)))
        return result({"subjectId": subject_id, "deleted": True}, "local")

    @router.get("/continue-watching")
    def continue_watching(session_id: Optional[str] = Cookie(None)):
        rows = db.all("""SELECT subject_id, title, cover,subject_type,season,episode,position_seconds,duration_seconds,updated_at
                        FROM progress WHERE sid=? AND position_seconds>0 ORDER BY updated_at DESC LIMIT 80""", (key(session_id),))
        rows = [dict(r, progress_ratio=round(r["position_seconds"] / r["duration_seconds"], 4) if r["duration_seconds"] else 0)
                for r in rows if not r["duration_seconds"] or r["position_seconds"] < r["duration_seconds"] * .95]
        return result({"items": rows}, "local")

    @router.get("/history")
    def history(session_id: Optional[str] = Cookie(None)):
        rows = db.all("SELECT * FROM progress WHERE sid=? ORDER BY updated_at DESC LIMIT 100", (key(session_id),))
        return result({"items": rows}, "local")

    @router.get("/preferences")
    def preferences(session_id: Optional[str] = Cookie(None)):
        row = db.one("SELECT content FROM preferences WHERE sid=?", (key(session_id),))
        return result({**DEFAULT_PREFS, **(json.loads(row["content"]) if row else {})}, "local")

    @router.patch("/preferences")
    def patch_preferences(prefs: PreferencesUpdate, session_id: Optional[str] = Cookie(None)):
        sid = key(session_id)
        row = db.one("SELECT content FROM preferences WHERE sid=?", (sid,))
        updated = {**DEFAULT_PREFS, **(json.loads(row["content"]) if row else {}), **prefs.model_dump(exclude_none=True)}
        db.execute("INSERT INTO preferences(sid, content, updated_at) VALUES(?,?,?) ON CONFLICT(sid) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                   (sid, json.dumps(updated), int(time.time())))
        return result(updated, "local")

    app.include_router(router)
    return router
