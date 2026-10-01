import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from knowgrain.upload_limits import UploadBodyLimitMiddleware


class UploadLimitTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=5)

        @app.post("/api/v1/sources")
        async def upload(request: Request):
            content = await request.body()
            return {"size": len(content)}

        self.client = TestClient(app)

    def test_content_length_exceeds_limit(self):
        response = self.client.post("/api/v1/sources", content=b"123456")
        self.assertEqual(response.status_code, 413)

    def test_stream_without_content_length_exceeds_limit(self):
        response = self.client.post("/api/v1/sources", content=iter([b"123", b"456"]))
        self.assertEqual(response.status_code, 413)

    def test_content_at_limit_is_accepted(self):
        response = self.client.post("/api/v1/sources", content=b"12345")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"size": 5})
