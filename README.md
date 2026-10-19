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

Passwords must use printable ASCII without whitespace. Changing `PLENX_WRAPPER_PASSWORD` or the saved password file takes effect after a server restart and does not change job ownership. A nonempty environment variable takes precedence over the file. `.env` files are not loaded automatically.

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

```sh
curl http://127.0.0.1:8000/api/v1/queue/submit \
  -H "Authorization: Bearer $PLENX_WRAPPER_PASSWORD" \
  -H "X-Account-ID: $ACCOUNT_ID" \
  -H 'Content-Type: application/json' \
  --data '{"payload":{"prompt":"a cat"}}'
```

Defaults: video uses `omni-flash`, `16:9`, 4 seconds, 720, and server `56947b53-029e-4751-bbca-b9de54f5d628`; image uses `image/gem-pix-2`, `1:1`, one JPEG at `1k`, and server `dd597ae5-6ab3-4e76-9914-9da5589e21c1`. Both resolve omitted workplace IDs from the selected account's profile. You can override the workplace with an actual authorized UUID.

Poll the returned task ID using the matching video or queue status route, with the wrapper password. The saved job selects its original account automatically. Upstream token replacement preserves the account ID and history; deleting and re-adding the same token restores its account ID/history.

`GET /api/v1/users/me` reports separate `credit_balance`, `free_credits`, and `ruby_balance` values and daily image usage/limit. The dashboard refreshes jobs every five seconds and balances once a minute, plus around submissions/completions and manual refresh. Profiles stay in page memory. Upstream 401/403 responses include `X-Plenx-Status`; they mean the operator needs to fix that server-side account credential. A wrapper-auth 401 does not include that header.

History keeps all active jobs plus the latest 200 terminal jobs per account. A failed poll keeps the task active. Generation POSTs are never retried automatically; uncertain acceptance is saved as `submission_unknown`. API clients must implement their own polling; dashboard polling runs only while the page is visible and auto refresh is enabled.

## Configuration and checks

| Variable | Default |
| --- | --- |
| `PLENX_WRAPPER_PASSWORD` | First-visit setup; saved in the private `wrapper-password` file beside the database |
| `PLENX_BASE_URL` | `https://plenxai.com` |
| `PLENX_DB_PATH` | `data/jobs.sqlite3` beside `main.py` |

Environment variables are read at startup. Use one Uvicorn worker. Keep `PLENX_BASE_URL` pointed at a trusted server: it receives the stored Plenx credentials.

```sh
uv run python -m unittest discover -s tests -v
node --check static/app.js
node tests/test_dashboard.cjs
node tests/test_pm2.cjs
bash -n scripts/setup-vps.sh
```

Tests use simulated Plenx responses and real FastAPI/SQLite. They do not spend credits. Live balances, model availability, and generation acceptance require a real configured Plenx account.
