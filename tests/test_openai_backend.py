import json

import httpx
import pymupdf
import pytest

from docstruc import extract
from docstruc.core import files
from docstruc.llm import get_backend
from docstruc.llm.openai_compat import ExtractionError, OpenAICompatBackend

REPLY = {"merchant_name": "Cafe", "date": "2026-10-01", "currency": "USD", "items": [], "total": 5.0}


def _jpeg():
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (40, 60), "white").save(out, "JPEG")
    return files.from_bytes(out.getvalue(), "r.jpg")


def _pdf(pages=2):
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"page {n}")
    data = doc.tobytes()
    doc.close()
    return files.from_bytes(data, "d.pdf")


def _backend(handler, **kw):
    return OpenAICompatBackend(model="m", base_url="http://local/v1", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def _ok(content, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})


def test_reads_an_image_and_constrains_the_reply_to_the_schema():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _ok(json.dumps(REPLY))

    result = extract(_jpeg(), "receipt", backend=_backend(handler))
    assert result.data["merchant_name"] == "Cafe" and result.model == "openai/m"
    body = seen[0]
    assert body["model"] == "m" and body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["properties"]["merchant_name"]
    user = body["messages"][1]["content"]
    assert [b["type"] for b in user] == ["image_url", "text"]
    assert user[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_every_pdf_page_is_sent_as_an_image():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _ok(json.dumps(REPLY))

    extract(_pdf(3), "receipt", backend=_backend(handler))
    blocks = seen[0]["messages"][1]["content"]
    assert [b["type"] for b in blocks] == ["image_url"] * 3 + ["text"]
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_server_without_response_format_gets_the_schema_in_the_prompt():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(400, text='{"error": "unknown field response_format"}')
        return _ok("```json\n" + json.dumps(REPLY) + "\n```")  # a fenced reply is accepted

    backend = _backend(handler)
    assert extract(_jpeg(), "receipt", backend=backend).data["total"] == 5.0
    assert "response_format" not in seen[-1] and "JSON Schema" in seen[-1]["messages"][0]["content"]
    extract(_jpeg(), "receipt", backend=backend)
    assert "response_format" not in seen[-1] and len(seen) == 3  # remembered: no second failed attempt


def test_an_invalid_reply_gets_one_corrective_retry_then_fails():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return _ok('{"total": "not a number"}' if len(calls) == 1 else json.dumps(REPLY))

    assert extract(_jpeg(), "receipt", backend=_backend(handler)).data["total"] == 5.0
    assert "invalid" in calls[1]["messages"][-1]["content"]

    with pytest.raises(ExtractionError, match="after a retry"):
        extract(_jpeg(), "receipt", backend=_backend(lambda r: _ok("nonsense")))


def test_errors_are_reported_plainly():
    with pytest.raises(ExtractionError, match="500"):
        extract(_jpeg(), "receipt", backend=_backend(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(ExtractionError, match="cut off"):
        extract(_jpeg(), "receipt", backend=_backend(lambda r: _ok("{", finish="length")))

    def unreachable(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ExtractionError, match="Cannot reach"):
        extract(_jpeg(), "receipt", backend=_backend(unreachable))


def test_the_model_must_be_named_and_the_backend_is_registered(monkeypatch):
    monkeypatch.delenv("DOCSTRUC_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="Name the model"):
        get_backend("openai")
    monkeypatch.setenv("DOCSTRUC_BASE_URL", "http://example.test:1234/v1/")
    backend = get_backend("openai", model="llava")
    assert backend.name == "openai/llava" and backend.base_url == "http://example.test:1234/v1"
