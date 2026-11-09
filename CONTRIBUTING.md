# Contributing

Bug reports, document types, export formats, country checks and fixes are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[web,dev]"
python -m pytest
```

Tests run without any API key: the model is replaced by a fake backend.

Optional, for the position report: `pip install -e ".[locate]"` (OCR for photos).

## Adding a document type

A document type is one module in `docstruc/doctypes/`: a Pydantic schema, short instructions for the model and a list of checks. Copy `invoice.py`, change the fields, pick the checks and register it in `docstruc/doctypes/__init__.py`.

```python
MY_DOC = DocType(
    name="my_doc",
    title="My document",
    schema=MyDoc,                      # fields the model must fill
    instructions="This is a ...",      # what is special about this document
    validators=[checks.required("number", "date"), checks.valid_date("date"), checks.tax_ids("seller.tax_id")],
)
```

## Adding a check

A check is a function that takes the extracted document and returns a list of `Issue`. Put reusable ones in `docstruc/validation/`. A check that can be wrong on correct data must give a warning, not an error: errors send a document to a human.

## Adding an export format

A module in `docstruc/exporters/` with `write(results, path)`, registered in `docstruc/exporters/__init__.py`.

## Pull requests

- Add a test for what you change.
- No real personal or business data in code, tests or samples. Use made-up values or public ones.
- Keep commit messages to one short line.
- By contributing you agree your work is released under AGPL-3.0, like the rest of the project.
