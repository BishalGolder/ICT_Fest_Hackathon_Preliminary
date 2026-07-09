# 🚀 Team Hackathon Bug Report & System Audit Log

Hey guys, here is the full breakdown of all the bugs found and patched in the codebase. Some of these were absolute landmines that would have completely tanked our score in the automated grading suite (especially the multi-tenancy leaks and the concurrency races). 

Everything listed below is now 100% fixed, committed, and passing the local smoke tests.

---

## 📊 Summary Matrix of Patched Bugs

| Bug ID | Severity | Component / File | What Was Wrong | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Bug 1** | 🟠 High | `app/routers/bookings.py` | Overlaps checked with `<=` instead of `<` (broke back-to-back bookings) | ✅ Fixed |
| **Bug 2** | 🟡 Medium | `app/routers/bookings.py` | 5-minute past timestamp loophole allowed historical bookings | ✅ Fixed |
| **Bug 3** | 🟠 High | `app/routers/bookings.py` | Hardcoded 8h max cap & whole-integer check broke minute-level spec | ✅ Fixed |
| **Bug 4** | 🟡 Medium | `app/routers/bookings.py` | Broken pagination math (`page * limit`) skipped items on page shifts | ✅ Fixed |
| **Bug 5** | 🔴 Critical | `app/routers/bookings.py` | Missing tenant validation let users query other orgs' metrics | ✅ Fixed |
| **Bug 6** | 🟡 Medium | `app/routers/bookings.py` | Refund tiers evaluated using broken hour limits instead of deltas | ✅ Fixed |
| **Bug 7** | 🔴 Critical | `app/routers/bookings.py` | Pass-by-cents vs expect-percent unit mismatch corrupted refund log | ✅ Fixed |
| **Bug 8** | 🔴 Critical | `app/routers/auth.py` | Duplicate username registration returned a dict instead of raising 409 | ✅ Fixed |
| **Bug 9** | 🔴 Critical | `app/auth.py` | Refresh token reuse (TOCTOU gap) allowed multiple valid pairs | ✅ Fixed |
| **Bug 10** | 🔴 Critical | `app/auth.py` | Token expiry multiplied by 60 accidentally; tokens lasted 15 hours | ✅ Fixed |
| **Bug 11** | 🔴 Critical | `app/services/export.py` | IDOR leak: Admin export endpoint completely skipped organization checks | ✅ Fixed |
| **Bug 12** | 🟠 High | `app/timeutils.py` | Timezone parser stripped offsets raw without converting to UTC first | ✅ Fixed |
| **Bug 13** | 🟠 High | `app/cache.py` | Unlocked global dicts caused `RuntimeError` concurrency crashes | ✅ Fixed |
| **Bug 14** | 🟡 Medium | `app/cache.py` | Asymmetric invalidation left stale data; failed on multi-date bounds | ✅ Fixed |
| **Bug 15** | 🟠 High | Shared Services | Module counters (`stats`, `reference`, `ratelimit`) had race conditions | ✅ Fixed |

---

## 🔍 Deep-Dive Breakdown

### 📂 Module: `app/routers/bookings.py`

* **Bug 1 (Back-to-Back Booking Conflict):** The scheduling validation logic was checking room overlaps inclusively using `<=`. This meant if a booking ended at 12:00 PM and a second legitimate booking wanted to start at exactly 12:00 PM, the code threw a false conflict.
    * *Fix:* Shifted operators to open boundary checking (`<`) so adjacent bookings go through cleanly.
* **Bug 2 (Historical Booking Loophole):** There was an unauthorized 5-minute fallback window permitted on booking registration requests, meaning users could accidentally or maliciously schedule room times in the past.
    * *Fix:* Stripped the offset padding to strictly enforce forward-facing timestamps.
* **Bug 3 (Duration Granularity Spec Violation):** The code was validating durations using whole integer hour blocks (`duration_hours != int(duration_hours)`) and capped bookings at an arbitrary 8 hours. The hackathon spec explicitly requires minute-level granularity and up to 24-hour durations. 
    * *Fix:* Rewrote constraints to check accurate `timedelta` structures tracking bounds between 1 minute and 24 hours.
* **Bug 4 (Off-by-One Pagination Math):** The database page-skipping offset calculation was written as `page * limit`. If you requested page 1 with a limit of 10, it skipped the first 10 items entirely, hiding data from the frontend.
    * *Fix:* Adjusted the formula to standard `(page - 1) * limit` and explicitly pinned sorting to ascending order.
* **Bug 5 (Bypassing Multi-Tenant Isolation Walls):** Normal members could query index statistics endpoints and harvest metadata belonging to other organizations because user/org matching dependencies weren't tightly integrated into the path queries.
    * *Fix:* Attached strict identity validation scopes checking the requesting user's `org_id` before serving the payload.
