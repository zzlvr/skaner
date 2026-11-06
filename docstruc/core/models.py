"""Result types shared by every part of the engine."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    """A problem found by validation. `field` is a dotted path, e.g. "items.2.total"."""

    field: str
    severity: Severity
    message: str


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
