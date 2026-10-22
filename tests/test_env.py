import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import main


class EnvFileTests(unittest.TestCase):
    def test_env_file_loads_before_password_and_monitor_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                '# Server-only settings\nPLENX_WRAPPER_PASSWORD="dotenv-password"\n'
                'TELEGRAM_BOT_TOKEN="123456:dotenv-test"\nTELEGRAM_CHAT_ID="-10012345"\n'
            )
            messages = []
            sent = threading.Event()

            def telegram(request):
                messages.append(json.loads(request.content))
                sent.set()
                return httpx.Response(200, json={"ok": True})

            real_client = httpx.AsyncClient
            def mock_client(**kwargs):
                return real_client(transport=httpx.MockTransport(telegram), **kwargs)

            with patch.dict(os.environ, {}, clear=True), patch.object(main, "ROOT", root), \
                    patch.object(main.httpx, "AsyncClient", side_effect=mock_client):
                with TestClient(main.create_app()) as client:
                    self.assertFalse(client.get("/auth/status").json()["setup_required"])
                    response = client.get("/api/accounts", headers={"Authorization": "Bearer dotenv-password"})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(sent.wait(timeout=1))
                    self.assertEqual(messages[0]["chat_id"], "-10012345")
                    self.assertIn("service started", messages[0]["text"].lower())

    def test_exported_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('PLENX_WRAPPER_PASSWORD="file-password"\nTELEGRAM_BOT_TOKEN="file-token"\n')
            env = {"PLENX_WRAPPER_PASSWORD": "exported-password", "TELEGRAM_BOT_TOKEN": ""}
            with patch.dict(os.environ, env, clear=True), patch.object(main, "ROOT", root):
                with TestClient(main.create_app()) as client:
                    response = client.get("/api/accounts", headers={"Authorization": "Bearer exported-password"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "")
                    self.assertEqual(client.get("/api/accounts", headers={"Authorization": "Bearer file-password"}).status_code, 401)

    def test_missing_env_file_keeps_first_visit_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True), patch.object(main, "ROOT", Path(directory)):
                with TestClient(main.create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertTrue(client.get("/auth/status").json()["setup_required"])


if __name__ == "__main__":
    unittest.main()
