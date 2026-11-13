"""HTTP body limits apply to received bytes, not only declared lengths."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app.config import Settings


with tempfile.TemporaryDirectory() as initial, patch.dict(os.environ, {
    "AGENT_DATA_DIR": initial,
    "APP_ACCESS_TOKEN_FILE": str(Path(initial) / "access-token"),
}):
    from app.main import create_app


class NoModelRequests:
    async def health(self):
        raise AssertionError("Body-limit tests must not call the model")

    async def chat(self, *args):
        raise AssertionError("Body-limit tests must not call the model")


async def chunks(body, size=8192):
    for start in range(0, len(body), size):
        yield body[start:start + size]


class RequestLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=Path(self.tmp.name))
        self.app = create_app(settings, NoModelRequests())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.token = (Path(self.tmp.name) / "access-token").read_text().strip()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_chunked_oversized_request_is_413_without_echo(self):
        marker = "INPUT_MUST_NOT_BE_REFLECTED"
        body = json.dumps({"token": marker + "x" * 200000}).encode()
        request = self.client.build_request(
            "POST", "/api/login", content=chunks(body),
            headers={"Content-Type": "application/json"},
        )
        self.assertNotIn("content-length", request.headers)
        response = await self.client.send(request)
        self.assertEqual(response.status_code, 413)
        self.assertNotIn(marker, response.text)
        self.assertLess(len(response.content), 4096)

    async def test_small_chunked_request_can_log_in(self):
        body = json.dumps({"token": self.token}).encode()
        request = self.client.build_request(
            "POST", "/api/login", content=chunks(body, size=5),
            headers={"Content-Type": "application/json"},
        )
        self.assertNotIn("content-length", request.headers)
        response = await self.client.send(request)
        self.assertEqual(response.status_code, 200)
        sessions = await self.client.get("/api/sessions")
        self.assertEqual(sessions.status_code, 200)
        self.assertEqual(sessions.json(), [])

    async def test_validation_error_does_not_echo_input(self):
        marker = "VALIDATION_INPUT_MUST_NOT_BE_REFLECTED"
        response = await self.client.post(
            "/api/login", json={"token": marker + "x" * 1000}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(marker, response.text)
        self.assertLess(len(response.content), 4096)

    async def test_malformed_json_does_not_echo_input(self):
        marker = "MALFORMED_JSON_MUST_NOT_BE_REFLECTED"
        body = ('{"token":"' + marker + '",').encode()
        response = await self.client.post(
            "/api/login", content=chunks(body, size=7),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(marker, response.text)
        self.assertLess(len(response.content), 4096)


if __name__ == "__main__":
    unittest.main()
