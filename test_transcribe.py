import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI

import app


class TranscriptionTests(unittest.TestCase):
    def test_returns_whisper_verbose_json(self):
        whisper_response = {
            "task": "transcribe",
            "language": "english",
            "duration": 2.5,
            "text": "Hello world.",
            "segments": [{"id": 0, "start": 0.0, "end": 2.5, "text": "Hello world."}],
        }
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=whisper_response)

        openai_client = OpenAI(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
        with patch.object(app, "OPENAI_API_KEY", "test-key"), patch.object(
            app, "OpenAI", return_value=openai_client
        ), TestClient(app.app) as client:
            response = client.post(
                "/api/transcribe", files={"audio": ("sample.webm", b"sample", "audio/webm")}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), whisper_response)
        self.assertIn(b'name="response_format"\r\n\r\nverbose_json', requests[0].content)
        self.assertIn(b'name="model"\r\n\r\nwhisper-1', requests[0].content)


if __name__ == "__main__":
    unittest.main()
