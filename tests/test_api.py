import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.config import Settings

with tempfile.TemporaryDirectory() as initial, patch.dict(os.environ, {"AGENT_DATA_DIR": initial}):
    from app.main import create_app


class Provider:
    async def health(self):
        return {"status": "UP"}

    async def chat(self, messages, tools, remaining):
        return {"role": "assistant", "content": "Received: " + messages[-1]["content"]}, {"prompt_tokens": 100, "completion_tokens": 10}


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name))
        self.app = create_app(self.settings, Provider())
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.token = (Path(self.tmp.name) / "access-token").read_text().strip()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def login(self):
        response = self.client.post("/api/login", json={"token": self.token})
        self.assertEqual(response.status_code, 200)

    def test_auth_session_logout_and_non_ascii_bad_code(self):
        self.assertEqual(self.client.get("/api/sessions").status_code, 401)
        self.assertFalse(self.client.get("/api/health").json()["authenticated"])
        self.assertEqual(self.client.post("/api/login", json={"token": "\u9519\u8bef\u53e3\u4ee4"}).status_code, 401)
        self.login()
        self.assertTrue(self.client.get("/api/health").json()["authenticated"])
        self.assertEqual(self.client.get("/api/sessions").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/sessions").status_code, 401)

    def test_run_complete_then_new_app_recovers_history(self):
        self.login()
        session = self.client.post("/api/sessions", json={}).json()
        run = self.client.post(f'/api/sessions/{session["id"]}/runs', json={"prompt": "Hello"}).json()
        for _ in range(100):
            detail = self.client.get('/api/runs/' + run["id"]).json()
            if detail["run"]["status"] == "completed":
                break
            time.sleep(.01)
        self.assertEqual(detail["run"]["status"], "completed")
        result = self.client.get('/api/sessions/' + session["id"]).json()
        self.assertEqual(result["messages"][-1]["content"], "Received: Hello")
        other = create_app(self.settings, Provider())
        self.assertEqual(other.state.store.messages(session["id"])[-1]["content"], "Received: Hello")

    def test_upload_read_no_overwrite_and_session_isolation(self):
        self.login()
        a, b = [self.client.post("/api/sessions", json={}).json()["id"] for _ in range(2)]
        url = f'/api/sessions/{a}/files'
        self.assertEqual(self.client.post(url, json={"path": "x.txt", "content": "\u4f60\u597d"}).status_code, 201)
        self.assertEqual(self.client.get(url + '/content', params={"path": "x.txt"}).text, "\u4f60\u597d")
        self.assertEqual(self.client.post(url, json={"path": "x.txt", "content": "changed"}).status_code, 400)
        self.assertEqual(self.client.post(url, json={"path": "../secret", "content": "bad"}).status_code, 400)
        self.assertEqual(self.client.get(f'/api/sessions/{b}/files').json()["files"], [])

    def test_cross_origin_and_missing_session_rejected(self):
        self.login()
        self.assertEqual(self.client.post("/api/sessions", json={}, headers={"Origin": "http://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/api/sessions/no-such-session").status_code, 404)
        self.assertEqual(self.client.post("/api/sessions", json={"title": " "}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions", json={"unexpected": 1}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
