# 🚀 Final Bug Report & System Audit Log

After exhaustive code review against the preliminary-round contract, the following issues were identified and patched.  
This document merges the original audit (Bugs 1–20) with the corrected remaining‑bug audit (Bugs 21–28) to reflect the **current, fixed state** of the codebase.  
Each entry lists severity, affected file(s), a description of the flaw, and the applied fix.

---

## 📊 Summary Matrix of All Patched Bugs

| Bug ID | Severity | Component / File | What Was Wrong | Fix |
|:---|:---|:---|:---|:---|
| **1** | 🟠 High | `app/routers/bookings.py` | Overlap check used `<=` → blocked back‑to‑back bookings | Changed to `<` |
| **2** | 🟡 Medium | `app/routers/bookings.py` | 5‑minute grace window allowed past‑dated bookings | Enforced strict `start > now` |
| **3** | 🔴 Critical | `app/routers/bookings.py` | Duration validation widened to 1 min–24 h (spec requires 1–8 h, whole hours) | Reverted to whole‑hour check, 1–8 h |
| **4** | 🟡 Medium | `app/routers/bookings.py` | Pagination offset `page * limit` skipped items | Corrected to `(page-1)*limit` + ascending sort |
| **5** | 🔴 Critical | `app/routers/bookings.py` | Missing tenant isolation → cross‑org data leak | Joined on `Room.org_id == user.org_id` |
| **6** | 🟡 Medium | `app/routers/bookings.py` | Refund tier logic used static limits instead of timedeltas | Rewrote with `timedelta` comparisons |
| **7** | 🔴 Critical | `app/routers/bookings.py` | Unit mismatch: `log_refund` received cents but treated them as percent | Fixed `log_refund` to accept cents directly |
| **8** | 🔴 Critical | `app/routers/auth.py` | Duplicate username returned dict instead of 409 | Raised `AppError(409, "USERNAME_TAKEN")` |
| **9** | 🔴 Critical | `app/auth.py` | Refresh token reuse allowed by TOCTOU gap | Atomic consume‑and‑revoke with `consume_refresh_token` |
| **10** | 🔴 Critical | `app/auth.py` | Access token lifetime multiplied by 60 (15 h instead of 15 min) | Removed `*60` multiplier |
| **11** | 🔴 Critical | `app/services/export.py` | Admin export bypassed org checks → IDOR | Rewrote to use scoped query with org join |
| **12** | 🟠 High | `app/timeutils.py` | Offset stripped without UTC conversion → time shifts | Converted to UTC before stripping tzinfo |
| **13** | 🟠 High | `app/cache.py` | Unlocked global dicts → RuntimeError under concurrency | Added `threading.Lock` around all cache ops |
| **14** | 🟡 Medium | `app/cache.py` | Asymmetric invalidation left stale data on date boundaries | Symmetric invalidation for both start & end dates |
| **15** | 🟠 High | Shared Services | Lost‑update races on `stats`, `reference`, `ratelimit` | Added per‑module locks |
| **16** | 🔴 Critical | `app/services/refunds.py` | Invalid column `created_at` + missing `status` → 500 crash | Used `processed_at` + explicit `status="processed"` |
| **17** | 🟠 High | `app/services/notifications.py` | Nested AB‑BA lock deadlock | Flattened locks to independent `with` blocks |
| **18** | 🟡 Medium | `app/routers/bookings.py` | Non‑atomic commit split refund log & status update | Single `db.commit()` after both operations |
| **19** | 🟠 High | `app/routers/bookings.py` | TOCTOU double‑booking on create | Added global `_booking_write_lock` |
| **20** | 🔴 Critical | `app/main.py` | Unhandled exceptions leaked HTML 500 | Global fallback handler returning JSON |
| **21** | 🔴 Critical | `app/main.py` | Database tables never created → `sqlite3.OperationalError` | Added `Base.metadata.create_all(bind=engine)` |
| **22** | 🔴 Critical | `app/main.py` | Admin routes not mounted → 404 on `/admin/*` | Included `admin.router` |
| **23** | 🔴 Critical | `app/routers/bookings.py` | Re‑introduced wrong duration rules (1 min‑24 h) | Re‑fixed to whole hours, 1‑8 h (see Bug 3) |
| **24** | 🔴 Critical | `app/routers/bookings.py` | Cancel response missing contract fields | Return `{id, status, refund_percent, refund_amount_cents}` |
| **25** | 🟠 High | `app/routers/bookings.py` | 48‑h refund boundary off‑by‑one (> instead of >=) | Changed to `>= timedelta(hours=48)` |
| **26** | 🟠 High | `app/routers/bookings.py` | Race condition on concurrent cancel → multiple refunds | Added per‑booking `_cancel_locks` |
| **27** | 🟠 High | `app/auth.py` | JWT missing `iat` and (refresh) `role` claims | Added both claims to both token factories |
| **28** | 🔴 Critical | `app/auth.py` + `routers/auth.py` | Refresh token still revocable after validation | Atomic `consume_refresh_token` implementation |

---

## 🔍 Detailed Breakdown

### 📂 Module: `app/main.py`
- **Bug 21 – Missing schema initialization**  
  `Base.metadata.create_all()` was never called, causing “no such table” errors on first request.  
  *Fix:* Added `Base.metadata.create_all(bind=engine)` before including routers.
