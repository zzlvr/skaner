"""Backend for any OpenAI-compatible chat endpoint: OpenAI itself, or a model on your own machine (Ollama, LM Studio,
vLLM, llama.cpp server). With a local model nothing leaves your computer.

    DOCSTRUC_BASE_URL=http://localhost:11434/v1     # Ollama (the default)
    DOCSTRUC_MODEL=qwen2.5vl:7b                     # a model that can read images
    docstruc extract receipts/ --type receipt --backend openai --out receipts.xlsx

These endpoints take images, not PDFs, so every PDF page is rendered to an image first. The reply is constrained to the
document schema when the server supports it, otherwise the schema goes into the prompt; an invalid reply gets one
corrective retry. Small local models misread more than the big hosted ones: the checks and the second read of the
document (`--report`) are there to catch exactly that.
"""

import base64
import json
import re
from io import BytesIO

import httpx
from pydantic import ValidationError

from docstruc import config
from docstruc.core.models import Document
from docstruc.llm.base import BASE_INSTRUCTIONS, MULTIPART_INSTRUCTIONS, T

PDF = "application/pdf"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
TASK = "Extract the data from the images above as instructed. Return the JSON object only."
PDF_DPI = 170
TIMEOUT = httpx.Timeout(900.0, connect=15.0)  # a local model on a CPU can take minutes per page
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class ExtractionError(RuntimeError):
    pass


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.standard_b64encode(data).decode('ascii')}"


def _images(part: Document) -> list[dict]:
    """The page images of one input file, as `image_url` content blocks."""
    if part.mime_type == PDF:
        import pymupdf

        blocks = []
        with pymupdf.open(stream=part.data, filetype="pdf") as pdf:
            for page in pdf:
                png = page.get_pixmap(dpi=PDF_DPI, alpha=False).tobytes("png")
                blocks.append({"type": "image_url", "image_url": {"url": _data_url(png, "image/png")}})
        return blocks
    if part.mime_type in IMAGE_TYPES:
        return [{"type": "image_url", "image_url": {"url": _data_url(part.data, part.mime_type)}}]
    raise ExtractionError(f"{part.name}: {part.mime_type} is not supported by this backend; convert it to JPG or PNG")


class OpenAICompatBackend:
    def __init__(self, model: str | None = None, api_key: str | None = None, base_url: str | None = None,
                 client: httpx.Client | None = None):
        self.model = model or config.openai_model()
        self.name = f"openai/{self.model}"
        self.base_url = (base_url or config.openai_base_url()).rstrip("/")
        key = api_key or config.openai_api_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = client or httpx.Client(timeout=TIMEOUT, headers=headers)
        self._constrained = True  # until the server says it does not support response_format

    def extract(self, parts: list[Document], schema: type[T], instructions: str) -> T:
        task = TASK if len(parts) == 1 else f"{TASK}\n{MULTIPART_INSTRUCTIONS.format(n=len(parts))}"
        content = [block for p in parts for block in _images(p)]
        name = parts[0].name
        json_schema = schema.model_json_schema()
        system = f"{BASE_INSTRUCTIONS}\n\n{instructions}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [*content, {"type": "text", "text": task}]},
        ]
        text = self._complete(messages, json_schema, name)
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That JSON is invalid: {exc}\nReturn the corrected JSON object only."},
            ]
        text = self._complete(messages, json_schema, name)
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise ExtractionError(f"{name}: model output does not match the schema after a retry: {exc}") from exc

    def _complete(self, messages: list, json_schema: dict, name: str) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if self._constrained:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": json_schema},
            }
        else:
            # The schema goes into the prompt instead (only the first call carries the system message to extend).
            system = dict(messages[0])
            if "JSON Schema" not in system["content"]:
                system["content"] += (
                    "\n\nReturn only a JSON object that matches this JSON Schema, with no other text:\n"
                    f"{json.dumps(json_schema, ensure_ascii=False)}"
                )
            body["messages"] = [system, *messages[1:]]
        try:
            response = self._client.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise ExtractionError(f"Cannot reach {self.base_url}: {exc}") from exc
        if response.status_code == 400 and self._constrained and "response_format" in response.text:
            self._constrained = False  # this server cannot constrain the output: ask in the prompt from now on
            return self._complete(messages, json_schema, name)
        if response.status_code >= 400:
            raise ExtractionError(f"{name}: the server answered {response.status_code}: {response.text[:300]}")
        try:
            choice = response.json()["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ExtractionError(f"{name}: unexpected reply from the server") from exc
        if choice.get("finish_reason") == "length":
            raise ExtractionError(f"{name}: output was cut off (document too long for one request)")
        return _FENCE.sub("", text.strip())
