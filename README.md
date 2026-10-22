# Plenx service

FastAPI wrapper and small dashboard for Plenx image/video generation. Callers authenticate with a **wrapper password**. Plenx account tokens stay on the server.

## Run and set the wrapper password

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Set your own password with `PLENX_WRAPPER_PASSWORD` before starting:

```sh
PLENX_WRAPPER_PASSWORD='choose-your-wrapper-password' uv run uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

If `PLENX_WRAPPER_PASSWORD` is unset or empty and no password has been saved, the first dashboard visit asks you to create and confirm a password. API access remains locked with HTTP 503 until setup completes. The password is saved in `data/wrapper-password` (beside the configured SQLite database), with file mode `0600`, and reused on restart. Setup works only once; it cannot replace an existing password. Existing saved passwords from earlier installations continue to work.

Passwords must use printable ASCII without whitespace. Changing `PLENX_WRAPPER_PASSWORD` or the saved password file takes effect after a server restart and does not change job ownership. A nonempty environment variable takes precedence over the saved password file. Server settings can also be set in `.env` beside `main.py`; it is loaded automatically at startup, and existing process environment variables take precedence over `.env` values. See [.env.example](.env.example).

Open [the dashboard](http://127.0.0.1:8000), enter the wrapper password, then add Plenx accounts. The wrapper password stays in page memory only; reload/logout requires signing in again. Plenx tokens are submitted once during account setup or replacement, stored in the private server database, and never returned by the API or saved in browser storage.

Existing accounts from the previous localStorage-based dashboard are imported after successful login. Each browser credential is removed only after the server saves that account. Importing the same token twice is safe, and its old job history is moved to the server account ID. If migration fails, remaining browser credentials are retained for retry and an error is shown.

The shared wrapper password grants access to all configured accounts, generation, history, and account management. There are no separate user/admin roles. Keep the service bound to localhost, or use HTTPS when accessing it across a network. SQLite contains plaintext Plenx credentials and is restricted to file mode `0600`; protect its backups too.

## VPS setup with PM2

On an Ubuntu/Debian VPS with PM2 already installed, copy/clone the project and run this as the user who owns PM2 (with sudo access for packages and UFW):

```sh
bash scripts/setup-vps.sh          # TCP 8000
# Or choose another port:
bash scripts/setup-vps.sh 9000
```

The script installs uv if missing, syncs the locked Python dependencies, starts/restarts one `plenx-service` PM2 process on `0.0.0.0`, checks `/health`, adds a UFW TCP allow rule, and saves the PM2 process list. It preserves existing accounts/passwords and UFW rules; an inactive UFW stays inactive. If your provider has a separate firewall/security group, allow the same TCP port there too.

Open `http://<VPS-IP>:8000/` (or your chosen port) to create the wrapper password on first visit. An existing saved password or exported `PLENX_WRAPPER_PASSWORD` skips setup. Use HTTPS via a reverse proxy for credentials sent over the internet.

```sh
pm2 logs plenx-service
pm2 restart plenx-service
pm2 stop plenx-service
```

