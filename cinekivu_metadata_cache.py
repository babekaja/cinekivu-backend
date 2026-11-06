"""Bounded, short-lived caches for public catalogue metadata only."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import threading
import time
from typing import Any, Callable, Hashable


class MetadataCache:
    """Coalesce identical loads; never retain failures or serve expired entries."""

    def __init__(self, *, ttl_seconds: float, max_entries: int,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, dict[str, Any]]] = OrderedDict()
        self._pending: dict[Hashable, Future] = {}

    def get_or_load(self, key: Hashable, load: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                expires, value = entry
                if self._clock() < expires:
                    self._entries.move_to_end(key)
                    return deepcopy(value)
                del self._entries[key]
            pending = self._pending.get(key)
            owner = pending is None
            if owner:
                pending = Future()
                self._pending[key] = pending

        if not owner:
            return deepcopy(pending.result())

        try:
            value = deepcopy(load())
        except BaseException as error:
            with self._lock:
                del self._pending[key]
            pending.set_exception(error)
            raise

        with self._lock:
            self._entries[key] = (self._clock() + self.ttl_seconds, value)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            del self._pending[key]
        pending.set_result(value)
        return deepcopy(value)
