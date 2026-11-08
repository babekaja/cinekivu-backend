from fastapi import FastAPI, HTTPException, Query, Response, Cookie, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import httpx
import requests
import urllib.request
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
import uvicorn
import os
import subprocess
import base64
import json
import asyncio
import logging
import uuid
import re
import hmac
import hashlib
from urllib.parse import quote
from moviebox_api import MovieBoxClient, MovieBoxAuth, MovieBoxContent, MovieBoxStream, MovieBoxUser

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CineKivu Streaming API", version="2.0.0", description="CineKivu-first + MovieBox Internal services")


@app.get("/")
def root_health():
    """Lightweight Render/Uvicorn health endpoint."""
    return {"status": "ok", "service": "cinekivu-api"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/health")
def api_health_check():
    """Keep the API health URL used by the Android client and local diagnostics."""
    from cinekivu_source.api import api_health
    return api_health()

# Enable CORS for local and deployed Wotchly frontends.
# Render/Netlify deployments have historically used both variable names, so
# treat FRONTEND_URL as a backwards-compatible alias for CORS_ORIGINS.
_allowed_origins = [
    origin.strip()
    for raw in (os.getenv("CORS_ORIGINS", ""), os.getenv("FRONTEND_URL", ""))
    for origin in raw.split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(_allowed_origins)),
    allow_credentials=True,
    allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?$",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition", "ETag", "Last-Modified"],
)

def _session_key() -> bytes:
    """Durable server-side secret so local per-browser state survives restarts."""
    value = os.getenv("MOVIEBOX_SESSION_SECRET")
    if value:
        if len(value) < 32:
            logger.warning("MOVIEBOX_SESSION_SECRET should contain at least 32 random characters")
        return value.encode("utf-8")
    db_dir = os.path.dirname(os.getenv("MOVIEBOX_DB_PATH", "data/moviebox_ott.sqlite3")) or "."
    os.makedirs(db_dir, exist_ok=True)
    key_file = os.path.join(db_dir, ".session_secret")
    try:
        fd = os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    except FileExistsError:
        pass
    with open(key_file, "rb") as f:
        return f.read()


_SESSION_KEY = _session_key()


def _sign_session(sid: str) -> str:
    digest = hmac.new(_SESSION_KEY, sid.encode(), hashlib.sha256).hexdigest()
    return f"{sid}.{digest}"


def _verify_session(cookie: Optional[str]) -> Optional[str]:
    if not cookie or "." not in cookie:
        return None
    sid, signature = cookie.split(".", 1)
    try:
        uuid.UUID(sid)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(_SESSION_KEY, sid.encode(), hashlib.sha256).hexdigest()
    return sid if hmac.compare_digest(signature, expected) else None


@app.middleware("http")
async def sticky_anonymous_session(request: Request, call_next):
    """Secure signed browser session ID, persisted across server restarts."""
    if (request.url.path in {"/", "/health", "/openapi.json", "/docs", "/redoc"}
            or request.url.path == '/api/v1/recommender'
            or request.url.path.startswith('/api/v1/recommender/')
            or request.url.path == '/api/v1/reference'
            or request.url.path.startswith('/api/v1/reference/')
            or request.url.path == '/api/v1/enrichment'
            or request.url.path.startswith('/api/v1/enrichment/')
            or request.url.path == '/api/v1/downloads'
            or request.url.path.startswith('/api/v1/downloads/')
            # Public source metadata and token-protected media do not consume
            # a personal OTT session. Avoid a second visitor login (and its
            # shared authentication lock) before every anonymous media request.
            or request.url.path == '/api/v1/cinekivu'
            or request.url.path.startswith('/api/v1/cinekivu/')
            or request.url.path.startswith(('/api/play/', '/api/adaptive/', '/api/download/'))):
        return await call_next(request)
    signed_cookie = request.cookies.get("session_id")
    sid = _verify_session(signed_cookie)
    if not sid or sid not in sessions:
        # A verified stale session is rehydrated with the same ID, preserving its SQLite data.
        session = await asyncio.to_thread(get_session, sid)
        sid = session["id"]
    existing = request.headers.get("cookie", "")
    existing = "; ".join(pair.strip() for pair in existing.split(";") if pair.strip() and pair.strip().split("=",1)[0] != "session_id")
    headers = [(k,v) for k,v in request.scope["headers"] if k.lower()!=b"cookie"]
    headers.append((b"cookie", (existing + "; session_id=" + sid).strip("; ").encode("latin-1")))
    request.scope["headers"] = headers
    response = await call_next(request)
    if request.url.path != "/logout":
        response.set_cookie("session_id", _sign_session(sid), httponly=True,
                            secure=request.url.scheme == "https", samesite="lax", max_age=30*86400)
    return response

# Multi-session management
# session_id -> {auth, client, content, stream, user}
sessions: Dict[str, Dict] = {}

HISTORY_FILE = "local_history.json"

def load_local_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load local history: {e}")
    return {"default": [], "blacklist": []}

def save_local_history(h: dict):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save local history: {e}")

def get_session(session_id: Optional[str] = None):
    # Check if session exists
    if session_id and session_id in sessions:
        return sessions[session_id]
        
    # Create new session
    sid = session_id or str(uuid.uuid4())
    auth = MovieBoxAuth()
    # Bootstrap a visitor token from MovieBoxEngine so all OTT requests
    # carry a valid bearer token from the start.
    try:
        from cinekivu_source.api import MovieBoxEngine
        token = MovieBoxEngine.get_instance().bootstrap_token()
        if token:
            auth.update_session(token)
    except Exception as exc:
        logger.warning('Auto-bootstrap token unavailable: %s', type(exc).__name__)
    client = MovieBoxClient(auth=auth)
    sessions[sid] = {
        "id": sid,
        "auth": auth,
        "client": client,
        "content": MovieBoxContent(client),
        "stream": MovieBoxStream(client),
        "user": MovieBoxUser(client)
    }
    logger.info(f"Created new session: {sid}")
    
    # CineKivu home/library do not require the Internal provider. Never block the
    # first page loading on a regional visitor-login request. Internal endpoints
    # still use the guest credentials returned dynamically by their provider.
    if os.getenv("CINEKIVU_EAGER_INTERNAL_LOGIN", "0") == "1":
        try:
            login_res = client.visitor_login()
            if login_res.get('status') != 'success':
                logger.warning('MovieBox Internal guest credentials are unavailable')
        except Exception as exc:
            logger.warning('MovieBox Internal guest login unavailable: %s', type(exc).__name__)

    return sessions[sid]

class LoginRequest(BaseModel):
    account: str = Field(pattern=r'^\S{3,254}$')
    password: str = Field(min_length=1, max_length=1024)
    authType: int = Field(1, ge=0, le=1)

class RegisterRequest(BaseModel):
    account: str = Field(pattern=r'^\S{3,254}$')
    password: str = Field(min_length=1, max_length=1024)
    otp: str = Field(pattern=r'^[A-Za-z0-9]{1,32}$')
    authType: int = Field(1, ge=0, le=1)

class OtpRequest(BaseModel):
    account: str = Field(pattern=r'^\S{3,254}$')
    authType: int = Field(1, ge=0, le=1)
    type: int = Field(1, ge=1, le=3) # Register, login, reset password

