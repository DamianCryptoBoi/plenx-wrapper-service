import asyncio
from collections import Counter
from contextlib import suppress
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

import httpx

import main
import test_service as fixtures
from store import Store
from test_service import AUTH, COMPLETED, IMAGE_BODY, PROFILE, QUEUED, TOKEN


class QueueTests(unittest.TestCase):
    submit = fixtures.ServiceTests.submit
    history = fixtures.ServiceTests.history

    def setUp(self):
        fixtures.ServiceTests.setUp(self)
        self.sent = []
        self.statuses = {}
        self.post_error = None
        self.post_response = None
        self.block_submissions = False
        self.submission_entered = asyncio.Event()
        self.poll_error = False
        self.submission_status = "queued"
        self.promoted = threading.Event()

        async def upstream(request):
            self.requests.append(request)
            if request.url.path == "/api/v1/users/me":
                return httpx.Response(200, json=PROFILE)
            if request.method == "POST":
                await asyncio.sleep(0.005)
                self.sent.append(request)
                if self.block_submissions:
                    self.submission_entered.set()
                    await asyncio.Event().wait()
                if self.post_error:
                    raise self.post_error
                if self.post_response is not None:
                    return self.post_response
                task_id = str(uuid4())
                if len(self.sent) > 10:
                    self.promoted.set()
                return httpx.Response(200, json={**QUEUED, "task_id": task_id, "status": self.submission_status})
            if self.poll_error:
                return httpx.Response(503, json={"detail": "Temporary status failure"})
            task_id = request.url.path.rsplit("/", 1)[-1]
            status = self.statuses.get(task_id, "processing")
            return httpx.Response(200, json={**COMPLETED, "task_id": task_id, "status": status})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="https://plenxai.com")
        self.app.dependency_overrides[main.get_upstream] = lambda: self.upstream
        self.app.state.upstream = self.upstream

    def cycle(self):
        self.client.portal.call(main.queue_cycle, self.app)

    def test_separate_ten_image_and_video_slots_per_account(self):
        for _ in range(10):
            self.assertEqual(self.submit().status_code, 200)
            self.assertEqual(self.client.post("/api/v1/queue/submit", headers=AUTH, json=IMAGE_BODY).status_code, 200)
        for kind, response in [
            ("video", self.submit()),
            ("image", self.client.post("/api/v1/queue/submit", headers=AUTH, json=IMAGE_BODY)),
        ]:
            self.assertEqual(response.status_code, 202)
            job = response.json()
            self.assertEqual(job["status"], "waiting")
            self.assertIsNone(job["task_id"])
            self.assertEqual(job["queue_position"], 1)
            status = self.client.get(f"/api/requests/{job['request_id']}", headers=AUTH)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["kind"], kind)
            self.assertEqual(status.json()["status"], "waiting")
            self.assertEqual(self.client.get(f"/api/requests/{job['request_id']}").status_code, 401)
        self.assertEqual(len(self.sent), 20)
        other = self.client.post("/api/accounts", headers=AUTH, json={"name": "Other", "token": "other-token"}).json()
        headers = {**AUTH, "X-Account-ID": other["id"]}
        response = self.client.post("/api/v1/veo/generate", data={"prompt": "other", "workplace_id": PROFILE["default_workplace_id"]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/requests/{job['request_id']}", headers=headers).status_code, 404)

    def test_concurrent_admission_never_submits_more_than_ten(self):
        async def concurrent():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                return await asyncio.gather(*[
                    client.post("/api/v1/veo/generate", headers=AUTH, data={"prompt": f"job {i}", "workplace_id": PROFILE["default_workplace_id"]})
                    for i in range(25)
                ])
        responses = self.client.portal.call(concurrent)
        self.assertEqual(Counter(response.status_code for response in responses), {200: 10, 202: 15})
        self.assertEqual(len(self.sent), 10)
        waiting = [job for job in self.history() if job["status"] == "waiting"]
        self.assertEqual(sorted(job["queue_position"] for job in waiting), list(range(1, 16)))

    def test_fifo_promotion_only_after_completion(self):
        running = [self.submit().json() for _ in range(10)]
        first = self.submit(prompt="first waiting").json()
        second = self.submit(prompt="second waiting").json()
        self.cycle()
        self.assertEqual(len(self.sent), 10)
        self.statuses[running[0]["task_id"]] = "completed"
        self.cycle()
        self.assertEqual(len(self.sent), 11)
        self.assertIn("first waiting", self.sent[-1].content.decode())
        self.assertIsNotNone(self.client.get(f"/api/requests/{first['request_id']}", headers=AUTH).json()["task_id"])
        self.assertEqual(self.client.get(f"/api/requests/{second['request_id']}", headers=AUTH).json()["queue_position"], 1)

    def test_poll_errors_and_uncertain_submissions_retain_slots(self):
        self.post_error = httpx.ReadTimeout("submission outcome unknown")
        for _ in range(10):
            self.assertEqual(self.submit().status_code, 504)
        waiting = self.submit()
        self.assertEqual(waiting.status_code, 202)
        self.cycle()
        self.assertEqual(len(self.sent), 10, "Uncertain POSTs must not be retried or assumed finished")

    def test_failed_poll_never_promotes_waiting_work(self):
        running = [self.submit().json() for _ in range(10)]
        self.submit()
        self.poll_error = True
        self.cycle()
        self.assertEqual(len(self.sent), 10)
        self.poll_error = False
        self.statuses[running[0]["task_id"]] = "failed"
        self.cycle()
        self.assertEqual(len(self.sent), 11)

    def test_queued_work_and_reservations_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            store = Store(path)
            account = store.save_account("Test", "restart-token")
            params = {"prompt": "persisted"}
            for _ in range(10):
                store.create(account["id"], "video", params)
            waiting_id = store.create(account["id"], "video", params)
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.get(waiting_id)["status"], "waiting")
                self.assertIsNone(store.claim_waiting(account["id"], "video"))
                self.assertEqual(sum(job["status"] == "submission_unknown" for job in store.list(account["id"])), 10)
            finally:
                store.close()

    def test_stale_poll_cannot_revive_a_terminal_generation(self):
        job = self.submit().json()
        async def complete_then_stale():
            self.app.state.store.update(job["request_id"], {**COMPLETED, "task_id": job["task_id"]})
            self.app.state.store.update(job["request_id"], {**QUEUED, "task_id": job["task_id"]})
        self.client.portal.call(complete_then_stale)
        self.assertEqual(self.history()[0]["status"], "completed")

    def test_removed_account_cancels_waiting_work(self):
        for _ in range(10):
            self.submit()
        waiting = self.submit().json()
        self.client.delete(f"/api/accounts/{self.account_id}", headers=AUTH)
        self.client.post("/api/accounts", headers=AUTH, json={"name": "Restored", "token": TOKEN})
        job = self.client.get(f"/api/requests/{waiting['request_id']}", headers=AUTH).json()
        self.assertEqual(job["status"], "cancelled")
        self.cycle()
        self.assertEqual(len(self.sent), 10)

    def test_worker_promotes_without_browser_or_status_requests(self):
        running = [self.submit().json() for _ in range(10)]
        waiting = self.submit().json()
        self.statuses[running[0]["task_id"]] = "completed"
        self.assertTrue(self.promoted.wait(timeout=8), "The background worker must run without a browser")
        job = self.client.get(f"/api/requests/{waiting['request_id']}", headers=AUTH).json()
        self.assertIsNotNone(job["task_id"])

    def test_upstream_waiting_tasks_are_not_local_queue_entries(self):
        self.submission_status = "waiting"
        running = []
        for _ in range(10):
            response = self.submit()
            self.assertEqual(response.status_code, 200)
            running.append(response.json())
        overflow = self.submit()
        self.assertEqual(overflow.status_code, 202)
        for job in running:
            self.statuses[job["task_id"]] = "waiting"
        self.cycle()
        self.assertEqual(len(self.sent), 10, "An accepted upstream waiting task must never be submitted twice")
        self.client.delete(f"/api/accounts/{self.account_id}", headers=AUTH)
        self.client.post("/api/accounts", headers=AUTH, json={"name": "Restored", "token": TOKEN})
        self.assertEqual(self.client.get(f"/api/requests/{running[0]['request_id']}", headers=AUTH).json()["status"], "waiting")
        self.assertEqual(self.client.get(f"/api/requests/{overflow.json()['request_id']}", headers=AUTH).json()["status"], "cancelled")

    def test_worker_honors_backoff_and_resumes_after_account_update(self):
        running = [self.submit().json() for _ in range(10)]
        self.submit()
        self.submit()
        self.statuses[running[0]["task_id"]] = "completed"
        self.post_response = httpx.Response(429, json={"detail": "Slow down"}, headers={"Retry-After": "60"})
        self.cycle()
        self.assertEqual(len(self.sent), 11)
        self.cycle()
        self.assertEqual(len(self.sent), 11, "Backoff must stop automatic dispatch, without retrying the failed POST")
        self.post_response = None
        self.client.patch(f"/api/accounts/{self.account_id}", headers=AUTH, json={"name": "Updated", "token": TOKEN})
        self.cycle()
        self.assertEqual(len(self.sent), 12)

    def test_shutdown_does_not_claim_jobs_that_have_not_been_sent(self):
        running = [self.submit().json() for _ in range(10)]
        for _ in range(3):
            self.submit()
        for job in running:
            self.statuses[job["task_id"]] = "completed"
        self.block_submissions = True
        async def interrupt_cycle():
            task = asyncio.create_task(main.queue_cycle(self.app))
            await asyncio.wait_for(self.submission_entered.wait(), timeout=2)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.client.portal.call(interrupt_cycle)
        jobs = self.history()
        self.assertEqual(sum(job["status"] == "submission_unknown" for job in jobs), 1)
        self.assertEqual(sum(job["status"] == "waiting" for job in jobs), 2)


if __name__ == "__main__":
    unittest.main()
