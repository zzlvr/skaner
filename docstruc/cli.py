"""Command line: process files or folders in a batch and write one output file.

    docstruc extract invoices/ --type invoice --out invoices.xlsx
"""

import argparse
import sys

from docstruc.core import files
from docstruc.core.models import ExtractionResult
from docstruc.core.pipeline import extract, extract_all
from docstruc.doctypes import DOCTYPES
from docstruc.exporters import EXPORTERS, FORMATS, export
from docstruc.llm import BACKENDS


def _run_extract(args: argparse.Namespace) -> int:
    from docstruc.llm import get_backend
    from docstruc.llm.gemini import QuotaExceededError

    paths = files.collect_paths(args.inputs)
    if not paths:
        print("No supported files found.", file=sys.stderr)
        return 1

    backend = get_backend(args.backend, model=args.model)
    # Each file is a document, or all files together are the ordered parts of one document.
    jobs = [paths] if args.one_document else [[p] for p in paths]
    if hasattr(backend, "cache_prefix"):
        backend.cache_prefix = len(jobs) > 1  # several files in a row: cache the shared instructions
    results: list[ExtractionResult] = []
    for n, job in enumerate(jobs, start=1):
        label = " + ".join(p.name for p in job)
        try:
            found = [extract(job, args.type, backend=backend)] if args.one_document else extract_all(job[0], args.type, backend=backend)
        except QuotaExceededError as exc:
            # Every remaining file would fail the same way: stop and keep what is done.
            print(f"\nStopped: {exc}", file=sys.stderr)
            results.extend(ExtractionResult.failed(" + ".join(p.name for p in j), args.type, str(exc)) for j in jobs[n - 1 :])
            break
        except Exception as exc:  # keep the batch going; the failure is reported in the output
            found = [ExtractionResult.failed(label, args.type, str(exc))]
        status = "REVIEW" if any(r.needs_review for r in found) else "ok"
        extra = f" ({len(found)} documents)" if len(found) > 1 else ""
        print(f"[{n}/{len(jobs)}] {status:6} {label}{extra}")
        results.extend(found)

    written = export(results, args.out, fmt=args.format)
    review = sum(r.needs_review for r in results)
    print(f"\n{len(results)} documents, {review} need review. Saved: {', '.join(map(str, written))}")
    cost = getattr(backend, "cost_usd", None)
    if cost is not None:
        print(
            f"Tokens: {backend.input_tokens} in / {backend.output_tokens} out, "
            f"estimated cost ${cost:.4f} (${cost / len(results):.4f} per document)"
        )
    return 0


def _run_digitize(args: argparse.Namespace) -> int:
    from pathlib import Path

    from docstruc.digitize import digitize
    from docstruc.llm import get_backend

    paths = files.collect_paths(args.inputs)
    if not paths:
        print("No supported files found.", file=sys.stderr)
        return 1
    backend = get_backend(args.backend, model=args.model)
    content, pages = digitize([files.from_path(p) for p in paths], backend)
    Path(args.out).write_bytes(content)
    print(f"{len(paths)} source page(s) -> {pages} PDF page(s). Saved: {args.out}")
    cost = getattr(backend, "cost_usd", None)
    if cost is not None:
        print(f"Estimated cost ${cost:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docstruc", description="Extract structured data from documents.")
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("extract", help="Extract data from files or folders")
    ex.add_argument("inputs", nargs="+", help="Files or folders (PDF, JPG, PNG, WEBP, HEIC)")
    ex.add_argument("--type", required=True, choices=sorted(DOCTYPES), help="Document type")
    ex.add_argument("--out", required=True, help=f"Output file ({', '.join(EXPORTERS)})")
    ex.add_argument(
        "--format", default=None, choices=sorted(FORMATS),
        help="Output format; default is chosen by the file extension. '1c' writes Excel prepared for 1C import",
    )
    ex.add_argument("--backend", default=None, choices=BACKENDS, help="Model provider (default: DOCSTRUC_BACKEND)")
    ex.add_argument("--model", default=None, help="Override the model name")
    ex.add_argument(
        "--one-document",
        action="store_true",
        help="Treat all inputs as ordered parts of a single document (e.g. a long receipt in several photos)",
    )
    ex.set_defaults(func=_run_extract)

    dg = sub.add_parser("digitize", help="Re-type photos of a document into a clean PDF with real text")
    dg.add_argument("inputs", nargs="+", help="Pages in order: files or a folder (PDF, JPG, PNG, WEBP)")
    dg.add_argument("--out", required=True, help="Output PDF file")
    dg.add_argument("--backend", default=None, choices=BACKENDS, help="Model provider (default: DOCSTRUC_BACKEND)")
    dg.add_argument("--model", default=None, help="Override the model name")
    dg.set_defaults(func=_run_digitize)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