- **Bug 22 – Admin endpoints unreachable**  
  The admin router was not included, causing `/admin/usage-report` and `/admin/export` to return 404.  
  *Fix:* Added `app.include_router(admin.router)`.

### 📂 Module: `app/routers/bookings.py`
- **Bug 1 – Back‑to‑back booking conflict**  
  Overlap condition `b.start_time <= end` prevented consecutive bookings.  
  *Fix:* Switched to strict `<`.
- **Bug 2 – Historical booking loophole**  
  `if start <= now` allowed a 5‑minute grace window.  
  *Fix:* Removed the grace period; `start` must be strictly in the future.
- **Bug 3 & 23 – Duration validation regression**  
  An earlier “fix” allowed 1 minute–24 hours, contradicting the spec’s whole‑hour 1–8 h requirement.  
  *Fix:* Re‑implemented check: `duration_hours != int(duration_hours) or <1 or >8`, error code `INVALID_BOOKING_WINDOW`.
- **Bug 4 – Pagination offset error**  
  `offset(page * limit)` skipped the first page’s items.  
  *Fix:* `offset((page-1)*limit)` and added `order_by(start_time.asc(), id.asc())`.
- **Bug 5 – Cross‑org data leak**  
  Endpoints did not verify `Room.org_id == user.org_id` for non‑admin users.  
  *Fix:* Added explicit tenant checks.
- **Bug 6 – Refund tier miscalculation**  
  Notice was compared against static thresholds instead of using `timedelta`.  
  *Fix:* Refactored `calculate_refund_tier` to use `now - start_time` and correct comparisons.
- **Bug 7 – Refund log unit mismatch**  
  `log_refund` received a value in cents but treated it as a percentage.  
  *Fix:* Modified `log_refund` to store the absolute cent amount directly.
- **Bug 18 – Non‑atomic cancellation commit**  
  `log_refund` committed separately from the status update, risking inconsistency.  
  *Fix:* Both operations now happen in a single `db.commit()`.
- **Bug 19 – TOCTOU double‑booking**  
  Concurrent create requests could pass the conflict check.  
  *Fix:* Wrapped the check‑and‑insert in a global `_booking_write_lock`.
- **Bug 24 – Cancel response missing contract fields**  
  Returned `{'status':'success','refund_cents':…}` instead of the required `id, status, refund_percent, refund_amount_cents`.  
  *Fix:* Returned the exact contract schema.
- **Bug 25 – 48‑h refund boundary off‑by‑one**  
  Condition `notice > timedelta(hours=48)` gave 50 % refund at exactly 48 h.  
  *Fix:* Changed to `>=`.
- **Bug 26 – Cancellation race condition**  
  Two concurrent cancel requests could both create refund rows.  
  *Fix:* Added per‑booking `_cancel_locks` using a dictionary guarded by `_cancel_locks_guard`.

### 📂 Module: `app/auth.py` & `app/routers/auth.py`
- **Bug 8 – Silent duplicate registration**  
  Duplicate username returned a dict instead of raising 409.  
  *Fix:* Raises `AppError(409, "USERNAME_TAKEN")`.
- **Bug 9 & 28 – Refresh token TOCTOU**  
  `decode_token` checked revocation under a lock, but the token wasn’t revoked until after the check completed, allowing concurrent reuse.  
  *Fix:* Introduced `consume_refresh_token` that atomically decodes, validates, and revokes the JTI.
- **Bug 10 – Access token lifetime inflation**  
  `create_access_token` multiplied `ACCESS_TOKEN_EXPIRE_MINUTES` by 60, yielding 15‑hour tokens.  
  *Fix:* Removed the multiplier.
- **Bug 27 – Missing JWT claims**  
  Access tokens lacked `iat`; refresh tokens lacked `iat` and `role`.  
  *Fix:* Added both claims to both token factories.

### 📂 Service modules (`app/services/`, `app/timeutils.py`, `app/cache.py`)
- **Bug 11 – Admin export IDOR**  
  `include_all=True` bypassed org‑based filtering.  
  *Fix:* Routed all queries through `_fetch_scoped` that joins on `Room.org_id`.
- **Bug 12 – Timezone parsing**  
  `replace(tzinfo=None)` dropped the offset without adjusting to UTC.  
  *Fix:* Convert to UTC via `.astimezone(timezone.utc)` before stripping.
- **Bug 13 – Cache concurrency crash**  
  Unlocked dicts caused `RuntimeError` under load.  
  *Fix:* Added `threading.Lock()` around all cache access.
- **Bug 14 – Stale cache asymmetry**  
  Cache wasn’t invalidated for both start and end dates, nor for both availability and reports.  
  *Fix:* Symmetric invalidation for both dates and both cache types.
- **Bug 15 – Race conditions in in‑memory services**  
  `stats.py`, `reference.py`, `ratelimit.py` had unprotected read‑modify‑write operations.  
  *Fix:* Added module‑level locks.
- **Bug 16 – Refund log column errors**  
  `RefundLog` creation used `created_at` (not a column) and omitted `status`.  
  *Fix:* Used `processed_at` and set `status="processed"`.
- **Bug 17 – Notification deadlock**  
  `notify_created` and `notify_cancelled` acquired locks in opposite order.  
  *Fix:* Un‑nested locks; each function acquires them independently.

---
