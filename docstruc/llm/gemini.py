"""Google Gemini backend.

Note: on the free tier Google may use submitted content to improve its products.
Use the free tier only for public or synthetic samples; client documents need a
paid API tier or a local backend.
"""

import re
import time

from google import genai
from google.genai import errors, types

from docstruc import config
from docstruc.core.models import Document
from docstruc.llm.base import BASE_INSTRUCTIONS, MULTIPART_INSTRUCTIONS, T

RETRYABLE_CODES = {429, 500, 503}


class ExtractionError(RuntimeError):
    pass


class QuotaExceededError(ExtractionError):
    pass


class GeminiBackend:
    def __init__(self, model: str | None = None, api_key: str | None = None, max_attempts: int = 4):
        self.model = model or config.model_name("gemini")
        self.name = f"gemini/{self.model}"
        self.max_attempts = max_attempts
        self._client = genai.Client(api_key=api_key or config.gemini_api_key())

    def extract(self, parts: list[Document], schema: type[T], instructions: str) -> T:
        if len(parts) > 1:
            instructions = f"{instructions}\n{MULTIPART_INSTRUCTIONS.format(n=len(parts))}"
        contents = [types.Part.from_bytes(data=p.data, mime_type=p.mime_type) for p in parts]
        contents.append(instructions)
        name = parts[0].name
        cfg = types.GenerateContentConfig(
            system_instruction=BASE_INSTRUCTIONS,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
        )
        response = self._generate(contents, cfg)
        parsed = response.parsed
        if isinstance(parsed, schema):
            return parsed
        if response.text:
            return schema.model_validate_json(response.text)
        raise ExtractionError(f"{name}: model returned no data")

    def _generate(self, contents, cfg):
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._client.models.generate_content(model=self.model, contents=contents, config=cfg)
            except errors.APIError as exc:
                if exc.code not in RETRYABLE_CODES or attempt == self.max_attempts:
                    raise ExtractionError(f"Gemini API error {exc.code}: {exc.message}") from exc
                delay = _retry_delay_seconds(exc.message or "")
                if delay is not None and delay > MAX_RETRY_WAIT:
                    # A daily quota is exhausted: waiting inside the request makes no sense.
                    raise QuotaExceededError(
                        f"Gemini quota exhausted for {self.model}; retry in {delay / 3600:.1f} h. "
                        "The free tier allows only a few requests per day; enable billing for real workloads."
                    ) from exc
                time.sleep(delay if delay is not None else 2**attempt)
        raise AssertionError("unreachable")


MAX_RETRY_WAIT = 60
_RETRY_IN = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)


def _retry_delay_seconds(message: str) -> float | None:
    """Parse "Please retry in 9h25m38.3s" from an API error message."""
    match = _RETRY_IN.search(message)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds
