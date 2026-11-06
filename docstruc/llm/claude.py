"""Anthropic Claude backend.

API inputs are not used for model training by default, so this backend can be used
for client documents (subject to the client's own requirements).

Structured outputs (constrained JSON) limit how many nullable fields a schema may have.
Small schemas use constrained decoding; larger ones get the schema in the prompt and the
reply is validated, with one corrective retry.
"""

import base64
from io import BytesIO
import copy
import json
import re

import anthropic
from pydantic import BaseModel, ValidationError

from docstruc import config
from docstruc.core import files
from docstruc.core.models import Document
from docstruc.llm.base import BASE_INSTRUCTIONS, MULTIPART_INSTRUCTIONS, T

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# API limit on parameters with union types (anyOf / type arrays) in a structured-output schema.
MAX_UNION_PARAMS = 16
MAX_TOKENS = 16000
TASK = "Extract the data from the file above as instructed."
# Output budget per request: enough for dense pages, but a single page can never run up the full MAX_TOKENS.
# Measured ≈130 output tokens per line item (Russian text), so one page gets room for ≈75 lines.
OUTPUT_TOKENS_BASE = 6000
OUTPUT_TOKENS_PER_PAGE = 4000


def output_budget(parts: list[Document]) -> int:
    pages = 0
    for p in parts:
        try:
            pages += files.page_count(p)
        except files.UnsupportedFileError:
            pages += 1  # an unreadable PDF is rejected by the API anyway
    return min(MAX_TOKENS, OUTPUT_TOKENS_BASE + OUTPUT_TOKENS_PER_PAGE * pages)
# USD per million tokens (input, output), standard API rates, prompts under 100K tokens.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class ExtractionError(RuntimeError):
    pass


API_IMAGE_MAX_BYTES = 4_500_000
API_IMAGE_MAX_SIDE = 7_900


