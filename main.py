"""Local Plenx API wrapper. Run: uv run uvicorn main:app --reload."""

from contextlib import asynccontextmanager
from hashlib import sha256
from hmac import compare_digest
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError

from store import Store


ROOT = Path(__file__).resolve().parent
VIDEO_SERVER_ID = "56947b53-029e-4751-bbca-b9de54f5d628"
IMAGE_SERVER_ID = "dd597ae5-6ab3-4e76-9914-9da5589e21c1"
bearer = HTTPBearer(auto_error=False)


class VideoRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    prompt: str = Field(min_length=1, max_length=20000)
    model: str = Field(default="omni-flash", min_length=1, max_length=100)
    aspect_ratio: str = Field(default="16:9", min_length=1, max_length=20)
    duration: int = Field(default=4, gt=0)
    resolution: str = Field(default="720", min_length=1, max_length=30)
    server_id: UUID = UUID(VIDEO_SERVER_ID)
    workplace_id: UUID | None = None


class ImagePayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    prompt: str = Field(min_length=1, max_length=20000)
    aspect_ratio: str = Field(default="1:1", min_length=1, max_length=20)
    num_outputs: int = Field(default=1, gt=0)
    guidance_scale: float = Field(default=3.5, ge=0, allow_inf_nan=False)
    num_inference_steps: int = Field(default=20, gt=0)
    output_format: str = Field(default="jpeg", min_length=1, max_length=20)
    go_fast: bool = True
    resolution: str = Field(default="1k", min_length=1, max_length=30)
    resolution_tier: str = Field(default="1k", min_length=1, max_length=30)
    server_id: UUID = UUID(IMAGE_SERVER_ID)
    uses_credits: bool = False
    references_urls: list[HttpUrl] = Field(default_factory=list, max_length=20)


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_type: Literal["image/gem-pix-2"] = "image/gem-pix-2"
    payload: ImagePayload
    workplace_id: UUID | None = None


