import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from hashlib import sha256

import httpx
from fastapi.testclient import TestClient

import main


TASK = "7de6c5a3-b599-40ab-a5ae-ee2112030dde"
TOKEN = "test-account-token-never-store"
PASSWORD = "test-wrapper-password"
QUEUED = {"task_id": TASK, "status": "queued", "message": "Flow Veo3 T2V task submitted"}
COMPLETED = {
    "task_id": TASK, "status": "completed", "message": "", "progress": 100,
    "current_step": "", "result_url": "https://cdn.plenxai.com/result.mp4",
    "duration": None, "generation_time": None, "is_nsfw": False,
}
IMAGE_BODY = {
    "task_type": "image/gem-pix-2",
    "payload": {"prompt": "a cat", "aspect_ratio": "1:1", "num_outputs": 1, "guidance_scale": 3.5,
                "num_inference_steps": 20, "output_format": "jpeg", "go_fast": True, "resolution": "1k",
                "resolution_tier": "1k", "server_id": "dd597ae5-6ab3-4e76-9914-9da5589e21c1",
                "uses_credits": False, "references_urls": []},
    "workplace_id": "45e06a7a-1318-433c-8338-dcfcf52d7b5a",
}
IMAGE_DONE = {
    "success": True, "task_id": TASK, "status": "completed", "task_type": "image/gem-pix-2",
    "result": {"success": True, "result_url": "https://images.plenxai.com/cat.jpeg",
               "image_urls": ["https://images.plenxai.com/cat.jpeg"], "model": "gem-pix-2"},
    "error_message": None, "is_nsfw": False,
}
AUTH = {"Authorization": f"Bearer {PASSWORD}"}
PROFILE = {
    "id": "2ca60c12-413e-4ef4-94f9-dccffc5b477c", "email": "studio@example.test",
    "display_name": "Test Studio", "plan_name": "Creator", "status": None,
    "credit_balance": 0.0, "free_credits": 9975.0, "ruby_balance": 0.0,
    "default_workplace_id": "45e06a7a-1318-433c-8338-dcfcf52d7b5a",
    "daily_image_used": 1, "daily_image_limit": 1000,
}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "jobs.sqlite3"
        self.env = patch.dict(os.environ, {"PLENX_DB_PATH": str(self.db), "PLENX_WRAPPER_PASSWORD": PASSWORD})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requests = []
        self.upstream_response = httpx.Response(200, json=QUEUED)
        self.profile_response = httpx.Response(200, json=PROFILE)

        def upstream(request):
            self.requests.append(request)
            if request.url.path == "/api/v1/users/me":
                return self.profile_response
            if isinstance(self.upstream_response, Exception):
                raise self.upstream_response
            return self.upstream_response

        self.app = main.create_app()
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="https://plenxai.com")
        self.app.dependency_overrides[main.get_upstream] = lambda: self.upstream
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        created = self.client.post("/api/accounts", headers=AUTH, json={"name": "Test account", "token": TOKEN})
        self.assertEqual(created.status_code, 201)
        self.account_id = created.json()["id"]

    def submit(self, **fields):
        return self.client.post("/api/v1/veo/generate", data={"prompt": "a cat", "workplace_id": PROFILE["default_workplace_id"], **fields}, headers=AUTH)

    def history(self, auth=AUTH):
        return self.client.get("/api/requests", headers=auth).json()["requests"]

    def test_forwards_multipart_and_bearer_with_defaults(self):
        response = self.client.post("/api/v1/veo/generate", data={"prompt": "a cat"}, headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({key: response.json()[key] for key in QUEUED}, QUEUED)
        self.assertEqual(response.json()["account_id"], self.account_id)
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/api/v1/veo/generate")
        self.assertEqual(request.headers["authorization"], f"Bearer {TOKEN}")
        self.assertTrue(request.headers["content-type"].startswith("multipart/form-data; boundary="))
        body = request.content.decode()
        for value in ["a cat", "omni-flash", "16:9", "720", "56947b53-029e-4751-bbca-b9de54f5d628"]:
            self.assertIn(value, body)
        job = self.history()[0]
        self.assertEqual(job["task_id"], TASK)
        self.assertEqual(job["parameters"]["workplace_id"], PROFILE["default_workplace_id"])

    def test_preserves_explicit_workplace_id(self):
        workplace = "45e06a7a-1318-433c-8338-dcfcf52d7b5a"
        self.assertEqual(self.submit(workplace_id=workplace).status_code, 200)
        self.assertIn(workplace, self.requests[0].content.decode())

    def test_all_job_routes_require_bearer(self):
        for method, path in [("GET", "/api/accounts"), ("POST", "/api/accounts"), ("PATCH", f"/api/accounts/{self.account_id}"), ("DELETE", f"/api/accounts/{self.account_id}"), ("GET", "/api/config"), ("GET", "/api/v1/users/me"), ("POST", "/api/v1/veo/generate"), ("GET", "/api/requests"), ("GET", f"/api/v1/video/status/{TASK}"), ("POST", "/api/v1/queue/submit"), ("GET", f"/api/v1/queue/status/{TASK}")]:
            for headers in [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {TOKEN}"}]:
                with self.subTest(path=path, headers=headers):
                    response = self.client.request(method, path, headers=headers)
                    self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.requests), 0)

    def test_history_and_polling_are_isolated_between_accounts(self):
        self.submit()
        second = self.client.post("/api/accounts", headers=AUTH, json={"name": "Other", "token": "other-account"}).json()
        other = {**AUTH, "X-Account-ID": second["id"]}
        self.assertEqual(self.history(other), [])
        response = self.client.get(f"/api/v1/video/status/{TASK}", headers=other)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.requests), 1)

    def test_status_response_is_preserved_and_persisted(self):
        self.submit()
        self.upstream_response = httpx.Response(200, json=COMPLETED)
        response = self.client.get(f"/api/v1/video/status/{TASK}", headers=AUTH)
        self.assertEqual({key: response.json()[key] for key in COMPLETED}, COMPLETED)
        self.assertEqual(self.requests[-1].url.path, f"/api/v1/video/status/{TASK}")
        job = self.history()[0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["response"]["result_url"], COMPLETED["result_url"])

    def test_rejects_invalid_input_without_submitting(self):
        for fields in [{"prompt": "   "}, {"duration": 0}, {"server_id": "bad"}, {"workplace_id": "bad"}]:
            with self.subTest(fields=fields):
                self.assertEqual(self.submit(**fields).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_upstream_rejection_is_visible_in_history(self):
        self.upstream_response = httpx.Response(429, json={"detail": "No credits"}, headers={"Retry-After": "30"})
        response = self.submit()
        self.assertEqual(response.status_code, 429)
        self.assertIn("No credits", response.text)
        self.assertEqual(response.headers["retry-after"], "30")
        self.assertEqual(self.history()[0]["status"], "failed")

    def test_timeout_does_not_resubmit_and_marks_uncertain_submission(self):
        self.upstream_response = httpx.ReadTimeout("slow upstream")
        self.assertEqual(self.submit().status_code, 504)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.history()[0]["status"], "submission_unknown")

    def test_poll_failure_keeps_job_active_for_retry(self):
        self.submit()
        self.upstream_response = httpx.Response(503, text="unavailable")
        response = self.client.get(f"/api/v1/video/status/{TASK}", headers=AUTH)
        self.assertEqual(response.status_code, 503)
        job = self.history()[0]
        self.assertEqual(job["status"], "queued")
        self.assertTrue(job["poll_error"])

    def test_malformed_upstream_success_becomes_gateway_error(self):
        self.upstream_response = httpx.Response(200, json={"unexpected": "payload"})
        self.assertEqual(self.submit().status_code, 502)
        self.assertEqual(self.history()[0]["status"], "submission_unknown")

    def test_token_stays_in_private_server_database_and_history_survives_restart(self):
        self.submit()
        self.assertNotIn(TOKEN, json.dumps(self.history()))
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, self.client.get("/api/accounts", headers=AUTH).text)
        with TestClient(main.create_app()) as restarted:
            jobs = restarted.get("/api/requests", headers=AUTH).json()["requests"]
            self.assertEqual(jobs[0]["task_id"], TASK)

    def test_image_forwards_exact_json_payload_and_tracks_kind(self):
        response = self.client.post("/api/v1/queue/submit", headers=AUTH, json=IMAGE_BODY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.requests[0].content), IMAGE_BODY)
        self.assertEqual(self.requests[0].url.path, "/api/v1/queue/submit")
        self.assertEqual(self.requests[0].headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(self.history()[0]["kind"], "image")

    def test_image_poll_preserves_nested_results(self):
        self.client.post("/api/v1/queue/submit", headers=AUTH, json=IMAGE_BODY)
        self.upstream_response = httpx.Response(200, json=IMAGE_DONE)
        response = self.client.get(f"/api/v1/queue/status/{TASK}", headers=AUTH)
        self.assertEqual({key: response.json()[key] for key in IMAGE_DONE}, IMAGE_DONE)
        self.assertEqual(self.requests[-1].url.path, f"/api/v1/queue/status/{TASK}")
        self.assertEqual(self.history()[0]["response"]["result"]["image_urls"], ["https://images.plenxai.com/cat.jpeg"])

    def test_wrong_media_poll_route_does_not_call_upstream(self):
        self.submit()
        response = self.client.get(f"/api/v1/queue/status/{TASK}", headers=AUTH)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.requests), 1)

    def test_invalid_image_reference_url_does_not_submit(self):
        body = {**IMAGE_BODY, "payload": {**IMAGE_BODY["payload"], "references_urls": ["javascript:alert(1)"]}}
        response = self.client.post("/api/v1/queue/submit", headers=AUTH, json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.requests, [])

    def test_terminal_image_failure_is_persisted_instead_of_polled_forever(self):
        self.client.post("/api/v1/queue/submit", headers=AUTH, json=IMAGE_BODY)
        failed = {"success": False, "task_id": TASK, "status": "failed", "error_message": "Generation failed"}
        self.upstream_response = httpx.Response(200, json=failed)
        response = self.client.get(f"/api/v1/queue/status/{TASK}", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({key: response.json()[key] for key in failed}, failed)
        self.assertEqual(self.history()[0]["status"], "failed")

    def test_image_submission_rejection_is_not_a_queued_task(self):
        self.upstream_response = httpx.Response(200, json={"success": False, "task_id": None, "status": "failed", "error": "No credits"})
        response = self.client.post("/api/v1/queue/submit", headers=AUTH, json=IMAGE_BODY)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.history()[0]["status"], "failed")

    def test_account_profile_preserves_separate_balances_and_does_not_persist_profile(self):
        response = self.client.get("/api/v1/users/me", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PROFILE)
        self.assertEqual(self.requests[0].url.path, "/api/v1/users/me")
        self.assertEqual(self.requests[0].headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.history(), [])
        for path in Path(self.temp.name).iterdir():
            self.assertNotIn(PROFILE["email"].encode(), path.read_bytes())

    def test_profile_upstream_auth_failure_is_forwarded(self):
        self.profile_response = httpx.Response(401, json={"detail": "Expired token"})
        response = self.client.get("/api/v1/users/me", headers=AUTH)
        self.assertEqual(response.status_code, 401)
        self.assertIn("Expired token", response.text)
        self.assertEqual(response.headers["x-plenx-status"], "401")

    def test_invalid_profile_does_not_return_misleading_zero_balances(self):
        self.profile_response = httpx.Response(200, json={"unexpected": "data"})
        self.assertEqual(self.client.get("/api/v1/users/me", headers=AUTH).status_code, 502)

    def test_image_uses_account_workplace_when_omitted(self):
        response = self.client.post("/api/v1/queue/submit", headers=AUTH, json={"payload": {"prompt": "a cat"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[0].url.path, "/api/v1/users/me")
        self.assertEqual(json.loads(self.requests[1].content)["workplace_id"], PROFILE["default_workplace_id"])

    def test_missing_default_workplace_does_not_submit_a_made_up_id(self):
        self.profile_response = httpx.Response(200, json={**PROFILE, "default_workplace_id": None})
        response = self.client.post("/api/v1/veo/generate", headers=AUTH, data={"prompt": "a cat"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.history(), [])

    def test_active_jobs_survive_the_terminal_history_limit(self):
        self.submit()
        self.upstream_response = httpx.Response(200, json={**QUEUED, "task_id": "4b318f0d-e982-4107-9796-9ec080ae57bc", "status": "completed"})
        for _ in range(201):
            self.submit()
        history = self.history()
        self.assertTrue(any(job["task_id"] == TASK and job["status"] == "queued" for job in history))
        self.assertEqual(sum(job["status"] == "completed" for job in history), 200)

    def test_wrapper_password_is_never_forwarded_to_plenx(self):
        self.submit()
        self.assertEqual(self.requests[0].headers["authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(PASSWORD, str(self.requests[0].headers))
        self.assertNotIn("x-account-id", self.requests[0].headers)

    def test_account_token_is_write_only_and_rotation_preserves_history(self):
        self.submit()
        replacement = "new-plenx-token"
        response = self.client.patch(f"/api/accounts/{self.account_id}", headers=AUTH, json={"name": "Renamed", "token": replacement})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(replacement, response.text)
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(self.history()[0]["task_id"], TASK)
        self.upstream_response = httpx.Response(200, json=COMPLETED)
        self.client.get(f"/api/v1/video/status/{TASK}", headers=AUTH)
        self.assertEqual(self.requests[-1].headers["authorization"], f"Bearer {replacement}")

    def test_status_uses_original_account_without_an_account_header(self):
        second = self.client.post("/api/accounts", headers=AUTH, json={"name": "Other", "token": "other-account"}).json()
        self.client.post("/api/v1/veo/generate", data={"prompt": "a cat", "workplace_id": PROFILE["default_workplace_id"]}, headers={**AUTH, "X-Account-ID": second["id"]})
        self.upstream_response = httpx.Response(200, json=COMPLETED)
        response = self.client.get(f"/api/v1/video/status/{TASK}", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer other-account")

    def test_legacy_token_history_is_claimed_when_account_is_imported(self):
        legacy_token = "legacy-browser-token"
        store = self.app.state.store
        # Execute in the service loop because SQLite belongs to that loop.
        async def legacy_job():
            job_id = store.create(sha256(legacy_token.encode()).hexdigest(), "video", {"prompt": "old job"})
            store.update(job_id, QUEUED)
        self.client.portal.call(legacy_job)
        created = self.client.post("/api/accounts", headers=AUTH, json={"name": "Imported", "token": legacy_token})
        self.assertEqual(created.status_code, 201)
        jobs = self.history({**AUTH, "X-Account-ID": created.json()["id"]})
        self.assertEqual(jobs[0]["prompt"], "old job")

    def test_deleted_account_cannot_be_used_and_can_be_restored(self):
        self.submit()
        response = self.client.delete(f"/api/accounts/{self.account_id}", headers=AUTH)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/accounts", headers=AUTH).json(), {"accounts": []})
        self.assertEqual(self.client.get(f"/api/v1/video/status/{TASK}", headers=AUTH).status_code, 404)
        restored = self.client.post("/api/accounts", headers=AUTH, json={"name": "Restored", "token": TOKEN})
        self.assertEqual(restored.json()["id"], self.account_id)
        self.assertEqual(self.history()[0]["task_id"], TASK)

    def test_success_and_error_responses_redact_upstream_credentials(self):
        self.upstream_response = httpx.Response(200, json={**QUEUED, "message": f"echo {TOKEN}", "nested": [TOKEN]})
        response = self.submit()
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(TOKEN, json.dumps(self.history()))
        self.upstream_response = httpx.Response(400, json={"detail": f"echo {TOKEN}"})
        self.assertNotIn(TOKEN, self.submit().text)

    def test_bad_account_payload_does_not_echo_secret_input(self):
        response = self.client.post("/api/accounts", headers=AUTH, json={"name": "Bad", "token": {"secret": TOKEN}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(TOKEN, response.text)

    def test_account_import_is_idempotent(self):
        response = self.client.post("/api/accounts", headers=AUTH, json={"name": "Again", "token": TOKEN})
        self.assertEqual(response.json()["id"], self.account_id)
        self.assertEqual(len(self.client.get("/api/accounts", headers=AUTH).json()["accounts"]), 1)

    def test_missing_wrapper_password_does_not_generate_a_password(self):
        with patch.dict(os.environ):
            os.environ.pop("PLENX_WRAPPER_PASSWORD", None)
            directory = Path(self.temp.name)
            self.assertIsNone(main.load_wrapper_password(directory))
            self.assertFalse((directory / "wrapper-password").exists())

    def test_empty_environment_password_requires_first_visit_setup(self):
        with patch.dict(os.environ, {"PLENX_WRAPPER_PASSWORD": ""}):
            self.assertIsNone(main.load_wrapper_password(Path(self.temp.name)))

    def test_configured_password_cannot_be_replaced_through_setup(self):
        response = self.client.post("/auth/setup", json={"password": "replacement", "confirm_password": "replacement"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/accounts", headers=AUTH).status_code, 200)

    def test_escaped_token_is_redacted_before_error_formatting(self):
        token = 'quoted-SECRET-MARKER-"end\\suffix'
        self.client.patch(f"/api/accounts/{self.account_id}", headers=AUTH, json={"name": "Test", "token": token})
        self.upstream_response = httpx.Response(400, json={"detail": token})
        response = self.submit()
        self.assertNotIn("SECRET-MARKER", response.json()["detail"])
        self.assertIn("[redacted]", response.json()["detail"])

    def test_token_is_redacted_before_error_truncation(self):
        token = "SECRET-MARKER-123456789"
        self.client.patch(f"/api/accounts/{self.account_id}", headers=AUTH, json={"name": "Test", "token": token})
        self.upstream_response = httpx.Response(400, text="x" * 990 + token)
        response = self.submit()
        self.assertNotIn("SECRET-MAR", response.json()["detail"])


class FirstVisitSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"PLENX_DB_PATH": str(self.directory / "jobs.sqlite3"), "PLENX_WRAPPER_PASSWORD": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(main.create_app())
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def setup_password(self, password="first-visit-password", **kwargs):
        return self.client.post("/auth/setup", json={"password": password, "confirm_password": password}, **kwargs)

    def test_first_visit_reports_setup_and_keeps_api_locked(self):
        response = self.client.get("/auth/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"setup_required": True})
        self.assertEqual(self.client.get("/api/accounts").status_code, 503)
        self.assertFalse((self.directory / "wrapper-password").exists())

    def test_setup_saves_private_password_and_survives_restart(self):
        response = self.setup_password()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"setup_required": False})
        path = self.directory / "wrapper-password"
        self.assertEqual(path.read_text().strip(), "first-visit-password")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        auth = {"Authorization": "Bearer first-visit-password"}
        self.assertEqual(self.client.get("/api/accounts", headers=auth).status_code, 200)
        with TestClient(main.create_app()) as restarted:
            self.assertEqual(restarted.get("/auth/status").json(), {"setup_required": False})
            self.assertEqual(restarted.get("/api/accounts", headers=auth).status_code, 200)

    def test_setup_is_one_time_and_does_not_reset_existing_password(self):
        self.assertEqual(self.setup_password().status_code, 200)
        self.assertEqual(self.setup_password("different-password").status_code, 409)
        self.assertEqual(self.client.get("/api/accounts", headers={"Authorization": "Bearer first-visit-password"}).status_code, 200)
        self.assertEqual(self.client.get("/api/accounts", headers={"Authorization": "Bearer different-password"}).status_code, 401)

    def test_stale_second_app_cannot_overwrite_a_password(self):
        with TestClient(main.create_app()) as other:
            self.assertEqual(other.get("/auth/status").json(), {"setup_required": True})
            self.setup_password()
            response = other.post("/auth/setup", json={"password": "second", "confirm_password": "second"})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(other.get("/api/accounts", headers={"Authorization": "Bearer first-visit-password"}).status_code, 200)

    def test_invalid_or_mismatched_password_never_enables_access(self):
        for password, confirmation in [("", ""), ("spaces in password", "spaces in password"), ("first", "different")]:
            with self.subTest(password=password):
                response = self.client.post("/auth/setup", json={"password": password, "confirm_password": confirmation})
                self.assertEqual(response.status_code, 422)
                self.assertFalse((self.directory / "wrapper-password").exists())
                self.assertEqual(self.client.get("/auth/status").json(), {"setup_required": True})

    def test_cross_origin_setup_is_rejected(self):
        self.assertEqual(self.setup_password(headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.assertFalse((self.directory / "wrapper-password").exists())
        self.assertEqual(self.setup_password(headers={"Origin": "http://testserver"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
