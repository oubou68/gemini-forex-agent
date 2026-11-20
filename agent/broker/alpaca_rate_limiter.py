import logging
import threading
import time
from collections import deque
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class AlpacaRateLimiter:
    """
    Zentraler, thread-sicherer Rate-Limiter & 429-Backoff-Koordinator,
    geteilt zwischen Alpaca Equities (Stocks) und Alpaca Crypto Clients.
    Verhindert zuverlässig HTTP 429 ('too many requests') gegen Alpacas 200 req/min Limit.
    """

    def __init__(self, max_requests_per_minute: int = 120):
        self.max_requests_per_minute = max_requests_per_minute
        self._timestamps: deque = deque()
        self._backoff_until: float = 0.0
        self._consecutive_429: int = 0
        self._lock = threading.Lock()

    def is_backed_off(self) -> Tuple[bool, float]:
        """Prüft, ob aktuell ein 429-Cooldown aktiv ist."""
        now = time.monotonic()
        with self._lock:
            if now < self._backoff_until:
                return True, round(self._backoff_until - now, 1)
            return False, 0.0

    def acquire(self, wait: bool = True) -> bool:
        """
        Reserviert einen Request-Slot im Sliding-Window (60s).
        Gibt False zurück, wenn ein 429-Backoff aktiv ist oder Limit erschöpft ist (bei wait=False).
        """
        while True:
            sleep_for = 0.0
            with self._lock:
                now = time.monotonic()
                if now < self._backoff_until:
                    if not wait:
                        return False
                    sleep_for = min(self._backoff_until - now, 2.0)
                else:
                    cutoff = now - 60.0
                    while self._timestamps and self._timestamps[0] <= cutoff:
                        self._timestamps.popleft()

                    if len(self._timestamps) < self.max_requests_per_minute:
                        self._timestamps.append(now)
                        return True

                    if not wait:
                        return False
                    sleep_for = max(0.05, (self._timestamps[0] + 60.0) - now)

            time.sleep(min(sleep_for, 1.5))

    def record_success(self):
        with self._lock:
            self._consecutive_429 = 0

    def record_rate_limit(self, retry_after_header: Optional[str] = None) -> float:
        """Aktiviert exponentiellen Backoff bei HTTP 429 (10s -> 20s -> 40s, max 60s)."""
        with self._lock:
            self._consecutive_429 += 1
            delay = min(60.0, 10.0 * (2 ** min(self._consecutive_429 - 1, 3)))
            if retry_after_header:
                try:
                    delay = max(delay, min(float(retry_after_header), 60.0))
                except (ValueError, TypeError):
                    pass
            self._backoff_until = time.monotonic() + delay
            logger.warning(
                f"Alpaca HTTP 429 Rate Limit erkannt (#{self._consecutive_429}). "
                f"Aktiviere gemeinsames Backoff für {delay:.0f}s und nutze Cache-Fallback."
            )
            return delay

    def reset(self):
        """Hilfsmethode für Tests."""
        with self._lock:
            self._timestamps.clear()
            self._backoff_until = 0.0
            self._consecutive_429 = 0


class SimpleTTLCache:
    """Thread-sicherer In-Memory TTL Cache mit Stale-Fallback bei Rate-Limit."""

    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl_seconds: float, allow_stale: bool = False) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            ts, val = entry
            if (now - ts) <= ttl_seconds or allow_stale:
                return val
            return None

    def set(self, key: str, value: Any):
        with self._lock:
            self._store[key] = (time.monotonic(), value)

    def invalidate_prefix(self, prefix: str):
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                self._store.pop(k, None)

    def clear(self):
        with self._lock:
            self._store.clear()


# Geteilte globale Instanz für Alpaca Stocks + Alpaca Crypto (teilen dasselbe 200 req/min API-Key-Limit!)
shared_alpaca_rate_limiter = AlpacaRateLimiter(max_requests_per_minute=120)
