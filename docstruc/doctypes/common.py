"""Building blocks shared by document schemas."""

from pydantic import BaseModel, Field


def uncertain_fields_field():
    """The last field of every document schema: fields the model could not read with certainty.

    It must come last so the model fills it after reading all the other fields.
    """
    return Field(
        [],
        description=(
            "Names of fields (e.g. 'date', 'merchant_tax_id', 'items.3.total') where any character was "
            "faded, blurred, partly hidden, folded or otherwise not clearly readable. Empty if all were clear."
        ),
    )


class LineItem(BaseModel):
    description: str | None = Field(None, description="Item or service name as printed")
    quantity: float | None = None
    unit_price: float | None = None
    total: float | None = Field(None, description="Line total as printed")


class Party(BaseModel):
    name: str | None = None
    address: str | None = None
    tax_id: str | None = Field(None, description="Tax / registration number (VAT, EIN, BIN, IIN, INN, ...)")


class UnitLineItem(BaseModel):
    """A line with a unit of measure, as on acts and waybills (шт, кг, час, усл. ...)."""

    description: str | None = Field(None, description="Item, work or service name as printed")
    unit: str | None = Field(None, description="Unit of measure as printed (pcs, kg, hour, шт, кг, час, усл., ...)")
    quantity: float | None = None
    unit_price: float | None = None
    total: float | None = Field(None, description="Line total as printed")
