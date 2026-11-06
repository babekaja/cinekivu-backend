"""Read the provider's current filter schema, without a bundled catalogue.

MovieBox renders filterItemsData in its Nuxt page response. The public filter
API returns titles/paging, but does not expose that configuration separately.
Only that JSON configuration is decoded; no page JavaScript is executed.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser

import httpx
from fastapi import HTTPException

from cinekivu_source import api as cine
from cinekivu_metadata_cache import MetadataCache

CHANNELS = {'movies': (1, 'film'), 'series': (2, 'tv-series'),
            'anime': (1006, 'animated-series')}
SORT_IDS = {'for_you': 'ForYou', 'hottest': 'Hottest',
            'latest': 'Latest', 'rating': 'Rating'}
LANGUAGE_IDS = {'fr': 'French dub', 'en': 'English dub'}
_filter_config_cache = MetadataCache(ttl_seconds=600, max_entries=len(CHANNELS))


class _NuxtPayload(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reading = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.reading = dict(attrs).get('id') == '__NUXT_DATA__'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.reading = False

    def handle_data(self, data):
        if self.reading:
            self.parts.append(data)


def decode_nuxt_subtree(html: str, required_keys: set[str], fields: set[str] | None = None):
    parser = _NuxtPayload()
    parser.feed(html)
    nodes = json.loads(''.join(parser.parts))
    if not isinstance(nodes, list) or len(nodes) > 100_000:
        raise ValueError('invalid_nuxt_payload')

    def decode(index, depth=0):
        if depth > 24 or not isinstance(index, int) or not 0 <= index < len(nodes):
            return None
        value = nodes[index]
        if isinstance(value, dict):
            return {key: decode(ref, depth + 1) for key, ref in value.items()}
        if isinstance(value, list):
            return [decode(ref, depth + 1) for ref in value]
        return value

    # Decode only the requested public subtree, never auth/session state.
    node = next((node for node in nodes if isinstance(node, dict)
                 and required_keys.issubset(node)), None)
    if node is None:
        raise ValueError('missing_nuxt_subtree')
    return {key: decode(ref) for key, ref in node.items() if fields is None or key in fields}


def decode_filter_config(html: str, channel_id: int) -> dict:
    config = decode_nuxt_subtree(html, {'typeList', 'mobile'})
    if not isinstance(config, dict):
        raise ValueError('missing_filter_config')
    desktop = next((x for x in config.get('typeList', [])
                    if isinstance(x, dict) and x.get('channelId') == channel_id), None)
    mobile = next((x for x in config.get('mobile', [])
                   if isinstance(x, dict) and x.get('channelId') == channel_id), {})
    if not desktop or not isinstance(desktop.get('items'), list):
        raise ValueError('missing_filter_channel')
    return {**desktop, 'mobileItems': mobile.get('items', [])}


def native_filter_config(category: str) -> dict:
    return _filter_config_cache.get_or_load(category, lambda: _load_filter_config(category))


def _load_filter_config(category: str) -> dict:
    channel_id, path = CHANNELS[category]
    try:
        extra = {'follow_redirects': False} if isinstance(cine.engine.session, httpx.Client) else {'allow_redirects': False}
        response = cine.engine.session.get(
            f'https://movieboxhd.net/fr/web/{path}',
            headers={'User-Agent': cine.WEB_USER_AGENT,
                     'Accept-Language': 'fr-FR,fr;q=0.9', 'X-Request-Lang': 'fr'},
            timeout=11, **extra)
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            raise ValueError('filter_config_unavailable')
        return decode_filter_config(response.text, channel_id)
    except Exception as exc:
        raise HTTPException(502, 'Les filtres actuels du fournisseur sont indisponibles') from exc


def native_value(config: dict, dimension: str, value: str) -> str:
    if not value or value.casefold() == 'all':
        return ''
    group = next((x for x in config['items'] if x.get('filterType') == dimension), {})
    for entry in group.get('filterVals') or []:
        if value.casefold() in (str(entry.get('id', '')).casefold(),
                                str(entry.get('name', '')).casefold()):
            return str(entry['id'])
    raise HTTPException(422, f'Filtre {dimension} non proposé par le fournisseur')


def filter_options(configs: list[dict]) -> dict:
    def facets(dimension):
        values = {}
        for config in configs:
            for group in config['items']:
                if group.get('filterType') != dimension:
                    continue
                for entry in group.get('filterVals') or []:
                    value = str(entry.get('id') or '')
                    if value and value != 'All':
                        values.setdefault(value, {'value': value, 'label': entry.get('name') or value})
        return list(values.values())
    rating = next((group.get('rangeVals') for config in configs
                   for group in config.get('mobileItems', [])
                   if group.get('filterType') == 'rate' and group.get('rangeVals')), None)
    return {'genres': facets('genre'), 'countries': facets('country'),
            'years': facets('year'), 'languages': facets('classify'),
            'sorts': facets('sort'), 'ratingRange': rating,
            'channels': [{'category': category, 'channelId': channel[0]}
                         for category, channel in CHANNELS.items()],
            'source': 'web-bff-native-filter-config', 'stale': False, 'partial': False}
