# Plenx service: agent API guide

Use this document to operate the running service through HTTP. No browser interaction is needed. This document is available as plain text at `GET /llms.txt` without authentication.

## Connection and credentials

- Default base URL: `http://127.0.0.1:8000`. Use the operator's configured wrapper URL if different. All paths below target this wrapper, not plenxai.com directly.
- All `/api/*` requests require `Authorization: Bearer <PLENX_WRAPPER_PASSWORD>`. This is the operator's wrapper password. It is **not** a Plenx token. Never send a Plenx token in the Authorization header.
- Plenx credentials are stored on the server. Ordinary generation callers need only the wrapper password, not a Plenx token or access to browser localStorage.
- Call `GET /api/accounts` to obtain account IDs/names. Select one using `X-Account-ID: <account-uuid>` on profile and generation requests. If omitted, these routes use the first configured account. There is no automatic rotation or credit-based account selection.
- Save `task_id` (which can initially be null), `account_id`, media type, local `request_id`, and `status_url` from the submission response. Polling resolves the request's saved account; you may include `X-Account-ID` to make the association explicit. A mismatched account returns 404. Ambiguous upstream task IDs require the header and return 409 without it.
- Account IDs/history survive token replacement and wrapper-password changes. Do not switch the account of an existing task.
- Do not print secrets or put them in URLs, prompts, artifacts, or result messages. Do not forward the wrapper Authorization header to returned CDN media URLs.
- One shared wrapper password permits using and managing all configured accounts; this service does not implement separate caller/admin roles.
- If the operator has not configured a password, `GET /auth/status` returns `{"setup_required":true}` and `/api/*` returns HTTP 503. Ask the operator to open the dashboard and create/confirm a wrapper password, then provide it to the caller. Generation agents must not choose or reset the operator's password automatically. A configured environment password or an existing saved password skips setup.
- The server checks every configured account's `/api/v1/users/me` on startup and every five minutes. If the operator sets server-only `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the process environment or `.env` beside `main.py`, it sends startup and failed-check notices. `.env` loads on server startup; process environment values take precedence. These Telegram settings are not generation request fields and are never returned by this API. Failed checks distinguish rejected authentication from network/upstream problems; do not infer token expiry from a timeout or 5xx. Alerts are deduplicated until recovery or token replacement. Monitoring does not change queue reservations or replace tokens.

Client example variables:

```sh
export PLENX_WRAPPER_BASE='http://127.0.0.1:8000'
export PLENX_WRAPPER_PASSWORD='replace-with-wrapper-password'
```