@app.post("/request-otp")
def request_otp(req: OtpRequest, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request_otp(req.account, req.authType, req.type)
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/login")
def login(req: LoginRequest, response: Response, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].login(req.account, req.password, req.authType)
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    
    # Always set/refresh the session cookie on login
    response.set_cookie(
        key="session_id", 
        value=s["id"], 
        httponly=True, 
        samesite="lax",
        max_age=3600 * 24 * 30 # 30 days
    )
    return res

@app.post("/register")
def register(req: RegisterRequest, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].register(req.account, req.password, req.otp, req.authType)
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/logout")
def logout(response: Response, session_id: Optional[str] = Cookie(None)):
    if session_id in sessions:
        sessions[session_id]["client"].logout()
        del sessions[session_id]
    response.delete_cookie("session_id")
    return {"status": "success"}

@app.get("/user-info")
def get_user_info(response: Response, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    
    # If no session_id was provided, set the newly created one
    if not session_id:
        response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")

    if s["auth"].is_guest_mode or not s["auth"].is_logged_in:
        return {"logged_in": False, "mode": "Guest Access", "session_id": s["id"]}
        
    return {
        "logged_in": True,
        "mode": "Official Account", 
        "user": s["auth"].user_info,
        "session_id": s["id"]
    }

def map_actor(actor: dict):
    avatar = actor.get("avatarUrl") or actor.get("avatar") or actor.get("photo") or actor.get("poster") or ""
    if isinstance(avatar, dict): avatar = avatar.get("url") or ""
    if isinstance(avatar, str) and avatar.startswith("//"): avatar = "https:" + avatar
    return {
        "name": actor.get("name") or actor.get("actorName") or "Unknown",
        "role": actor.get("character") or actor.get("role") or "Cast",
        "avatar": avatar
    }

def map_room(src: dict):
    """Maps Community/Room data to local format."""
    return {
        "id": str(src.get("groupId") or src.get("id")),
        "name": src.get("name") or "Community Room",
        "title": src.get("name") or "Community Room",
        "avatar": src.get("cover", {}).get("url") if isinstance(src.get("cover"), dict) else src.get("avatar") or "",
        "description": src.get("description") or "",
        "members": src.get("userCount") or 0,
        "posts": src.get("postCount") or 0,
        "tags": src.get("tags") or []
    }

def map_item(src: dict, depth: int = 0):
    if "subject" in src and isinstance(src["subject"], dict):
        item = src["subject"]
    else:
        item = src

    sid = str(item.get("subjectId") or item.get("id") or "")
    # Title mapping: favor specific names, then check wrappers
    title = (
        item.get("title") or 
        item.get("name") or 
        item.get("subjectName") or 
        item.get("subject_name") or
        item.get("categoryName") or
        item.get("content") or 
        item.get("keyword") or
        item.get("keywordName") or
        item.get("itemName") or
        item.get("show_name") or
        item.get("showTitle") or
        item.get("titleName") or
        item.get("title_en") or
        item.get("tag") or
        item.get("label") or
        item.get("extra") or
        item.get("subtitle") or
        item.get("tabName") or
        item.get("tab_name") or
        item.get("searchName") or
        item.get("promotionName") or
        src.get("title") or
        src.get("name") or
        src.get("content") or
        src.get("keyword") or
        src.get("label") or
        "Unknown"
    )
    
    # Final effort: try to infer from deepLink if still unknown
    dlink = str(item.get("deepLink") or src.get("deepLink") or "")
    action_type = "movie" # Default
    category_id = None
    
    if dlink:
        if "/home/category" in dlink:
            action_type = "category"
            if "categoryType=" in dlink:
                category_id = dlink.split("categoryType=")[1].split("&")[0]
                # Only use category_id as sid if item has no real subjectId of its own
                if not sid or sid == "0":
                    sid = category_id
        elif "/playlist/detail" in dlink:
            action_type = "playlist"
            if "playlistId=" in dlink:
                extracted_id = dlink.split("playlistId=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = extracted_id
        elif "/movie/detail" in dlink:
            action_type = "movie"
            if "subjectId=" in dlink:
                extracted_id = dlink.split("subjectId=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = extracted_id
        elif "/actor/" in dlink:
            action_type = "actor"
            if "actorId=" in dlink:
                extracted_id = dlink.split("actorId=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = extracted_id
        elif "/post/" in dlink or "detailVideo" in dlink:
            action_type = "post"
            if "id=" in dlink:
                # Extract the post ID from the deepLink
                extracted_id = dlink.split("id=")[1].split("&")[0]
                if sid == "0" or not sid:
                    sid = extracted_id

    if title == "Unknown":
        pass
    
    poster = item.get("poster")
    poster_url = ""
    if isinstance(poster, dict): poster_url = poster.get("url")
    elif isinstance(poster, str): poster_url = poster
    
    if not poster_url:
        cover = item.get("cover")
        poster_url = cover.get("url") if isinstance(cover, dict) else cover

    # Aggressive Deep Search for Images
    if not poster_url:
        img_terms = ["image", "img", "thumb", "thumbnail", "poster", "cover", "icon", "banner", "pic", "picture"]
        # Priority 1: Check standard nested objects
        for term in img_terms:
            val = item.get(term)
            if isinstance(val, dict) and val.get("url"):
                poster_url = val.get("url")
                break
            elif isinstance(val, str) and (val.startswith("http") or val.startswith("//")):
                poster_url = val
                break
        
        # Priority 2: Check keys with _url or _path suffix
        if not poster_url:
            for k, v in item.items():
                if any(t in k.lower() for t in img_terms) and isinstance(v, str) and (v.startswith("http") or v.startswith("//")):
                    poster_url = v
                    break

    if not poster_url:
        hp = item.get("horizontalPoster") or item.get("horizontalCover")
        poster_url = hp.get("url") if isinstance(hp, dict) else hp

    if not poster_url:
        # Final fallback - check for a banner object
        banner = item.get("banner")
        if isinstance(banner, dict):
            poster_url = banner.get("image", {}).get("url") or banner.get("url")

    if isinstance(poster_url, str) and poster_url.startswith("//"):
        poster_url = "https:" + poster_url

    score = item.get("imdbRatingValue") or item.get("imdbRate") or item.get("starRating") or item.get("score") or "N/A"
    
    # Year mapping fix using official releaseDate field
    release_date = item.get("releaseDate") or item.get("releaseTime") or item.get("year") or ""
    display_year = str(release_date)[:4] if release_date and len(str(release_date)) >= 4 else "N/A"

    runtime = item.get("duration") or item.get("runtime") or item.get("minute")
    if isinstance(runtime, int): runtime = f"{runtime}m"

    return {
        "subjectId": sid,
        "id": sid,
        "title": title,
        "cover": poster_url,
        "poster": poster_url,
        "score": str(score),
        "language": item.get("language") or item.get("lanName") or "",
        "releaseTime": display_year,
        "subjectType": item.get("subjectType") or item.get("type") or item.get("subject_type") or (2 if item.get("episodeCount") or item.get("seasonCount") else 1),
        "runtime": runtime,
        "season": item.get("season"),
        "episode": item.get("episode") or item.get("ep"),
        "seeTime": item.get("seeTime"),
        "seenStatus": item.get("seenStatus"),
        # Map ALL variations of favorite/like status across ALL regional API versions
        # Standard: isFavorite, isLike
        # Legacy: collected, collectedStatus, is_favorite, fav, is_fav, isCollect
        "likeStatus": 1 if (
            item.get("isFavorite") == 1 or 
            item.get("is_favorite") == 1 or
            item.get("fav") == 1 or
            item.get("is_fav") == 1 or
            item.get("isLike") == 1 or 
            item.get("is_like") == 1 or
            item.get("wantToSee") == 1 or
            item.get("likeStatus") == 1 or
            item.get("collected") == 1 or
            item.get("isCollect") == 1 or
            item.get("collectedStatus") == 1 or
            str(item.get("likeType")) == "1" or
            item.get("isCollect") is True or
            item.get("isFavorite") is True
        ) else 0,
        "description": item.get("description") or "",
        "actionType": action_type,
        "categoryId": category_id,
        "deepLink": dlink,
        "isPost": action_type == "post",
        "streamUrl": item.get("playUrl", {}).get("playUrl") if isinstance(item.get("playUrl"), dict) else None,
        "isCollection": item.get("isCollection", False),
        "items": item.get("items", [])
    }

@app.get("/home")
def get_home(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        # Category ID 1 corresponds to "Trending", which is the primary "Home" feed in the app.
        res = s["content"].get_categories(category_id=1, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        
        # We can format it identically to all other robust sections we built
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Home error: {e}")
        return {"code": 500, "message": str(e), "data": {"list": []}}

@app.get("/anime")
def get_anime(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=8, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Anime error: {e}")
        return {"code": 1, "data": []}

@app.get("/rankings")
def get_rankings(response: Response, tabId: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    # Ensure session persists even for guest GET requests
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    # The official path from Smali class zm/c is /wefeed-mobile-bff/tab/ranking-list
    variants = ["/wefeed-mobile-bff/tab/ranking-list", "/tab/ranking-list", "/subject-api/ranking-list"]
    
    for v in variants:
        try:
            res = s["content"].get_rankings(v, tab_id=tabId)
            logger.info(f"RANKINGS RAW ({v}): {json.dumps(res)}")
            data = res.get("data")
            if not data: continue
            
            formatted = []
            # RankAllData format: 'subjects' contains the items
            if "subjects" in data and isinstance(data["subjects"], list):
                items = data["subjects"]
                if items:
                    formatted.append({"title": "Top Rankings", "items": [map_item(i) for i in items[:10]]})
            else:
                lists = data.get("lists") or data.get("list") 
                if isinstance(lists, list):
                    for l in lists:
                        if not isinstance(l, dict): continue
                        title = l.get("name") or l.get("title") or "Rankings"
                        items = l.get("items") or l.get("list") or []
                        if items:
                            formatted.append({"title": title, "items": [map_item(i) for i in items[:10]]})
            
            if formatted: return {"code": 0, "data": formatted}
        except Exception as e:
            logger.error(f"Error parsing rankings variant {v}: {e}")
            continue
        
    return {"code": 0, "data": []}

@app.get("/discovery")
def get_discovery(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_discovery()
        logger.info(f"DISCOVERY RAW: {json.dumps(res)[:1000]}")
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or []
        return {"code": 0, "data": [map_item(i) for i in items[:20]]}
    except Exception as e:
        logger.error(f"Discovery error: {e}")
        return {"code": 1, "data": []}

def _extract_items(value, depth: int = 0) -> list:
    """Extract content from the different envelopes returned by MovieBox regions."""
    if value is None or depth > 6:
        return []
    if isinstance(value, list):
        flattened = []
        for item in value:
            if isinstance(item, dict) and any(isinstance(item.get(k), (list, dict)) for k in ("items", "list", "subjects", "movieList", "data")):
                nested = next((item.get(k) for k in ("items", "list", "subjects", "movieList", "data") if item.get(k) is not None), None)
                flattened.extend(_extract_items(nested, depth + 1) or [item])
            else:
                flattened.append(item)
        return flattened
    if isinstance(value, dict):
        for key in ("items", "list", "subjects", "movieList", "results", "data"):
            if key in value:
                items = _extract_items(value[key], depth + 1)
                if items:
                    return items
    return []

@app.get("/trending")
def get_trending(response: Response, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    try:
        # MovieBox has moved the guest home feed between BFF endpoints. Try
        # known contracts and return the first non-empty catalogue.
        candidates = [
            s["content"].get_trending(),
            s["content"].get_home_list(category_id=1),
            s["content"].get_categories(category_id=1, page=1),
        ]
        for res in candidates:
            logger.info("TRENDING RAW: %s", json.dumps(res, default=str)[:1200])
            items = _extract_items(res)
            mapped = [map_item(i) for i in items[:40] if isinstance(i, dict)]
            if mapped:
                return {"code": 0, "data": mapped}
        return {"code": 0, "data": [], "meta": {"source": "moviebox", "empty": True}}
    except Exception as e:
        logger.exception("Trending upstream error")
        raise HTTPException(status_code=502, detail=f"MovieBox trending unavailable: {e}")

@app.get("/movies")
def get_movies(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=2, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.exception("Movies upstream error")
        raise HTTPException(status_code=502, detail=f"MovieBox movies unavailable: {e}")

def format_tab_sections(items: list):
    sections = []
    
    # Catch-all if it's already a direct movie list (no inner sections)
    is_direct_movies = True
    for row in items:
        if isinstance(row, dict) and (row.get("list") or row.get("items") or row.get("subjects") or row.get("movieList") or row.get("customData") or row.get("banner")):
            is_direct_movies = False
            break
            
    if is_direct_movies and items:
         mapped = [map_item(m) for m in items if m.get("subjectId") or m.get("id")]
         if mapped: return [{"title": "Content", "items": mapped}]

    for row in items:
        if not isinstance(row, dict): continue
        title = row.get("title") or row.get("name") or "Section"
        
        inner = []
        if isinstance(row.get("list"), list) and row.get("list"): inner = row.get("list")
        elif isinstance(row.get("items"), list) and row.get("items"): inner = row.get("items")
        elif isinstance(row.get("subjects"), list) and row.get("subjects"): inner = row.get("subjects")
        elif isinstance(row.get("movieList"), list) and row.get("movieList"): inner = row.get("movieList")
        elif isinstance(row.get("customData"), dict) and isinstance(row["customData"].get("items"), list) and row["customData"]["items"]:
            inner = row["customData"]["items"]
        elif isinstance(row.get("banner"), dict) and isinstance(row["banner"].get("banners"), list) and row["banner"]["banners"]:
            inner = row["banner"]["banners"]
            
        real_movies = []
        for i in inner:
            if not isinstance(i, dict): continue
            if isinstance(i.get("subject"), dict):
                 real_movies.append(i["subject"])
            elif i.get("subjectId") or i.get("id"):
                 real_movies.append(i)
        
        if real_movies:
            mapped = [map_item(m) for m in real_movies]
            sections.append({
                "title": title,
                "type": row.get("subjectType") or row.get("type") or "SUBJECTS_MOVIE",
                "items": mapped
            })
    return sections

@app.get("/short-tv")
def get_short_tv(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=13, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"ShortTV error: {e}")
        return {"code": 1, "data": []}

@app.get("/kids")
def get_kids(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=23, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Kids error: {e}")
        return {"code": 1, "data": []}

@app.get("/education")
def get_education(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=3, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Education error: {e}")
        return {"code": 1, "data": []}

@app.get("/music")
def get_music(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=4, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Music error: {e}")
        return {"code": 1, "data": []}

@app.get("/asian")
def get_asian(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=18, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Asian error: {e}")
        return {"code": 1, "data": []}

@app.get("/western")
def get_western(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=19, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Western error: {e}")
        return {"code": 1, "data": []}

@app.get("/nollywood")
def get_nollywood(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        # Reverting to tab-operating GET vertical for tabId 28
        res = s["content"].get_categories(category_id=28, page=page)
        if not isinstance(res, dict):
             return {"code": 1, "message": "Invalid response format", "data": {"list": []}}
        
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        
        # Log detected items to verify if we are getting Home content
        if items:
            sample = items[0].get('name') or items[0].get('title') or "Unknown"
            logger.info(f"Nollywood Detection: First item is '{sample}'")
            
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Nollywood error: {e}")
        return {"code": 1, "message": str(e), "data": {"list": []}}

@app.get("/tab/{tab_id}")
def get_tab_generic(tab_id: int, page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=tab_id, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Generic Tab {tab_id} error: {e}")
        return {"code": 1, "message": str(e), "data": {"list": []}}



@app.get("/game")
def get_game(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        # Reverting to tab-operating GET vertical for tabId 11
        res = s["content"].get_categories(category_id=11, page=page)
        if not isinstance(res, dict):
             return {"code": 1, "message": "Invalid response format", "data": {"list": []}}
             
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        
        if items:
            sample = items[0].get('name') or items[0].get('title') or "Unknown"
            logger.info(f"Game Detection: First item is '{sample}'")
            
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Game error: {e}")
        return {"code": 1, "message": str(e), "data": {"list": []}}

@app.get("/search-suggestions")
def get_search_suggestions(response: Response, q: Optional[str] = None, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    # Ensure session persists even for guest GET requests
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    try:
        if q:
            from cinekivu_discovery import cine_autocomplete
            values = cine_autocomplete(q.strip())
            return {"code": 0, "data": list(dict.fromkeys(
                row.get('title') or row.get('query') for row in values
                if row.get('title') or row.get('query')))}
        else:
            res = s["content"].get_search_suggestions()
            logger.info(f"SEARCH SUGGESTIONS RAW: {json.dumps(res)}")
            data = res.get("data") if isinstance(res, dict) else {}
            if not isinstance(data, dict):
                return {"code": 0, "data": []}
            items = data.get("list") or data.get("items") or data.get("movie") or data.get("subjects") or []
            
        suggestions = []
        for i in items:
            if isinstance(i, str):
                suggestions.append(i)
            elif isinstance(i, dict):
                suggestions.append(i.get("keyword") or i.get("title") or i.get("name"))
        
        return {"code": 0, "data": [s for s in suggestions if s]}
    except Exception as e:
        logger.error(f"Search suggestions error: {e}")
        return {"code": 0, "data": []}

@app.get("/search")
def search(response: Response, q: str, page: int = 1, subject_type: int = 0, type: Optional[int] = None, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    try:
        if not q.strip():
            return {"code": 0, "data": {"items": []}}
        effective_type = type if type is not None else subject_type
        from cinekivu_bridge import cine_search
        data = cine_search(q.strip(), page=page, subject_type=effective_type)
        return {"code": 0, "data": data}
    except Exception as e:
        logger.exception("Search upstream error for %s", q)
        raise HTTPException(status_code=502, detail=f"MovieBox search unavailable: {e}")

@app.get("/rooms/recommend")
def get_rooms(page: int = Query(1, ge=1, le=200), session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        from moviebox_api.ott import _remote
        res = _remote(s["content"].get_rooms(page=page))
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or []
        return {"code": 0, "data": [map_room(r) for r in items], 'source': 'native-trending-communities'}
    except Exception as e:
        raise HTTPException(503, 'Recommandations de communautés indisponibles chez le fournisseur') from e

@app.get("/rooms/{room_id}")
def get_room_detail(room_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        from moviebox_api.ott import _remote
        res = _remote(s["content"].get_room_detail(room_id))
        data = res.get("data") or {}
        return {"code": 0, "data": map_room(data)}
    except Exception as e:
        raise HTTPException(503, 'Communauté indisponible chez le fournisseur') from e


@app.get("/detail/{subject_id}")
def get_detail(subject_id: str, depth: int = 0, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["content"].get_movie_detail(subject_id)
    data = res.get("data", {})
    
    is_collection = False
    items = []

    # 1. Check if it's a category ID (If standard detail fails or has no title)
    if not data or not (data.get("title") or data.get("name")):
        try:
           # Try as post detail first
           post_res = s["client"].request("GET", "/wefeed-mobile-bff/post/get", params={"postId": subject_id})
           post_data = post_res.get("data", {})
           if post_data and post_data.get("postId"):
               media = post_data.get("media") or {}
               video_url = ""
               if media.get("mediaType") == "VIDEO":
                   video_list = media.get("video") or []
                   if video_list and len(video_list) > 0:
                       video_url = video_list[0].get("url") or video_list[0].get("playUrl") or ""
                   elif media.get("url"):
                       video_url = media.get("url")
                       
               # Synthesize a "Movie" object for the post
               data = {
                   "subjectId": subject_id,
                   "title": "Community Post",
                   "description": post_data.get("content") or post_data.get("text") or "",
                   "isPost": True,
                   "streamUrl": video_url,
                   "poster": media.get("cover", {}).get("url") if isinstance(media.get("cover"), dict) else None,
                   "source": f"@{post_data.get('user', {}).get('nickname', 'User')}"
               }
        except: pass

        if not data or not (data.get("title") or data.get("name")):
            try:
               cat_res = s["client"].request(
                   "POST",
                   "/wefeed-mobile-bff/subject-api/genre-top",
                   data={"type": subject_id, "page": 1, "perPage": 50}
               )
               cat_data = cat_res.get("data", {})
               desc = cat_data.get("desc") or "Curated Collection"
               items = cat_data.get("list") or cat_data.get("items") or cat_data.get("subjects") or []
               
               # Unbox nested info objects from genre-top response
               raw_items = [i.get("info") if isinstance(i, dict) and i.get("info") else i for i in items]
               mapped_items = [map_item(m) for m in raw_items]
               if mapped_items:
                   items = mapped_items  # update outer items variable for collectionItems
                   is_collection = True
                   # Synthesize a "Movie" object for the collection
                   data = {
                       "subjectId": subject_id,
                       "title": desc,
                       "isCollection": True,
                       "items": mapped_items
                   }
            except: pass

    if not data: return {"code": 1, "msg": "Not found"}
    
    # Logic to force status sync: Check the actual list if cloud detail is stale
    is_fav = False
    if not s["auth"].is_guest_mode:
        try:
            wl_res = s["user"].get_watchlist(page=1, per_page=50) # Check first page of favorites
            wl_items = wl_res.get("data", {}).get("items") or wl_res.get("data", {}).get("list") or []
            for item in wl_items:
                if str(item.get("subject_id") or item.get("id") or item.get("subjectId")) == str(subject_id):
                    is_fav = True
                    break
        except: pass

    if is_fav:
        data["isFavorite"] = 1 # Force it

    # Existing field cross-check
    status_fields = ["isFavorite", "is_favorite", "fav", "is_fav", "collected", "isLike", "wantToSee", "likeStatus"]
    for f in status_fields:
        if f in res and f not in data:
            data[f] = res[f]

    # Fetch Community Post Count
    try:
        post_res = s["client"].request("GET", "/wefeed-mobile-bff/post/count/subject", params={"subjectId": subject_id})
        data["postCount"] = post_res.get("data", {}).get("count") or "0"
    except:
        data["postCount"] = "0"

    mapped = map_item(data, depth=depth)
    mapped["postCount"] = data.get("postCount", "0")
    mapped["isCollection"] = is_collection
    
    # Restore synthesized post fields that map_item might have overwritten
    if data.get("isPost"):
        mapped["isPost"] = True
        mapped["streamUrl"] = data.get("streamUrl")
        mapped["source"] = data.get("source")
    
    if is_collection and depth == 0:
        mapped["collectionItems"] = [map_item(i, depth=depth+1) for i in items[:24]]
        # Use first item's poster as collection poster
        if items and not mapped.get("poster"):
            first = map_item(items[0], depth=depth+1)
            mapped["poster"] = first.get("poster")
            mapped["cover"] = first.get("cover")

    raw_cast = data.get("staffList") or data.get("actorList") or []
    mapped["cast"] = [map_actor(a) for a in raw_cast]
    
    # Fetch Available Languages / Dubs (Consolidated)
    all_languages = []
    
    # 1. Check for Dubs list (Versions linked via different Subject IDs)
    raw_dubs = data.get("dubs") or []
    for dub in raw_dubs:
        all_languages.append({
            "id": None, 
            "subjectId": dub.get("subjectId"), 
            "name": dub.get("lanName") or "Custom Dub",
            "type": "dub"
        })
        
    # 2. Check for Resource Detectors (Tracks within the same/similar ID)
    try:
        det_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id})
        detectors = (det_res.get('data') or {}).get('resourceDetectors') or []
        for d in detectors:
            # Avoid duplicates if we already have it link-wise
            d_name = d.get("name") or "Resource"
            all_languages.append({
                "id": d.get("resourceId"),
                "subjectId": subject_id, 
                "name": d_name,
                "type": "resource"
            })
    except:
        pass
        
    mapped["languages"] = all_languages
    return {"code": 0, "data": mapped}

@app.get("/episodes/{series_id}")
def get_episodes(series_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["content"].get_episode_list(series_id)
    logger.info(f"EPISODES CLOUD RAW: {json.dumps(res)[:2000]}")
    data = res.get("data") or {}
    # Handle data wrapper vs top-level
    raw_seasons = data.get("seasons") or res.get("seasons") or []
    if not raw_seasons:
        # Check deep nested fields if first layer failed
        raw_seasons = data.get("seasonList") or data.get("list") or []
        
    mapped_seasons = []
    for s_raw in raw_seasons:
        num = s_raw.get("se") or s_raw.get("seasonNumber") or 1
        eps = []
        
        # Check all possible list/string fields for episodes
        for key in ["allEp", "epList", "episodeList", "episodes", "list", "items"]:
            pool = s_raw.get(key)
            if not pool: continue
            
            if isinstance(pool, str):
                for e_num in pool.split(","):
                    if e_num: eps.append({"episodeNumber": e_num, "title": f"Episode {e_num}", "id": f"{series_id}_{num}_{e_num}"})
            elif isinstance(pool, list):
                for item in pool:
                    if isinstance(item, dict):
                        en = item.get("ep") or item.get("episodeNumber") or item.get("episode_number")
                        if en: eps.append({"episodeNumber": str(en), "title": item.get("title") or f"Episode {en}", "id": f"{series_id}_{num}_{en}"})
                    else:
                        eps.append({"episodeNumber": str(item), "title": f"Episode {item}", "id": f"{series_id}_{num}_{item}"})
            
            if eps: break # Stop once we found a pool

        # FALLBACK: If no explicit pool found, but maxEp is set, generate sequence [1..maxEp]
        if not eps:
            max_ep = s_raw.get("maxEp") or s_raw.get("max_ep") or 0
            if isinstance(max_ep, str) and max_ep.isdigit(): max_ep = int(max_ep)
            if max_ep and isinstance(max_ep, int):
                for i in range(1, max_ep + 1):
                    eps.append({"episodeNumber": str(i), "title": f"Episode {i}", "id": f"{series_id}_{num}_{i}"})

        if eps:
            mapped_seasons.append({"seasonNumber": num, "episodes": eps})
    return {"code": 0, "data": {"seasons": mapped_seasons}}

def _get_stream_raw(subject_id: str, season: int = 1, episode: int = 1, quality: Optional[str] = "720p", resource_id: Optional[str] = None, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    # 0. Detect subject type to avoid se/ep for movies
    try:
        subject_detail = s["content"].get_movie_detail(subject_id).get("data") or {}
        is_movie = (str(subject_detail.get("subjectType") or subject_detail.get("type")) == "1")
    except: is_movie = False

    logger.info(f"RESOLVING STREAM: ID={subject_id} Type={'Movie' if is_movie else 'Series'} Resource={resource_id}")
    
    # If it's a movie, se and ep MUST be None for the official play-info API to respond correctly in some regions
    res_se = None if is_movie else season
    res_ep = None if is_movie else episode
    

    # Helper: Detect fake/dummy upgrade notice clips (anti-piracy trap)
    def is_deprecation_notice_url(u: str) -> bool:
        if not u: return False
        lower = u.lower()
        return (
            "notice.mp4" in lower
            or ("macdn.aoneroom.com" in lower and "/other/" in lower)
            or "1c7de0bd3393702d9191801f15f88f8d" in lower
            or "9a0461bc39da389663bf3dbb17091d3f" in lower
            or "80da52722ee7de7a4303290af6783424" in lower
            or "b164fbfb4347792950bdfbfb563d39d9" in lower
        )

    # 1. Primary: Try play-info/v2, then fallback to play-info
    res = s["stream"].get_play_info_v2(subject_id, season=res_se, episode=res_ep)
    data = res.get("data", {}) if isinstance(res, dict) else {}
    streams = data.get("streams") or data.get("streamList") or []
    
    if not streams:
        logger.info("play-info/v2 empty, falling back to legacy play-info...")
        res = s["stream"].get_play_info(subject_id, season=res_se, episode=res_ep, resource_id=resource_id)
        data = res.get("data", {}) if isinstance(res, dict) else {}
        streams = data.get("streamList") or data.get("streams") or []
    
    logger.info(f"PLAY-INFO RESPONSE: code={res.get('code')}, found {len(streams)} streams")

    # 2. Episode Resource Mirrors via /resource/v2 with isVip=true spoofing
    try:
        r2_se = 0 if is_movie else season
        r2_ep = 0 if is_movie else episode
        res2 = s["stream"].get_resources_v2(subject_id, season=r2_se, episode=r2_ep)
        r2_items = res2.get("data", {}).get("list") or []
        for r_item in r2_items:
            r_link = r_item.get("resourceLink") or r_item.get("downloadUrl") or r_item.get("url")
            if r_link:
                streams.append({
                    "url": r_link,
                    "quality": f"{r_item.get('resolution')}p" if r_item.get('resolution') else "Auto",
                    "signCookie": r_item.get("signCookie") or "",
                    "codec": r_item.get("codecName") or "",
                    "id": str(r_item.get("resourceId") or ""),
                    "duration": r_item.get("duration") or 0
                })
        if r2_items:
            logger.info(f"Appended {len(r2_items)} mirror resources from /resource/v2 (isVip=true)")
    except Exception as e:
        logger.warning(f"Failed to fetch /resource/v2 mirrors: {e}")

    # 3. Detect anti-piracy trap clips and recover original media file via /resource-position/v2
    recovered_streams = []
    for _st in streams:
        st_url = _st.get("url", "")
        st_rid = str(_st.get("id") or "")
        if is_deprecation_notice_url(st_url) and st_rid:
            logger.info(f"Detected anti-piracy dummy video clip for resource {st_rid}. Engaging /resource-position/v2 failover...")
            try:
                rec_res = s["stream"].recover_resource_position_v2(subject_id, st_rid, st_url)
                rec_list = rec_res.get("data", {}).get("list") or []
                if rec_list:
                    rec_item = rec_list[0]
                    real_link = rec_item.get("resourceLink") or rec_item.get("url")
                    if real_link and not is_deprecation_notice_url(real_link):
                        logger.info(f"FAILOVER SUCCESS: Recovered real stream link: {real_link[:60]}...")
                        recovered_streams.append({
                            "url": real_link,
                            "quality": f"{rec_item.get('resolution')}p" if rec_item.get('resolution') else _st.get("quality", "Auto"),
                            "signCookie": rec_item.get("signCookie") or _st.get("signCookie") or "",
                            "codec": rec_item.get("codecName") or _st.get("codec", ""),
                            "id": str(rec_item.get("resourceId") or st_rid),
                            "duration": rec_item.get("duration") or _st.get("duration", 0)
                        })
            except Exception as re_err:
                logger.warning(f"Recovery via /resource-position/v2 failed: {re_err}")
        elif not is_deprecation_notice_url(st_url):
            recovered_streams.append(_st)

    if recovered_streams:
        streams = recovered_streams

    # Fallback to resourceDetectors inside movie/show detail if no streams found
    if not streams:
        logger.info(f"No streams yet. Checking resourceDetectors for subject {subject_id}...")
        try:
            detectors = subject_detail.get("resourceDetectors") or []
            for det in detectors:
                if resource_id and str(det.get("resourceId")) != str(resource_id):
                    continue
                res_list = det.get("resolutionList") or []
                for res_item in res_list:
                    if not is_movie:
                        item_se = res_item.get("se") or res_item.get("season")
                        item_ep = res_item.get("ep") or res_item.get("episode")
                        if item_se is not None and item_ep is not None:
                            if int(item_se) != int(season) or int(item_ep) != int(episode):
                                continue
                    stream_url = res_item.get("resourceLink") or res_item.get("downloadUrl")
                    if stream_url and not is_deprecation_notice_url(stream_url):
                        streams.append({
                            "url": stream_url,
                            "quality": f"{res_item.get('resolution')}p" if res_item.get("resolution") else "Auto",
                            "signCookie": det.get("signCookie") or res_item.get("signCookie") or "",
                            "codec": res_item.get("codecName") or det.get("codecName") or "",
                            "id": res_item.get("resourceId") or det.get("resourceId") or "",
                            "duration": res_item.get("duration") or det.get("duration") or 0
                        })
            if streams:
                logger.info(f"Recovered {len(streams)} streams directly from details resourceDetectors.")
        except Exception as e:
            logger.error(f"Error extracting from resourceDetectors: {e}")
            
    # signCookie can be in root, in data, or in the session cookies
    # FALLBACK: If all else fails, the signCookie is often just the user token
    global_cookie = res.get("signCookie") or data.get("signCookie") or s["client"].session.cookies.get("signCookie") or s["auth"].token
    logger.info(f"Phase 1 - Primary Result: {len(streams)} streams found (code: {res.get('code')}) (Cookie: {'YES' if global_cookie else 'NO'})")
    import requests
    
    # Silent Failover Logic - Mobile Handshake Enforcement
    working_stream = None
    working_cookie = None
    
    # Normalize quality for official API (expects lowercase)
    official_quality = quality.lower() if quality else "720p"
    
    # Try current stream list first (Strict Codec Enforcement)
    # Browsers generally FAIL to play H.265 (HEVC) or complex HEV1 DASH manifests.
    # We strictly prioritize H.264 (AVC) MP4 > HLS > (Anything else).
    
    def prioritize_h264(st):
        u = st.get("url", "").lower()
        if "h265" in u or "x265" in u or "hev1" in u: return 10 # Very low priority
        if ".mp4" in u: return 0  # Highest priority
        if ".m3u8" in u: return 1 # Good priority
        return 5 # DASH is risky but better than H265
    
    prioritized_streams = sorted(streams, key=prioritize_h264)

    # Pass 1: Try to find a playable non-HEVC stream
    for st in prioritized_streams:
        url = st.get("url")
        if not url: continue
        if any(bad in url.lower() for bad in ["h265", "x265", "hev1"]):
            continue
        cookie = st.get("signCookie") or global_cookie or ""
        try:
            head_res = requests.head(url, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}, timeout=3, verify=True)
            if head_res.status_code in [200, 206, 302]:
                working_stream = st
                working_cookie = cookie
                break
        except: continue

    # Pass 2: Fallback to playable HEVC stream if no non-HEVC stream is available
    if not working_stream:
        for st in prioritized_streams:
            url = st.get("url")
            if not url: continue
            cookie = st.get("signCookie") or global_cookie or ""
            try:
                head_res = requests.head(url, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}, timeout=3, verify=True)
                if head_res.status_code in [200, 206, 302]:
                    logger.info(f"Using fallback H265/HEVC stream: {url}")
                    working_stream = st
                    working_cookie = cookie
                    break
            except: continue
    subtitles_source = data.get("subTitleList", [])
    
    # PROIRITY: Resource Mirrors (UGC/Dubs like eyosi_as_iam)
    if not working_stream:
        logger.info(f"Primary Cloud Offline for {subject_id}. Engaing Resource Mirror Rotation...")
        try:
            hdrs = {
                "User-Agent": "MovieBox/4.0.02 (Android 14; Pixel 6)",
                "X-M-Version": "4.0.02"
            }
            
            # PHASE 1: Resource Discovery via Metadata (High Parity with Subtitles)
            r_ids = []
            try:
                det_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id}, headers=hdrs)
                logger.info(f"Metadata Probe Result: {det_res.get('code')} - {len(det_res.get('data', {}).get('resourceDetectors', []))} detectors found")
                detectors = (det_res.get('data') or {}).get('resourceDetectors') or []
                for d in detectors:
                    if d.get('resourceId'): r_ids.append(d.get('resourceId'))
            except Exception as ex: 
                logger.error(f"Metadata Probe Crash: {ex}")

            # PHASE 2: Fallback to See-List (UGC Discoverability)
            if not r_ids:
                try:
                    res_list = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/see-list-v2', params={'subjectId': subject_id, 'page': 1, 'pageSize': 20, 'seeType': 1}, headers=hdrs)
                    logger.info(f"See-List Probe Result: {res_list.get('code')}")
                    r_items = (res_list.get('data') or {}).get('items') or []
                    for item in r_items:
                        if item.get('id'): r_ids.append(item.get('id'))
                except: pass

            # PHASE 3: Iterative Recovery
            for r_id in r_ids:
                logger.info(f"Probing Resource: {r_id} for {subject_id}")
                # USE BOTH FULL AND ABBREVIATED PARAMS
                # ALSO PROBE WITHOUT QUALITY (UGC often fails if quality mismatch)
                p_params = {
                    'subjectId': subject_id, 
                    'resourceId': r_id
                }
                if not is_movie:
                    p_params.update({'se': season, 'ep': episode, 'season': season, 'episode': episode})
                
                # First attempt: With quality
                q_params = p_params.copy()
                q_params['quality'] = official_quality
                p_info = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/play-info', params=q_params, headers=hdrs)
                p_data = p_info.get('data') or {}
                p_streams = p_data.get('streamList') or p_data.get('streams') or []
                
                # Second attempt: Without quality (Discovery mode)
                if not p_streams:
                    logger.info(f"Retrying probe {r_id} without quality constraint...")
                    p_info = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/play-info', params=p_params, headers=hdrs)
                    p_data = p_info.get('data') or {}
                    p_streams = p_data.get('streamList') or p_data.get('streams') or []

                logger.info(f"Probe {r_id} Result: {len(p_streams)} streams recovered")
                if p_streams:
                    working_stream = p_streams[0]
                    
                    # PROACTIVE PING: Force a session handshake with the media CDN
                    if "hakunaymatata" in working_stream.get("url", ""):
                        try:
                            import requests
                            logger.info(f"Ping session for CDN handshake: {working_stream.get('url')[:60]}")
                            # Matches Android okhttp behavior: HEAD with timeout
                            requests.head(working_stream.get("url"), headers={"User-Agent": "ExoPlayerLib/2.19.1"}, timeout=3, cookies=s["client"].session.cookies)
                        except: pass

                    # DEEP CAPTURE: Check body, then individual stream, then session cookies, then fallback
                    working_cookie = (
                        p_info.get('signCookie') or 
                        p_data.get('signCookie') or 
                        working_stream.get('signCookie') or # EMBEDDED IN STREAM OBJECT (CRITICAL)
                        s["client"].session.cookies.get("signCookie") or 
                        global_cookie or ""
                    )
                    
                    # If still NO cookie for a protected CDN, we have a failover issue
                    if not working_cookie and "hakunaymatata" in working_stream.get("url", ""):
                        logger.warning("PROTECTED CDN DETECTED WITH NO COOKIE AFTER PING! Attempting session flush...")
                        working_cookie = s["client"].session.cookies.get("signCookie") or ""

                    subtitles_source = p_data.get("subTitleList", []) or subtitles_source
                    logger.info(f"RESOURCE MIRROR SUCCESS: Recovered stream via User-Resource {r_id} (Cookie: {'YES' if working_cookie else 'NO'})")
                    break
        except Exception as e:
            logger.warning(f"Resource Mirror Lookup Failed: {e}")

    # Never override an already-selected AVC-compatible stream with an HEVC candidate.
    # HEVC is used only when the previous verified stream-selection fallback selected it.

    # SECONDARY FALLBACK: API Cluster Rotation
    if not working_stream:
        logger.info(f"Entering Phase 4 Cluster Rotation for {subject_id}")
        
        clusters = [
            ("https://apig.inmoviebox.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://api5.aoneroom.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://api-sin.aoneroom.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://v-ios.aoneroom.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://h5-api.aoneroom.com", "/wefeed-h5api-bff/subject/detail-rec"),
            ("https://h5.aoneroom.com", "/index/video/v_detail")
        ]
        
        hdrs = {
            "User-Agent": "MovieBox/4.0.02 (Android 14; Pixel 6)",
            "X-M-Version": "4.0.02"
        }
        
        orig_base = s["client"].BASE_URL
        for cluster_url, endpoint in clusters:
            try:
                logger.info(f"ROTATING CLUSTER: {cluster_url} -> {endpoint}")
                s["client"].BASE_URL = cluster_url
                
                # Standard params
                pms = {'subjectId': subject_id, 'se': season, 'ep': episode, 'quality': official_quality}
                if "detail-rec" in endpoint:
                    pms["perPage"] = 12
                    pms["page"] = 1
                
                method = 'POST' if "v_detail" in endpoint else 'GET'
                
                fb_res = s["client"].request(method, endpoint, params=pms if method=='GET' else None, data=pms if method=='POST' else None, headers=hdrs)
                if not isinstance(fb_res, dict):
                    logger.warning(f"Cluster {cluster_url} returned invalid response type: {type(fb_res)}")
                    continue
                fb_data = fb_res.get('data', {})
                srcs = fb_data.get('streamList') or fb_data.get('streams') or []
                
                if not srcs and "detail-rec" in endpoint:
                    items = fb_data.get('items') or []
                    if items:
                         # Try to find a playable resource in items
                         logger.info(f"H5 Detail contains {len(items)} sibling items, probing first...")
                
                logger.info(f"Cluster {cluster_url} Result: {len(srcs)} streams found")
                
                # Handshake validation for any candidate found
                if not srcs and fb_data.get('url'): srcs = [fb_data]
                
                for cand in srcs:
                    url = cand.get('url')
                    if not url: continue
                    cookie = cand.get('signCookie') or fb_res.get('signCookie') or fb_data.get('signCookie') or s["client"].session.cookies.get("signCookie") or global_cookie or ""
                    try:
                        v_res = requests.head(url, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}, timeout=3, verify=True)
                        if v_res.status_code in [200, 206]:
                            working_stream = cand
                            working_cookie = cookie
                            subtitles_source = fb_data.get("subTitleList", []) or subtitles_source
                            logger.info(f"CLUSTER SUCCESS: Recovered stream via {cluster_url} Cluster")
                            break
                    except: continue
                if working_stream: break
            except Exception as e:
                logger.warning(f"Cluster {cluster_url} failed: {e}")
            finally:
                s["client"].BASE_URL = orig_base
    
    if not working_stream and streams:
        working_stream = streams[0]
        working_cookie = working_stream.get("signCookie") or global_cookie or ""

    if not working_stream:
        logger.error(f"RESOLUTION FAILURE: No usable streams found for subject {subject_id}")
        raise HTTPException(status_code=404, detail="No streams found.")

    # CALCULATE METADATA DURATION FOR FRONTEND OVERRIDE
    total_duration = 0
    source_method = "metadata"
    try:
        subject_detail = s["content"].get_movie_detail(subject_id).get("data") or {}
        runtime_str = subject_detail.get("runtime") or subject_detail.get("duration") or subject_detail.get("totalDuration") or "0"
        
        # Deep probe for episode-specific runtime if possible (Strict String Comparison)
        for sl in subject_detail.get("seasonList", []):
            if str(sl.get("season")) == str(season):
                for ep_item in sl.get("episodeList", []):
                    if str(ep_item.get("episode")) == str(episode):
                        runtime_str = ep_item.get("runtime") or ep_item.get("duration") or ep_item.get("totalDuration") or runtime_str
                        break
        
        runtime_str = str(runtime_str)
        if ":" in runtime_str:
            parts = [int(p) for p in runtime_str.split(':')]
            if len(parts) == 3: total_duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2: total_duration = parts[0] * 60 + parts[1]
        else:
            total_duration = int(''.join(filter(str.isdigit, runtime_str))) * 60
    except Exception as e:
        logger.error(f"Metadata duration extraction failed: {e}")
    
    # ADVANCED PROBE: Native HLS Parsing (Fast & Accurate for .m3u8)
    if (total_duration < 600 or total_duration > 15000) and working_stream.get("url", "").lower().endswith(".m3u8"):
        try:
           target_v = working_stream.get("url")
           logger.info(f"Metadata suspicious. Engaging HLS Parse for {target_v[:60]}...")
           h_res = httpx.get(target_v, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": working_cookie}, verify=True, timeout=5)
           if h_res.status_code == 200:
              lines = h_res.text.splitlines()
              h_dur = sum(float(line.split(":")[1].split(",")[0]) for line in lines if line.startswith("#EXTINF:"))
              if h_dur > 0:
                 total_duration = int(h_dur)
                 source_method = "hls-parse"
        except Exception as he:
           logger.warning(f"HLS duration parse failed: {he}")

    # ADVANCED PROBE: ffprobe for absolute accuracy for MP4/Others
    if source_method == "metadata" and (total_duration < 600 or total_duration > 15000):
        try:
           target_v = working_stream.get("url")
           if target_v:
              logger.info(f"Metadata suspicious ({total_duration}s). Engaging FFPROBE for {target_v[:60]}...")
              ff_cmd = [
                  'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                  '-of', 'default=noprint_wrappers=1:nokey=1',
                  '-headers', f'Cookie: {working_cookie}\r\nUser-Agent: ExoPlayerLib/2.18.7\r\n',
                  target_v
              ]
              ff_proc = subprocess.run(ff_cmd, capture_output=True, text=True, timeout=5)
              if ff_proc.returncode == 0 and ff_proc.stdout.strip():
                  f_dur = float(ff_proc.stdout.strip())
                  if f_dur > 0:
                      total_duration = int(f_dur)
                      source_method = "ffprobe"
        except Exception as fe:
           logger.warning(f"FFPROBE duration probe failed: {fe}")

    if total_duration < 60:  # Unknown duration: never invent an hour.
        total_duration = 0

    logger.info(f"DURATION LOCK [{source_method}]: {subject_id} S{season}E{episode} -> {total_duration}s (Source: {runtime_str})")

    # SUBTITLE DISCOVERY: Combine stream-internal and external BFF captions (TUI logic)
    all_subtitles = []
    seen_sub_urls = set()

    def add_subtitle_track(sub: dict):
        if not isinstance(sub, dict): return
        url = sub.get("url") or ""
        if not url or "aa348f2541d13ffe" in url:
            return  # Blacklist known placeholder/dummy tracks
        
        # Check size (TUI drops corrupted/dummy <= 50 bytes responses)
        try:
            sz = int(sub.get("size") or 0)
            if 0 < sz <= 50: return
        except: pass

        if url not in seen_sub_urls:
            seen_sub_urls.add(url)
            lan_name = sub.get("lanName") or sub.get("lan") or "Unknown"
            sub["lanName"] = lan_name
            all_subtitles.append(sub)

    for st_sub in subtitles_source:
        add_subtitle_track(st_sub)

    try:
        ext_hdrs = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-S918B Build/TP1A.220624.014)", "X-M-Version": "11.7.0"}
        # Priority 1: Use the exact resourceId of the chosen working stream (like TUI)
        sub_resource_id = str(working_stream.get("id") or "")
        
        # Priority 2: Fallback to metadata resourceDetectors
        if not sub_resource_id or sub_resource_id == "None":
            try:
                sd_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id}, headers=ext_hdrs)
                sd_detectors = sd_res.get('data', {}).get('resourceDetectors', [])
                if sd_detectors:
                    sub_resource_id = str(sd_detectors[0].get('resourceId') or subject_id)
            except:
                sub_resource_id = subject_id
        
        ext_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get-ext-captions', 
                                      params={'resourceId': sub_resource_id, 'subjectId': subject_id, 'episode': episode}, 
                                      headers=ext_hdrs)
        ext_list = ext_res.get("data", {}).get("extCaptions") or ext_res.get("data", {}).get("list") or []
        for esub in ext_list:
            add_subtitle_track(esub)
    except Exception as se:
        logger.warning(f"External subtitle probe failed: {se}")

    # Preferred subtitle: French first, then English, never advertise verified audio.
    def priority(caption):
        language = str(caption.get("lan") or caption.get("lanName") or "").lower()
        if language.startswith("fr") or "french" in language or "français" in language: return 0
        if language.startswith("en") or "english" in language: return 1
        return 2
    all_subtitles.sort(key=priority)
    best_sub = all_subtitles[0].get("url") if all_subtitles else None

    # Referer and headers matching MovieBox-Tui: DASH/HLS requires https://sportslive.wine
    is_dash_or_hls = (
        working_stream.get("url", "").lower().endswith(".mpd")
        or ".mpd" in working_stream.get("url", "").lower()
        or working_stream.get("url", "").lower().endswith(".m3u8")
        or ".m3u8" in working_stream.get("url", "").lower()
    )
    stream_referer = "https://sportslive.wine" if is_dash_or_hls else ""

    headers_dict = {
        "User-Agent": "ExoPlayerLib/2.19.1",
        "Cookie": working_cookie or ""
    }
    if stream_referer:
        headers_dict["Referer"] = stream_referer

    # DIRECT NATIVE LINK
    return {
        "url": working_stream.get("url", ""),
        "cookie": working_cookie,
        "duration": total_duration,
        "subtitles": all_subtitles,
        "subtitle_url": best_sub,
        "isHls": working_stream.get("url", "").lower().endswith(".m3u8") or ".m3u8" in working_stream.get("url", "").lower(), 
        "streamId": working_stream.get("id"),
        "qualityList": list(set([st.get("quality") for st in streams if st.get("quality")])),
        "episode": episode,
        "season": season,
        "referer": stream_referer,
        "headers": headers_dict
    }

# Safe browser-facing form of the historical stream route.
# Provider tokens, signed cookies and CDN URLs never leave the server.
def _legacy_verified_stream(subject_id: str, season: int = 1, episode: int = 1,
               quality: Optional[str] = "720p", resource_id: Optional[str] = None,
               session_id: Optional[str] = Cookie(None)):
    from urllib.parse import urlsplit
    from cinekivu_source.delivery import prepare_streams
    from cinekivu_bridge import _register_captions, valid_id
    valid_id(subject_id)
    if not 0 <= season <= 999 or not 0 <= episode <= 9999:
        raise HTTPException(422, "Saison ou épisode invalide")
    private = _get_stream_raw(subject_id, season=season, episode=episode, quality=quality,
                              resource_id=resource_id, session_id=session_id)
    remote_url = private.get("url") or ""
    path = urlsplit(remote_url).path.lower()
    kind = 'DASH' if path.endswith('.mpd') else 'MP4' if path.endswith('.mp4') else 'unknown'
    if kind == 'unknown':
        raise HTTPException(502, "Cette source ne dispose pas encore d'un relais vidéo vérifié")
    candidate = {'url': remote_url, 'format': kind, 'quality': quality or 'Auto',
                 'quality_value': int(re.search(r'\d+', quality or '').group()) if re.search(r'\d+', quality or '') else 0,
                 'duration_sec': private.get('duration'), 'headers': private.get('headers') or {},
                 'cookie': private.get('cookie')}
    checked = prepare_streams([candidate])
    playable = next((p for p in checked if p.get('play_url')), None)
    if not playable:
        raise HTTPException(502, 'Le CDN a refusé la vérification ou le média n’est pas compatible')
    raw_subtitles = [{**sub, 'url': sub.get('url') or sub.get('subPath'),
                      'language': sub.get('lanName') or sub.get('language'),
                      'code': sub.get('lan')} for sub in private.get('subtitles') or []]
    subtitles = _register_captions(raw_subtitles)
    return {'url': playable['play_url'], 'cookie': None, 'headers': {},
            'duration': private.get('duration') or None,
            'subtitles': subtitles, 'subtitle_url': subtitles[0]['url'] if subtitles else None,
            'qualityList': private.get('qualityList') or [],
            'streamId': private.get('streamId'), 'season': season, 'episode': episode,
            'source': 'moviebox-internal-verified-relay', 'audioVerified': False}


@app.get("/stream/{subject_id}")
def get_stream(subject_id: str, season: int = Query(1, ge=0, le=999),
               episode: int = Query(1, ge=0, le=9999),
               quality: Optional[str] = Query('720p', pattern=r'(?i)^(auto|best|worst|360p|480p|720p|1080p|1440p|2160p)$'),
               resource_id: Optional[str] = None, session_id: Optional[str] = Cookie(None)):
    from cinekivu_bridge import valid_id, _register_captions, _remote_error
    from cinekivu_source import api as cine, delivery
    valid_id(subject_id)
    try:
        return _legacy_verified_stream(subject_id, season, episode, quality, resource_id, session_id)
    except HTTPException as exc:
        if exc.status_code in (401, 403, 429):
            raise
    except Exception as exc:
        logger.info('Legacy playback provider unavailable: %s', type(exc).__name__)
    try:
        detail = cine.engine.get_details(subject_id)
        source_season, source_episode = (0, 0) if str(detail.get('subject_type') or detail.get('subjectType')) == '1' else (season, episode)
        sources = delivery.prepare_streams(cine.engine.get_streams(subject_id,
            season=source_season, episode=source_episode,
            detail_path=detail.get('detail_path'), prefer_web_mp4=True))
        playable = [s for s in sources if str(s.get('play_url') or '').startswith('/api/')]
        playable.sort(key=lambda s: (int(s.get('quality_value') or 0), str(s.get('format')).lower() == 'mp4'), reverse=True)
        desired = int(quality.lower().rstrip('p')) if quality and quality.lower().endswith('p') else None
        selected = next((s for s in playable if s.get('quality_value') == desired), None)
        selected = selected or (playable[-1] if quality and quality.lower() == 'worst' and playable else next(iter(playable), None))
        if selected is None:
            raise HTTPException(502, 'Aucune source complète vérifiée disponible')
        captions_partial = False
        try:
            captions = _register_captions(cine.engine.get_subtitles(subject_id,
                season=source_season, episode=source_episode))
        except Exception:
            captions, captions_partial = [], True
        return {'url': selected['play_url'], 'cookie': None, 'headers': {},
                'duration': selected.get('duration_sec'), 'subtitles': captions,
                'subtitle_url': captions[0]['url'] if captions else None,
                'qualityList': list(dict.fromkeys(s.get('quality') for s in playable if s.get('quality'))),
                'streamId': None, 'season': season, 'episode': episode,
                'source': 'cinekivu-verified-relay-fallback', 'audioVerified': False,
                'subtitlesPartial': captions_partial}
    except Exception as exc:
        raise _remote_error(exc) from exc

import os

@app.get("/download/{subject_id}")
async def proxy_download(
    request: Request,
    subject_id: str,
    season: int = Query(1, ge=0, le=999),
    episode: int = Query(1, ge=0, le=9999),
    quality: str = Query("720p", pattern=r"(?i)^(auto|best|worst|360p|480p|720p|1080p|1440p|2160p)$"),
    title: str = Query("Movie", max_length=240),
    session_id: Optional[str] = Cookie(None)
):
    """Compatibility download using the same verified MP4 relay as the SDK API."""
    from starlette.concurrency import run_in_threadpool
    from urllib.parse import quote
    from cinekivu_bridge import valid_id, _subject, _remote_error
    from cinekivu_downloads import Options, prepare, filename
    from cinekivu_source import api as cine, delivery
    valid_id(subject_id)
    def resolve():
        selected = _subject(cine.engine.get_details(subject_id))
        if not selected or selected['subjectId'] != subject_id:
            raise HTTPException(404, 'Titre introuvable')
        options = Options(quality='BEST' if quality.lower() == 'auto' else quality)
        return prepare(selected, options, max(1, season), max(1, episode))
    try:
        data = await run_in_threadpool(resolve)
        response = await delivery.stable_download_stream(data['file']['mediaId'], request, cine.engine)
        name = data['file']['filename']
        if title != 'Movie':
            name = filename(title, data['year'] or '', data['season'], data['episode'],
                            data['file']['quality'] or 'video', 'mp4')
        response.headers['content-disposition'] = "attachment; filename=\"cinekivu-download.mp4\"; filename*=UTF-8''" + quote(name, safe='')
        return response
    except Exception as exc:
        raise _remote_error(exc) from exc

@app.get("/subtitles/search")
def subtitle_search(query: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        hdrs = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-S918B Build/TP1A.220624.014)", "X-M-Version": "11.7.0"}
        # Parameter 'q' is mandatory for v11.7.0 BFF search
        res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/subtitle-search', params={'q': query}, headers=hdrs)
        data = res.get("data", {})
        if not isinstance(data, dict): data = {}
        ls = data.get("items") or data.get("list") or []
        return {"code": 0, "data": {"list": ls}}
    except: return {"code": 0, "data": {"list": []}}



@app.get("/subtitles/{subject_id}")
def get_subtitles(subject_id: str, se: int = 1, ep: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        hdrs = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-S918B Build/TP1A.220624.014)", "X-M-Version": "11.7.0"}
        
        # Resolve actual resourceId for content mapping parity
        resource_id = subject_id
        try:
            det_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id}, headers=hdrs)
            detectors = det_res.get('data', {}).get('resourceDetectors', [])
            if detectors:
                resource_id = detectors[0].get('resourceId') or subject_id
        except: pass

        # Fetch using high-parity external captions endpoint
        res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get-ext-captions', 
                                    params={'resourceId': resource_id, 'subjectId': subject_id, 'episode': ep}, 
                                    headers=hdrs)
        data = res.get("data", {})
        if not isinstance(data, dict): data = {}
        ls = data.get("extCaptions") or data.get("list") or []
        return {"code": 0, "data": {"list": ls}}
    except:
        return {"code": 0, "data": {"list": []}}

# --- NEW: Cloud Sync & Advanced Subtitles ---

@app.get("/history/position")
def get_history_position(subject_id: str, resource_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/resource-position', params={'subjectId': subject_id, 'resourceId': resource_id})
    from moviebox_api.ott import _remote
    return _remote(res)

@app.post("/history/position")
def save_history_position(subject_id: str, resource_id: str, position: int = Query(ge=0), session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request('POST', '/wefeed-mobile-bff/subject-api/resource-position', data={'subjectId': subject_id, 'resourceId': resource_id, 'position': position})
    from moviebox_api.ott import _remote
    return _remote(res)

@app.post("/history/seen")
def mark_have_seen(subject_id: str = Query(pattern=r'^[A-Za-z0-9_-]{1,100}$'),
                   progress: int = Query(0, ge=0), total: int = Query(0, ge=0),
                   session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["user"].report_history(subject_id, progress, total)
    return res
@app.post("/analytics/operation")
def track_operation(action: str, target: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request('POST', '/wefeed-mobile-bff/statistics/user-operation', data={'action': action, 'target': target})
    return res

@app.get("/history")
def get_history(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    h = load_local_history()
    
    # Never expose legacy "default" history shared across all browsers.
    default_history = []
    user_specific = h.get(s["id"], [])
    blacklist = set(h.get("blacklist:" + s["id"], []))
    
    # Combine uniquely by subjectId
    combined_history_dict = {str(x.get("subjectId") or x.get("id")): x for x in default_history + user_specific}
    user_history = list(combined_history_dict.values())
    
    if not s["auth"].is_guest_mode:
        try:
            res = s["user"].get_history(page=page)
            data = res.get("data", {})
            if not isinstance(data, dict): data = {}
            cloud_list = data.get("items") or data.get("list") or []
            
            seen_ids = set(combined_history_dict.keys())
            for c in cloud_list:
                sid_str = str(c.get("subjectId"))
                # CRITICAL FIX: Only add from cloud if NOT in our local blacklist
                if sid_str not in seen_ids and sid_str not in blacklist:
                    mapped = map_item(c)
                    mapped["seeTime"] = c.get("seeTime") or c.get("updateTime") or c.get("progress") or 0
                    mapped["subjectId"] = c.get("subjectId")
                    mapped["id"] = c.get("subjectId")
                    user_history.append(mapped)
        except Exception as e:
            pass
        
    user_history = [x for x in user_history if x.get("subjectId") and str(x.get("subjectId")) != "None"]
    # AND filter out anything in blacklist again to be 100% sure
    user_history = [x for x in user_history if str(x.get("subjectId")) not in blacklist]
    
    # Sort history by seeTime/updateTime descending
    user_history.sort(key=lambda x: int(x.get("seeTime", 0) or 0), reverse=True)
    
    return {"code": 0, "data": {"list": user_history}}


@app.get("/watchlist")
def get_watchlist(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        return {"code": 0, "data": {"list": []}}
    try:
        res = s["user"].get_watchlist(page=page)
        data = res.get("data", {})
        if not isinstance(data, dict): data = {}
        cloud_list = data.get("items") or data.get("list") or []
        return {"code": 0, "data": {"list": [map_item(x) for x in cloud_list]}}
    except:
        return {"code": 0, "data": {"list": []}}

@app.post("/history/delete/{subject_id}")
def delete_history_item(subject_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        raise HTTPException(status_code=401, detail="Please Sign In to manage your history.")
    res = s["user"].report_history(subject_id, 0, 0, status=0) 
    return {"status": "success", "raw": res}


@app.post("/watchlist/toggle")
def toggle_watchlist(subject_id: str, active: bool, subject_type: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        raise HTTPException(status_code=401, detail="Please Sign In to manage your watchlist.")
    action = 1 if active else 2
    res = s["user"].toggle_watchlist(subject_id, action=action, subject_type=subject_type)
    return {"status": "success", "raw": res}

class ProgressReport(BaseModel):
    subject_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    progress_ms: int = Field(ge=0)
    total_ms: int = Field(ge=0)
    status: int = 1

@app.post("/history/progress")
def report_progress(req: ProgressReport, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        return {"status": "success"}
    return s["user"].report_history(req.subject_id, req.progress_ms, req.total_ms, req.status)

@app.post("/launch-player")
def launch_player_disabled():
    """Remote process execution is deliberately disabled in server builds."""
    raise HTTPException(status_code=410, detail="Server-side player launch disabled. Use the web player or a local player directly.")

@app.get("/post/count/{subject_id}")
def get_post_count(subject_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("GET", "/wefeed-mobile-bff/post/count/subject", params={"subjectId": subject_id})
        from moviebox_api.ott import _remote
        res = _remote(res)
        count = res.get("data", {}).get("count") or "0"
        return {"code": 0, "count": count}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'Compteur de commentaires indisponible chez le fournisseur') from exc

@app.get("/groups/trending")
def get_trending_groups(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("POST", "/wefeed-mobile-bff/group/list/trending-entrance", data={})
        from moviebox_api.ott import _remote
        res = _remote(res)
        data = res.get("data", {})
        items = data.get("items") or []
        return {"code": 0, "data": items}
    except Exception as exc:
        raise HTTPException(503, 'Groupes tendance indisponibles chez le fournisseur') from exc

@app.post("/post/like")
def like_post(post_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("POST", "/wefeed-mobile-bff/interactive/post/like", data={"postId": post_id})
        return res
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.post("/post/create")
def create_post(subject_id: str, content: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        # /post/create logic from Smali
        data = {
            "subjectId": subject_id,
            "content": content,
            "type": "1" # Public post
        }
        res = s["client"].request("POST", "/wefeed-mobile-bff/post/create", data=data)
        return res
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.get("/groups/interactive")
def get_interactive_posts(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        # Pull from public interactive ranking feed
        res = s["client"].request("POST", "/wefeed-mobile-bff/interactive/post/list", 
                                 data={"page": 1, "pageSize": 20})
        from moviebox_api.ott import _remote
        return _remote(res)
    except HTTPException as exc:
        raise HTTPException(503, 'Flux communautaire interactif momentanément indisponible chez le fournisseur') from exc
    except Exception as e:
        raise HTTPException(503, 'Flux communautaire interactif momentanément indisponible chez le fournisseur') from e

@app.get("/post/list/{subject_id}")
def get_subject_posts(subject_id: str, page: int = Query(1, ge=1, le=200), session_id: Optional[str] = Cookie(None)):
    from cinekivu_bridge import valid_id
    from moviebox_api.ott import _remote
    valid_id(subject_id)
    s = get_session(session_id)
    try:
        # subject-specific post list
        res = s["client"].request("GET", "/wefeed-mobile-bff/post/list/subject",
                                 params={"id": subject_id, "page": page-1, "perPage": 10})
        return _remote(res)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'Commentaires du fournisseur indisponibles') from exc

@app.get("/debug_history")
def debug_history_disabled():
    raise HTTPException(status_code=410, detail="Debug inspection endpoint disabled")

# DIRECT NATIVE PLAYBACK ONLY (PROXIES REMOVED)

@app.get("/sub-proxy")
def subtitle_proxy_disabled():
    """Do not provide an unauthenticated arbitrary-URL proxy (SSRF)."""
    raise HTTPException(status_code=410, detail="Arbitrary subtitle proxy disabled for security.")

# Versioned, application-oriented OTT endpoints with local persistence.
from moviebox_api.ott import install_ott_routes
from cinekivu_bridge import install_cinekivu_routes, cine_home, cine_catalog, cine_search, cine_genre
install_cinekivu_routes(app)
install_ott_routes(app, get_session, map_item, format_tab_sections, _extract_items,
                   home_loader=cine_home, catalog_loader=cine_catalog, search_loader=cine_search,
                   genre_loader=cine_genre)

# Context-aware search and catalog recommendations (CineKivu-first).
from cinekivu_discovery import install_discovery_routes
install_discovery_routes(app, get_session, map_item)

# Local, stateless corpus discovery; IDs are separate from provider subject IDs.
from cinekivu_recommendations import install_recommendation_routes
install_recommendation_routes(app)
from cinekivu_reference import install_reference_routes
install_reference_routes(app)
from cinekivu_enrichment import install_enrichment_routes
install_enrichment_routes(app)
from cinekivu_downloads import install_download_routes
install_download_routes(app)

if __name__ == "__main__":
    # DISABLE RELOAD: Prevents Windows-specific crash loops during active streaming/editing
    uvicorn.run("moviebox_api_server:app", host="0.0.0.0", port=8000, reload=False)

# CineKivu V3: current Web BFF catalogue and discovery routes.
from cinekivu_web import install_web_routes
install_web_routes(app, get_session)

# Read-only subject metadata and public comments for Compose media screens.
from cinekivu_media import install_media_routes
install_media_routes(app)

_preview_directory = Path(__file__).resolve().parent.parent / 'preview'
if _preview_directory.is_dir():
    app.mount('/preview', StaticFiles(directory=str(_preview_directory), html=True), name='preview')

# Serve the application and API on one origin, including browser session cookies.
_frontend_directory = Path(__file__).resolve().parent.parent / 'web-release'
if _frontend_directory.is_dir():
    app.mount('/app', StaticFiles(directory=str(_frontend_directory), html=True), name='frontend')

# Outermost request context: copied into FastAPI worker threads, never shared
# mutable session headers between concurrent clients.
from cinekivu_client_context import ProviderClientContextMiddleware
app.add_middleware(ProviderClientContextMiddleware)
