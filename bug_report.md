# bug_report.md – corrected remaining-bug audit

After reviewing the uploaded repository against the official preliminary-round PDF, the project still has **remaining bugs**. The previous bug_report marked several items as fixed even though the current code still violates the contract in multiple places.

## Remaining bugs that still need fixing

### Bug 1 — Database tables are never created
- **File(s):** `app/main.py`
- **Why it is still broken:** The app starts serving requests without initializing the SQLite schema. The current smoke test already fails on the first /auth/register call with sqlite3.OperationalError: no such table: organizations.
- **Required fix:** Import Base and engine, then create tables before serving requests.

```python
from .database import Base, engine
from .routers import admin, auth, bookings, rooms

app = FastAPI()

Base.metadata.create_all(bind=engine)

app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(rooms.router)
app.include_router(admin.router)
```

### Bug 2 — Admin endpoints are not mounted
- **File(s):** `app/main.py`
- **Why it is still broken:** The PDF contract requires GET /admin/usage-report and GET /admin/export, but app.main only includes auth/bookings/rooms routers. Both admin endpoints currently return 404.
- **Required fix:** Include admin.router in main.py.

```python
app.include_router(admin.router)
```

### Bug 3 — Booking window validation was changed to the wrong rules
- **File(s):** `app/routers/bookings.py`
- **Why it is still broken:** The current code allows 1 minute–24 hours and even prices fractional durations. The spec is stricter: duration must be a whole number of hours, minimum 1 and maximum 8. Any violation must return 400 with code INVALID_BOOKING_WINDOW.
- **Required fix:** Replace the minute-level validation with the original contract logic and compute integer duration hours only.

```python
start = parse_input_datetime(payload.start_time)
end = parse_input_datetime(payload.end_time)

if end <= start:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "end_time must be after start_time")

duration = end - start
duration_hours = duration.total_seconds() / 3600

if duration_hours != int(duration_hours) or duration_hours < 1 or duration_hours > 8:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid booking duration")

now = datetime.utcnow()
if start <= now:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")

price_cents = room.hourly_rate_cents * int(duration_hours)
```

### Bug 4 — Cancellation response no longer matches the API contract
- **File(s):** `app/routers/bookings.py`
- **Why it is still broken:** The spec requires POST /bookings/{id}/cancel -> {id, status:'cancelled', refund_percent, refund_amount_cents}. The current implementation returns {'status':'success','refund_cents':...}, which will fail the grader even if the cancellation itself succeeds.
- **Required fix:** Return the exact schema from the spec.

```python
return {
    "id": booking.id,
    "status": "cancelled",
    "refund_percent": refund_percent,
    "refund_amount_cents": refund_amount_cents,
}
```

### Bug 5 — 48-hour refund boundary is off by one condition
- **File(s):** `app/routers/bookings.py`
- **Why it is still broken:** The business rule says notice >= 48 hours gets a 100% refund. The current code uses `if notice > timedelta(hours=48)`, so exactly 48 hours incorrectly falls to the 50% bucket.
- **Required fix:** Change the first condition to >= 48 hours.

```python
if notice >= timedelta(hours=48):
    return 100
elif notice >= timedelta(hours=24):
    return 50
return 0
```

### Bug 6 — Cancellation path is still race-prone
- **File(s):** `app/routers/bookings.py`
- **Why it is still broken:** The spec explicitly says concurrent cancel requests for the same booking must still result in exactly one RefundLog and one successful cancellation. Right now the route does a plain read -> status check -> log_refund -> status update with no booking-level lock, so two concurrent requests can both pass the status check and both write refund rows.
- **Required fix:** Protect the entire cancel critical section with a dedicated lock keyed by booking_id, or use a DB transaction strategy that guarantees single-winner cancellation. Minimal in-process fix:

```python
# module scope
_cancel_locks: dict[int, threading.Lock] = {}
_cancel_locks_guard = threading.Lock()

def _get_cancel_lock(booking_id: int) -> threading.Lock:
    with _cancel_locks_guard:
        lock = _cancel_locks.get(booking_id)
        if lock is None:
            lock = threading.Lock()
            _cancel_locks[booking_id] = lock
        return lock

@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(...):
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
```

### Bug 7 — JWT claims do not match the required contract
- **File(s):** `app/auth.py`
- **Why it is still broken:** The spec requires both access and refresh JWTs to include sub, org, role, jti, iat, exp, and type. Current access tokens omit iat; refresh tokens omit both role and iat.
- **Required fix:** Add the missing claims to both token factories.

```python
def create_access_token(user) -> str:
    lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + lifetime,
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(user) -> str:
    lifetime = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + lifetime,
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
```

### Bug 8 — Refresh token rotation is still not atomic under concurrency
- **File(s):** `app/routers/auth.py + app/auth.py`
- **Why it is still broken:** decode_token() checks revocation under a lock, but the refresh endpoint only revokes the token after decode_token() returns. Two concurrent refresh requests using the same refresh token can both pass validation before either one is revoked.
- **Required fix:** Consume refresh tokens atomically under the auth lock. One simple approach is a helper in app/auth.py that decodes + validates + revokes in one locked section, and then use it from /auth/refresh.

```python
# app/auth.py
def consume_refresh_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token")

    if payload.get("type") != "refresh":
        raise AppError(401, "UNAUTHORIZED", "Wrong token type")

    jti = payload.get("jti")
    with _auth_lock:
        if jti in _revoked_tokens:
            raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
        _revoked_tokens.add(jti)
    return payload

# app/routers/auth.py
@router.post("/refresh")
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    data = consume_refresh_token(payload.refresh_token)
    user = db.query(User).filter(User.id == int(data["sub"])).first()
    if user is None:
        raise AppError(401, "UNAUTHORIZED", "Unknown user")
    return {
        "access_token": create_access_token(user),
        "refresh_token": create_refresh_token(user),
        "token_type": "bearer",
    }
```