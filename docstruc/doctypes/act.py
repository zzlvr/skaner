"""Act of completed works / rendered services (акт выполненных работ, e.g. Kazakh form Р-1)."""

from pydantic import BaseModel, Field

from docstruc.doctypes.base import DocType
from docstruc.doctypes.common import Party, UnitLineItem, uncertain_fields_field
from docstruc.validation import checks


class Act(BaseModel):
    act_number: str | None = None
    act_date: str | None = Field(None, description="YYYY-MM-DD")
    contract_number: str | None = Field(None, description="Number of the contract the act refers to")
    contract_date: str | None = Field(None, description="YYYY-MM-DD")
    period_from: str | None = Field(None, description="Start of the period the works cover, YYYY-MM-DD")
    period_to: str | None = Field(None, description="End of the period the works cover, YYYY-MM-DD")
    contractor: Party = Field(Party(), description="Who performed the works (исполнитель, орындаушы)")
    customer: Party = Field(Party(), description="Who ordered and accepts the works (заказчик, тапсырыс беруші)")
    currency: str | None = Field(None, description="ISO 4217 code")
    items: list[UnitLineItem] = []
    subtotal: float | None = Field(None, description="Total without VAT, only if printed")
    tax_rate: float | None = Field(None, description="VAT rate in percent, e.g. 12 for 12%")
    tax_amount: float | None = Field(None, description="VAT amount, only if printed")
    total: float | None = Field(None, description="Total amount of the act, including VAT if any")
    uncertain_fields: list[str] = uncertain_fields_field()


ACT = DocType(
    name="act",
    title="Act of completed works",
    schema=Act,
    instructions=(
        "This is an act of completed works or rendered services (акт выполненных работ / оказанных услуг, "
        "орындалған жұмыстар актісі, e.g. Kazakh form Р-1; a certificate of completion). "
        "Extract the contractor and the customer with their BIN/IIN, the contract reference, the period, "
        "every line of work with its unit, quantity, price and cost, and the totals. "
        "Ignore signatures, stamps and the names of signing persons."
    ),
    validators=[
        checks.model_uncertain,
        checks.required("act_number", "act_date", "total"),
        checks.valid_date("act_date", "contract_date", "period_from", "period_to"),
        checks.line_totals,
        checks.items_sum,
        checks.total_consistent,
        checks.tax_consistent,
        checks.tax_ids("contractor.tax_id", "customer.tax_id"),
    ],
)