def _fit_image(part: Document) -> tuple[bytes, str]:
    """Shrink an image that the API would reject (over 5 MB or 8000 px). Others are sent unchanged."""
    from PIL import Image, ImageOps

    try:
        with Image.open(BytesIO(part.data)) as probe:
            fits = len(part.data) <= API_IMAGE_MAX_BYTES and max(probe.size) <= API_IMAGE_MAX_SIDE
    except Exception:
        return part.data, part.mime_type  # unreadable: files are validated on upload; the API will refuse it
    if fits:
        return part.data, part.mime_type
    with Image.open(BytesIO(part.data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((4000, 4000))
        out = BytesIO()
        image.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def _content_block(part: Document) -> dict:
    if part.mime_type in IMAGE_TYPES:
        raw, mime = _fit_image(part)
        return {"type": "image", "source": {"type": "base64", "media_type": mime,
                                            "data": base64.standard_b64encode(raw).decode("ascii")}}
    data = base64.standard_b64encode(part.data).decode("ascii")
    if part.mime_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if part.mime_type in IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": part.mime_type, "data": data}}
    raise ExtractionError(f"{part.name}: {part.mime_type} is not supported by Claude; convert it to JPG or PNG")


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema in the compact form structured outputs accept.

    Inlines $refs, writes `X | None` as a type array instead of anyOf, marks every
    property required (absent values are explicit nulls) and forbids extra properties.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
        if "anyOf" in node:
            options = [walk(x) for x in node["anyOf"]]
            non_null = [o for o in options if o.get("type") != "null"]
            if len(options) == 2 and len(non_null) == 1 and isinstance(non_null[0].get("type"), str):
                merged = {**non_null[0], "type": [non_null[0]["type"], "null"]}
                if "description" in node:
                    merged["description"] = node["description"]
                return merged
        out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(schema)


def union_param_count(schema: dict) -> int:
    count = 0

    def walk(node):
        nonlocal count
        if isinstance(node, dict):
            if isinstance(node.get("type"), list) or "anyOf" in node:
                count += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return count


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _json_text(response) -> str:
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return _FENCE.sub("", text)


class ClaudeBackend:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or config.model_name("claude")
        self.name = f"claude/{self.model}"
        # The SDK retries rate limits (429), overload and server errors with backoff.
        self._client = anthropic.Anthropic(api_key=api_key or config.anthropic_api_key(), max_retries=4, timeout=300)
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_tokens = 0
        self.cache_read_tokens = 0
        self._max_tokens = MAX_TOKENS
        # Cache the instructions between requests: worth it when several files go one after another
        # (cached input costs 10%, writing the cache once costs 125%). Set by batch processing.
        self.cache_prefix = False
        self._system = BASE_INSTRUCTIONS

    @property
    def cost_usd(self) -> float | None:
        """Estimated spend of this backend instance so far, or None for a model without a known price."""
        if self.model not in PRICES:
            return None
        price_in, price_out = PRICES[self.model]
        input_cost = self.input_tokens + 1.25 * self.cache_write_tokens + 0.1 * self.cache_read_tokens
        return (input_cost * price_in + self.output_tokens * price_out) / 1_000_000

    def extract(self, parts: list[Document], schema: type[T], instructions: str) -> T:
        # The document-type instructions (and, for large schemas, the schema) go into the system prompt,
        # which is the same for every file of a type and can be cached; the request itself only varies.
        task = TASK
        if len(parts) > 1:
            task = f"{task}\n{MULTIPART_INSTRUCTIONS.format(n=len(parts))}"
        content = [_content_block(p) for p in parts]
        self._max_tokens = output_budget(parts)
        name = parts[0].name
        json_schema = strict_json_schema(schema)

        if union_param_count(json_schema) <= MAX_UNION_PARAMS:
            try:
                self._set_system(instructions)
                return self._extract_constrained(content, task, schema, json_schema, name)
            except anthropic.BadRequestError as exc:
                if "schema" not in str(exc.message).lower():
                    raise ExtractionError(f"Claude API error 400: {exc.message}") from exc
                # The API found the schema too complex after all: use the prompt-based path.
        self._set_system(
            f"{instructions}\n\nReturn only a JSON object that matches this JSON Schema, with no other text:\n"
            f"{json.dumps(json_schema, ensure_ascii=False)}"
        )
        return self._extract_prompted(content, task, schema, json_schema, name)

    def _set_system(self, instructions: str) -> None:
        text = f"{BASE_INSTRUCTIONS}\n\n{instructions}"
        if self.cache_prefix:
            self._system = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        else:
            self._system = text

    def _extract_constrained(self, content, task, schema: type[T], json_schema: dict, name: str) -> T:
        response = self._call(
            [{"role": "user", "content": [*content, {"type": "text", "text": task}]}],
            output_config={"format": {"type": "json_schema", "schema": json_schema}},
            name=name,
            raise_bad_request=True,
        )
        try:
            return schema.model_validate_json(_json_text(response))
        except ValidationError as exc:
            raise ExtractionError(f"{name}: model output does not match the schema: {exc}") from exc

    def _extract_prompted(self, content, task, schema: type[T], json_schema: dict, name: str) -> T:
        messages = [{"role": "user", "content": [*content, {"type": "text", "text": f"{task} Return the JSON object only."}]}]
        response = self._call(messages, name=name)
        text = _json_text(response)
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That JSON is invalid: {exc}\nReturn the corrected JSON object only."},
            ]
        text = _json_text(self._call(messages, name=name))
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise ExtractionError(f"{name}: model output does not match the schema after a retry: {exc}") from exc

    def _call(self, messages: list, name: str, raise_bad_request: bool = False, **kwargs):
        try:
            response = self._client.messages.create(
                model=self.model, max_tokens=self._max_tokens, system=self._system, messages=messages, **kwargs
            )
        except anthropic.BadRequestError as exc:
            if raise_bad_request:
                raise
            raise ExtractionError(f"Claude API error 400: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise ExtractionError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ExtractionError(f"Cannot reach the Claude API: {exc}") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        if response.stop_reason == "refusal":
            raise ExtractionError(f"{name}: the model declined to process this document")
        if response.stop_reason == "max_tokens":
            raise ExtractionError(f"{name}: output was cut off (document too long for one request)")
        return response
