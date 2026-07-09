"""Refund processing ledger tracking."""
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from ..models import RefundLog

def log_refund(db: Session, booking, amount_cents: int) -> None:
    # FIXED: Replaced 'created_at' with 'processed_at' and populated 'status'
    refund_log = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.now(timezone.utc).replace(tzinfo=None)
    )
    db.add(refund_log)
    # REMOVED db.commit() here to allow the caller to commit both operations atomically