"""Catalog search that discovers separately indexed French-language editions.

A provider title such as 'Naruto [Version Française]' is a *separate subject*,
not necessarily a dub resource attached to the ordinary 'Naruto' subject.
This module searches both spelling patterns and preserves the actual subject ID.

IMPORTANT: a language label in the catalog title is evidence of how a title is
advertised, not a verification that its stream has a French audio track.
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Callable

from fastapi import HTTPException

# Require explicit edition markers, not a substring such as "French Connection".
_BRACKET_MARKER = re.compile(
    r"\s*[\[(]\s*(?:version\s+fran[cç]ais(?:e|es|s)?|french\s+version|"
    r"french\s+dub(?:bed)?|french\s+audio|doubl(?:age|[ée])\s+fran[cç]ais|"
    r"vf|v\.?f\.?)\s*[\])]",
    re.IGNORECASE,
)
_SUFFIX_MARKER = re.compile(
    r"\s*[-–—:]\s*(?:version\s+fran[cç]ais(?:e|es|s)?|"
    r"french\s+version|french\s+dub(?:bed)?|vf|v\.?f\.?)\s*$|\s+vf\s*$",
    re.IGNORECASE,
)
_LOOSE_SEARCH_MARKER = re.compile(
    r"(?:\bversion\s+fran[cç]ais(?:e|es|s)?\b|\bfrench\s+(?:version|dub(?:bed)?)\b|\bvf\b)",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _cleanup(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\n-–—:()[]")


def base_title(title: str) -> str:
    """Strip only *recognized* edition markers; preserve franchise subtitles."""
    text = _BRACKET_MARKER.sub(" ", title)
    text = _SUFFIX_MARKER.sub("", text)
    return _cleanup(text)


def is_french_edition(title: str) -> bool:
    return bool(_BRACKET_MARKER.search(title) or _SUFFIX_MARKER.search(title))


def parse_query(query: str) -> tuple[str, bool]:
    """Accept 'Naruto[version françaises]' and bracketless 'Naruto VF'."""
    marked = is_french_edition(query) or bool(_LOOSE_SEARCH_MARKER.search(query))
    clean = base_title(query)
    if marked:
        clean = _LOOSE_SEARCH_MARKER.sub(" ", clean)
        clean = _cleanup(clean)
    return clean, marked


def title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _fold(base_title(value))).strip()


def candidate_queries(query: str, page: int = 1) -> list[str]:
    """Bounded expansion on the first page; maintain paging beyond page one."""
    original = re.sub(r"\s+", " ", query).strip()
    base, marked = parse_query(original)
    if not base:
        return [original]
    if page > 1 or len(base) < 3:
        return [original]
    # Explicit requests also search the bare title, so original versions stay visible.
    variants = [
        original,
        base,
        f"{base} [Version Française]",
        f"{base} [Version Françaises]",
        f"{base}[version françaises]",
        f"{base} VF",
    ]
    queries: list[str] = []
    seen = set()
    for item in variants:
        if len(item) > 120:
            continue
        key = _fold(item)
        if key not in seen:
            queries.append(item)
            seen.add(key)
        if len(queries) >= 6:
            break
    return queries


def _compatible_edition(a: dict, b: dict) -> bool:
    if title_key(a.get("title") or "") != title_key(b.get("title") or ""):
        return False
    a_type, b_type = a.get("subjectType"), b.get("subjectType")
    if a_type and b_type and str(a_type) != str(b_type):
        return False
    a_year, b_year = a.get("releaseTime"), b.get("releaseTime")
    if a_year and b_year and a_year != "N/A" and b_year != "N/A" and str(a_year)[:4] != str(b_year)[:4]:
        return False
    return True


def search_editions(
    content: Any, mapper: Callable[[dict], dict], extract: Callable[[Any, Callable[[dict], dict], int], list[dict]],
    query: str, *, page: int = 1, subject_type: int = 0, edition: str = "all",
    max_results: int = 120,
) -> dict:
    """Merge independently indexed editions by subject ID, with partial-error reporting.

    The upstream service may return only the first N rows per query; there is no
    claim that all languages of a title have been enumerated.
    """
    probes = candidate_queries(query, page)
    found: dict[str, dict] = {}
    used: list[str] = []
    failed: list[str] = []
    successful = 0
    started = time.monotonic()
    for index, q in enumerate(probes):
        if index and time.monotonic() - started > 25:
            failed.extend(probes[index:])
            break
        try:
            raw = content.search(q, page=page if q == probes[0] else 1, subject_type=subject_type)
            if not isinstance(raw, dict) or raw.get("code", 0) not in (0, 200, "0", "200", None):
                raise ValueError("Invalid search provider response")
            successful += 1
            used.append(q)
            for record in extract(raw, mapper, 100):
                identifier = str(record.get("subjectId") or "")
                if not identifier or identifier == "None":
                    continue
                record = dict(record)
                record["baseTitle"] = base_title(record.get("title") or "")
                record["edition"] = "fr" if is_french_edition(record.get("title") or "") else "unknown"
                record["audioVerified"] = False
                if identifier not in found:
                    found[identifier] = record
                elif record["edition"] == "fr":
                    # Same ID with differing labels: retain explicit edition title
                    # only if that title genuinely carries a French marker.
                    found[identifier] = record
        except Exception:
            # A partial index outage must not throw away successful results.
            # Never leak upstream authentication/tokens through public errors.
            failed.append(q)
    if not successful:
        raise HTTPException(status_code=502, detail="Recherche indisponible chez le fournisseur")

    clean, explicit_fr = parse_query(query)
    key = title_key(clean)
    # Bring exact franchise matches and separately indexed French editions forward,
    # while retaining unrelated results as low-ranked fallbacks.
    def rank(row: dict) -> tuple[int, int, int]:
        target = title_key(row.get("title") or "")
        if target == key:
            relevance = 0
        elif target.startswith(key + " "):
            relevance = 1
        elif key in target:
            relevance = 2
        else:
            relevance = 3
        french = row["edition"] == "fr"
        return (relevance, 0 if french and explicit_fr else 1 if french else 2, 0)

    ordered = sorted(found.values(), key=rank)[:max_results]
    french_editions = [r for r in ordered if r["edition"] == "fr"]
    selected = french_editions if edition == "fr" else ordered
    groups: dict[str, list[dict]] = {}
    for item in ordered:
        groups.setdefault(title_key(item["title"]), []).append(item)
    for item in ordered:
        # A related edition must match title, type, and known year.
        siblings = [r for r in groups.get(title_key(item["title"]), [])
                    if r["subjectId"] != item["subjectId"] and _compatible_edition(r, item)]
        item["otherEditions"] = [{"subjectId": r["subjectId"], "title": r["title"],
                                  "edition": r["edition"], "audioVerified": False} for r in siblings[:10]]
    return {
        "items": selected,
        "french_editions": french_editions,
        "page": page,
        "query": query,
        "subject_type": subject_type,
        "edition_filter": edition,
        "searched_queries": used,
        "partial": bool(failed),
        "failed_search_count": len(failed),
        "edition_note": "L'édition française est déduite du titre du catalogue, pas d'une piste audio vérifiée.",
        "pagination_note": "Seule la première page est enrichie avec plusieurs formulations." if page == 1 else None,
    }


def related_editions(items: list[dict], current: dict) -> list[dict]:
    """Exclude similarly-named but different series/movies."""
    return [item for item in items if item.get("subjectId") != current.get("subjectId")
            and _compatible_edition(item, current)]
