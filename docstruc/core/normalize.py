"""Clean-ups applied to every extracted document before validation."""

import re

from pydantic import BaseModel

# Cash registers prefix item names with service marks such as "^^#КУС ВКУС" or "*МОЛОКО".
_ITEM_MARKS = re.compile(r"^[\^#*~\s]+")
# Document numbers are often read together with their label: "№00003344", "No. 17", "# A-1".
_NUMBER_LABEL = re.compile(r"^(?:№|no\.?|#)\s*", re.IGNORECASE)


def clean_item_name(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = _ITEM_MARKS.sub("", name).strip()
    return cleaned or name.strip()


def clean_number(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _NUMBER_LABEL.sub("", value.strip()).strip()
    return cleaned or value.strip()


def normalize(data: BaseModel) -> BaseModel:
    """Clean document numbers and item names in place; returns the same object."""
    for name in type(data).model_fields:
        value = getattr(data, name)
        if name.endswith("_number") and isinstance(value, str):
            setattr(data, name, clean_number(value))
    for item in getattr(data, "items", None) or []:
        if hasattr(item, "description"):
            item.description = clean_item_name(item.description)
    return data