* **Bug 6 (Broken Tiered Cancellation Logic):** Notice deadlines for cancellations evaluated tier hours using static limits instead of relative time calculations, completely throwing off the 50% refund threshold when processed close to boundaries.
    * *Fix:* Refactored notice windows to compare timestamps cleanly via standard `timedelta`.
* **Bug 7 (Corrupted Refund Logs / Unit Mismatch):** The cancellation flow calculated a refund value in *cents* (e.g., 5000) and passed it to `log_refund`. However, `log_refund` treated that input parameter as a *percentage* and divided it by 100 again under the hood. This severely corrupted the database ledger audit rows, making the history files completely inconsistent with the actual API responses.
    * *Fix:* Standardized `log_refund` to receive, track, and write pure currency cents directly to the table.

---

### 📂 Module: `app/routers/auth.py` & `app/auth.py`

* **Bug 8 (Silent Registration Collision Bypass):** When a user tried to register with a username that already existed in their organization, the pipeline caught the hit but simply returned a dictionary payload instead of failing. The grader would read this as a 200 OK success instead of a collision block.
    * *Fix:* Explicitly raised a FastAPI `HTTPException` with a `409 USERNAME_TAKEN` status code.
* **Bug 9 (Refresh Token Reuse Security Gap):** The token rotation route checked if a token's `jti` claim was inside the revoked list, but it didn't write the token to the blacklist until *after* the validation passed. Two concurrent requests using a stolen token could hit the check at the same time, passing it before either was marked invalid (TOCTOU race condition).
    * *Fix:* Encapsulated the validation and revocation sequence inside a thread lock using unique `jti` strings to close the window.
* **Bug 10 (Access Token 15-Hour Inflation):** Inside `create_access_token()`, someone multiplied the lifetime delta by 60 (`minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60`). Since the configuration file already defined the value as 15 minutes, this stretched the token's life to **15 hours**, which is a massive security leak.
    * *Fix:* Removed the `* 60` multiplier so tokens expire after exactly 15 minutes as requested.

---

### 📂 Module: `app/services/export.py`

* **Bug 11 (Critical Administrative IDOR Leak):** When a tenant admin requested a full CSV data export with `include_all=True` while specifying a `room_id`, the code bypassed multi-tenancy entirely and routed straight to `fetch_bookings_raw(db, room_id)`. Because that query did not join the `Room` table to filter on `org_id`, any admin could guess room IDs sequentially and dump the private booking data, user tracking, and revenues of every other company on the server.
    * *Fix:* Completely deleted the raw function and rerouted logic through `_fetch_scoped`, forcing proper table joins that validate the current user's `org_id` on every query.

---

### 📂 Module: `app/timeutils.py`

* **Bug 12 (Broken Timezone Shift / Offset Truncation):** When an ISO datetime string carrying a timezone offset (like `+06:00`) came from the client, the parsing script ran `dt.replace(tzinfo=None)`. This stripped the offset data *without adjusting the hour numbers*. A booking submitted for 10:00 AM local time in Dhaka (+06:00) got saved as 10:00 AM UTC, shifting the actual appointment in real-world time by 6 hours. This would completely throw off automated clash checking.
    * *Fix:* Patched parsing to transform localized objects to UTC via `.astimezone(timezone.utc)` before stripping the timezone reference wrapper.

---

### 📂 Module: `app/cache.py` & Shared Services

* **Bug 13 (Cache Concurrency Mutation Crash):** The global in-memory dictionary stores tracking application reporting lists and room availability data had no thread synchronization. Under high concurrency loads from multi-threaded grader testing, simultaneous reads and writes would trigger a `RuntimeError: dictionary changed size during iteration` and crash the application worker.
    * *Fix:* Integrated mutual exclusion structures (`threading.Lock()`) around all operational cache read, write, and eviction execution blocks.
* **Bug 14 (Stale Cache Asymmetry across Date Boundaries):** Booking creation only cleared the room availability cache, while cancellation only cleared reporting files. This left old, dead data active on other endpoints. Additionally, if a booking crossed past midnight into a second day, the availability cache for that next day never got cleared.
    * *Fix:* Synchronized the eviction paths so *both* cache engines are invalidated during state updates, and added a loop clearing consecutive calendar buckets for multi-date spanning bookings.
* **Bug 15 (Race Conditions on In-Memory Microservices):** Global shared tracking files (`stats.py` for revenue counts, `reference.py` for generated reference IDs, and `ratelimit.py` for user request buckets) were executing multi-step read-modify-write statements without synchronization locks. This allowed parallel threads to generate duplicate booking confirmation codes and drop financial stats logs.
    * *Fix:* Locked down all three memory objects using localized thread locks (`threading.Lock()`) to guarantee state consistency during heavy automated load testing.