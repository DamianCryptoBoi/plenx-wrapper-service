# Plenx service: agent API guide

Use this document to operate the running service through HTTP. No browser interaction is needed. This document is available as plain text at `GET /llms.txt` without authentication.

## Connection and credentials

- Default base URL: `http://127.0.0.1:8000`. Use the operator's configured wrapper URL if different. All paths below target this wrapper, not plenxai.com directly.
- All `/api/*` requests require `Authorization: Bearer <PLENX_WRAPPER_PASSWORD>`. This is the operator's wrapper password. It is **not** a Plenx token. Never send a Plenx token in the Authorization header.
- Plenx credentials are stored on the server. Ordinary generation callers need only the wrapper password, not a Plenx token or access to browser localStorage.
- Call `GET /api/accounts` to obtain account IDs/names. Select one using `X-Account-ID: <account-uuid>` on profile and generation requests. If omitted, these routes use the first configured account. There is no automatic rotation or credit-based account selection.
- Save `task_id`, `account_id`, media type, and local `request_id` from the submission response. Polling resolves the task's saved account; you may include `X-Account-ID` to make the association explicit. A mismatched account returns 404. Ambiguous task IDs require the header and return 409 without it.
- Account IDs/history survive token replacement and wrapper-password changes. Do not switch the account of an existing task.
- Do not print secrets or put them in URLs, prompts, artifacts, or result messages. Do not forward the wrapper Authorization header to returned CDN media URLs.
- One shared wrapper password permits using and managing all configured accounts; this service does not implement separate caller/admin roles.
- If the operator has not configured a password, `GET /auth/status` returns `{"setup_required":true}` and `/api/*` returns HTTP 503. Ask the operator to open the dashboard and create/confirm a wrapper password, then provide it to the caller. Generation agents must not choose or reset the operator's password automatically. A configured environment password or an existing saved password skips setup.

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
4. Save the returned `task_id`, `account_id`, `request_id`, and media type before continuing. HTTP 200 with `status:"queued"` means accepted, not finished.
5. Poll the appropriate status GET about every five seconds, using the wrapper password and the original account ID. Use a finite caller-defined deadline. If that deadline expires, report the task ID and last status as still pending; do not submit another generation to restart it.
6. On a completed status, extract media URLs using the media-specific fields below. If completed has no usable URL, report the response anomaly rather than inventing a URL or claiming a deliverable exists.
7. On a failed/cancelled status, report the task error. On an uncertain submission, follow the recovery rules below.

No synchronous generation/wait endpoint, server-side polling worker, webhook, cancellation, account-token listing, or automatic account rotation is implemented. API clients must perform their own polling. The dashboard polls only while open and visible with auto refresh enabled.

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
  -F 'server_id=56947b53-029e-4751-bbca-b9de54f5d628'
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

- Active: `queued`, `pending`, `processing`, `running`, `submitting`, `in_progress`, `starting`.
- Completed: `completed`, `succeeded`, `success`. Extract the correct URL fields before reporting a result.
- Terminal failure: `failed`, `error`, `cancelled`, `canceled`. Report `error_message`, `error`, or `message` when available; stop polling.
- A valid terminal status response can be HTTP 200 with `success:false`. Inspect its status and error fields.
- For an unrecognized status, retain and report the raw response rather than interpreting it as completed.
- A transient status-GET failure leaves the saved job active. Retry GET with bounded backoff, honor `Retry-After`, and retain a finite deadline. A failed poll is not a reason to resubmit generation.

| HTTP result | Agent action |
| --- | --- |
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

- `id` is not a generation task ID; only `task_id` belongs in status URLs.
- All active jobs and the latest 200 terminal jobs are returned per server account. Do not assume older history is complete.
- Use `kind` to choose the correct status route and `account_id` to select the original account when resuming a task.
- Local `submission_unknown` means the wrapper could not confirm acceptance, including a process stopping during submission. `task_id` may be null. Do not auto-replay that request; check Plenx before deciding whether a new submission is needed.
- Failed default-workplace lookup happens before generation submission and creates no job. Supply a valid actual workplace ID or resolve the account/profile error.
- History records request metadata and prompts. Plenx tokens are stored only in the private server account table, not returned in account/history/generation/profile responses. Account profiles are not persisted. Credentials echoed by an upstream response are redacted.

## Auxiliary endpoints

- `GET /health`: no token, returns local health only.
- `GET /auth/status`: no token, returns `{"setup_required":true}` or `{"setup_required":false}`; never returns the password.
- `POST /auth/setup`: operator-only first-time action, no token; JSON `{"password":"<chosen-password>","confirm_password":"<same-password>"}`. Returns `{"setup_required":false}` on success. A password must be nonempty printable ASCII without whitespace. A configured environment password or existing saved password makes this return 409; it is not a reset endpoint. Browser requests must come from the wrapper's own origin. The chosen password is stored privately on the server and reused on restart.
- `GET /api/config`: wrapper password required, returns default image/video server IDs, image capability, and dashboard polling interval.
- `GET /llms.txt`: no token, this complete plain-text agent guide.

Generation and status requests depend on the upstream Plenx account. The implementation is tested with simulated upstream responses; do not treat example balances, queue estimates, or URLs as live account state.
