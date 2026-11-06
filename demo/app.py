"""Streamlit demo: upload documents, review extracted fields, download Excel or JSON.

    streamlit run demo/app.py
"""

import json
import os
from pathlib import Path

import streamlit as st

from docstruc.core import files
from docstruc.core.models import ExtractionResult
from docstruc.core.pipeline import extract, extract_all
from docstruc.doctypes import DOCTYPES
from docstruc.exporters import excel, json_export, onec

st.set_page_config(page_title="docstruc", page_icon="📄", layout="wide")
st.title("docstruc — documents to structured data")
st.caption("Upload receipts or invoices (PDF, JPG, PNG). Fields that need a human check are flagged.")

# Showcase mode: open with saved results (JSON from `docstruc extract --out x.json`), e.g. for screenshots.
demo_results = os.environ.get("DOCSTRUC_DEMO_RESULTS")
if demo_results and "results" not in st.session_state:
    st.session_state["results"] = json_export.from_dict(json.loads(Path(demo_results).read_text(encoding="utf-8")))

shown = st.session_state.get("results") or []
default_type = list(DOCTYPES).index(shown[0].doctype) if shown and shown[0].doctype in DOCTYPES else 0
doctype = st.selectbox("Document type", list(DOCTYPES), index=default_type, format_func=lambda n: DOCTYPES[n].title)
uploads = st.file_uploader(
    "Documents", type=[s.lstrip(".") for s in files.MIME_TYPES], accept_multiple_files=True
)
one_document = st.checkbox(
    "All files are parts of one document (e.g. a long receipt photographed in pieces, top to bottom)"
)


@st.cache_resource
def cached_backend():
    from docstruc.llm import get_backend

    return get_backend()


if uploads and st.button("Extract", type="primary"):
    results: list[ExtractionResult] = []
    progress = st.progress(0.0)
    jobs = [uploads] if one_document else [[u] for u in uploads]
    for n, job in enumerate(jobs, start=1):
        try:
            parts = [files.from_bytes(u.getvalue(), u.name) for u in job]
            if one_document:
                results.append(extract(parts, doctype, backend=cached_backend()))
            else:
                results.extend(extract_all(parts[0], doctype, backend=cached_backend()))
        except Exception as exc:
            results.append(ExtractionResult.failed(" + ".join(u.name for u in job), doctype, str(exc)))
        progress.progress(n / len(jobs))
    st.session_state["results"] = results

results: list[ExtractionResult] = st.session_state.get("results", [])
if results:
    ok = sum(not r.needs_review for r in results)
    st.subheader(f"{len(results)} documents: {ok} ok, {len(results) - ok} need review")

    col1, col2, col3 = st.columns(3)
    col1.download_button(
        "Download Excel",
        excel.to_bytes(results),
        file_name="extracted.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    col2.download_button(
        "Download JSON",
        json.dumps(json_export.to_dict(results), ensure_ascii=False, indent=2),
        file_name="extracted.json",
        mime="application/json",
    )
    col3.download_button(
        "Download for 1C",
        onec.to_bytes(results),
        file_name="extracted_1c.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    for r in results:
        label = f"{'⚠️' if r.needs_review else '✅'} {r.source}"
        with st.expander(label, expanded=r.needs_review):
            for issue in r.issues:
                (st.error if issue.severity == "error" else st.warning)(f"**{issue.field}**: {issue.message}")
            header = {k: v for k, v in r.data.items() if k != "items"}
            st.json(header, expanded=True)
            if r.data.get("items"):
                st.dataframe(r.data["items"], width="stretch")
