# Per-account Generation Queue Implementation Plan

> Execute inline in this session. Preserve the existing generation endpoints and immediate-submission responses.

**Goal:** Limit each server account to 10 active image generation requests and 10 active video generation requests, queuing overflow persistently.

**Architecture:** SQLite reserves slots atomically and stores FIFO waiting jobs. A single FastAPI background worker polls active jobs and submits waiting jobs when capacity opens. Existing upstream task IDs remain unchanged; queued callers track the stable local request ID through an authenticated request-status endpoint.

**Tech Stack:** Existing FastAPI, HTTPX, SQLite and Python asyncio; no new dependencies.

**Constraints:** One application worker. Bearer wrapper authentication and account isolation apply to every queue route. Never retry a generation POST automatically. Uncertain submissions retain their slot; poll errors never establish completion. A terminal update cannot be undone by a stale poll. Deleting an account cancels work that has not been submitted.

## Implementation and checks

- [x] Add failing tests for the eleventh job queuing, independent account/media capacity, concurrent admission, FIFO promotion after completion, restart persistence, uncertain submissions, and stale poll responses.
- [x] Extend `store.py` with atomic admission/claim, waiting positions, active-job queries and request lookup. Keep all waiting and unresolved jobs in history.
- [x] Extend `main.py` with submission reuse, periodic bounded polling/dispatch, worker shutdown handling, HTTP 202 overflow responses, and authenticated `GET /api/requests/{request_id}`.
- [x] Update dashboard active states and queue cards; add a regression check for queued responses and local request tracking.
- [x] Update agent docs and README with concurrency counts, queued response/status polling, persistence and failure semantics.
- [x] Run backend, dashboard, PM2 and syntax checks. Exercise the dashboard against a simulated upstream without real generation charges.

Verification: 57 backend tests, six dashboard regressions, PM2 launch checks and syntax checks pass. Browser verification submitted an eleventh video and displayed separate waiting image/video cards. Review reproduced a collision with an upstream `waiting` status; queue operations now require a null upstream task ID, and the regression passes. Additional checks cover backoff and cancelling a worker during dispatch without claiming unsent jobs.
