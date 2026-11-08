"""Local movie discovery, adapted from Moviebox's TF-IDF/cosine approach.

Corpus IDs are intentionally separate from streaming-provider subject IDs.
No provider requests, dataframe libraries or dense all-pairs matrix are needed.
Original project notice: resources/moviebox/LICENSE.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Path as PathParam, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

CORPUS_PATH = Path(__file__).resolve().parent / 'resources/moviebox/movies.csv'
SOURCE = 'moviebox-corpus'
# Common English function words; synopsis language is explicitly reported as en.
STOP_WORDS = frozenset('''a about after again against all am an and any are as at
be because been before being below between both but by can could did do does doing
down during each few for from further had has have having he her here hers herself
him himself his how i if in into is it its itself just me more most my myself no nor
not now of off on once only or other our ours ourselves out over own same she should
so some such than that the their theirs them themselves then there these they this
those through to too under until up very was we were what when where which while
who whom why will with would you your yours yourself yourselves'''.split())


def normalized(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))


def features(text: str) -> Counter:
    words = [w for w in re.findall(r'\b[^\W\d_]{2,}\b', normalized(text))
             if w not in STOP_WORDS]
    return Counter(words + [f'{a} {b}' for a, b in zip(words, words[1:])])


@dataclass(frozen=True)
class Movie:
    movie_id: int
    title: str
    genres: tuple[str, ...]
    plot: str

    def public(self, include_plots: bool = False) -> dict:
        result = {'movieId': self.movie_id, 'catalogId': f'{SOURCE}:{self.movie_id}',
                  'title': self.title, 'genres': list(self.genres)}
        if include_plots:
            result['plot'] = self.plot
        return result


class RecommendationEngine:
    def __init__(self, movies: list[Movie], *, min_df: int = 3):
        if not movies or any(m.movie_id < 0 or not m.title or not m.plot for m in movies):
            raise ValueError('Invalid or empty movie corpus')
        self.movies = tuple(sorted(movies, key=lambda m: m.movie_id))
        self.by_id = {m.movie_id: m for m in self.movies}
        if len(self.by_id) != len(self.movies):
            raise ValueError('Duplicate corpus IDs')
        self.min_df = min_df
        self._titles = {m.movie_id: normalized(m.title) for m in self.movies}
        self._index = None
        self._lock = threading.Lock()

    @classmethod
    def from_csv(cls, path: Path) -> RecommendationEngine:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not {'ID', 'Title', 'Category', 'Plot'} <= set(reader.fieldnames or []):
                raise ValueError('Invalid corpus columns')
            movies = [Movie(int(row['ID']), row['Title'].strip(),
                            tuple(g.strip().strip('"') for g in row['Category'].split(',')
                                  if g.strip().strip('"')), row['Plot'].strip())
                      for row in reader]
        return cls(movies)

    def movie(self, movie_id: int) -> Movie:
        try:
            return self.by_id[movie_id]
        except KeyError:
            raise HTTPException(404, 'Film absent du corpus local') from None

    def search(self, q: str = '', genre: str = '', page: int = 1,
               page_size: int = 20, include_plots: bool = False) -> dict:
        needle, category = normalized(q.strip()), normalized(genre.strip())
        matches = [m for m in self.movies
                   if (not needle or needle in self._titles[m.movie_id])
                   and (not category or category in {normalized(g) for g in m.genres})]
        offset = (page - 1) * page_size
        return {'items': [m.public(include_plots) for m in matches[offset:offset + page_size]],
                'total': len(matches), 'page': page, 'pageSize': page_size,
                'hasMore': offset + page_size < len(matches), 'query': q.strip(),
                'genre': genre.strip()}

    def genres(self) -> list[dict]:
        counts = Counter(g for m in self.movies for g in set(m.genres))
        return [{'name': g, 'count': counts[g]} for g in sorted(counts, key=str.casefold)]

    def _get_index(self):
        if self._index is not None:
            return self._index
        with self._lock:
            if self._index is not None:
                return self._index
            document_frequency = Counter()
            for movie in self.movies:
                document_frequency.update(features(movie.plot).keys())
            count = len(self.movies)
            idf = {term: math.log((1 + count) / (1 + freq)) + 1
                   for term, freq in document_frequency.items()
                   if self.min_df <= freq <= 0.9 * count}
            postings = defaultdict(list)
            for movie in self.movies:
                weights = {t: freq * idf[t] for t, freq in features(movie.plot).items() if t in idf}
                norm = math.sqrt(sum(w * w for w in weights.values()))
                if norm:
                    for term, weight in weights.items():
                        postings[term].append((movie.movie_id, weight / norm))
            self._index = (idf, dict(postings))
            return self._index

    def recommend(self, text: str, limit: int = 3, *, exclude: int | None = None,
                  include_plots: bool = False) -> list[dict]:
        idf, postings = self._get_index()
        weights = {t: freq * idf[t] for t, freq in features(text).items() if t in idf}
        norm = math.sqrt(sum(w * w for w in weights.values()))
        if not norm:
            return []
        scores = defaultdict(float)
        for term, weight in weights.items():
            for movie_id, movie_weight in postings[term]:
                if movie_id != exclude:
                    scores[movie_id] += (weight / norm) * movie_weight
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [{**self.movie(movie_id).public(include_plots),
                 'similarity': round(min(1.0, max(0.0, score)), 6)}
                for movie_id, score in ranked if score > 0]


_engine_lock = threading.Lock()


@lru_cache(maxsize=1)
def get_engine() -> RecommendationEngine:
    # lru_cache alone can execute its factory twice on simultaneous first calls.
    with _engine_lock:
        return _load_engine()


@lru_cache(maxsize=1)
def _load_engine() -> RecommendationEngine:
    return RecommendationEngine.from_csv(CORPUS_PATH)


def engine_or_503() -> RecommendationEngine:
    try:
        return get_engine()
    except (OSError, ValueError, KeyError, csv.Error):
        raise HTTPException(503, 'Corpus de recommandations indisponible') from None


def envelope(**data) -> dict:
    return {'code': 0, 'data': {'source': SOURCE, 'language': 'en',
                               'streamAvailability': 'unknown', **data}}


class SynopsisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=3, max_length=10000)
    limit: int = Field(default=3, ge=1, le=30, strict=True)
    include_plots: bool = False

    @field_validator('text')
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError('Le synopsis doit contenir au moins trois caractères')
        return value


def install_recommendation_routes(app: FastAPI):
    router = APIRouter(prefix='/api/v1/recommender', tags=['CineKivu / recommandations locales'])

    @router.get('/status')
    def status():
        engine = engine_or_503()
        return envelope(total=len(engine.movies), indexReady=engine._index is not None,
                        method='tfidf-cosine', maxRecommendations=30)

    @router.get('/genres')
    def genres():
        return envelope(items=engine_or_503().genres())

    @router.get('/movies')
    def movies(q: str = Query('', max_length=120), genre: str = Query('', max_length=120),
               page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
               include_plots: bool = False):
        return envelope(**engine_or_503().search(q, genre, page, page_size, include_plots))

    @router.get('/movies/{movie_id}')
    def movie(movie_id: int = PathParam(ge=0)):
        return envelope(movie=engine_or_503().movie(movie_id).public(True))

    @router.get('/movies/{movie_id}/recommendations')
    def recommendations(movie_id: int = PathParam(ge=0), limit: int = Query(3, ge=1, le=30),
                        include_plots: bool = False):
        engine = engine_or_503()
        seed = engine.movie(movie_id)
        return envelope(seed=seed.public(include_plots), method='tfidf-cosine',
                        items=engine.recommend(seed.plot, limit, exclude=movie_id,
                                               include_plots=include_plots))

    @router.post('/recommendations')
    def synopsis(body: SynopsisRequest):
        return envelope(method='tfidf-cosine', items=engine_or_503().recommend(
            body.text, body.limit, include_plots=body.include_plots))

    app.include_router(router)


def main():
    parser = argparse.ArgumentParser(description='Catalogue et recommandations locales CineKivu')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--movie', type=int, help='Identifiant du film (0 à 4999)')
    group.add_argument('--text', help='Synopsis à comparer au corpus')
    group.add_argument('--search', default='', help='Recherche par titre')
    parser.add_argument('--recommend', type=int, default=3, choices=range(1, 31), metavar='1..30')
    parser.add_argument('--plot', action='store_true', help='Inclure les synopsis')
    parser.add_argument('--page', type=int, default=1)
    args = parser.parse_args()
    if args.page < 1:
        parser.error('--page doit être supérieur ou égal à 1')
    engine = get_engine()
    if args.movie is not None:
        try:
            movie = engine.movie(args.movie)
        except HTTPException as exc:
            parser.error(exc.detail)
        data = envelope(seed=movie.public(args.plot), items=engine.recommend(
            movie.plot, args.recommend, exclude=args.movie, include_plots=args.plot))
    elif args.text:
        data = envelope(items=engine.recommend(args.text, args.recommend, include_plots=args.plot))
    else:
        data = envelope(**engine.search(args.search, page=args.page, include_plots=args.plot))
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
