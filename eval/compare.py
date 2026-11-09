"""Field-level accuracy of extraction results against hand-labelled ground truth.

    python eval/compare.py results.json [more_results.json ...] --truth ground_truth.json

Ground truth format: {source: {field: value | [accepted values...], "items": [[qty, unit_price, total], ...]}}.
A null inside items means "not readable on the document" and is not scored.
"""

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

NUM_TOLERANCE = 0.011  # money: one cent
QTY_TOLERANCE = 0.0005  # quantities and weights are printed to three decimals
TEXT_SIMILARITY = 0.8


def _norm_text(value) -> str:
    return " ".join(str(value).replace("«", '"').replace("»", '"').casefold().split())


# Only free-text names are compared approximately; ids, dates and times must match exactly.
FUZZY_FIELDS = {"merchant_name", "description"}


def _match(expected, actual, fuzzy: bool = False, tolerance: float = NUM_TOLERANCE) -> bool:
    if expected is None:
        return actual in (None, "", 0)
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and abs(expected - actual) <= tolerance
    if actual is None:
        return False
    e, a = _norm_text(expected), _norm_text(actual)
    if not fuzzy:
        return e.replace(" ", "") == a.replace(" ", "")
    return e == a or e in a or SequenceMatcher(None, e, a).ratio() >= TEXT_SIMILARITY


def _match_any(expected, actual, fuzzy: bool = False) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return any(_match(e, actual, fuzzy) for e in options)


def compare(results: list[dict], truth: dict) -> dict:
    fields_total = fields_ok = items_total = items_ok = 0
    report = []
    for r in results:
        gt = truth.get(r["file"])
        if gt is None:
            continue
        data = r["data"]
        errors = []
        for field, expected in gt.items():
            if field == "items" or field.startswith("_"):  # "_..." keys are notes about the sample, not fields
                continue
            fields_total += 1
            if _match_any(expected, data.get(field), fuzzy=field in FUZZY_FIELDS):
                fields_ok += 1
            else:
                errors.append(f"{field}: expected {expected!r}, got {data.get(field)!r}")

        pred_items = data.get("items") or []
        if len(pred_items) != len(gt["items"]):
            errors.append(f"items: expected {len(gt['items'])} lines, got {len(pred_items)}")
        for i, expected_item in enumerate(gt["items"]):
            pred = pred_items[i] if i < len(pred_items) else {}
            for name, expected in zip(("quantity", "unit_price", "total"), expected_item):
                if expected is None:
                    continue
                items_total += 1
                tolerance = QTY_TOLERANCE if name == "quantity" else NUM_TOLERANCE
                if _match(expected, pred.get(name), tolerance=tolerance):
                    items_ok += 1
                else:
                    errors.append(f"items.{i}.{name}: expected {expected!r}, got {pred.get(name)!r}")

        report.append({"file": r["file"], "needs_review": r["needs_review"], "errors": errors})

    wrong = [d for d in report if d["errors"]]
    silent = [d for d in wrong if not d["needs_review"]]
    return {
        # documents with a wrong value, and how many of them were NOT sent to review (the dangerous ones)
        "documents_with_errors": len(wrong),
        "silent_errors": len(silent),
        "silent_error_files": [d["file"] for d in silent],
        "documents": len(report),
        "header_accuracy": fields_ok / fields_total if fields_total else None,
        "item_accuracy": items_ok / items_total if items_total else None,
        "overall_accuracy": (fields_ok + items_ok) / (fields_total + items_total) if fields_total + items_total else None,
        "counts": {"header": [fields_ok, fields_total], "items": [items_ok, items_total]},
        "per_document": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="+", help="JSON files written by `docstruc extract --out x.json`")
    parser.add_argument("--truth", required=True, help="Ground truth JSON")
    args = parser.parse_args(argv)

    results = [r for path in args.results for r in json.loads(Path(path).read_text(encoding="utf-8"))]
    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    summary = compare(results, truth)

    for doc in summary["per_document"]:
        status = "REVIEW" if doc["needs_review"] else "ok"
        print(f"\n[{status}] {doc['file'][:70]}")
        for e in doc["errors"] or ["all fields correct"]:
            print(f"   - {e}")
    h, it = summary["counts"]["header"], summary["counts"]["items"]
    print(f"\nDocuments: {summary['documents']}")
    print(f"Header fields: {h[0]}/{h[1]} = {summary['header_accuracy']:.1%}")
    print(f"Line item values: {it[0]}/{it[1]} = {summary['item_accuracy']:.1%}")
    print(f"Overall: {summary['overall_accuracy']:.1%}")
    print(f"Documents with a wrong value: {summary['documents_with_errors']}, "
          f"of them not sent to review (silent): {summary['silent_errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
