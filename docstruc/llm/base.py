"""Provider-agnostic interface for models that read a document and return structured data.

Any provider (Gemini, Claude, OpenAI, a local model) plugs in by implementing `LLMBackend`.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from docstruc.core.models import Document

T = TypeVar("T", bound=BaseModel)

BASE_INSTRUCTIONS = """You extract data from business documents (scans, photos, PDFs).
Rules:
- Copy values exactly as printed. Never invent or guess a value; use null when it is absent or unreadable.
- Numbers: plain numbers without currency symbols or thousands separators; use "." as decimal separator.
  Keep every printed digit, including decimals (550.96 stays 550.96): never round.
- Dates: ISO format YYYY-MM-DD. Times: HH:MM. A two-digit year YY means 20YY.
  If the same date is printed more than once (e.g. "07.10.26" and "07.10.2026"), read every copy
  and use the reading they agree on; faded or partly hidden digits are resolved from the clearer copy.
- Currency: ISO 4217 code (USD, EUR, KZT, RUB, ...) when it can be determined, otherwise null.
- Keep text in its original language (English, Russian, Kazakh, ...).
"""


MULTIPART_INSTRUCTIONS = """The document is split across {n} images, given in order from top to bottom.
Consecutive images may overlap: lines visible in two images belong to the document only once.
Return one merged result for the whole document."""


class LLMBackend(Protocol):
    name: str

    def extract(self, parts: list[Document], schema: type[T], instructions: str) -> T:
        """Read one document (given as one or more ordered parts) and return an instance of `schema`."""
        ...