Discover accounts, then set `ACCOUNT_ID` to one returned UUID:

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/accounts" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD"
export ACCOUNT_ID='replace-with-account-uuid'
```

The response is `{"accounts":[{"id":"<uuid>","name":"Personal","has_token":true,"created_at":"<timestamp>","updated_at":"<timestamp>"}]}`. Tokens and token fingerprints are never returned. If the list is empty, an operator must add a server-side account before generation.

## Operator account provisioning

Generation agents should use existing account IDs. These endpoints are for an operator provisioning or maintaining credentials; they use the same wrapper password.

- `POST /api/accounts`: JSON `{"name":"Personal","token":"<plenx-token>"}`. The token is a write-only field stored on the server. Returns account metadata with HTTP 201. Repeating the same token returns its existing account ID and does not duplicate the account.
- `PATCH /api/accounts/{account_id}`: JSON `{"name":"Personal"}` renames without changing the token. Include `token` to replace the credential; account ID and history remain unchanged. Supplying a token already assigned to another account returns 409.
- `DELETE /api/accounts/{account_id}`: returns HTTP 204 with no body, removes the active credential, and stops using that account. Re-adding the same token restores its ID/history.

Never request an existing Plenx token from the service; there is no endpoint to read it. The dashboard submits a token only when adding/replacing an account, then clears the input. Old browser accounts are migrated after login, with browser tokens removed only after successful server save.

## Execution procedure

1. Optionally check `GET /health`; `{"status":"ok"}` means the wrapper is running, not that the account is valid.
2. Discover/select a server account using `GET /api/accounts`, then call `GET /api/v1/users/me` with its account header to check balances, quota, and default workplace.
3. Submit exactly one generation POST using the image or video contract below. Submission can consume credits. There is no idempotency key or automatic deduplication.
4. Save `account_id`, `request_id`, `status_url`, media type, and `task_id` when available. HTTP 200 with `status:"queued"` means accepted upstream, not finished. HTTP 202 with `status:"waiting"` and `task_id:null` means saved in this wrapper's queue; do not submit it again.
5. Poll `GET /api/requests/{request_id}` about every five seconds for either response type. This returns the saved job; its `response` field contains the upstream media result after submission. Once `task_id` exists, the original media-specific status GET also remains available and returns the upstream response directly. Use a finite caller-defined deadline. If it expires, report the request/task IDs and last status as pending; do not submit another generation to restart it. A caller timeout does not cancel queued work.
6. On a completed status, extract media URLs using the media-specific fields below. If completed has no usable URL, report the response anomaly rather than inventing a URL or claiming a deliverable exists.
7. On a failed/cancelled status, report the task error. On an uncertain submission, follow the recovery rules below.

No synchronous generation/wait endpoint, webhook, individual-job cancellation, account-token listing, or automatic account rotation is implemented. The server polls Plenx and drains its queue independently of browser/client polling. Clients poll to retrieve the saved result, and the dashboard refreshes its view while open and visible.

## Per-account concurrency and local queue

Each server account has **10 active image generation requests and 10 active video generation requests**, independently. This limits concurrent jobs, not requests per minute. An image request with multiple outputs occupies one slot. Submitting, upstream-queued and processing tasks occupy slots until a confirmed terminal status; another account and the other media type have separate capacity.

When that account/media type is full, generation POST returns **HTTP 202**, for example:

```json
{
  "task_id": null,
  "request_id": "bbbe39bd-f3b3-40cc-85b1-c8f75f70bf39",
  "account_id": "b332c52f-4771-4f97-93a7-0e02c1b1cac0",
  "status": "waiting",
  "queue_position": 1,
  "status_url": "/api/requests/bbbe39bd-f3b3-40cc-85b1-c8f75f70bf39",
  "message": "Waiting for an available video slot (limit 10 per account)."
}
```

Poll the returned wrapper-relative `status_url` with the wrapper password. For example:

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/requests/$REQUEST_ID" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID"
```

This GET returns the saved job object: `id` (the stable local request ID), `account_id`, `kind`, `status`, `task_id`, `queue_position`, `parameters`, `response`, `error`, `poll_error`, and timestamps. Local `waiting` has `task_id:null` and a one-based position within that account/media queue. After automatic submission, the same request ID exposes the upstream task ID and response; queue position becomes null. For completed videos extract `response.result_url`; for completed images extract `response.result.image_urls` or `response.result.result_url`. These extra `response` prefixes apply to this saved-job endpoint, not to the original media-specific status endpoints.

The server checks active tasks and submits waiting jobs about every five seconds. Waiting jobs persist in SQLite across restarts and are admitted FIFO per account/media type. Closing the browser or disabling dashboard auto refresh does not pause generation. Existing active jobs from before this limit was installed are allowed to finish; new jobs wait until fewer than 10 slots are occupied.

Submission POSTs are never automatically retried. `submission_unknown` reserves its slot because Plenx may have accepted it; it requires operator reconciliation. A failed status GET also retains its slot. Automatic work honors `Retry-After` on 429; upstream 401/403 pauses automatic work for that account until the operator updates it. Other accounts continue. Removing an account cancels its unsent local waiting jobs; re-adding it does not restart those cancelled jobs. Upstream tasks that already have a task ID are never treated as unsent jobs, even if their status text is `waiting`.

## Profile, balances, and workplace

