import json
from types import SimpleNamespace

import pytest

from docstruc.core.models import Document
from docstruc.doctypes.invoice import Invoice
from docstruc.doctypes.receipt import Receipt
from docstruc.llm.claude import (
    MAX_UNION_PARAMS,
    ClaudeBackend,
    ExtractionError,
    strict_json_schema,
    union_param_count,
)


def reply(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeMessages:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def backend_with(*responses):
    backend = ClaudeBackend(model="test-model", api_key="test-key")
    backend._client = SimpleNamespace(messages=FakeMessages(*responses))
    return backend


def doc(name, mime):
    return Document(name=name, mime_type=mime, data=b"bytes")


def test_strict_schema_is_compact():
    schema = strict_json_schema(Invoice)
    assert "$defs" not in json.dumps(schema) and "anyOf" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(Invoice.model_fields)
    assert schema["properties"]["seller"]["properties"]["tax_id"]["type"] == ["string", "null"]


def test_receipt_uses_constrained_decoding():
    assert union_param_count(strict_json_schema(Receipt)) <= MAX_UNION_PARAMS
    backend = backend_with(reply(json.dumps({"total": 10})))
    result = backend.extract([doc("a.jpg", "image/jpeg"), doc("b.pdf", "application/pdf")], Receipt, "Extract.")

    assert result.total == 10
    call = backend._client.messages.calls[0]
    assert call["model"] == "test-model"
    assert call["output_config"]["format"]["type"] == "json_schema"
    blocks = call["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "document", "text"]
    assert "split across 2 images" in blocks[-1]["text"]


def test_large_schema_is_prompted_and_retried_once():
    assert union_param_count(strict_json_schema(Invoice)) > MAX_UNION_PARAMS
    backend = backend_with(reply('{"total": "not a number"}'), reply('```json\n{"total": 99}\n```'))
    result = backend.extract([doc("a.png", "image/png")], Invoice, "Extract.")

    assert result.total == 99
    first, second = backend._client.messages.calls
    assert "output_config" not in first
    assert "JSON Schema" in first["system"]
    assert second["messages"][-1]["content"].startswith("That JSON is invalid")


def test_heic_is_rejected_with_a_clear_message():
    with pytest.raises(ExtractionError, match="convert it to JPG"):
        backend_with().extract([doc("a.heic", "image/heic")], Receipt, "Extract.")


@pytest.mark.parametrize(("stop_reason", "message"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_incomplete_responses_raise(stop_reason, message):
    backend = backend_with(reply("", stop_reason=stop_reason))
    with pytest.raises(ExtractionError, match=message):
        backend.extract([doc("a.png", "image/png")], Receipt, "Extract.")