class CreditBalances(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    credit_balance: float
    free_credits: float
    ruby_balance: float


class AccountWrite(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    token: SecretStr | None = None


class PasswordSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr
    confirm_password: SecretStr


def valid_password(password: str) -> bool:
    return bool(password) and all(33 <= ord(char) <= 126 for char in password)


def load_wrapper_password(directory: Path) -> str | None:
    password = os.getenv("PLENX_WRAPPER_PASSWORD")
    if not password:
        path = directory / "wrapper-password"
        if not path.exists():
            return None
        path.chmod(0o600)
        password = path.read_text().strip()
    if not valid_password(password):
        raise RuntimeError("Wrapper password must use printable ASCII characters without whitespace")
    return password


def configured_password_digest(request: Request):
    if request.app.state.password_digest is None:
        # Another process may have completed initial setup since this process started.
        password = load_wrapper_password(request.app.state.password_directory)
        if password is not None:
            request.app.state.password_digest = sha256(password.encode()).digest()
    return request.app.state.password_digest


async def require_wrapper_password(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    expected = configured_password_digest(request)
    if expected is None:
        raise HTTPException(503, "Wrapper password setup required. Open the dashboard to set it.")
    supplied = credentials.credentials if credentials else ""
    digest = sha256(supplied.encode()).digest()
    if not supplied or not compare_digest(digest, expected):
        raise HTTPException(401, "Invalid or missing wrapper password", headers={"WWW-Authenticate": 'Bearer realm="plenx-wrapper"'})


AccountHeader = Annotated[UUID | None, Header(alias="X-Account-ID")]


async def get_account(request: Request, account_id: AccountHeader = None):
    account = request.app.state.store.account(str(account_id) if account_id else None)
    if not account:
        raise HTTPException(404 if account_id else 409, "Account not found" if account_id else "Add a server-side Plenx account first")
    return account


def account_token(fields: AccountWrite) -> str | None:
    if fields.token is None:
        return None
    token = fields.token.get_secret_value().strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or len(token) > 20000 or any(not 33 <= ord(char) <= 126 for char in token):
        raise HTTPException(422, "Plenx token must use printable ASCII characters without whitespace")
    return token


def redact(value, token: str):
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {redact(key, token): redact(item, token) for key, item in value.items()}
    return value


def get_upstream(request: Request) -> httpx.AsyncClient:
    return request.app.state.upstream


async def plenx_request(client: httpx.AsyncClient, method: str, path: str, token: str, *, fields=None, body=None) -> dict:
    try:
        response = await client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body,
            files={key: (None, str(value)) for key, value in fields.items()} if fields else None,
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Plenx timed out.") from None
    except httpx.RequestError:
        raise HTTPException(502, "Could not reach Plenx.") from None
    if not response.is_success:
        try:
            error = response.json()
        except ValueError:
            error = response.text or "Empty upstream response"
        detail = str(redact(error, token))[:2000]
        headers = {"X-Plenx-Status": str(response.status_code)}
        if "retry-after" in response.headers:
            headers["Retry-After"] = response.headers["retry-after"]
        code = response.status_code if 400 <= response.status_code <= 599 else 502
        raise HTTPException(code, f"Plenx: {detail}", headers=headers)
    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "Plenx returned an invalid JSON response") from None
    if not isinstance(data, dict):
        raise HTTPException(502, "Plenx returned an invalid JSON object")
    return redact(data, token)


async def account_profile(client: httpx.AsyncClient, token: str) -> dict:
    data = await plenx_request(client, "GET", "/api/v1/users/me", token)
    try:
        CreditBalances.model_validate(data)
    except ValidationError:
        raise HTTPException(502, "Plenx returned invalid account credit balances") from None
    return data


async def task_request(client: httpx.AsyncClient, method: str, path: str, token: str, **kwargs) -> dict:
    data = await plenx_request(client, method, path, token, **kwargs)
    # A failed status envelope is still a valid task update. Submission rejections
    # without a task ID, and application-level errors without terminal state, are errors.
    terminal_failure = method == "GET" and isinstance(data, dict) and str(data.get("status", "")).lower() in {"failed", "error", "cancelled", "canceled"}
    if isinstance(data, dict) and data.get("success") is False and not terminal_failure:
        detail = str(data.get("error") or data.get("error_message") or data.get("message") or "Task rejected")
        raise HTTPException(400, f"Plenx: {detail.replace(token, '[redacted]')[:2000]}")
    if not isinstance(data, dict) or not isinstance(data.get("status"), str) or not data["status"].strip():
        raise HTTPException(502, "Plenx returned an invalid task response")
    try:
        UUID(data["task_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HTTPException(502, "Plenx returned an invalid task ID") from None
    return data


async def submit(request: Request, account: dict, client: httpx.AsyncClient, kind: str, params: dict):
    token = account["token"]
    if params["workplace_id"] is None:
        profile = await account_profile(client, token)
        try:
            params["workplace_id"] = str(UUID(profile.get("default_workplace_id")))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(502, "Plenx did not return a valid default workplace ID. Supply workplace_id explicitly.") from None
    store = request.app.state.store
    job_id = store.create(account["id"], kind, params)
    try:
        if kind == "video":
            data = await task_request(client, "POST", "/api/v1/veo/generate", token, fields=params)
        else:
            data = await task_request(client, "POST", "/api/v1/queue/submit", token, body=params)
    except HTTPException as error:
        # No automatic retries: a network failure can hide an accepted generation.
        if error.status_code >= 500:
            error.detail += " A submission may have been accepted; check Plenx before retrying."
        store.fail(job_id, "submission_unknown" if error.status_code >= 500 else "failed", error.detail)
        raise
    store.update(job_id, data)
    return {**data, "account_id": account["id"], "request_id": job_id}


async def poll(request: Request, client: httpx.AsyncClient, kind: str, task_id: UUID, account_id: UUID | None):
    store = request.app.state.store
    try:
        job = store.find(str(account_id) if account_id else None, kind, str(task_id))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if not job:
        raise HTTPException(404, "Task not found for this account and media type. Submit through this wrapper first.")
    path = f"/api/v1/{'video' if kind == 'video' else 'queue'}/status/{task_id}"
    account = store.account(job["owner"])
    try:
        data = await task_request(client, "GET", path, account["token"])
        if data["task_id"] != str(task_id):
            raise HTTPException(502, "Plenx returned a different task ID")
    except HTTPException as error:
        store.poll_error(job["id"], error.detail)
        raise
    store.update(job["id"], data)
    return {**data, "account_id": account["id"], "request_id": job["id"]}


Account = Annotated[dict, Depends(get_account)]
Upstream = Annotated[httpx.AsyncClient, Depends(get_upstream)]


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_path = Path(os.getenv("PLENX_DB_PATH", str(ROOT / "data/jobs.sqlite3")))
        store = Store(db_path)
        app.state.store = store
        try:
            app.state.password_directory = db_path.parent
            password = load_wrapper_password(db_path.parent)
            app.state.password_digest = sha256(password.encode()).digest() if password is not None else None
            async with httpx.AsyncClient(
                base_url=os.getenv("PLENX_BASE_URL", "https://plenxai.com"),
                timeout=httpx.Timeout(60, connect=10), follow_redirects=False,
            ) as client:
                app.state.upstream = client
                yield
        finally:
            store.close()

    app = FastAPI(
        title="Plenx service", version="0.1.0", lifespan=lifespan,
        description=(
            "Local wrapper for Plenx image/video generation and account credits. "
            "Use **Authorize** with your wrapper password; clients send `Authorization: Bearer <wrapper-password>`. "
            "Plenx tokens are stored on the server and never returned. Select accounts with `X-Account-ID`. "
            "Video submission uses multipart form data; image submission uses a JSON queue envelope. "
            "Omitted workplace IDs resolve from the same account's `/api/v1/users/me` profile. "
            "Generation responses include account_id and request_id. Polling resolves the task's original account."
        ),
    )
    api = APIRouter(dependencies=[Depends(require_wrapper_password)])

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Never echo submitted credential values in validation responses.
        return JSONResponse(status_code=422, content={"detail": [
            {key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()
        ]})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' https: data:; media-src 'self' https:; connect-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return response

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(ROOT / "static/index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/llms.txt", include_in_schema=False)
    async def agent_guide():
        return FileResponse(ROOT / "docs/agent-api.md", media_type="text/plain", headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/auth/status", tags=["Authentication"])
    async def auth_status(request: Request):
        return {"setup_required": configured_password_digest(request) is None}

    @app.post("/auth/setup", tags=["Authentication"])
    async def setup_password(request: Request, fields: PasswordSetup):
        """Set the wrapper password once, only when no environment/saved password exists."""
        if configured_password_digest(request) is not None:
            raise HTTPException(409, "A wrapper password is already configured. Sign in instead.")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Password setup must be submitted from this service's own page")
        password = fields.password.get_secret_value()
        if not valid_password(password):
            raise HTTPException(422, "Choose a nonempty password using printable ASCII without whitespace")
        if password != fields.confirm_password.get_secret_value():
            raise HTTPException(422, "Passwords do not match")
        path = request.app.state.password_directory / "wrapper-password"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise HTTPException(409, "A wrapper password was already saved. Sign in instead.") from None
        with os.fdopen(fd, "w") as file:
            file.write(password + "\n")
        request.app.state.password_digest = sha256(password.encode()).digest()
        return {"setup_required": False}

    @api.get("/api/config")
    async def config():
        return {"video_server_id": VIDEO_SERVER_ID, "image_server_id": IMAGE_SERVER_ID,
                "image_enabled": True, "poll_interval_seconds": 5}

    @api.post("/api/v1/veo/generate", tags=["Video"], summary="Submit a video generation")
    async def generate_video(request: Request, fields: Annotated[VideoRequest, Form(media_type="multipart/form-data")], account: Account, client: Upstream):
        """`prompt` is required; other fields default to the captured Plenx video request.

        Returns the upstream `task_id`, `status`, and `message`. Poll the task with
        `GET /api/v1/video/status/{task_id}`. Do not automatically retry timed-out
        submissions: Plenx may have accepted them.
        """
        return await submit(request, account, client, "video", fields.model_dump(mode="json"))

    @api.get("/api/v1/users/me", tags=["Accounts"], summary="Get profile, credits, and default workplace")
    async def users_me(account: Account, client: Upstream):
        """Returns the full Plenx profile, including separate `credit_balance`,
        `free_credits`, and `ruby_balance` values, daily image usage/limit, and
        `default_workplace_id`. Profiles are not persisted or cached by this service.
        """
        return await account_profile(client, account["token"])

    @api.post("/api/v1/queue/submit", tags=["Image"], summary="Submit an image generation")
    async def generate_image(request: Request, fields: ImageRequest, account: Account, client: Upstream):
        """The minimal body is `{"payload":{"prompt":"a cat"}}`.

        The task type defaults to `image/gem-pix-2`. Returns the upstream queue
        response including `task_id`; poll `GET /api/v1/queue/status/{task_id}`.
        Plenx determines credit charges, including when `uses_credits` is false.
        """
        return await submit(request, account, client, "image", fields.model_dump(mode="json"))

    @api.get("/api/v1/video/status/{task_id}", tags=["Video"], summary="Poll a video generation")
    async def video_status(task_id: UUID, request: Request, client: Upstream, account_id: AccountHeader = None):
        """Uses the task's saved account. Completed videos expose `result_url`.
        Poll failures preserve the saved task state and can be retried.
        """
        return await poll(request, client, "video", task_id, account_id)

    @api.get("/api/v1/queue/status/{task_id}", tags=["Image"], summary="Poll an image generation")
    async def image_status(task_id: UUID, request: Request, client: Upstream, account_id: AccountHeader = None):
        """Uses the task's saved account. Completed images expose nested
        `result.image_urls` and `result.result_url`; upstream fields are preserved.
        """
        return await poll(request, client, "image", task_id, account_id)

    @api.get("/api/requests", tags=["Requests"], summary="List saved jobs")
    async def requests(request: Request, account_id: AccountHeader = None):
        """Returns all current jobs and the latest 200 terminal jobs per account.
        Each entry includes parameters, the last upstream response, and any errors.
        """
        store = request.app.state.store
        if account_id and not store.account(str(account_id)):
            raise HTTPException(404, "Account not found")
        selected = [store.account(str(account_id))] if account_id else [store.account(item["id"]) for item in store.accounts()]
        jobs = [job for account in selected for job in redact(store.list(account["id"]), account["token"])]
        return {"requests": sorted(jobs, key=lambda job: job["created_at"], reverse=True)}

    @api.get("/api/accounts", tags=["Accounts"])
    async def accounts(request: Request):
        return {"accounts": request.app.state.store.accounts()}

    @api.post("/api/accounts", tags=["Accounts"], status_code=201)
    async def add_account(request: Request, fields: AccountWrite):
        token = account_token(fields)
        if token is None:
            raise HTTPException(422, "A server-side Plenx token is required")
        return request.app.state.store.save_account(fields.name, token)

    @api.patch("/api/accounts/{account_id}", tags=["Accounts"])
    async def update_account(account_id: UUID, request: Request, fields: AccountWrite):
        store = request.app.state.store
        if not store.account(str(account_id)):
            raise HTTPException(404, "Account not found")
        try:
            return store.save_account(fields.name, account_token(fields), str(account_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "This Plenx token already belongs to another account") from None

    @api.delete("/api/accounts/{account_id}", tags=["Accounts"], status_code=204)
    async def remove_account(account_id: UUID, request: Request):
        store = request.app.state.store
        if not store.account(str(account_id)):
            raise HTTPException(404, "Account not found")
        store.remove_account(str(account_id))
        return Response(status_code=204)

    app.include_router(api)
    app.mount("/static", StaticFiles(directory=ROOT / "static", check_dir=False), name="static")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
