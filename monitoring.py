"""Server-side account token checks and private Telegram operator notices."""

import asyncio
from datetime import datetime, timezone
import logging
import os
import socket
import time

import httpx


TOKEN_CHECK_INTERVAL = 300
logger = logging.getLogger(__name__)


class ServiceMonitor:
    def __init__(self, app, telegram: httpx.AsyncClient):
        self.app = app
        self.telegram = telegram
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.notified_failures = {}
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.instance = socket.gethostname()
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram alerts disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")

        # HTTPX INFO logs include URLs; Telegram's URL contains the bot credential.
        def redact_bot_token(record):
            if self.bot_token:
                record.msg = record.getMessage().replace(self.bot_token, "[redacted]")
                record.args = ()
            return True

        self.log_filter = redact_bot_token
        logging.getLogger("httpx").addFilter(self.log_filter)

    def close(self):
        logging.getLogger("httpx").removeFilter(self.log_filter)

    async def notify(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        try:
            response = await self.telegram.post(
                f"/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text.replace(self.bot_token, "[redacted]")[:4096]},
            )
            data = response.json()
            if response.is_success and isinstance(data, dict) and data.get("ok") is True:
                return True
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            pass
        # Exceptions, URLs and Telegram response bodies may contain credentials.
        logger.warning("Telegram notice could not be delivered; will retry on the next check.")
        return False

    async def check_accounts(self):
        store = self.app.state.store
        accounts = store.accounts()
        active_ids = {account["id"] for account in accounts}
        self.notified_failures = {key: value for key, value in self.notified_failures.items() if key in active_ids}
        network_slots = asyncio.Semaphore(5)

        async def check(account_id):
            async with network_slots:
                account = store.account(account_id)
                if account is None:
                    return
                reason = None
                try:
                    response = await self.app.state.upstream.get(
                        "/api/v1/users/me", headers={"Authorization": f"Bearer {account['token']}"}, timeout=10,
                    )
                    if not response.is_success:
                        label = "Authentication rejected" if response.status_code in {401, 403} else "Upstream check failed"
                        reason = f"{label} (HTTP {response.status_code})"
                    else:
                        data = response.json()
                        if not isinstance(data, dict) or not data.get("id") or data.get("success") is False:
                            reason = "Invalid profile response"
                        elif data.get("is_active") is False:
                            reason = "Account is inactive"
                except httpx.TimeoutException:
                    reason = "Profile request timed out"
                except httpx.RequestError:
                    reason = "Could not reach Plenx"
                except ValueError:
                    reason = "Invalid profile response"

                current = store.account(account_id)
                if current is None or current["token_hash"] != account["token_hash"]:
                    return  # The operator removed/replaced this credential during the check.
                if reason is None:
                    self.notified_failures.pop(account_id, None)
                    return
                if self.notified_failures.get(account_id) == account["token_hash"]:
                    return
                name = current["name"].replace(account["token"], "[redacted]")
                message = (
                    f"Plenx token check failed\nService: {self.instance}\nAccount: {name}\n"
                    f"Account ID: {account_id}\nReason: {reason}\n"
                    f"Time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                )
                if await self.notify(message):
                    self.notified_failures[account_id] = account["token_hash"]

        await asyncio.gather(*(check(account["id"]) for account in accounts))

    async def run(self):
        startup_sent = False
        while True:
            started = time.monotonic()
            try:
                if not startup_sent:
                    startup_sent = await self.notify(
                        f"Plenx service started\nService: {self.instance}\nTime: {self.started_at}"
                    )
                await self.check_accounts()
            except Exception as error:
                logger.warning("Token check cycle failed (%s); will check again.", type(error).__name__)
            await asyncio.sleep(max(0, TOKEN_CHECK_INTERVAL - (time.monotonic() - started)))
