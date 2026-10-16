"""In-memory sliding-window rate limits for the auth endpoints.

Fiscus runs as a single uvicorn worker on purpose, so one process sees every
request and memory is the right place for this. Keys combine the endpoint with
the client IP (from trusted proxy headers only) and, where there is one, the
username, so one person cannot lock another out from a different address.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    count: int
    seconds: int


DEFAULT_LIMITS = {
    'login_ip': Limit(30, 300),
    'login_user': Limit(10, 300),
    'setup_ip': Limit(20, 300),
    'register_ip': Limit(20, 300),
}


class RateLimiter:
    def __init__(self, limits: dict[str, Limit] | None = None) -> None:
        self.limits = dict(DEFAULT_LIMITS if limits is None else limits)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, bucket: str, key: str, now: float | None = None) -> int | None:
        """Record a hit. Returns seconds to wait if over the limit, else None."""
        limit = self.limits[bucket]
        now = time.monotonic() if now is None else now
        full = f'{bucket}:{key}'
        with self._lock:
            q = self._hits.setdefault(full, deque())
            while q and q[0] <= now - limit.seconds:
                q.popleft()
            if len(q) >= limit.count:
                return max(1, int(q[0] + limit.seconds - now))
            q.append(now)
            if len(self._hits) > 50_000:
                self._prune(now)
            return None

    def _prune(self, now: float) -> None:
        longest = max(lim.seconds for lim in self.limits.values())
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - longest]:
            del self._hits[k]
