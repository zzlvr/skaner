"""Result types shared by every part of the engine."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    """A problem found by validation. `field` is a dotted path, e.g. "items.2.total"."""

    field: str
    severity: Severity
    message: str


class Evidence(BaseModel):
    """Where a value was found on the source. `box` is (x0, y0, x1, y1) as fractions of the page size."""

    part: int = 0  # index of the input file (a document can be given as several parts)
    page: int = 0
    box: tuple[float, float, float, float]
    text: str  # what is printed there


class Document(BaseModel):
    """A loaded input file."""

    name: str
    mime_type: str
    data: bytes = Field(repr=False)


class ExtractionResult(BaseModel):
    source: str
    doctype: str
    model: str
    data: dict[str, Any]
    issues: list[Issue] = []
    evidence: dict[str, Evidence] = {}  # dotted field path -> where it was found; empty unless located

    @property
    def needs_review(self) -> bool:
        """True when a human should check this document before it is delivered."""
        return any(i.severity == "error" for i in self.issues)

    @property
    def flagged_fields(self) -> set[str]:
        """Every field with an issue, warnings included."""
        return {i.field for i in self.issues}

    @property
    def review_fields(self) -> set[str]:
        """Fields a human must check (errors only); these are highlighted in exports."""
        return {i.field for i in self.issues if i.severity == "error"}

    @classmethod
    def failed(cls, source: str, doctype: str, error: str) -> "ExtractionResult":
        """A placeholder result for a file that could not be processed, so batches keep going."""
        return cls(
            source=source,
            doctype=doctype,
            model="",
            data={},
            issues=[Issue(field="document", severity="error", message=f"Extraction failed: {error}")],
        )
