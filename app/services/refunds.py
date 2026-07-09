"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog


def log_refund(db: Session, booking, amount_cents: int) -> None:
    # FIXED: Expect and store raw cents directly to eliminate percentage calculation division bugs
    refund_log = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        created_at=datetime.utcnow()
    )
    db.add(refund_log)
    db.commit()