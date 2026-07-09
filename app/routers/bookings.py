"""Booking creation, listing, detail and cancellation."""
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import cache
from ..auth import get_current_user
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..schemas import BookingCreateRequest
from ..serializers import serialize_booking
from ..services import notifications, ratelimit, reference, stats
from ..services.refunds import log_refund
from ..timeutils import iso_utc, parse_input_datetime

router = APIRouter(tags=["bookings"])

# Thread lock for create-booking TOCTOU protection
_booking_write_lock = threading.Lock()

# Per-booking cancellation locks (prevents double-cancel races)
_cancel_locks: dict[int, threading.Lock] = {}
_cancel_locks_guard = threading.Lock()


def _get_cancel_lock(booking_id: int) -> threading.Lock:
    with _cancel_locks_guard:
        lock = _cancel_locks.get(booking_id)
        if lock is None:
            lock = threading.Lock()
            _cancel_locks[booking_id] = lock
        return lock


MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 8
QUOTA_LIMIT = 3
QUOTA_WINDOW_HOURS = 24


def _pricing_warmup() -> None:
    time.sleep(0.12)


def _quota_audit() -> None:
    time.sleep(0.1)


def _settlement_pause() -> None:
    time.sleep(0.12)


def _check_quota(db: Session, user_id: int, now: datetime, start: datetime) -> None:
    window_end = now + timedelta(hours=QUOTA_WINDOW_HOURS)
    if not (now < start <= window_end):
        return
    count = (
        db.query(Booking)
        .filter(
            Booking.user_id == user_id,
            Booking.status == "confirmed",
            Booking.start_time > now,
            Booking.start_time <= window_end,
        )
        .count()
    )
    _quota_audit()
    if count >= QUOTA_LIMIT:
        raise AppError(409, "QUOTA_EXCEEDED", "Booking quota exceeded")


def calculate_refund_tier(booking: Booking) -> int:
    """Return the refund percentage based on notice before booking start."""
    now = datetime.utcnow()
    notice = booking.start_time - now
    # BUG 5: Use >= 48 hours for 100% refund
    if notice >= timedelta(hours=48):
        return 100
    elif notice >= timedelta(hours=24):
        return 50
    else:
        return 0


@router.post("/bookings", status_code=201)
def create_booking(
    payload: BookingCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ratelimit.record_and_check(current_user.id)

    with _booking_write_lock:
        start = parse_input_datetime(payload.start_time)
        end = parse_input_datetime(payload.end_time)

        # BUG 3: Restore whole‑hour, 1‑8 hour window validation
        if end <= start:
            raise AppError(400, "INVALID_BOOKING_WINDOW", "end_time must be after start_time")

        duration_hours = (end - start).total_seconds() / 3600

        if duration_hours != int(duration_hours) or duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS:
            raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid booking duration")

        now = datetime.utcnow()
        if start <= now:
            raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")

        room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == current_user.org_id).first()
        if room is None:
            raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

        # Conflict detection inside the lock (TOCTOU safe)
        has_clash = db.query(Booking).filter(
            Booking.room_id == payload.room_id,
            Booking.status != "cancelled",
            Booking.start_time < end,
            Booking.end_time > start
        ).first()
        if has_clash:
            raise AppError(409, "ROOM_CONFLICT", "The selected room is occupied during this time frame.")

        _check_quota(db, current_user.id, now, start)

        # Price based on integer hours
        price_cents = room.hourly_rate_cents * int(duration_hours)

        booking = Booking(
            room_id=room.id,
            user_id=current_user.id,
            start_time=start,
            end_time=end,
            status="confirmed",
            reference_code=reference.next_reference_code(),
            price_cents=price_cents,
            created_at=now,
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        stats.record_create(room.id, price_cents)
        cache.invalidate_availability(room.id, start.strftime("%Y-%m-%d"))
        if start.date() != end.date():
            cache.invalidate_availability(room.id, end.strftime("%Y-%m-%d"))
        cache.invalidate_report(current_user.org_id)
        notifications.notify_created(booking)

        return serialize_booking(booking)


@router.get("/bookings")
def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = db.query(Booking).filter(Booking.user_id == user.id)
    total = base.count()
    
    items = (
        base.order_by(Booking.start_time.asc(), Booking.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_booking(b) for b in items],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    response = serialize_booking(booking)
    response["refunds"] = [
        {
            "amount_cents": r.amount_cents,
            "status": r.status,
            "processed_at": iso_utc(r.processed_at),
        }
        for r in booking.refunds
    ]
    return response


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # BUG 6: Acquire per‑booking lock to prevent double cancellation
    lock = _get_cancel_lock(booking_id)
    with lock:
        booking = (
            db.query(Booking)
            .join(Room, Booking.room_id == Room.id)
            .filter(Booking.id == booking_id, Room.org_id == user.org_id)
            .first()
        )
        if booking is None:
            raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
        if user.role != "admin" and booking.user_id != user.id:
            raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

        if booking.status == "cancelled":
            raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

        refund_percent = calculate_refund_tier(booking)
        refund_amount_cents = round(booking.price_cents * (refund_percent / 100.0))

        log_refund(db, booking, refund_amount_cents)
        booking.status = "cancelled"
        db.commit()

    stats.record_cancel(booking.room_id, booking.price_cents)
    cache.invalidate_report(user.org_id)
    cache.invalidate_availability(booking.room_id, booking.start_time.strftime("%Y-%m-%d"))
    if booking.start_time.date() != booking.end_time.date():
        cache.invalidate_availability(booking.room_id, booking.end_time.strftime("%Y-%m-%d"))
    notifications.notify_cancelled(booking)

    # BUG 4: Return the exact contract fields
    return {
        "id": booking.id,
        "status": "cancelled",
        "refund_percent": refund_percent,
        "refund_amount_cents": refund_amount_cents,
    }