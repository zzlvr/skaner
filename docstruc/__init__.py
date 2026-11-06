"""docstruc: turn document scans and photos into validated structured data."""

from docstruc.core.models import ExtractionResult, Issue
from docstruc.core.pipeline import extract, extract_all

__all__ = ["extract", "extract_all", "ExtractionResult", "Issue"]
__version__ = "0.1.0"
