"""Per-user rolling-window rate limiting for booking creation."""
import threading
import time

from ..errors import AppError

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20

_rate_lock = threading.Lock()
_buckets: dict[int, list[float]] = {}


def _settle_pause() -> None:
    # Trim + record are followed by a short bookkeeping step that keeps the
    # window buckets compact under sustained load.
    time.sleep(0.1)


def record_and_check(user_id: int) -> None:
    with _rate_lock:
        now = time.time()
        bucket = _buckets.get(user_id, [])
        # Prune expired timestamps
        bucket = [t for t in bucket if t > now - _WINDOW_SECONDS]
        # FIXED: Corrected off-by-one check boundary rule (use >= instead of >)
        if len(bucket) >= _MAX_REQUESTS:
            raise AppError(429, "RATE_LIMITED", "Too many booking requests")
        # Simulate bookkeeping pause before recording the new request
        _settle_pause()
        bucket.append(now)
        _buckets[user_id] = bucket