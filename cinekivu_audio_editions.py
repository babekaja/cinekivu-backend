"""Bounded, catalog-grounded discovery of separately indexed audio editions.

Never infer playable dubbed audio from a catalog title. Audio-track availability
must be established by the player/media manifest separately.
"""
from __future__ import annotations

import re
import time
import unicodedata
import copy
import threading
from typing import Callable, Iterable

# Explicit edition suffixes only. A film called "French Connection" is not French audio.
_MARKER = (
    r"version\s+fran[cç]ais(?:e|es|s)?|version\s+anglaise?|"
    r"french\s+(?:dub(?:bed)?|audio|version)|english\s+(?:dub(?:bed)?|audio|version)|"
    r"(?:japanese|spanish|hindi|arabic|portuguese|german|italian|korean|russian|tamil|telugu)\s+dub(?:bed)?|"
    r"(?:vf|v\.f\.|vo|vostfr|vosta|french|english|japanese|spanish|hindi|arabic|portuguese|german|italian|korean|russian|tamil|telugu|bengali|malayalam)"
)
_END_EDITION = re.compile(rf"\s*(?:\[\s*(?:{_MARKER})\s*\]|\(\s*(?:{_MARKER})\s*\)|[-–—:]\s*(?:{_MARKER})|\s+(?:VF|VOSTFR|VOSTA))\s*$", re.I)
_DIACRITICS = lambda value: ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(c))
_LANGS = {
    'fr': ('fr', 'french', 'francais', 'francaise', 'francaises', 'vf', 'version francaise', 'version francaises', 'french dub'),
    'en': ('en', 'english', 'anglais', 'anglaise', 'vo anglais', 'english dub'),
    'ja': ('ja', 'jp', 'japanese', 'japonais', 'japonais(e)', 'nihongo'),
    'es': ('es', 'spanish', 'espagnol', 'espanol', 'castellano'),
    'pt': ('pt', 'portuguese', 'portugais', 'portugues'),
    'hi': ('hi', 'hindi', 'hindî'),
    'ar': ('ar', 'arabic', 'arabe'),
    'de': ('de', 'german', 'allemand', 'deutsch'),
    'it': ('it', 'italian', 'italien', 'italiano'),
    'ko': ('ko', 'kr', 'korean', 'coreen'),
    'ru': ('ru', 'russian', 'russe'),
    'ta': ('ta', 'tamil'), 'te': ('te', 'telugu'),
    'bn': ('bn', 'bengali', 'bangla'), 'ml': ('ml', 'malayalam'),
    'es-419': ('esla',),
}
_DISPLAY = {'fr':'Français','en':'Anglais','ja':'Japonais','es':'Espagnol',
            'pt':'Portugais','hi':'Hindi','ar':'Arabe','de':'Allemand',
            'it':'Italien','ko':'Coréen','ru':'Russe','ta':'Tamoul','te':'Télougou','bn':'Bengali','ml':'Malayalam','es-419':'Espagnol (Amérique latine)'}
_QUERY_SUFFIXES = ('[Version Française]', '[Version Françaises]',
                   '[version françaises]', 'VF', '[French Dub]', '[English Dub]',
                   '[Japanese Dub]', '[Spanish Dub]', '[Hindi Dub]',
                   '[Arabic Dub]', '[Portuguese Dub]', '[German Dub]',
                   '[Italian Dub]', '[Korean Dub]')


def strip_edition(value: str) -> str:
    cleaned = (value or '').strip()
    for _ in range(3):
        without = _END_EDITION.sub('', cleaned).strip(' -–—:()[]')
        if without == cleaned: break
        cleaned = without
    return cleaned


def _key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', _DIACRITICS(strip_edition(value))).strip()


def language_code(value: str | None, *, title: bool = False) -> str | None:
    raw = _DIACRITICS(value or '').strip()
    if not raw: return None
    # VOSTFR means FRENCH SUBTITLES, not French dubbing.
    if re.search(r'\bvostfr\b|\bfrench\s+sub(?:title)?s?\b|\bsous[- ]titres?\s+francais\b', raw):
        return None
    if title:
        # Look at a marker ONLY, not the film's normal title words.
        marker = (value or '')[len(strip_edition(value)):].strip(' -–—:()[]')
        raw = _DIACRITICS(marker).strip()
        if not raw: return None
    for code, aliases in _LANGS.items():
        if raw == code or raw.startswith(code + '-') or raw.startswith(code + '_'):
            return code
        if any(re.search(r'(?<![a-z])'+re.escape(name)+r'(?![a-z])', raw) for name in aliases if len(name) > 2):
            return code
    return None


def edition_language(title: str, metadata: str | None = None) -> str | None:
    # The provider keeps the work's original language (e.g. English) on French
    # editions too. An explicit edition suffix describes this version instead.
    return language_code(title, title=True) or language_code(metadata)