`GET /api/v1/users/me` — wrapper Bearer header required; optional `X-Account-ID`; no body.

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/v1/users/me" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID"
```

The service preserves the full upstream profile. Relevant fields, shown as an example subset:

```json
{
  "credit_balance": 0.0,
  "free_credits": 9975.0,
  "ruby_balance": 0.0,
  "daily_image_used": 1,
  "daily_image_limit": 1000,
  "default_workplace_id": "45e06a7a-1318-433c-8338-dcfcf52d7b5a",
  "plan_name": "Creator"
}
```

Keep the three balances separate; do not assume they are interchangeable or sum them into spendable credits. Zero `credit_balance` alone does not establish that generation is unavailable. Daily image quota remaining is `max(0, daily_image_limit - daily_image_used)` when both fields are supplied. Plenx decides eligibility and actual charges; `uses_credits:false` does not guarantee a free generation.

For both generation types, omit `workplace_id` to resolve `default_workplace_id` from the selected server account's profile automatically. An explicit valid workplace UUID skips that lookup. If the profile has no valid default workplace, the wrapper returns an error before submitting; supply an actual authorized workplace UUID, never a fabricated one. Do not reuse the example UUID for a different account without checking its profile.

## Video generation

`POST /api/v1/veo/generate` — Bearer header and **multipart form data**. Do not send a JSON body. Let the HTTP client set the multipart Content-Type and boundary.

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/v1/veo/generate" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID" \
  -F 'prompt=a cat' \
  -F 'model=omni-flash' \
  -F 'aspect_ratio=16:9' \
  -F 'duration=4' \
  -F 'resolution=720' \
  -F 'server_id=56947b53-029e-4751-bbca-b9de54f5d628' \
  --form-string 'reference_images=["https://images.plenxai.com/cdn-cgi/imagedelivery/kdQv2TNFsgg6nS5bfVdpTg/3b98720c-0655-4255-755e-f5486a5ddc00/format=png,quality=100"]'
```

| Form field | Type / constraint | Default |
| --- | --- | --- |
| `prompt` | Required nonblank string; max 20,000 characters | None |
| `model` | Nonblank string; max 100 characters | `omni-flash` |
| `aspect_ratio` | Nonblank string; max 20 characters | `16:9` |
| `duration` | Positive integer, seconds | `4` |
| `resolution` | Nonblank string; max 30 characters | `720` |
| `server_id` | UUID | `56947b53-029e-4751-bbca-b9de54f5d628` |
| `workplace_id` | Optional UUID; omit rather than send an empty field | Account default |
| `reference_images` | Optional JSON-encoded array of HTTP/HTTPS image URLs, in a single form field | `[]` |

For video references, send one `reference_images` multipart text part containing the JSON array, for example `["https://example.test/first.png","https://example.test/second.png"]`. Do not repeat the form field or send a comma-separated list. Omit it or send `[]` for text-only video. Saved request parameters retain the parsed URL array. Video uses `reference_images`; the image-generation payload uses the separate spelling `references_urls`.

Minimal valid submission: the Bearer header and only `prompt` as a form field. Unknown fields are rejected. The wrapper permits other nonblank model/option strings, but their actual availability is determined by Plenx; the defaults above are the supplied integration contract.

Example accepted response:

```json
{
  "task_id": "7de6c5a3-b599-40ab-a5ae-ee2112030dde",
  "status": "queued",
  "message": "Flow Veo3 T2V task submitted",
  "account_id": "b332c52f-4771-4f97-93a7-0e02c1b1cac0",
  "request_id": "bbbe39bd-f3b3-40cc-85b1-c8f75f70bf39"
}
```

Poll `GET /api/v1/video/status/{task_id}` with the wrapper password and original account ID:

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/v1/video/status/$TASK_ID" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID"
```

Use the real submitted UUID as `TASK_ID`. Status responses include `status`, `progress`, `message`, `current_step`, and `result_url`. While processing, `result_url` may be null. A completed video exposes its media URL at the **top-level `result_url`**, not inside `result`. Fields such as `duration` and `generation_time` may remain null after completion. Return the actual URL received from the service.

## Image generation

`POST /api/v1/queue/submit` — Bearer header and `Content-Type: application/json`.

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/v1/queue/submit" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID" \
  -H 'Content-Type: application/json' \
  --data '{
    "task_type": "image/gem-pix-2",
    "payload": {
      "prompt": "a cat",
      "aspect_ratio": "1:1",
      "num_outputs": 1,
      "guidance_scale": 3.5,
      "num_inference_steps": 20,
      "output_format": "jpeg",
      "go_fast": true,
      "resolution": "1k",
      "resolution_tier": "1k",
      "server_id": "dd597ae5-6ab3-4e76-9914-9da5589e21c1",
      "uses_credits": false,
      "references_urls": []
    }
  }'
```

