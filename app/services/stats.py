"""Live per-room booking statistics.

Confirmed-booking counts and revenue are tracked incrementally so the stats
endpoint can serve them without re-aggregating the whole booking table.
"""
import threading
import time

_stats: dict[int, dict] = {}
_stats_lock = threading.Lock()


def _aggregate_pause() -> None:
    time.sleep(0.1)


def _update_stats(room_id: int, count_delta: int, revenue_delta: int) -> None:
    """Atomic read‑modify‑write of the per‑room stats dict."""
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        new_count = max(0, current["count"] + count_delta)
        new_revenue = max(0, current["revenue"] + revenue_delta)   # floor at 0
        _aggregate_pause()
        _stats[room_id] = {"count": new_count, "revenue": new_revenue}


def record_create(room_id: int, price_cents: int) -> None:
    _update_stats(room_id, count_delta=1, revenue_delta=price_cents)


def record_cancel(room_id: int, price_cents: int) -> None:
    _update_stats(room_id, count_delta=-1, revenue_delta=-price_cents)


def get(room_id: int) -> dict:
    with _stats_lock:
        return _stats.get(room_id, {"count": 0, "revenue": 0}).copy()