def same_work(left: dict, right: dict) -> bool:
    if not _key(str(left.get('title') or '')) or _key(str(left.get('title') or '')) != _key(str(right.get('title') or '')):
        return False
    l_type = left.get('subjectType'); r_type = right.get('subjectType')
    if l_type and r_type and str(l_type) != str(r_type): return False
    l_year = str(left.get('releaseTime') or '')[:4]
    r_year = str(right.get('releaseTime') or '')[:4]
    if len(l_year) == 4 and len(r_year) == 4 and l_year.isdigit() and r_year.isdigit() and l_year != r_year:
        return False
    return True


def edition_queries(title: str, max_queries: int = 16) -> list[str]:
    base = strip_edition(title)[:100]
    if len(base) < 2: return [base] if base else []
    queries = [base, *(f'{base} {suffix}' for suffix in _QUERY_SUFFIXES)]
    return list(dict.fromkeys(queries))[:max_queries]


def discovered_editions(
    subject_id: str, current: dict, linked: Iterable[dict],
    search: Callable[[str], Iterable[dict]], *, max_seconds: float = 14.0,
    max_queries: int = 16, clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Keep actual subject IDs; merge declared dubs first, then bounded index probes."""
    found: dict[str, dict] = {}
    failed = 0
    attempted: list[str] = []
    deadline = clock() + max_seconds
    for dub in list(linked)[:60]:
        if not isinstance(dub, dict): continue
        sid = str(dub.get('subject_id') or dub.get('subjectId') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', sid) or sid == subject_id: continue
        name = str(dub.get('lang_name') or dub.get('lanName') or dub.get('language') or '').strip()
        code = language_code(str(dub.get('lanCode') or '')) or edition_language('', name) or language_code(str(dub.get('language') or ''))
        label = _DISPLAY.get(code, name[:70] or 'Langue non indiquée')
        found[sid] = {
            'subjectId': sid, 'id': sid, 'title': strip_edition(str(current.get('title') or 'Titre'))[:200],
            'subjectType': current.get('subjectType'), 'releaseTime': current.get('releaseTime'),
            'language': label, 'languageCode': code, 'edition': code or 'unknown',
            'original': bool(dub.get('original')) or name.casefold() == 'original audio',
            'audioVerified': False, 'availability': 'catalog-linked', 'source': 'catalog-dubs',
        }
    queries = edition_queries(str(current.get('title') or ''), max_queries)
    for idx, term in enumerate(queries):
        if idx and clock() >= deadline:
            failed += len(queries) - idx
            break
        attempted.append(term)
        try:
            candidates = list(search(term))[:60]
        except Exception:
            failed += 1
            continue
        for row in candidates:
            if not isinstance(row, dict): continue
            sid = str(row.get('subjectId') or row.get('subject_id') or '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', sid) or sid == subject_id: continue
            if not same_work(current, row): continue
            declared = row.get('languageCode') or row.get('language') or ''
            code = edition_language(str(row.get('title') or ''), str(declared))
            prior = found.get(sid, {})
            code = prior.get('languageCode') or code
            label = _DISPLAY.get(code, str(prior.get('language') or declared or '').strip()[:70] or 'Langue non indiquée')
            found[sid] = {
                **row, 'subjectId': sid, 'id': sid,
                'title': str(row.get('title') or current.get('title') or '')[:240],
                'language': label, 'languageCode': code, 'edition': code or 'unknown',
                'original': prior.get('original', False),
                'audioVerified': False, 'availability': 'catalog-edition', 'source': 'catalog-search',
            }
    priority = {'fr':0, 'en':1, 'ja':2, 'es':3}
    results = sorted(found.values(), key=lambda e: (priority.get(e.get('languageCode'), 10),
                                                    str(e.get('language') or ''), e['subjectId']))
    return {'items': results[:60], 'french_editions': [r for r in results if r.get('languageCode') == 'fr'],
            'partial': bool(failed), 'failed_search_count': failed, 'searched_queries': attempted,
            'audio_verified': False,
            'note': 'Éditions annoncées par le catalogue ; pistes audio à confirmer pendant la lecture.'}


class EditionCache:
    """Small, expiring cache of public catalogue metadata (never stream URLs/tokens)."""
    def __init__(self, capacity: int = 80, ttl: float = 300.0):
        self.capacity = capacity
        self.ttl = ttl
        self._lock = threading.RLock()
        self._values: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._values.get(key)
            if not item: return None
            expiry, value = item
            if expiry <= time.monotonic():
                self._values.pop(key, None)
                return None
            return copy.deepcopy(value)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            # Do not keep partial data longer than a brief provider outage.
            lifetime = 25.0 if value.get('partial') else self.ttl
            if len(self._values) >= self.capacity and key not in self._values:
                self._values.pop(next(iter(self._values)))
            self._values[key] = (time.monotonic() + lifetime, copy.deepcopy(value))