Minimal valid JSON: `{"payload":{"prompt":"a cat"}}`.

Top-level fields: `task_type` defaults to `image/gem-pix-2` and only that literal is supported; `payload` is required; optional `workplace_id` is a UUID or null and defaults to the account's workplace. Unknown top-level or payload fields are rejected.

| `payload` field | Type / constraint | Default |
| --- | --- | --- |
| `prompt` | Required nonblank string; max 20,000 characters | None |
| `aspect_ratio` | Nonblank string; max 20 characters | `1:1` |
| `num_outputs` | Positive integer | `1` |
| `guidance_scale` | Finite number >= 0 | `3.5` |
| `num_inference_steps` | Positive integer | `20` |
| `output_format` | Nonblank string; max 20 characters | `jpeg` |
| `go_fast` | Boolean | `true` |
| `resolution` | Nonblank string; max 30 characters | `1k` |
| `resolution_tier` | Nonblank string; max 30 characters | `1k` |
| `server_id` | UUID | `dd597ae5-6ab3-4e76-9914-9da5589e21c1` |
| `uses_credits` | Boolean; does not override Plenx billing | `false` |
| `references_urls` | Array of HTTP/HTTPS URLs; max 20 entries | `[]` |

Preserve the spelling `references_urls`. Use a JSON array, not a comma-separated string. The server IDs for images and videos differ; do not swap them.

Example accepted queue response:

```json
{
  "success": true,
  "task_id": "fffe51f7-3dbd-482e-9f11-e5c08a0183ce",
  "status": "queued",
  "position": 1,
  "estimated_wait_seconds": 30,
  "message": "Task queued (reserved 2.0 credits)",
  "error": null,
  "account_id": "b332c52f-4771-4f97-93a7-0e02c1b1cac0",
  "request_id": "f9b8d4a2-c306-4a72-b4bd-5723d63c766b"
}
```

Poll `GET /api/v1/queue/status/{task_id}` with the wrapper password and original account ID:

```sh
curl --silent --show-error --fail-with-body "$PLENX_WRAPPER_BASE/api/v1/queue/status/$TASK_ID" \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID"
```

While queued/processing, `result` may be null. Completed image URLs are nested: prefer **`result.image_urls`** when it is a nonempty array; otherwise use **`result.result_url`** if present. Deduplicate URLs if using both. Do not look for the video's top-level `result_url` on image responses. Upstream may supply additional metadata; preserve it if needed, but do not require non-null queue position, message, timestamps, or generation ID to recognize completion.

## Polling and failure handling

Normalize status text to lowercase before comparison. HTTP success alone does not establish generation success.

- Active: `waiting`, `queued`, `pending`, `processing`, `running`, `submitting`, `in_progress`, `starting`. Local `waiting` has no upstream task ID yet; keep polling its local request ID.
- Completed: `completed`, `succeeded`, `success`. Extract the correct URL fields before reporting a result.
- Terminal failure: `failed`, `error`, `cancelled`, `canceled`. Report `error_message`, `error`, or `message` when available; stop polling.
- A valid terminal status response can be HTTP 200 with `success:false`. Inspect its status and error fields.
- For an unrecognized status, retain and report the raw response rather than interpreting it as completed.
- A transient status-GET failure leaves the saved job active. Retry GET with bounded backoff, honor `Retry-After`, and retain a finite deadline. A failed poll is not a reason to resubmit generation.

