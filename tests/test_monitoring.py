import asyncio
import json
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx


class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import monitoring
        from store import Store
        self.monitoring = monitoring
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.first = self.store.save_account("Studio", "private-plenx-a")
        self.second = self.store.save_account("Backup", "private-plenx-b")
        self.results = {"private-plenx-a": httpx.Response(200, json={"id": "user-a"}),
                        "private-plenx-b": httpx.Response(200, json={"id": "user-b"})}
        self.profiles = []
        self.messages = []
        self.telegram_response = httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        def profile(request):
            self.profiles.append(request)
            token = request.headers["authorization"].removeprefix("Bearer ")
            result = self.results[token]
            if callable(result):
                return result(request)
            if isinstance(result, Exception):
                raise result
            return result

        def telegram(request):
            self.messages.append(request)
            return self.telegram_response

        self.upstream = httpx.AsyncClient(base_url="https://plenxai.com", transport=httpx.MockTransport(profile))
        self.telegram = httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(telegram))
        self.addAsyncCleanup(self.upstream.aclose)
        self.addAsyncCleanup(self.telegram.aclose)
        self.app = SimpleNamespace(state=SimpleNamespace(store=self.store, upstream=self.upstream))
        self.env = patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "123456:test-bot-token", "TELEGRAM_CHAT_ID": "-10012345"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.monitor = monitoring.ServiceMonitor(self.app, self.telegram)
        self.addCleanup(self.monitor.close)

    def texts(self):
        return [json.loads(request.content)["text"] for request in self.messages]

    async def test_startup_notice_once_and_profiles_checked_every_five_minutes(self):
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
        with patch.object(self.monitoring.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.monitor.run()
        self.assertEqual(len(sleep.await_args_list), 2)
        for call in sleep.await_args_list:
            self.assertGreater(call.args[0], 299)
            self.assertLessEqual(call.args[0], 300)
        self.assertEqual(len(self.profiles), 4, "Both accounts are checked on each cycle even without jobs")
        self.assertEqual(len(self.messages), 1)
        self.assertIn("service started", self.texts()[0].lower())
        request = self.messages[0]
        self.assertEqual(str(request.url), "https://api.telegram.org/bot123456:test-bot-token/sendMessage")
        self.assertEqual(json.loads(request.content)["chat_id"], "-10012345")
        self.assertNotIn("authorization", request.headers)
        self.assertTrue(all(request.url.path == "/api/v1/users/me" for request in self.profiles))

    async def test_failure_alert_is_deduplicated_until_recovery(self):
        self.results["private-plenx-a"] = httpx.Response(401, json={"detail": "secret raw error"})
        await self.monitor.check_accounts()
        await self.monitor.check_accounts()
        self.assertEqual(len(self.messages), 1)
        text = self.texts()[0]
        self.assertIn("Studio", text)
        self.assertIn(self.first["id"], text)
        self.assertIn("401", text)
        self.assertNotIn("secret raw error", text)
        self.assertNotIn("private-plenx", text)
        self.results["private-plenx-a"] = httpx.Response(200, json={"id": "user-a"})
        await self.monitor.check_accounts()
        self.results["private-plenx-a"] = httpx.Response(403)
        await self.monitor.check_accounts()
        self.assertEqual(len(self.messages), 2)

    async def test_token_replacement_rearms_failure_notice(self):
        self.results["private-plenx-a"] = httpx.Response(401)
        await self.monitor.check_accounts()
        self.store.save_account("Studio", "replacement-token", self.first["id"])
        self.results["replacement-token"] = httpx.Response(401)
        await self.monitor.check_accounts()
        self.assertEqual(len(self.messages), 2)
        self.assertNotIn("replacement-token", " ".join(self.texts()))

    async def test_network_and_upstream_failures_do_not_claim_token_expiry(self):
        self.results["private-plenx-a"] = httpx.ReadTimeout("secret error")
        self.results["private-plenx-b"] = httpx.Response(503)
        await self.monitor.check_accounts()
        self.assertEqual(len(self.messages), 2)
        self.assertIn("timed out", " ".join(self.texts()).lower())
        self.assertIn("503", " ".join(self.texts()))
        self.assertNotIn("expired", " ".join(self.texts()).lower())

    async def test_failed_telegram_delivery_retries_on_next_failed_check(self):
        self.results["private-plenx-a"] = httpx.Response(401)
        self.telegram_response = httpx.Response(200, json={"ok": False, "description": "secret telegram error"})
        with self.assertLogs("monitoring", level="WARNING") as logs:
            await self.monitor.check_accounts()
        self.assertNotIn("secret telegram error", " ".join(logs.output))
        self.telegram_response = httpx.Response(200, json={"ok": True})
        await self.monitor.check_accounts()
        await self.monitor.check_accounts()
        self.assertEqual(len(self.messages), 2)

    async def test_stale_failure_after_token_replacement_is_ignored(self):
        def changed(request):
            self.store.save_account("Studio", "new-token", self.first["id"])
            return httpx.Response(401)
        self.results["private-plenx-a"] = changed
        await self.monitor.check_accounts()
        self.assertEqual(self.messages, [])

    async def test_unconfigured_telegram_never_sends(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            with self.assertLogs("monitoring", level="WARNING"):
                monitor = self.monitoring.ServiceMonitor(self.app, self.telegram)
        self.addCleanup(monitor.close)
        self.results["private-plenx-a"] = httpx.Response(401)
        await monitor.check_accounts()
        self.assertEqual(len(self.profiles), 2)
        self.assertEqual(self.messages, [])

    async def test_httpx_request_logs_hide_the_bot_token(self):
        with self.assertLogs("httpx", level="INFO") as logs:
            self.assertTrue(await self.monitor.notify("Test notice"))
        self.assertNotIn("123456:test-bot-token", " ".join(logs.output))

    async def test_startup_notice_retries_after_delivery_failure(self):
        calls = 0
        def flaky(request):
            nonlocal calls
            calls += 1
            self.messages.append(request)
            return httpx.Response(503 if calls == 1 else 200, json={"ok": calls > 1})
        async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(flaky)) as client:
            self.monitor.telegram = client
            sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError])
            with patch.object(self.monitoring.asyncio, "sleep", sleep), self.assertLogs("monitoring", level="WARNING"):
                with self.assertRaises(asyncio.CancelledError):
                    await self.monitor.run()
        self.assertEqual(calls, 2, "Startup delivery is retried until acknowledged, then stops")
        self.assertEqual(self.texts()[0], self.texts()[1])

    async def test_bad_profile_response_or_inactive_account_alerts(self):
        for payload in [{"detail": "raw secret error"}, {"id": "user-a", "is_active": False}]:
            self.monitor.notified_failures.clear()
            self.results["private-plenx-a"] = httpx.Response(200, json=payload)
            await self.monitor.check_accounts()
        self.assertEqual(len(self.messages), 2)
        self.assertNotIn("raw secret error", " ".join(self.texts()))

    async def test_removed_accounts_are_not_checked_or_alerted(self):
        self.store.remove_account(self.first["id"])
        self.results["private-plenx-a"] = httpx.Response(401)
        await self.monitor.check_accounts()
        self.assertEqual(len(self.profiles), 1)
        self.assertEqual(self.messages, [])

    async def test_malformed_telegram_token_does_not_skip_profile_checks(self):
        self.monitor.bot_token = "bad\ntoken"
        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        with patch.object(self.monitoring.asyncio, "sleep", sleep), self.assertLogs("monitoring", level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await self.monitor.run()
        self.assertEqual(len(self.profiles), 2)


class MonitoringLifecycleTests(unittest.TestCase):
    def test_service_start_sends_notice_and_shuts_monitor_down_cleanly(self):
        import main
        from fastapi.testclient import TestClient
        from monitoring import ServiceMonitor
        sent = threading.Event()
        messages = []
        def telegram(request):
            messages.append(json.loads(request.content))
            sent.set()
            return httpx.Response(200, json={"ok": True})
        telegram_client = httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(telegram))
        with tempfile.TemporaryDirectory() as directory:
            env = {"PLENX_DB_PATH": str(Path(directory) / "jobs.sqlite3"), "PLENX_WRAPPER_PASSWORD": "lifecycle-password",
                   "TELEGRAM_BOT_TOKEN": "123456:lifecycle-test", "TELEGRAM_CHAT_ID": "-10012345"}
            with patch.dict("os.environ", env), patch.object(main, "ServiceMonitor", lambda app, client: ServiceMonitor(app, telegram_client)):
                with TestClient(main.create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertTrue(sent.wait(timeout=1))
        asyncio.run(telegram_client.aclose())
        self.assertEqual(len(messages), 1)
        self.assertIn("service started", messages[0]["text"].lower())


if __name__ == "__main__":
    unittest.main()