For reboot restoration, run `pm2 startup` once as the same user, execute the sudo command it prints, then run `pm2 save`. The installer already saves the process list, so an existing PM2 boot setup will restore it. These follow the official [uv installer](https://docs.astral.sh/uv/reference/installer/), [PM2 configuration](https://pm2.keymetrics.io/docs/usage/application-declaration/), and [PM2 startup](https://pm2.keymetrics.io/docs/usage/startup/) instructions.

After updating the project, rerun the setup script with the same port to sync dependencies and restart. `ecosystem.config.js` is the PM2 launch configuration; `PLENX_PORT` and `PLENX_UV_BIN` select the port and uv executable when loading it directly. The setup script supplies both automatically. No Uvicorn reload watcher or extra workers run on the VPS.

## Telegram startup and token alerts

Create `.env` beside `main.py` (copy [.env.example](.env.example) if needed), then fill in these server-only values:

```dotenv
TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
TELEGRAM_CHAT_ID="your-chat-id"
```

Run `uv sync --locked` after updating the code to install the new dependency (the VPS setup script does this too). Run the VPS setup script for a new installation, or `pm2 restart plenx-service` for an existing installation. Restart again after changing `.env`; edits are not applied live. `.env` is ignored by Git. Restrict access with `chmod 600 .env`.

The bot must be able to send messages to the destination chat. For a private chat, send `/start` to your bot first. The chat ID can identify your private chat or a group. Notifications use Telegram's [sendMessage API](https://core.telegram.org/bots/api#sendmessage). Keep these settings on the server; do not put them in generation requests or commit them. Exported process/PM2 values take precedence over `.env`, including empty values. To use exports instead, set both variables and run `pm2 restart plenx-service --update-env`, then `pm2 save`. PM2 saves its process environment, so protect its home directory and backups too.

The service sends a startup notice for each application start/restart, checks every configured Plenx account immediately, then checks `/api/v1/users/me` every **300 seconds**. Accounts without active generation jobs are included. It runs independently of the dashboard and generation queue.

A failed check sends the account name/ID, service hostname, time, and a safe reason. HTTP 401/403 is identified as rejected authentication; timeouts and other upstream failures are reported as check failures, without claiming the token expired. Inactive accounts and invalid profile responses also fail the check. Plenx tokens, wrapper passwords, and raw upstream response bodies are never included in alerts.

Only one alert is sent per ongoing failure for a particular account token. A successful check or token replacement re-arms the alert. Failed Telegram deliveries, including startup notices, are retried at the next five-minute check. Telegram failures do not prevent startup, API requests, or generation. The monitor does not alter queue reservations or automatically replace credentials. Without both Telegram settings, checks still run, but notifications are disabled and startup logs explain the missing configuration.

## API for agents

Read the complete [agent API guide](docs/agent-api.md), also served as plain text at [llms.txt](http://127.0.0.1:8000/llms.txt). It covers exact requests, account selection, polling, media extraction, and uncertain-submission recovery.

All `/api/*` routes require:

```http
Authorization: Bearer <wrapper-password>
```

Select a server account using:

```http
X-Account-ID: <account-uuid>
```

Generation/profile calls use the first configured account when this header is omitted. Status calls resolve the account from the saved task; an explicit header must match. Submission and status responses preserve upstream fields (with credential redaction) and add `account_id` and `request_id`. The caller password is never sent to Plenx.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/accounts` | Account IDs/names/readiness, never tokens |
| POST | `/api/accounts` | Save account; JSON `{ "name": "Personal", "token": "<plenx-token>" }` |
| PATCH | `/api/accounts/{account_id}` | Rename or replace token; JSON `name` required, `token` optional |
| DELETE | `/api/accounts/{account_id}` | Remove active account credential; keep history for restoration |
| GET | `/api/v1/users/me` | Profile, balances, and default workplace for selected account |
| POST | `/api/v1/veo/generate` | Submit video as multipart form data |
| GET | `/api/v1/video/status/{task_id}` | Poll video |
| POST | `/api/v1/queue/submit` | Submit image as JSON queue envelope |
| GET | `/api/v1/queue/status/{task_id}` | Poll image |
| GET | `/api/requests` | All configured accounts' history; optional account header filters |
| GET | `/api/requests/{request_id}` | Track a local queued/submitted request and its saved upstream result |
| GET | `/api/config` | Default generation configuration |

`GET /health`, `GET /llms.txt`, `GET /auth/status`, and dashboard/static/documentation pages are public; they expose no stored credentials or account data. `POST /auth/setup` creates a password only when none is configured; it requires matching JSON `password` and `confirm_password` fields. [Swagger](http://127.0.0.1:8000/docs), [ReDoc](http://127.0.0.1:8000/redoc), and [OpenAPI JSON](http://127.0.0.1:8000/openapi.json) remain available.

## Caller examples

An operator first adds Plenx accounts through the dashboard. Generation callers only need the wrapper password.

```sh
export PLENX_WRAPPER_PASSWORD='your-wrapper-password'
curl http://127.0.0.1:8000/api/accounts \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD"
```

Use an account ID from that response:

```sh
export ACCOUNT_ID='account-uuid-from-api'
curl http://127.0.0.1:8000/api/v1/veo/generate \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID" \
  -F 'prompt=a cat'
```

For a video guided by reference images, add a single multipart field containing a JSON array of HTTP/HTTPS image URLs:

```sh
--form-string 'reference_images=["https://example.test/reference.png"]'
```

The dashboard accepts one reference URL per line in Video mode. This field is optional; omit it or use `[]` for text-only video. Video references use `reference_images`, distinct from `references_urls` in the image-generation JSON payload.

```sh
curl http://127.0.0.1:8000/api/v1/queue/submit \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID" \
  -H 'Content-Type: application/json' \
  --data '{"payload":{"prompt":"a cat"}}'
```

Defaults: video uses `omni-flash`, `16:9`, 4 seconds, 720, and server `56947b53-029e-4751-bbca-b9de54f5d628`; image uses `image/gem-pix-2`, `1:1`, one JPEG at `1k`, and server `dd597ae5-6ab3-4e76-9914-9da5589e21c1`. Both resolve omitted workplace IDs from the selected account's profile. You can override the workplace with an actual authorized UUID.

Each account can run **10 image generation requests and 10 video generation requests in parallel**, independently. A slot stays occupied while submitting, queued at Plenx, or processing. An image request with multiple outputs counts as one request. Extra requests are saved in a FIFO SQLite queue and return HTTP 202 with `status:"waiting"`, `task_id:null`, `request_id`, `queue_position`, and a `status_url` such as `/api/requests/<request_id>`. Do not resubmit an accepted queued request.

Poll `/api/requests/{request_id}` with the wrapper password for queued or submitted work. Its `response` field contains the upstream result after submission. Once an upstream `task_id` exists, you can also poll the original matching video/queue status route for the upstream response directly. The server checks tasks and starts waiting jobs every five seconds, including when the browser is closed. The queue survives restarts. Removing an account cancels its unsent waiting jobs; deleting/re-adding the same token restores other history without resubmitting cancelled work.

The saved job selects its original account automatically. Upstream token replacement preserves the account ID and history. Existing jobs submitted before the limit was installed finish normally; new jobs wait until capacity is available.

`GET /api/v1/users/me` reports separate `credit_balance`, `free_credits`, and `ruby_balance` values and daily image usage/limit. The dashboard refreshes jobs every five seconds and balances once a minute, plus around submissions/completions and manual refresh. Profiles stay in page memory. Upstream 401/403 responses include `X-Plenx-Status`; they mean the operator needs to fix that server-side account credential. A wrapper-auth 401 does not include that header.

History keeps all active, waiting and unresolved jobs plus the latest 200 terminal jobs per account. A failed poll keeps the task active. Generation POSTs are never retried automatically; uncertain acceptance is saved as `submission_unknown` and retains its slot pending operator reconciliation. Automatic work honors upstream 429 backoff and pauses an account on upstream 401/403 until it is updated. Clients poll saved results; the dashboard refreshes its view while visible and auto refresh is enabled. Disabling that display refresh does not pause the server queue.

## Configuration and checks

| Variable | Default |
| --- | --- |
| `PLENX_WRAPPER_PASSWORD` | First-visit setup; saved in the private `wrapper-password` file beside the database |
| `PLENX_BASE_URL` | `https://plenxai.com` |
| `PLENX_DB_PATH` | `data/jobs.sqlite3` beside `main.py` |
| `TELEGRAM_BOT_TOKEN` | Unset; Telegram notifications require this and `TELEGRAM_CHAT_ID` |
| `TELEGRAM_CHAT_ID` | Unset; destination private/group chat ID |

Server settings are loaded at startup from `.env` beside `main.py`, with the existing process environment taking precedence. Missing `.env` is allowed. Use one Uvicorn worker. Keep `PLENX_BASE_URL` pointed at a trusted server: it receives the stored Plenx credentials. `PLENX_PORT` and `PLENX_UV_BIN` configure the PM2 launcher through its process environment or VPS script; the app's `.env` loader does not configure that launcher.

```sh
uv run python -m unittest discover -s tests -v
node --check static/app.js
node tests/test_dashboard.cjs
node tests/test_pm2.cjs
bash -n scripts/setup-vps.sh
```

Tests use simulated Plenx responses and real FastAPI/SQLite. They do not spend credits. Live balances, model availability, and generation acceptance require a real configured Plenx account.