| HTTP result | Agent action |
| --- | --- |
| `202` on generation | Accepted into the persistent local queue. Save `request_id` and poll `status_url`; do not resubmit. `task_id` is initially null. |
| `400` | Read `detail`; a submission may have been rejected by Plenx. Correct the cause before intentionally making another submission. |
| `401` / `403` | Without `X-Plenx-Status`, fix the wrapper password. With `X-Plenx-Status`, the server-side Plenx credential was rejected; ask the operator to update that account. Do not resubmit or switch the job to another account. |
| `404` on status | Check original account ID, media-specific status route, and task UUID. The task must have been submitted through this wrapper. Do not resubmit merely to obtain a different task ID. |
| `409` | Add a server-side account if none exist, or supply the original `X-Account-ID` for an ambiguous task. Check `detail`. |
| `422` | Request validation failed before generation submission. Correct field names, nesting, types, UUIDs, and required prompt. |
| `429` | Honor `Retry-After` (seconds or HTTP date). Back off GET requests. Do not blindly replay a generation POST. |
| `503` with password-setup detail | Confirm `GET /auth/status` and ask the operator to complete first-visit password setup. API requests are blocked before upstream submission while setup is required. |
| `502` / `504` / other `5xx`, or a client transport error | A generation POST might have been accepted. Do not automatically repeat it. Inspect saved history and reconcile with Plenx first. GET requests can be retried within the polling deadline. |

Wrapper errors normally use `{"detail":"message"}`; validation errors use a `detail` array. Upstream HTTP status codes and `Retry-After` are retained when supplied; upstream HTTP errors also include `X-Plenx-Status` so they can be distinguished from wrapper authentication errors. Invalid upstream payloads become 502; upstream timeouts become 504. There is no guarantee that retrying a POST is safe, and no idempotency support to make it safe automatically.

## Saved history and recovery

`GET /api/requests` with the wrapper password returns all active accounts' history; include `X-Account-ID` to filter to one account. The response is `{"requests":[...]}`. Each entry includes `id` (local request UUID), `task_id` (upstream UUID or null), `account_id`, `kind` (`image` or `video`), `prompt`, `parameters`, `status`, `response`, `error`, `poll_error`, `created_at`, and `updated_at`.

- `id` is the local request ID used with `/api/requests/{request_id}`. Only upstream `task_id` belongs in media-specific status URLs.
- All active jobs and the latest 200 terminal jobs are returned per server account. Do not assume older history is complete.
- Use `kind` to choose the correct status route and `account_id` to select the original account when resuming a task.
- Local `submission_unknown` means the wrapper could not confirm acceptance, including a process stopping during submission. `task_id` may be null. It continues to reserve a concurrency slot and remains in history. Do not auto-replay that request; check Plenx and ask the operator to reconcile it before deciding whether a new submission is needed.
- Failed default-workplace lookup happens before generation submission and creates no job. Supply a valid actual workplace ID or resolve the account/profile error.
- History records request metadata and prompts. Plenx tokens are stored only in the private server account table, not returned in account/history/generation/profile responses. Account profiles are not persisted. Credentials echoed by an upstream response are redacted.

## Auxiliary endpoints

- `GET /health`: no token, returns local health only.
- `GET /auth/status`: no token, returns `{"setup_required":true}` or `{"setup_required":false}`; never returns the password.
- `POST /auth/setup`: operator-only first-time action, no token; JSON `{"password":"<chosen-password>","confirm_password":"<same-password>"}`. Returns `{"setup_required":false}` on success. A password must be nonempty printable ASCII without whitespace. A configured environment password or existing saved password makes this return 409; it is not a reset endpoint. Browser requests must come from the wrapper's own origin. The chosen password is stored privately on the server and reused on restart.
- `GET /api/config`: wrapper password required, returns default image/video server IDs, image capability, polling interval and `concurrency_limits:{"image":10,"video":10}`.
- `GET /llms.txt`: no token, this complete plain-text agent guide.

Generation and status requests depend on the upstream Plenx account. The implementation is tested with simulated upstream responses; do not treat example balances, queue estimates, or URLs as live account state.
