"""A chunked request must not bypass admission by omitting Content-Length."""
import pytest
from starlette.exceptions import HTTPException

from rag_poc.api import UploadBodyLimit


@pytest.mark.asyncio
async def test_chunked_upload_stops_reading_at_body_limit():
    events = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": True},
        {"type": "http.request", "body": b"not consumed", "more_body": False},
    ]
    consumed = []

    async def receive():
        event = events.pop(0)
        consumed.append(event)
        return event

    async def downstream(scope, incoming, send):
        while True:
            event = await incoming()
            if not event["more_body"]:
                break

    middleware = UploadBodyLimit(downstream, limit=7)
    with pytest.raises(HTTPException) as error:
        await middleware({"type": "http", "method": "POST", "path": "/api/documents"}, receive, None)
    assert error.value.status_code == 413
    assert len(consumed) == 2
    assert events[0]["body"] == b"not consumed"


def test_chunked_multipart_returns_safe_http_413():
    from fastapi.testclient import TestClient
    from rag_poc.api import create_app
    from rag_poc.config import load_config
    config = load_config("configs/mock.yaml")
    config.ingestion.max_upload_bytes = 16
    app = create_app(config, store=object(), hub=object(), initialize=False)

    def body():
        yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.txt"\r\n\r\n'
        for _ in range(1025):
            yield b"x" * 1024
        yield b"\r\n--boundary--\r\n"

    with TestClient(app) as client:
        response = client.post("/api/documents", content=body(),
                               headers={"Content-Type": "multipart/form-data; boundary=boundary"})
    assert response.status_code == 413
    assert response.json()["code"] == "upload_too_large"
