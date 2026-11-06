from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

from docstruc.core.models import Issue
from docstruc.validation.checks import corroborate

Validator = Callable[[BaseModel], list[Issue]]


@dataclass(frozen=True)
class DocType:
    """Everything the engine needs to know about one kind of document."""

    name: str
    title: str
    schema: type[BaseModel]
    instructions: str
    validators: list[Validator] = field(default_factory=list)

    def validate(self, data: BaseModel) -> list[Issue]:
        issues: list[Issue] = []
        for check in self.validators:
            issues.extend(check(data))
        return corroborate(data, issues)
